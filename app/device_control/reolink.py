"""Reolink HTTP API client for floodlight, siren and IR / day-night control.

Reolink cameras expose these controls only through their own JSON API at
/cgi-bin/api.cgi, never over ONVIF, so this is how the proxy bridges them into
UniFi Protect. Payload shapes follow the reolink_aio library (the de facto
reference implementation) and were checked read-only against an Elite Pro
Floodlight (F760P, fw v3.2.0.5241):

- Floodlight: SetWhiteLed with only `state` / `bright`. Sending `mode` or
  `LightingSchedule` would overwrite the owner's automatic-light settings.
- Siren: AudioAlarmPlay. Starting always uses "times" mode (N plays of about
  5 s) so the camera stops the siren by itself even if this process dies;
  the latched "manul" mode is never used to start it. Stopping early sends
  manual switch 1 then 0, because a bare switch 0 does not cancel a running
  "times" play (firmware quirk documented in reolink_aio).
- IR emitters: SetIrLights "Auto" / "Off" (this model has no "On").
- Day/night: SetIsp requires the complete Isp object, so it is read first and
  only `dayNight` is changed.

Sessions: the camera allows a limited number of concurrent logins and leases a
token for ~1 hour. One token is reused per client, refreshed shortly before
expiry under a lock, and re-acquired once when a call reports rspCode -6
("please login first"). Credential failures are never retried in a loop,
because repeated failures lock the account on the camera.
"""

import math
import threading
import time
import logging

import requests
import urllib3

from . import state

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

_CREDENTIAL_ERRORS = ('invalid user', 'login failed', 'password wrong', 'locked', 'user name or password')


class ReolinkError(Exception):
    pass


class ReolinkAuthError(ReolinkError):
    """Credentials rejected. Further logins are suppressed to avoid a lockout."""


class ReolinkClient:
    SIREN_MAX_SECONDS = 60

    def __init__(self, host, username, password, channel=0, timeout=6):
        self.host = host
        self.username = username
        self.password = password
        self.channel = channel
        # HTTPS first so the login password is not sent in clear JSON; the
        # camera uses a self-signed certificate. Falls back to HTTP once if
        # HTTPS is unreachable.
        self._schemes = ['https', 'http']
        self._http_since = None
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        self._lock = threading.RLock()
        self._cred_key = state.cred_key('reolink', host, username, password)
        failure = state.get_auth_failure(self._cred_key)
        self._auth_failed = failure['message'] if failure else None
        self._session = requests.Session()
        self._session.verify = False

    @property
    def base(self):
        # After falling back to HTTP, try HTTPS again hourly (a camera that was
        # merely rebooting shouldn't be left on plain HTTP forever).
        if self._http_since and time.time() - self._http_since > 3600:
            self._schemes = ['https', 'http']
            self._http_since = None
        return f"{self._schemes[0]}://{self.host}/cgi-bin/api.cgi"

    def _fall_back_to_http(self):
        if len(self._schemes) > 1:
            self._schemes.pop(0)
            self._http_since = time.time()
            return True
        return False

    # ------------------------------------------------------------------ session
    def _post(self, cmd, body, token=None):
        url = f"{self.base}?cmd={cmd}" + (f"&token={token}" if token else "")
        # Exception texts are never surfaced: requests puts the URL (with the
        # session token) in them.
        try:
            resp = self._session.post(url, json=body, timeout=self.timeout)
        except requests.exceptions.SSLError:
            if self._fall_back_to_http():
                return self._post(cmd, body, token)
            raise ReolinkError(f"{cmd}: TLS error talking to {self.host}")
        except requests.exceptions.ConnectionError:
            if self._schemes[0] == 'https' and self._fall_back_to_http():
                # Keep the fallback only if HTTP actually works; otherwise the camera
                # is simply down and HTTPS should be tried first next time.
                try:
                    return self._post(cmd, body, token)
                except ReolinkError:
                    self._schemes = ['https', 'http']
                    self._http_since = None
                    raise
            raise ReolinkError(f"{cmd}: cannot reach {self.host}")
        except requests.exceptions.Timeout:
            raise ReolinkError(f"{cmd}: {self.host} timed out")
        except requests.exceptions.RequestException as e:
            raise ReolinkError(f"{cmd}: request to {self.host} failed ({type(e).__name__})")
        if resp.status_code >= 400:
            raise ReolinkError(f"{cmd}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise ReolinkError(f"{cmd}: non-JSON response from {self.host}")
        if not isinstance(data, list):
            raise ReolinkError(f"{cmd}: unexpected response type")
        return data

    def _login(self):
        if self._auth_failed:
            raise ReolinkAuthError(self._auth_failed)
        if self._token:
            self._logout_quiet()
        body = [{"cmd": "Login", "param": {"User": {"Version": "0", "userName": self.username,
                                                     "password": self.password}}}]
        data = self._post("Login", body)
        item = data[0]
        if item.get('code') != 0:
            detail = str(item.get('error', {}).get('detail', item))
            if any(s in detail.lower() for s in _CREDENTIAL_ERRORS):
                self._auth_failed = f"Reolink {self.host} rejected the credentials: {detail}"
                state.set_auth_failure(self._cred_key, self._auth_failed)
                raise ReolinkAuthError(self._auth_failed)
            raise ReolinkError(f"Reolink login failed: {detail}")
        tok = item['value']['Token']
        self._token = tok['name']
        lease = int(tok.get('leaseTime', 3600))
        self._token_expiry = time.time() + max(60, lease - 300)

    def _logout_quiet(self):
        tok, self._token = self._token, None
        if not tok:
            return
        try:
            self._post("Logout", [{"cmd": "Logout", "param": {}}], tok)
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._logout_quiet()

    def _ensure_token(self):
        if not self._token or time.time() >= self._token_expiry:
            self._login()

    def call(self, cmd, param=None, action=None):
        """Run one command, re-logging in once if the token was rejected."""
        entry = {"cmd": cmd, "param": param or {}}
        if action is not None:
            entry["action"] = action
        with self._lock:
            for attempt in (1, 2):
                self._ensure_token()
                item = self._post(cmd, [entry], self._token)[0]
                if item.get('code') == 0:
                    return item.get('value', {})
                err = item.get('error', {})
                if err.get('rspCode') == -6 and attempt == 1:
                    self._token = None
                    continue
                raise ReolinkError(f"{cmd} failed: {err.get('detail', err)} (rspCode {err.get('rspCode')})")
        raise ReolinkError(f"{cmd} failed after re-login")

    @property
    def auth_error(self):
        return self._auth_failed

    # ------------------------------------------------------------- floodlight
    def get_floodlight(self):
        wl = self.call("GetWhiteLed", {"channel": self.channel}, action=0).get('WhiteLed', {})
        return {"on": bool(wl.get('state')), "brightness": wl.get('bright'), "mode": wl.get('mode')}

    def set_floodlight(self, on, brightness=None):
        wl = {"channel": self.channel, "state": 1 if on else 0}
        if brightness is not None:
            wl["bright"] = max(0, min(100, int(brightness)))
        self.call("SetWhiteLed", {"WhiteLed": wl})

    # ------------------------------------------------------------------ siren
    def siren_pulse(self, seconds):
        """Sound the siren for about `seconds` (rounded up to whole ~5 s plays, max 60 s).

        "times" mode means the camera stops on its own; nothing has to remember
        to turn it off.
        """
        seconds = max(1, min(self.SIREN_MAX_SECONDS, float(seconds)))
        plays = max(1, math.ceil(seconds / 5.0))
        self.call("AudioAlarmPlay", {"alarm_mode": "times", "times": plays, "channel": self.channel}, action=0)
        return plays * 5

    def siren_off(self):
        # Switch on then off: a bare manual_switch 0 does not cancel a "times" play.
        # The "off" half is retried, because if it were lost the siren would be left
        # latched in manual mode - the one state the design must never leave behind.
        first_error = None
        try:
            self.call("AudioAlarmPlay", {"alarm_mode": "manul", "manual_switch": 1, "channel": self.channel}, action=0)
        except ReolinkError as e:
            first_error = e
        last = None
        deadline = time.time() + 8   # bounded: this runs on a request thread
        for attempt in range(4):
            try:
                self.call("AudioAlarmPlay", {"alarm_mode": "manul", "manual_switch": 0, "channel": self.channel}, action=0)
                return
            except ReolinkError as e:
                last = e
                if time.time() + 0.5 * (attempt + 1) > deadline:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise last or first_error

    # ------------------------------------------------------------- IR / ISP
    def get_ir_lights(self):
        return self.call("GetIrLights", {"channel": self.channel}, action=0).get('IrLights', {}).get('state')

    def set_ir_lights(self, mode):
        if mode not in ("Auto", "Off", "On"):
            raise ValueError(mode)
        self.call("SetIrLights", {"IrLights": {"channel": self.channel, "state": mode}}, action=0)

    def get_isp(self):
        return self.call("GetIsp", {"channel": self.channel}, action=0).get('Isp', {})

    def set_day_night(self, mode):
        """mode: 'Auto' | 'Color' | 'Black&White'"""
        if mode not in ("Auto", "Color", "Black&White"):
            raise ValueError(mode)
        with self._lock:
            isp = self.get_isp()
            if not isp:
                raise ReolinkError("GetIsp returned no data")
            isp['dayNight'] = mode
            isp['channel'] = self.channel
            self.call("SetIsp", {"Isp": isp}, action=0)


    def get_image(self):
        """Return the camera's Image settings (bright/contrast/saturation/sharpen) if available."""
        return self.call("GetImage", {"channel": self.channel}, action=0).get('Image', {})

    def set_image(self, **values):
        with self._lock:
            img = self.get_image()
            if not img:
                raise ReolinkError("GetImage returned no data")
            for k, v in values.items():
                if v is not None:
                    img[k] = max(0, min(255, int(round(v))))
            img['channel'] = self.channel
            self.call("SetImage", {"Image": img}, action=0)
