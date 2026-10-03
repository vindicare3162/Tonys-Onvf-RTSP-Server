"""Per-physical-device controllers and the per-virtual-camera view of them.

A physical device (one IP) gets exactly one DeviceController, shared by every
virtual camera that points at it (the Tapo C575D appears as two virtual
cameras). The controller owns:

- `ptz`: a PtzController, or None
- `relays`: name -> Relay, presented to the NVR as ONVIF relay outputs
  (Floodlight, Siren, IR_Illuminator, Spotlight, Privacy)
- `imaging`: an imaging adapter (sliders + IrCutFilter), or None

ONVIF semantics used for relays: IdleState is always "open", so
LogicalState "active" == closed == ON. Monostable relays (sirens) switch
themselves back off after DelayTime.
"""

import logging
import threading
import time
from urllib.parse import urlparse

from .ptz import PtzController
from .reolink import ReolinkClient, ReolinkError
from .soap import SoapClient, find_first, find_all
from .tapo import TapoLocalClient, TapoUnavailable

logger = logging.getLogger(__name__)


class ControlError(Exception):
    pass


# ----------------------------------------------------------------- relays
class Relay:
    """One controllable output.

    `expire_off`: for monostable relays whose hardware latches (the Tapo
    siren), the off function to call when DelayTime runs out. Reolink's siren
    stops by itself, so it has none. Each activation bumps a generation
    counter, so an expiry timer that fires late can never switch off (or mark
    inactive) a newer activation.
    """

    def __init__(self, token, on_fn, off_fn, monostable=False, delay=0, max_delay=60, description="",
                 expire_off=None, skip_idle_off=False):
        self.token = token
        self._on, self._off = on_fn, off_fn
        self.monostable = monostable
        self.delay = int(delay)
        self.max_delay = max_delay
        self.description = description
        self.expire_off = expire_off
        # Reolink only: its stop sequence (switch 1 then 0) would itself chirp the
        # siren, so "off" is skipped when we believe it isn't sounding. The Tapo
        # siren latches and must always be stoppable (e.g. after a restart, or if
        # it was started from the vendor app).
        self.skip_idle_off = skip_idle_off
        self.active = False
        self._timer = None
        self._gen = 0
        self._lock = threading.Lock()
        self.dispatch = None   # callable(token, active), set by the registry

    @property
    def mode(self):
        return 'Monostable' if self.monostable else 'Bistable'

    def _notify(self):
        cb = self.dispatch
        if cb:
            try:
                cb(self.token, self.active)
            except Exception:
                pass

    def _expire(self, gen, attempt=1):
        with self._lock:
            if gen != self._gen or not self.active:
                return
            self._timer = None
            if self.expire_off:
                try:
                    self.expire_off()
                except Exception as e:
                    # Still sounding as far as we know: stay active and retry.
                    logger.warning("%s auto-off failed (attempt %d): %s", self.token, attempt, e)
                    if attempt < 5:
                        self._timer = threading.Timer(5.0, self._expire, args=(gen, attempt + 1))
                        self._timer.daemon = True
                        self._timer.start()
                    return
            self.active = False
        self._notify()

    def set(self, active, seconds=None):
        with self._lock:
            if not active and self.monostable and not self.active and self.skip_idle_off:
                return
            self._gen += 1
            gen = self._gen
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self.monostable:
                try:
                    secs = float(seconds) if seconds else float(self.delay)
                except (TypeError, ValueError):
                    secs = float(self.delay)
                secs = max(1.0, min(float(self.max_delay), secs))
            try:
                if active:
                    dur = self._on(secs) if self.monostable else self._on()
                else:
                    self._off()
            except (ReolinkError, TapoUnavailable) as e:
                raise ControlError(str(e))
            self.active = bool(active)
            if active and self.monostable:
                hold = float(dur) if isinstance(dur, (int, float)) and dur else secs
                self._timer = threading.Timer(hold, self._expire, args=(gen,))
                self._timer.daemon = True
                self._timer.start()
        self._notify()

    def configure(self, mode=None, delay=None):
        if mode in ('Monostable', 'Bistable') and (mode == 'Monostable') != self.monostable:
            # Only the siren is meaningfully monostable; refuse to latch it.
            if self.monostable:
                raise ControlError(f"{self.token} only supports Monostable")
        if delay is not None:
            self.delay = int(max(1, min(self.max_delay, delay))) if self.monostable else 0


# ---------------------------------------------------------------- imaging
SLIDERS = ('Brightness', 'ColorSaturation', 'Contrast', 'Sharpness')


class ReolinkImaging:
    """Imaging via the Reolink HTTP API: GetImage/SetImage sliders (0-255),
    IrCutFilter <-> Isp.dayNight (ON=Color, OFF=Black&White, AUTO=Auto).
    The IR emitters are a separate relay and are never touched from here."""
    IR_TO_DN = {'ON': 'Color', 'OFF': 'Black&White', 'AUTO': 'Auto'}
    DN_TO_IR = {v: k for k, v in IR_TO_DN.items()}
    MAP = {'Brightness': 'bright', 'ColorSaturation': 'saturation', 'Contrast': 'contrast', 'Sharpness': 'sharpen'}

    def __init__(self, client):
        self.c = client
        self.slider_range = (0, 255)
        self.ir_modes = ['ON', 'OFF', 'AUTO']
        self._cache = (0.0, None)
        self.available = True

    def stop(self):
        pass

    def get(self):
        ts, cached = self._cache
        if cached and time.time() - ts < 5:
            return cached
        try:
            img = self.c.get_image()
            isp = self.c.get_isp()
            out = {k: float(img.get(v, 128)) for k, v in self.MAP.items()}
            out['IrCutFilter'] = self.DN_TO_IR.get(isp.get('dayNight'), 'AUTO')
            self._cache = (time.time(), out)
            return out
        except Exception as e:
            if cached:
                return cached
            raise ControlError(str(e))

    def set(self, values):
        current = self.get()
        sliders = {self.MAP[k]: v for k, v in values.items()
                   if k in self.MAP and v is not None and round(float(v)) != round(current.get(k, -1))}
        ir = values.get('IrCutFilter')
        try:
            if sliders:
                self.c.set_image(**sliders)
            if ir and ir != current.get('IrCutFilter'):
                if ir not in self.IR_TO_DN:
                    raise ControlError(f"unsupported IrCutFilter {ir}")
                self.c.set_day_night(self.IR_TO_DN[ir])
        except ReolinkError as e:
            raise ControlError(str(e))
        self._cache = (0.0, None)
        return bool(sliders or (ir and ir != current.get('IrCutFilter')))


class OnvifImaging:
    """Sliders forwarded to the physical camera's own ONVIF Imaging service;
    IrCutFilter optionally provided by a Tapo local client (night-vision mode)."""
    IR_TO_TAPO = {'ON': 'off', 'OFF': 'on', 'AUTO': 'auto'}
    TAPO_TO_IR = {v: k for k, v in IR_TO_TAPO.items()}

    def __init__(self, host, port, username, password, tapo=None):
        self.host, self.port = host, int(port)
        self.soap = SoapClient(username, password, timeout=3)
        self.xaddr = None
        self.source_token = None
        self.slider_range = (0, 100)
        self.sliders = []
        self.tapo = tapo
        self._cache = (0.0, None)
        self.probed = False
        self.stopped = False
        threading.Thread(target=self._probe, daemon=True, name=f"img-probe-{host}").start()

    @property
    def ir_modes(self):
        return ['ON', 'OFF', 'AUTO'] if (self.tapo and self.tapo.available) else []

    @property
    def available(self):
        # Until the probe finishes, assume yes (so capabilities stay stable while
        # the camera answers); afterwards only if there is something to control.
        return (not self.probed) or bool(self.sliders) or bool(self.ir_modes)

    def stop(self):
        self.stopped = True

    def _probe(self):
        delay = 5
        device = f"http://{self.host}:{self.port}/onvif/device_service"
        while not self.stopped:
            try:
                try:
                    self.soap.sync_clock(device)
                except Exception:
                    pass
                body = self.soap.call(device, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>', timeout=8)
                img = find_first(body, 'Imaging')
                media = find_first(body, 'Media')
                if img is None or find_first(img, 'XAddr') is None:
                    logger.info("Camera %s has no ONVIF imaging service", self.host)
                    self.probed = True
                    return
                self.xaddr = find_first(img, 'XAddr').text.strip()
                murl = find_first(media, 'XAddr').text.strip()
                prof = self.soap.call(murl, '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>', timeout=8)
                st = find_first(prof, 'SourceToken')
                self.source_token = st.text.strip() if st is not None else None
                opts = self.soap.call(self.xaddr, f'<timg:GetOptions><timg:VideoSourceToken>{self.source_token}</timg:VideoSourceToken></timg:GetOptions>', timeout=8)
                found = []
                for name in SLIDERS:
                    e = find_first(opts, name)
                    if e is not None and find_first(e, 'Max') is not None:
                        found.append(name)
                        self.slider_range = (float(find_first(e, 'Min').text), float(find_first(e, 'Max').text))
                self.sliders = found
                self.probed = True
                return
            except Exception as e:
                logger.warning("Imaging probe of %s failed: %s", self.host, e)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def get(self):
        ts, cached = self._cache
        if cached and time.time() - ts < 5:
            return cached
        out = dict(cached or {})
        if self.xaddr and self.source_token:
            try:
                body = self.soap.call(self.xaddr, f'<timg:GetImagingSettings><timg:VideoSourceToken>{self.source_token}</timg:VideoSourceToken></timg:GetImagingSettings>')
                for name in SLIDERS:
                    e = find_first(body, name)
                    if e is not None and e.text:
                        out[name] = float(e.text)
            except Exception as e:
                logger.debug("GetImagingSettings on %s failed: %s", self.host, e)
        if self.tapo and self.tapo.available:
            try:
                out['IrCutFilter'] = self.TAPO_TO_IR.get(self.tapo.get_day_night(), 'AUTO')
            except TapoUnavailable:
                pass
        self._cache = (time.time(), out)
        return out

    def set(self, values):
        current = self.get()
        changed = False
        sl = {k: v for k, v in values.items() if k in self.sliders and v is not None
              and round(float(v)) != round(current.get(k, -1))}
        if sl and self.xaddr:
            inner = ''.join(f'<tt:{k}>{float(sl[k])}</tt:{k}>' for k in SLIDERS if k in sl)
            try:
                self.soap.call(self.xaddr, f'<timg:SetImagingSettings><timg:VideoSourceToken>{self.source_token}</timg:VideoSourceToken>'
                                           f'<timg:ImagingSettings>{inner}</timg:ImagingSettings><timg:ForcePersistence>true</timg:ForcePersistence></timg:SetImagingSettings>')
                changed = True
            except Exception as e:
                raise ControlError(f"SetImagingSettings failed: {e}")
        ir = values.get('IrCutFilter')
        if ir and ir != current.get('IrCutFilter'):
            if not (self.tapo and self.tapo.available):
                raise ControlError("IrCutFilter control needs the Tapo cloud password")
            try:
                self.tapo.set_day_night(self.IR_TO_TAPO[ir])
                changed = True
            except (TapoUnavailable, KeyError) as e:
                raise ControlError(str(e))
        self._cache = (0.0, None)
        return changed


# ------------------------------------------------------------- controller
class DeviceController:
    def __init__(self, backend, host):
        self.backend = backend
        self.host = host
        self.ptz = None
        self.relays = {}
        self.imaging = None
        self.reolink = None
        self.tapo = None

    def stop(self):
        for r in self.relays.values():
            r.dispatch = None   # a replaced controller must not emit events any more
        if self.ptz:
            self.ptz.shutdown()
        if self.imaging:
            self.imaging.stop()
        if self.reolink:
            threading.Thread(target=self.reolink.close, daemon=True).start()

    def apply_settings(self, dc, ptz_enabled):
        """Per-camera settings that live on the shared device (siren length, PTZ
        profile/limits). Only the camera that exposes the feature applies it."""
        siren = self.relays.get('Siren')
        if siren and dc.get('sirenSeconds'):
            try:
                siren.configure(delay=int(dc['sirenSeconds']))
            except (ValueError, TypeError, ControlError):
                pass
        if self.ptz and ptz_enabled:
            ptz_cfg = dc.get('ptz') or {}
            tok = (ptz_cfg.get('profileToken') or '').strip()
            if tok and tok != self.ptz.profile_token:
                self.ptz.profile_token = tok
            try:
                self.ptz.max_move_seconds = float(ptz_cfg.get('maxMoveSeconds', self.ptz.max_move_seconds))
            except (TypeError, ValueError):
                pass

    def status(self):
        st = {"backend": self.backend, "host": self.host,
              "relays": {k: r.active for k, r in self.relays.items()},
              "ptz": self.ptz.status_summary() if self.ptz else None}
        if self.reolink:
            st["authError"] = self.reolink.auth_error
        if self.tapo:
            st["tapoLocal"] = {"configured": bool(self.tapo._password), "available": self.tapo.available,
                               "authError": self.tapo.auth_error, "lastError": self.tapo.last_error}
        return st


def build_reolink(host, cam, dc):
    ctl = DeviceController('reolink', host)
    client = ReolinkClient(host, cam.username, cam.password)
    ctl.reolink = client
    siren_s = int(dc.get('sirenSeconds', 10) or 10)
    ctl.relays['Floodlight'] = Relay('Floodlight', lambda: client.set_floodlight(True),
                                     lambda: client.set_floodlight(False), description="Floodlight")
    ctl.relays['Siren'] = Relay('Siren', lambda s: client.siren_pulse(s), client.siren_off,
                                monostable=True, delay=siren_s, description="Siren", skip_idle_off=True)
    ctl.relays['IR_Illuminator'] = Relay('IR_Illuminator', lambda: client.set_ir_lights('Auto'),
                                         lambda: client.set_ir_lights('Off'), description="IR illuminator")
    # Seed real states so the NVR sees the truth at first poll.
    def seed():
        try:
            ctl.relays['Floodlight'].active = bool(client.get_floodlight().get('on'))
            ctl.relays['IR_Illuminator'].active = client.get_ir_lights() != 'Off'
        except Exception as e:
            logger.info("Reolink %s initial state read failed: %s", host, e)
    threading.Thread(target=seed, daemon=True).start()
    ctl.imaging = ReolinkImaging(client)
    return ctl


def build_tapo(host, cam, dc, cloud_password):
    ctl = DeviceController('tapo', host)
    user = cam.onvif_forwarding_username or cam.username
    pw = cam.onvif_forwarding_password or cam.password
    port = cam.physical_onvif_port or 2020
    ptz_cfg = dc.get('ptz') or {}
    ctl.ptz = PtzController(host, port, user, pw, profile_token=ptz_cfg.get('profileToken') or None,
                            max_move_seconds=float(ptz_cfg.get('maxMoveSeconds', 30)))
    tapo = TapoLocalClient(host, cloud_password) if cloud_password else None
    ctl.tapo = tapo
    if tapo:
        siren_s = int(dc.get('sirenSeconds', 10) or 10)
        def siren_on(seconds):
            tapo.siren_on()
            return seconds
        ctl.relays['Spotlight'] = Relay('Spotlight', lambda: tapo.set_spotlight(True),
                                        lambda: tapo.set_spotlight(False), description="Spotlight")
        # The Tapo siren latches on the camera side, so the monostable timer must
        # actively switch it off (Reolink's 'times' mode stops by itself).
        ctl.relays['Siren'] = Relay('Siren', siren_on, tapo.siren_off, monostable=True, delay=siren_s,
                                    description="Siren", expire_off=tapo.siren_off)
        ctl.relays['Privacy'] = Relay('Privacy', lambda: tapo.set_privacy(True),
                                      lambda: tapo.set_privacy(False), description="Privacy mode")
    ctl.imaging = OnvifImaging(host, port, user, pw, tapo=tapo)
    return ctl


class Registry:
    """One DeviceController per physical device, keyed by (backend, host).

    The controller is rebuilt only when a *device-level* setting changes
    (credentials, port, cloud password). Per-camera settings are applied to the
    shared controller in place, so two virtual cameras on one device (the Tapo
    C575D's two lenses) share one session, one PTZ worker and one siren timer.

    Relay-change listeners are registered here by camera id rather than on
    Relay objects, so NVR event subscriptions survive a controller rebuild.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._controllers = {}   # key -> (device_sig, DeviceController)
        self._listeners = {}     # key -> {camera_id: callable(token, active)}
        self._applied = {}       # camera_id -> settings signature last applied
        self._decrypt_cache = {}

    @staticmethod
    def _key_and_host(cam):
        dc = getattr(cam, 'device_control', None) or {}
        backend = (dc.get('backend') or 'none').lower()
        if backend not in ('reolink', 'tapo'):
            return None, None, dc, backend
        host = dc.get('host') or urlparse(cam.main_stream_url).hostname
        if not host:
            return None, None, dc, backend
        return (backend, host), host, dc, backend

    def _cloud_password(self, dc):
        enc = dc.get('tapoCloudPasswordEnc') or ''
        if not enc:
            return ''
        if enc in self._decrypt_cache:
            return self._decrypt_cache[enc]
        try:
            from ..secrets_manager import secrets_manager
            value = secrets_manager.decrypt(enc) or ''
        except Exception:
            value = ''
        if value:   # don't cache failures: they may be transient
            self._decrypt_cache[enc] = value
        return value

    def _dispatch(self, key, token, active):
        for cb in list(self._listeners.get(key, {}).values()):
            try:
                cb(token, active)
            except Exception:
                pass

    def peek(self, cam):
        """The existing controller for this camera, without building one."""
        key, _, _, _ = self._key_and_host(cam)
        with self._lock:
            entry = self._controllers.get(key) if key else None
            return entry[1] if entry else None

    def controller_for(self, cam):
        key, host, dc, backend = self._key_and_host(cam)
        if not key:
            return None
        cloud_pw = self._cloud_password(dc) if backend == 'tapo' else ''
        device_sig = (cam.username, cam.password, cam.onvif_forwarding_username, cam.onvif_forwarding_password,
                      cam.physical_onvif_port, cloud_pw)
        with self._lock:
            entry = self._controllers.get(key)
            if entry and entry[0] == device_sig:
                ctl = entry[1]
            else:
                if entry:
                    entry[1].stop()
                ctl = build_reolink(host, cam, dc) if backend == 'reolink' else build_tapo(host, cam, dc, cloud_pw)
                for r in ctl.relays.values():
                    r.dispatch = lambda token, active, _k=key: self._dispatch(_k, token, active)
                self._controllers[key] = (device_sig, ctl)
                self._applied = {cid: v for cid, v in self._applied.items() if v[0] != key}
            ptz_enabled = bool((dc.get('ptz') or {}).get('enabled'))
            settings_sig = (key, repr(sorted((k, str(v)) for k, v in dc.items() if k != 'tapoCloudPasswordEnc')))
            if self._applied.get(cam.id) != settings_sig:
                ctl.apply_settings(dc, ptz_enabled)
                self._applied[cam.id] = settings_sig
            return ctl

    def add_listener(self, cam, cb):
        key, _, _, _ = self._key_and_host(cam)
        self.remove_listener(cam)
        if key:
            with self._lock:
                self._listeners.setdefault(key, {})[cam.id] = cb

    def remove_listener(self, cam):
        with self._lock:
            for listeners in self._listeners.values():
                listeners.pop(cam.id, None)


registry = Registry()


class CameraControl:
    """What one virtual camera exposes, filtered by its own config."""

    def __init__(self, cam, build=True):
        self.cam = cam
        self.dc = getattr(cam, 'device_control', None) or {}
        self.ctl = registry.controller_for(cam) if build else registry.peek(cam)

    @property
    def enabled(self):
        return self.ctl is not None

    @property
    def ptz(self):
        if not self.ctl or not self.ctl.ptz:
            return None
        return self.ctl.ptz if (self.dc.get('ptz') or {}).get('enabled') else None

    @property
    def relays(self):
        if not self.ctl:
            return []
        wanted = self.dc.get('relays')
        return [r for name, r in self.ctl.relays.items() if wanted is None or name in wanted]

    def relay(self, token):
        for r in self.relays:
            if r.token == token:
                return r
        return None

    @property
    def imaging(self):
        if not self.ctl or not self.dc.get('imaging', True) or not self.ctl.imaging:
            return None
        return self.ctl.imaging if getattr(self.ctl.imaging, 'available', True) else None
