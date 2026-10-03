"""Small persisted runtime state for device control.

Kept out of camera_config.json on purpose: save_config() has no lock and the
web UI rewrites that file, so runtime flags written there could be lost or
could clobber a concurrent UI save.

The one flag that must survive restarts is "these credentials were rejected".
Both Reolink and Tapo lock an account after a handful of failed logins, so a
container restart loop must not keep re-trying known-bad credentials. A flag
is keyed by a hash of host + user + password, so it clears itself as soon as
the credentials change.
"""

import hashlib
import json
import os
import threading
import time

from ..config import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "device_control_state.json")
_lock = threading.Lock()


def _load():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _hmac_key():
    """Key the credential fingerprint with the app's secret key, so the state
    file can't be used to test password guesses offline. The key is created on
    first use (not only when something is first encrypted), so a fingerprint
    recorded early still matches later."""
    try:
        from ..secrets_manager import secrets_manager
        return secrets_manager._load_or_create_key()
    except Exception:
        return b'device-control-state'


def cred_key(kind, host, user, password):
    import hmac
    h = hmac.new(_hmac_key(), f"{kind}|{host}|{user}|{password}".encode('utf-8'), hashlib.sha256).hexdigest()[:24]
    return f"{kind}:{host}:{h}"


def get_auth_failure(key):
    with _lock:
        return _load().get('auth_failures', {}).get(key)


def set_auth_failure(key, message):
    with _lock:
        data = _load()
        data.setdefault('auth_failures', {})[key] = {"message": message, "time": int(time.time())}
        _save(data)


def clear_auth_failure(key):
    with _lock:
        data = _load()
        if data.get('auth_failures', {}).pop(key, None) is not None:
            _save(data)
