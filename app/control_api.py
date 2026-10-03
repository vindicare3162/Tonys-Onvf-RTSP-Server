"""HTTP API for physical-device control.

Two audiences:

1. UniFi Protect itself, via Alarm Manager "Custom Webhook" actions and live
   view webhook shortcuts. Those can only fire a URL, so every control has a
   URL: /api/control/<camera_id>/<action>[/<arg>]. They are authenticated by a
   per-camera webhook token (query ?token= or header X-Token), not by the web
   UI session, because Protect cannot log in.
2. The web UI / owner: GET/PUT /api/cameras/<id>/device-control to view and
   change the device-control settings (behind the normal UI login).
"""

import hmac
import secrets
import threading
import time

from flask import jsonify, request

from .device_control import CameraControl, ControlError
from .device_control.ptz import PtzUnavailable

ACTIONS_HELP = {
    "light_on": "Floodlight/spotlight on", "light_off": "Floodlight/spotlight off",
    "siren_on": "Siren for ?seconds=N (default per camera, max 60)", "siren_off": "Stop siren",
    "ir_on": "IR illuminator on (auto)", "ir_off": "IR illuminator off",
    "privacy_on": "Privacy mode on", "privacy_off": "Privacy mode off",
    "daynight_auto": "Day/night auto", "daynight_day": "Force day/colour", "daynight_night": "Force night/B&W",
    "ptz_preset/<token-or-name>": "Go to a PTZ preset", "ptz_home": "Go to home",
    "ptz_stop": "Stop PTZ", "ptz_move?x=&y=&seconds=": "Continuous move (velocity -1..1)",
    "state": "Current relay/PTZ state",
}

_rate = {}
_rate_lock = threading.Lock()


def _rate_ok(camera_id, per_second=4):
    now = time.time()
    with _rate_lock:
        hist = [t for t in _rate.get(camera_id, []) if now - t < 1.0]
        if len(hist) >= per_second:
            _rate[camera_id] = hist
            return False
        hist.append(now)
        _rate[camera_id] = hist
        return True


def ensure_webhook_token(camera):
    dc = camera.device_control
    if dc and not dc.get('webhookToken'):
        dc['webhookToken'] = secrets.token_urlsafe(18)
        return True
    return False


def register(app, manager, login_required):

    def _light_relay(c):
        return c.relay('Floodlight') or c.relay('Spotlight')

    @app.route('/api/control/<int:camera_id>/<action>', methods=['GET', 'POST'])
    @app.route('/api/control/<int:camera_id>/<action>/<path:arg>', methods=['GET', 'POST'])
    def device_control_action(camera_id, action, arg=None):
        camera = manager.get_camera(camera_id)
        if not camera or not camera.device_control:
            return jsonify({'error': 'not found'}), 404
        expected = camera.device_control.get('webhookToken') or ''
        supplied = request.headers.get('X-Token') or request.args.get('token') or ''
        if not expected or not hmac.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')):
            return jsonify({'error': 'not found'}), 404
        if not _rate_ok(camera_id):
            return jsonify({'error': 'rate limited'}), 429

        c = CameraControl(camera)
        if not c.enabled:
            return jsonify({'error': 'device control not configured'}), 409

        def relay_set(r, active, **kw):
            if not r:
                return jsonify({'error': f'{action} is not available on this camera'}), 409
            r.set(active, **kw)
            return jsonify({'ok': True, 'relay': r.token, 'active': r.active})

        try:
            if action == 'state':
                st = c.ctl.status()
                st['relays'] = {r.token: r.active for r in c.relays}
                if c.ptz:
                    st['presets'] = c.ptz.get_presets()
                return jsonify(st)
            if action == 'help':
                return jsonify(ACTIONS_HELP)
            if action == 'light_on':
                return relay_set(_light_relay(c), True)
            if action == 'light_off':
                return relay_set(_light_relay(c), False)
            if action == 'siren_on':
                secs = request.args.get('seconds', type=float)
                r = c.relay('Siren')
                return relay_set(r, True, seconds=min(60, secs) if secs else None)
            if action == 'siren_off':
                return relay_set(c.relay('Siren'), False)
            if action in ('ir_on', 'ir_off'):
                return relay_set(c.relay('IR_Illuminator'), action == 'ir_on')
            if action in ('privacy_on', 'privacy_off'):
                return relay_set(c.relay('Privacy'), action == 'privacy_on')
            if action.startswith('daynight_'):
                mode = {'daynight_auto': 'AUTO', 'daynight_day': 'ON', 'daynight_night': 'OFF'}.get(action)
                img = c.imaging
                if not mode or img is None or mode not in img.ir_modes:
                    return jsonify({'error': 'day/night control not available on this camera'}), 409
                img.set({'IrCutFilter': mode})
                return jsonify({'ok': True, 'irCutFilter': mode})
            if action.startswith('ptz_'):
                ptz = c.ptz
                if not ptz:
                    return jsonify({'error': 'PTZ not enabled on this camera'}), 409
                if action == 'ptz_stop':
                    ptz.stop()
                    return jsonify({'ok': True})
                if action == 'ptz_home':
                    if not ptz.goto_home():
                        return jsonify({'error': 'no home position (save a preset named "Home")'}), 409
                    return jsonify({'ok': True})
                if action == 'ptz_preset':
                    presets = ptz.get_presets()
                    match = next((p for p in presets if p['token'] == arg), None) or \
                        next((p for p in presets if (p['name'] or '').lower() == (arg or '').lower()), None)
                    if not match:
                        return jsonify({'error': 'unknown preset', 'presets': presets}), 404
                    ptz.goto_preset(match['token'])
                    return jsonify({'ok': True, 'preset': match})
                if action == 'ptz_move':
                    x = request.args.get('x', 0.0, type=float)
                    y = request.args.get('y', 0.0, type=float)
                    secs = min(10.0, request.args.get('seconds', 1.0, type=float))
                    ptz.continuous_move(x, y, 0.0, secs)
                    return jsonify({'ok': True})
            return jsonify({'error': 'unknown action', 'actions': ACTIONS_HELP}), 400
        except (ControlError, PtzUnavailable) as e:
            return jsonify({'error': str(e)[:200]}), 502

    @app.route('/api/cameras/<int:camera_id>/device-control', methods=['GET'])
    @login_required
    def get_device_control(camera_id):
        camera = manager.get_camera(camera_id)
        if not camera:
            return jsonify({'error': 'Camera not found'}), 404
        return jsonify({'config': camera._device_control_public(), 'status': camera._device_control_status(),
                        'actions': ACTIONS_HELP})

    @app.route('/api/cameras/<int:camera_id>/device-control', methods=['PUT', 'POST'])
    @login_required
    def update_device_control(camera_id):
        camera = manager.get_camera(camera_id)
        if not camera:
            return jsonify({'error': 'Camera not found'}), 404
        data = request.get_json(silent=True) or {}
        try:
            return _apply_device_control(camera, data)
        except (ValueError, TypeError) as e:
            return jsonify({'error': f'invalid value: {e}'}), 400

    def _apply_device_control(camera, data):
        dc = dict(camera.device_control or {})
        backend = data.get('backend', dc.get('backend', 'none'))
        if backend not in ('none', 'reolink', 'tapo'):
            return jsonify({'error': 'backend must be none, reolink or tapo'}), 400
        dc['backend'] = backend
        if 'host' in data:
            dc['host'] = (data.get('host') or '').strip()
        if 'ptz' in data and isinstance(data['ptz'], dict):
            ptz = dict(dc.get('ptz') or {})
            for k in ('enabled', 'inProfile'):
                if k in data['ptz']:
                    ptz[k] = bool(data['ptz'][k])
            if 'profileToken' in data['ptz']:
                ptz['profileToken'] = (data['ptz']['profileToken'] or '').strip()
            if 'maxMoveSeconds' in data['ptz']:
                ptz['maxMoveSeconds'] = max(2, min(120, float(data['ptz']['maxMoveSeconds'])))
            dc['ptz'] = ptz
        if 'relays' in data:
            dc['relays'] = data['relays'] if isinstance(data['relays'], list) else None
            if dc['relays'] is None:
                dc.pop('relays')
        if 'imaging' in data:
            dc['imaging'] = bool(data['imaging'])
        if 'sirenSeconds' in data:
            dc['sirenSeconds'] = int(max(1, min(60, int(data['sirenSeconds']))))
        if 'tapoCloudPassword' in data:
            pw = data.get('tapoCloudPassword') or ''
            if pw:
                from .secrets_manager import secrets_manager
                enc = secrets_manager.encrypt(pw)
                if not enc:
                    return jsonify({'error': 'could not encrypt the password (cryptography unavailable)'}), 500
                dc['tapoCloudPasswordEnc'] = enc
            else:
                dc.pop('tapoCloudPasswordEnc', None)
        if data.get('regenerateWebhookToken') or not dc.get('webhookToken'):
            dc['webhookToken'] = secrets.token_urlsafe(18)
        if backend == 'none':
            dc = {'backend': 'none', 'webhookToken': dc.get('webhookToken')}
        camera.device_control = dc
        manager.save_config()
        if camera.onvif_service:
            try:
                camera.onvif_service.controls.attach()
            except Exception as e:
                return jsonify({'warning': f'saved, but controller failed to start: {e}',
                                'config': camera._device_control_public()}), 200
        return jsonify({'ok': True, 'config': camera._device_control_public(),
                        'status': camera._device_control_status(),
                        'note': 'Re-adopt the camera in UniFi Protect so it picks up new PTZ/relay/imaging capabilities.'})
