"""PTZ control of a physical ONVIF camera on behalf of a virtual camera.

Design points (each one fixes a concrete failure mode):

- Discovery responses to the NVR are never built from a live camera call.
  The physical camera is probed once in the background (PTZ XAddr, PTZ
  profile token, node limits, clock offset); until that finishes, static
  defaults are reported.
- All upstream commands run on ONE worker thread per physical camera. Flask
  serves the NVR's requests on a thread pool, so a burst of joystick
  ContinuousMove/Stop requests can otherwise reach the camera out of order
  and leave it panning after the user let go. Moves and stops are
  latest-wins: queuing either discards any move or stop still waiting.
- Every upstream ContinuousMove carries a short Timeout and is re-sent about
  once a second while the move is held (dead-man refresh), capped at
  `max_move_seconds`. A lost Stop therefore costs at most ~2 seconds of
  movement instead of the camera's own default (3 minutes on the Tapo).
- Request threads wait a bounded time for the worker, so a dead camera can
  never tie up the WSGI pool that also serves streaming and event polling.
- Home position is emulated with a preset named "Home" when the camera has
  no native home (the Tapo reports HomeSupported=false).
"""

import logging
import queue
import threading
import time

from .soap import SoapClient, SoapFault, find_all, find_first

logger = logging.getLogger(__name__)

HOME_PRESET_NAME = "Home"
GENERIC = "http://www.onvif.org/ver10/tptz/PanTiltSpaces"
ZGENERIC = "http://www.onvif.org/ver10/tptz/ZoomSpaces"

DEFAULT_NODE = {
    "pan_tilt_continuous": True, "pan_tilt_absolute": True, "pan_tilt_relative": True,
    "zoom_continuous": False, "zoom_absolute": False, "zoom_relative": False,
    "max_presets": 8, "home_supported_native": False,
}


class PtzUnavailable(Exception):
    """Raised to the ONVIF layer when the camera can't be reached in time."""


def _clamp(v, lo=-1.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0


class PtzController:
    def __init__(self, host, port, username, password, profile_token=None,
                 max_move_seconds=30.0, refresh_interval=1.0, upstream_timeout=2.0):
        self.host = host
        self.port = int(port)
        self.device_url = f"http://{host}:{self.port}/onvif/device_service"
        self.ptz_url = None
        self.profile_token = profile_token or None
        self.node = dict(DEFAULT_NODE)
        self.max_move_seconds = float(max_move_seconds)
        self.refresh_interval = float(refresh_interval)
        self.soap = SoapClient(username, password, timeout=upstream_timeout)
        self.username, self.password = username, password
        self.probed = False
        self.last_error = None
        self._breaker_until = 0.0
        self._status_cache = (0.0, None)
        self._presets_cache = (0.0, None)

        self._jobs = queue.Queue()
        self._move_lock = threading.Lock()
        self._pending_motion = None   # latest-wins slot for move/stop
        self._wake = threading.Event()
        self._moving = None           # (x, y, zoom, deadline) while a move is held
        self._stop_worker = False
        threading.Thread(target=self._probe, daemon=True, name=f"ptz-probe-{host}").start()
        threading.Thread(target=self._worker, daemon=True, name=f"ptz-worker-{host}").start()

    # ------------------------------------------------------------ discovery
    def _probe(self):
        delay = 5
        while not self._stop_worker:
            try:
                self._probe_once()
                self.probed = True
                logger.info("PTZ probe of %s ok: profile=%s node=%s", self.host, self.profile_token, self.node)
                return
            except Exception as e:
                self.last_error = f"probe failed: {e}"
                logger.warning("PTZ probe of %s failed: %s (retry in %ss)", self.host, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def _probe_once(self):
        try:
            self.soap.sync_clock(self.device_url)
        except Exception as e:
            logger.info("PTZ clock sync with %s skipped: %s", self.host, e)
        body = self.soap.call(self.device_url, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>',
                              timeout=8)
        ptz_caps = find_first(body, 'PTZ')
        xaddr = find_first(ptz_caps, 'XAddr') if ptz_caps is not None else None
        if xaddr is None or not (xaddr.text or '').strip():
            raise RuntimeError("camera does not advertise a PTZ service")
        self.ptz_url = xaddr.text.strip()
        media = find_first(body, 'Media')
        media_url = find_first(media, 'XAddr').text.strip() if media is not None and find_first(media, 'XAddr') is not None else self.device_url

        if not self.profile_token:
            prof_body = self.soap.call(media_url, '<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>', timeout=8)
            for p in find_all(prof_body, 'Profiles'):
                if find_first(p, 'PTZConfiguration') is not None:
                    self.profile_token = p.get('token')
                    break
            if not self.profile_token:
                raise RuntimeError("no media profile carries a PTZConfiguration")

        nodes = self.soap.call(self.ptz_url, '<tptz:GetNodes/>', timeout=8)
        node = find_first(nodes, 'PTZNode')
        if node is not None:
            sp = find_first(node, 'SupportedPTZSpaces')
            has = lambda name: sp is not None and find_first(sp, name) is not None
            mp = find_first(node, 'MaximumNumberOfPresets')
            hs = find_first(node, 'HomeSupported')
            self.node = {
                "pan_tilt_continuous": has('ContinuousPanTiltVelocitySpace'),
                "pan_tilt_absolute": has('AbsolutePanTiltPositionSpace'),
                "pan_tilt_relative": has('RelativePanTiltTranslationSpace'),
                "zoom_continuous": has('ContinuousZoomVelocitySpace'),
                "zoom_absolute": has('AbsoluteZoomPositionSpace'),
                "zoom_relative": has('RelativeZoomTranslationSpace'),
                "max_presets": int(mp.text) if mp is not None and (mp.text or '').isdigit() else 0,
                "home_supported_native": (hs is not None and (hs.text or '').strip().lower() == 'true'),
            }

    @property
    def home_supported(self):
        # Native home, or emulated via a preset (needs at least one preset slot).
        return self.node["home_supported_native"] or self.node["max_presets"] > 0

    # --------------------------------------------------------------- worker
    def _submit(self, fn, wait=1.5):
        """Run fn on the worker; wait up to `wait` s for its result."""
        if time.time() < self._breaker_until:
            raise PtzUnavailable(self.last_error or "camera unreachable")
        done = threading.Event()
        box = {}
        self._jobs.put((fn, done, box))
        self._wake.set()
        if not done.wait(wait):
            # The caller has been told it failed: make sure the job never runs late.
            box['abandoned'] = True
            raise PtzUnavailable("camera did not respond in time")
        if 'error' in box:
            raise box['error']
        return box.get('result')

    def _submit_motion(self, motion):
        """Queue a move/stop, replacing any not yet sent. Returns immediately."""
        with self._move_lock:
            self._pending_motion = motion
        self._wake.set()

    def _upstream(self, body_xml, idempotent=True, force=False):
        """Send one PTZ request. Retries once on a fresh connection, but a
        non-idempotent command (RelativeMove, SetPreset...) is only retried when
        the failure happened before the request could have been delivered."""
        import requests
        if not self.ptz_url or not self.profile_token:
            raise PtzUnavailable(self.last_error or "PTZ not discovered yet")
        if time.time() < self._breaker_until and not force:
            raise PtzUnavailable(self.last_error or "camera unreachable")
        if not idempotent and time.time() - self.soap.last_used > 5:
            # Cameras (Tapo especially) drop idle keep-alive connections; a command
            # that must not be re-sent should start on a fresh connection.
            self.soap.reset_session()
        for attempt in (1, 2):
            try:
                result = self.soap.call(self.ptz_url, body_xml)
                self.last_error = None
                return result
            except SoapFault:
                raise
            except Exception as e:
                # A stale keep-alive connection fails like this too.
                self.soap.reset_session()
                not_sent = isinstance(e, requests.exceptions.ConnectTimeout) or (
                    isinstance(e, requests.exceptions.ConnectionError)
                    and 'NewConnectionError' in repr(e))
                retryable = (idempotent or not_sent) and not isinstance(e, requests.exceptions.ReadTimeout)
                if attempt == 2 or not retryable:
                    self.last_error = f"{type(e).__name__}"
                    # Trip the breaker only when the camera really looks down: a
                    # connect failure, or a failure on the retry. A single error
                    # after sending a non-idempotent command is reported but
                    # doesn't lock out PTZ.
                    if not_sent or attempt == 2:
                        self._breaker_until = time.time() + 10
                    raise PtzUnavailable(f"camera unreachable ({type(e).__name__})")

    def shutdown(self):
        self._stop_worker = True
        self._wake.set()

    def _take_motion(self):
        with self._move_lock:
            motion, self._pending_motion = self._pending_motion, None
        return motion

    def _worker(self):
        while not self._stop_worker:
            try:
                self._worker_iteration()
            except Exception as e:
                # The worker must never die: a dead worker means Stop can't reach
                # the camera. Fail safe by forgetting any held move.
                logger.warning("PTZ worker error on %s: %s", self.host, e)
                self._moving = None
                time.sleep(0.2)

    def _worker_iteration(self):
        if self._moving:
            # Wake for the next refresh, or exactly at the move's deadline if sooner.
            wait = max(0.05, min(self.refresh_interval, self._moving[3] - time.time()))
        else:
            wait = 5
        self._wake.wait(timeout=wait)
        self._wake.clear()

        motion = self._take_motion()
        if motion is not None:
            self._apply_motion(motion)

        # One job at a time, re-checking for a newer move/stop between jobs, so
        # a Stop is never stuck behind a queue of slow commands.
        while not self._stop_worker:
            try:
                fn, done, box = self._jobs.get_nowait()
            except queue.Empty:
                break
            if not box.get('abandoned'):
                try:
                    box['result'] = fn()
                except Exception as e:
                    box['error'] = e
            done.set()
            newer = self._take_motion()
            if newer is not None:
                motion = newer
                self._apply_motion(newer)

        if self._moving and motion is None:
            x, y, z, deadline = self._moving
            if time.time() >= deadline:
                self._apply_motion(('stop',))
            else:
                try:
                    self._send_move(x, y, z, deadline - time.time())
                except Exception as e:
                    # Refresh failed: stop holding the move and try to stop the head.
                    logger.warning("PTZ refresh on %s failed: %s", self.host, e)
                    self._apply_motion(('stop',))

    def _apply_motion(self, motion):
        try:
            if motion[0] == 'stop':
                self._moving = None
                # Stops always go out, even while the breaker is open.
                self._upstream(self._stop_xml(), force=True)
            else:
                _, x, y, z, duration = motion
                deadline = time.time() + min(duration or self.max_move_seconds, self.max_move_seconds)
                self._moving = (x, y, z, deadline)
                self._send_move(x, y, z, deadline - time.time())
        except Exception as e:
            logger.warning("PTZ %s on %s failed: %s", motion[0], self.host, e)

    def _send_move(self, x, y, z, remaining=None):
        vel = ''
        if (x or y) and self.node["pan_tilt_continuous"]:
            vel += f'<tt:PanTilt x="{x:.4f}" y="{y:.4f}" space="{GENERIC}/VelocityGenericSpace"/>'
        if z and self.node["zoom_continuous"]:
            vel += f'<tt:Zoom x="{z:.4f}" space="{ZGENERIC}/VelocityGenericSpace"/>'
        if not vel:
            return
        # Upstream Timeout: long enough to bridge to the next refresh, never past
        # the move's own deadline, so a lost Stop costs at most ~2 s of movement.
        # Whole seconds only: the Tapo's SOAP server breaks on fractional xs:duration
        # values (it answers "PT1.0S" with a corrupted HTTP response). Stopping on
        # time is the worker's job anyway; this is only the safety net.
        window = self.refresh_interval * 2
        if remaining is not None:
            window = min(window, remaining)
        window = max(1, int(window + 0.999))
        self._upstream(f'<tptz:ContinuousMove><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                       f'<tptz:Velocity>{vel}</tptz:Velocity><tptz:Timeout>PT{window}S</tptz:Timeout>'
                       '</tptz:ContinuousMove>')

    def _stop_xml(self):
        zoom = 'true' if self.node["zoom_continuous"] else 'false'
        return (f'<tptz:Stop><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                f'<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>{zoom}</tptz:Zoom></tptz:Stop>')

    # ------------------------------------------------------------- commands
    def continuous_move(self, x=0.0, y=0.0, zoom=0.0, timeout=None):
        x, y, zoom = _clamp(x), _clamp(y), _clamp(zoom)
        if not self.node["zoom_continuous"]:
            zoom = 0.0
        if x == 0 and y == 0 and zoom == 0:
            return self.stop()
        if time.time() < self._breaker_until:
            raise PtzUnavailable(self.last_error or "camera unreachable")
        self._submit_motion(('move', x, y, zoom, timeout))

    def stop(self):
        self._submit_motion(('stop',))

    def _speed_xml(self, speed):
        return f'<tptz:Speed><tt:PanTilt x="{_clamp(speed, 0, 1)}" y="{_clamp(speed, 0, 1)}"/></tptz:Speed>' if speed else ''

    def relative_move(self, x=0.0, y=0.0, zoom=0.0, speed=None):
        tr = ''
        if (x or y) and self.node["pan_tilt_relative"]:
            tr += f'<tt:PanTilt x="{_clamp(x):.4f}" y="{_clamp(y):.4f}"/>'
        if zoom and self.node["zoom_relative"]:
            tr += f'<tt:Zoom x="{_clamp(zoom):.4f}"/>'
        if not tr:
            return
        self._moving = None
        body = (f'<tptz:RelativeMove><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                f'<tptz:Translation>{tr}</tptz:Translation>{self._speed_xml(speed)}</tptz:RelativeMove>')
        self._submit(lambda: self._upstream(body, idempotent=False))

    def absolute_move(self, x=None, y=None, zoom=None, speed=None):
        pos = ''
        if x is not None and y is not None and self.node["pan_tilt_absolute"]:
            pos += f'<tt:PanTilt x="{_clamp(x):.4f}" y="{_clamp(y):.4f}"/>'
        if zoom is not None and self.node["zoom_absolute"]:
            pos += f'<tt:Zoom x="{_clamp(zoom, 0, 1):.4f}"/>'
        if not pos:
            return
        self._moving = None
        body = (f'<tptz:AbsoluteMove><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                f'<tptz:Position>{pos}</tptz:Position>{self._speed_xml(speed)}</tptz:AbsoluteMove>')
        self._submit(lambda: self._upstream(body))

    def get_status(self):
        ts, cached = self._status_cache
        if cached and time.time() - ts < 1.0:
            return cached

        def fetch():
            body = self._upstream(f'<tptz:GetStatus><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken></tptz:GetStatus>')
            pos = find_first(body, 'Position')
            pt = find_first(pos, 'PanTilt') if pos is not None else None
            zm = find_first(pos, 'Zoom') if pos is not None else None
            mv = find_first(body, 'MoveStatus')
            mpt = find_first(mv, 'PanTilt') if mv is not None else None
            def f(e, a):
                try:
                    return float(e.get(a)) if e is not None and e.get(a) is not None else None
                except ValueError:
                    return None
            status = (mpt.text or '').strip().upper() if mpt is not None else ''
            if status not in ('IDLE', 'MOVING'):
                status = 'MOVING' if self._moving else 'IDLE'
            return {"pan": f(pt, 'x'), "tilt": f(pt, 'y'), "zoom": f(zm, 'x'), "move_status": status}
        try:
            st = self._submit(fetch, wait=2.0)
            self._status_cache = (time.time(), st)
            return st
        except Exception:
            if cached:
                return cached
            return {"pan": None, "tilt": None, "zoom": None, "move_status": 'MOVING' if self._moving else 'IDLE'}

    def get_presets(self, max_age=3.0):
        ts, cached = self._presets_cache
        if cached is not None and time.time() - ts < max_age:
            return cached

        def fetch():
            body = self._upstream(f'<tptz:GetPresets><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken></tptz:GetPresets>')
            out = []
            for p in find_all(body, 'Preset'):
                name = find_first(p, 'Name')
                pos = find_first(p, 'PanTilt')
                out.append({"token": p.get('token'), "name": (name.text if name is not None else None) or p.get('token'),
                            "pan": pos.get('x') if pos is not None else None,
                            "tilt": pos.get('y') if pos is not None else None})
            return out
        try:
            presets = self._submit(fetch, wait=2.5)
            self._presets_cache = (time.time(), presets)
            return presets
        except Exception:
            if cached is not None:
                return cached
            raise

    def goto_preset(self, token, speed=None):
        self._moving = None
        body = (f'<tptz:GotoPreset><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                f'<tptz:PresetToken>{token}</tptz:PresetToken>{self._speed_xml(speed)}</tptz:GotoPreset>')
        self._submit(lambda: self._upstream(body))

    def set_preset(self, name=None, token=None):
        inner = f'<tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
        if name:
            from xml.sax.saxutils import escape
            inner += f'<tptz:PresetName>{escape(name)}</tptz:PresetName>'
        if token:
            inner += f'<tptz:PresetToken>{token}</tptz:PresetToken>'

        def go():
            body = self._upstream(f'<tptz:SetPreset>{inner}</tptz:SetPreset>', idempotent=False)
            t = find_first(body, 'PresetToken')
            return t.text if t is not None else token
        result = self._submit(go, wait=3.0)
        self._presets_cache = (0.0, None)
        return result

    def remove_preset(self, token):
        self._submit(lambda: self._upstream(
            f'<tptz:RemovePreset><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
            f'<tptz:PresetToken>{token}</tptz:PresetToken></tptz:RemovePreset>'), wait=3.0)
        self._presets_cache = (0.0, None)

    def _home_preset(self):
        for p in self.get_presets(max_age=0):
            if (p["name"] or '').strip().lower() == HOME_PRESET_NAME.lower():
                return p
        return None

    def goto_home(self, speed=None):
        if self.node["home_supported_native"]:
            self._moving = None
            self._submit(lambda: self._upstream(
                f'<tptz:GotoHomePosition><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>'
                f'{self._speed_xml(speed)}</tptz:GotoHomePosition>'))
            return True
        home = self._home_preset()
        if not home:
            return False
        self.goto_preset(home["token"], speed)
        return True

    def set_home(self):
        if self.node["home_supported_native"]:
            self._submit(lambda: self._upstream(
                f'<tptz:SetHomePosition><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken></tptz:SetHomePosition>'))
            return
        home = self._home_preset()
        self.set_preset(HOME_PRESET_NAME, home["token"] if home else None)

    def status_summary(self):
        return {"probed": self.probed, "profileToken": self.profile_token, "node": self.node,
                "lastError": self.last_error, "moving": bool(self._moving)}
