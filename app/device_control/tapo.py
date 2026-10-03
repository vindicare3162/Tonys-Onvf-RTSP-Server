"""Tapo local control API (siren, spotlight, night-vision mode, privacy).

Tapo cameras expose these only through their own local HTTPS API, driven
here with the pytapo library. That API rejects the "camera account" used for
RTSP/ONVIF; it needs user "admin" with the owner's TP-Link cloud password and
"Third-Party Compatibility" enabled in the Tapo app. Everything stays local.

Lockout safety: the camera suspends logins after ~10 failed attempts, and
pytapo retries internally unless told not to. So:
- pytapo is created with retryStok=False,
- a login is attempted at most once per credential set per process, and a
  rejection is persisted (state.py) so restarts don't burn attempts either,
- the backend is inert unless a cloud password is configured and decrypts to
  a non-empty value (secrets_manager returns '' on key mismatch, which must be
  treated as "not configured", never as an empty password).
"""

import logging
import threading

from . import state

logger = logging.getLogger(__name__)


class TapoUnavailable(Exception):
    pass


class TapoLocalClient:
    def __init__(self, host, cloud_password, user='admin'):
        self.host = host
        self.user = user
        self._password = cloud_password
        self._tapo = None
        self._lock = threading.Lock()
        self._attempted = False
        self._cred_key = state.cred_key('tapo', host, user, cloud_password)
        failure = state.get_auth_failure(self._cred_key)
        self.auth_error = failure['message'] if failure else None
        self.last_error = None

    @property
    def available(self):
        return bool(self._password) and not self.auth_error

    def _client(self):
        if self._tapo is not None:
            return self._tapo
        if not self._password:
            raise TapoUnavailable("Tapo cloud password not configured")
        if self.auth_error:
            raise TapoUnavailable(self.auth_error)
        if self._attempted:
            raise TapoUnavailable(self.last_error or "Tapo login already failed this session")
        self._attempted = True
        try:
            from pytapo import Tapo
        except ImportError:
            self.last_error = "pytapo is not installed"
            raise TapoUnavailable(self.last_error)
        try:
            self._tapo = Tapo(self.host, self.user, self._password, self._password,
                              retryStok=False, printDebugInformation=False, printWarnInformation=False)
            self._tapo.getBasicInfo()
            self.last_error = None
            return self._tapo
        except Exception as e:
            msg = str(e)
            self._tapo = None
            if 'invalid authentication' in msg.lower() or 'suspension' in msg.lower() or '-40401' in msg:
                self.auth_error = f"Tapo {self.host} rejected the cloud-password login: {msg[:120]}"
                state.set_auth_failure(self._cred_key, self.auth_error)
                raise TapoUnavailable(self.auth_error)
            # Network-type failure: allow a later attempt.
            self._attempted = False
            self.last_error = f"Tapo {self.host} unreachable: {type(e).__name__}"
            raise TapoUnavailable(self.last_error)

    def _do(self, fn):
        with self._lock:
            tapo = self._client()
            try:
                return fn(tapo)
            except TapoUnavailable:
                raise
            except Exception as e:
                # Session may have expired; drop it so the next call re-logs in
                # (re-login with known-good credentials is safe).
                self._tapo = None
                self._attempted = False
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                raise TapoUnavailable(self.last_error)

    # Siren
    def siren_on(self):
        self._do(lambda t: t.startManualAlarm())

    def siren_off(self):
        self._do(lambda t: t.stopManualAlarm())

    # Spotlight (forced white lamp)
    def set_spotlight(self, on):
        self._do(lambda t: t.setForceWhitelampState(bool(on)))

    def get_spotlight(self):
        return self._do(lambda t: t.getForceWhitelampState())

    # Privacy (lens mask)
    def set_privacy(self, on):
        self._do(lambda t: t.setPrivacyMode(bool(on)))

    def get_privacy(self):
        return self._do(lambda t: (t.getPrivacyMode() or {}).get('enabled') == 'on')

    # Night vision: 'on' = forced IR night mode, 'off' = day, 'auto'
    def set_day_night(self, mode):
        self._do(lambda t: t.setDayNightMode(mode))

    def get_day_night(self):
        return self._do(lambda t: t.getDayNightMode())
