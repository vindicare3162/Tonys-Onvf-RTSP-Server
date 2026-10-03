"""Regression tests for the fixes made after the first QA pass."""

import hashlib
import json
import threading
import time

import pytest

from app.device_control import CameraControl
from app.device_control.controller import Relay, registry
from app.device_control.reolink import ReolinkClient, ReolinkError
from app.onvif_service import VirtualSubscription
from tests.helpers import NS, body_child, wait_for

PROFILE = 'mainStream_7'


def tapo(make_camera, upstream, cam_id=7, **dc):
    cfg = {'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}}
    cfg.update(dc)
    return make_camera(cam_id=cam_id, device_control=cfg, physical_onvif_port=upstream.port)


@pytest.fixture
def probed_ptz(make_camera, upstream):
    cam = tapo(make_camera, upstream)
    ptz = CameraControl(cam).ptz
    assert wait_for(lambda: ptz.probed, 5)
    upstream.log.clear()
    return ptz


# ------------------------------------------------------------------ PTZ
def test_stop_bypasses_open_breaker(probed_ptz, upstream):
    probed_ptz._breaker_until = time.time() + 10
    probed_ptz.stop()
    assert wait_for(lambda: 'Stop' in upstream.ops('ptz_service'), 3)


def test_breaker_blocks_other_upstream_calls(probed_ptz, upstream):
    from app.device_control.ptz import PtzUnavailable
    probed_ptz._breaker_until = time.time() + 10
    with pytest.raises(PtzUnavailable):
        probed_ptz.continuous_move(0.5, 0)
    with pytest.raises(PtzUnavailable):
        probed_ptz.goto_preset('1')
    time.sleep(0.3)
    assert upstream.ops('ptz_service') == []


def test_abandoned_job_never_runs(probed_ptz, upstream):
    upstream.ptz_delay = 1.2
    ths = [threading.Thread(target=lambda: _quiet(lambda: probed_ptz.relative_move(0.05, 0))),
           threading.Thread(target=lambda: _quiet(lambda: probed_ptz.absolute_move(0.1, 0.1)))]
    for t in ths:
        t.start()
        time.sleep(0.05)
    with pytest.raises(Exception):
        probed_ptz.goto_preset('1')      # queued third; caller gives up at 1.5 s
    [t.join() for t in ths]
    time.sleep(1.5)
    assert 'GotoPreset' not in upstream.ops('ptz_service'), upstream.ops('ptz_service')


def _quiet(fn):
    try:
        fn()
    except Exception:
        pass


def test_set_preset_not_resent_on_read_timeout(probed_ptz, upstream):
    upstream.ptz_delay = 2.3
    _quiet(lambda: probed_ptz.set_preset('Gate'))
    time.sleep(5)
    assert upstream.ops('ptz_service').count('SetPreset') == 1


def test_get_presets_retried_on_fresh_connection(probed_ptz, upstream):
    """Idempotent reads may retry once when the kept-alive connection was dropped."""
    probed_ptz.get_presets(max_age=0)
    probed_ptz.soap._session.close()
    assert probed_ptz.get_presets(max_age=0)


def test_refresh_failure_stops_head(probed_ptz, upstream):
    probed_ptz.continuous_move(0.4, 0)
    assert wait_for(lambda: 'ContinuousMove' in upstream.ops('ptz_service'), 2)
    upstream.ptz_fail_ops.add('ContinuousMove')
    assert wait_for(lambda: 'Stop' in upstream.ops('ptz_service'), 3)
    assert probed_ptz._moving is None


# ---------------------------------------------------- registry / sharing
def test_per_camera_settings_applied_to_shared_controller(make_camera, upstream):
    zoom = tapo(make_camera, upstream, cam_id=2, ptz={'enabled': True, 'profileToken': 'profX', 'maxMoveSeconds': 7})
    wide = tapo(make_camera, upstream, cam_id=4, ptz={'enabled': False, 'profileToken': 'other'})
    a = CameraControl(zoom).ctl
    b = CameraControl(wide).ctl
    assert a is b
    assert a.ptz.profile_token == 'profX'          # PTZ-disabled camera doesn't override
    assert a.ptz.max_move_seconds == 7
    assert CameraControl(wide).ptz is None and CameraControl(zoom).ptz is a.ptz


def test_device_level_change_stops_old_controller(make_camera, upstream):
    cam = tapo(make_camera, upstream)
    old = CameraControl(cam).ctl
    cam.onvif_forwarding_password = 'changed'
    new = CameraControl(cam).ctl
    assert new is not old
    assert old.ptz._stop_worker is True
    assert getattr(old.imaging, 'stopped', False) is True


def test_events_only_for_relays_the_camera_exposes(make_camera, upstream, onvif_client):
    host = f'127.0.0.1:{upstream.port}'
    a = make_camera(cam_id=3, device_control={'backend': 'reolink', 'host': host, 'relays': ['Floodlight']})
    b = make_camera(cam_id=5, device_control={'backend': 'reolink', 'host': host, 'relays': ['Siren']})
    ca, cb = onvif_client(a), onvif_client(b)
    ca.service.controls.attach()
    cb.service.controls.attach()
    sa, sb = VirtualSubscription('a'), VirtualSubscription('b')
    ca.service.subscriptions['a'] = sa
    cb.service.subscriptions['b'] = sb
    CameraControl(a).relay('Floodlight').set(True)
    assert sa.queue.get(timeout=1)['relay_token'] == 'Floodlight'
    assert sb.queue.empty()
    ca.service.controls.detach()
    CameraControl(a).relay('Floodlight').set(False)
    assert sa.queue.empty()


def test_ui_status_does_not_build_controller(upstream):
    from tests.test_control_api import make_real_camera
    cam = make_real_camera(3, dc={'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}'})
    d = cam.to_dict()
    assert d['deviceControlStatus'] is None
    assert registry.peek(cam) is None
    time.sleep(0.3)
    assert upstream.ops('reolink') == []


# ------------------------------------------------------------- relays
def test_relay_generation_ignores_stale_expiry():
    calls = []
    r = Relay('Siren', lambda s: calls.append(('on', s)) or s, lambda: calls.append('off'), monostable=True, delay=10)
    r.set(True)
    stale = r._gen
    r.set(True)
    r._expire(stale)                 # timer of the first activation firing late
    assert r.active
    r._timer.cancel()


def test_monostable_seconds_clamped():
    seen = []
    r = Relay('Siren', lambda s: seen.append(s) or s, lambda: None, monostable=True, delay=10, max_delay=60)
    r.set(True, seconds=999)
    r.set(True, seconds=-5)
    r._timer.cancel()
    assert seen == [60.0, 1.0]


def test_inactive_monostable_off_is_noop_only_when_skip_idle_off():
    # Reolink siren (skip_idle_off): off while idle sends nothing, because its stop
    # sequence would chirp the siren.
    offs = []
    r = Relay('Siren', lambda s: s, lambda: offs.append(1), monostable=True, delay=10, skip_idle_off=True)
    r.set(False)
    assert offs == []
    # Tapo siren (latched): off must always go through, e.g. to stop a siren that
    # was started before a restart or from the vendor app.
    offs2 = []
    r2 = Relay('Siren', lambda s: s, lambda: offs2.append(1), monostable=True, delay=10)
    r2.set(False)
    assert offs2 == [1]


def test_latched_expire_off_retries_and_stays_active(monkeypatch):
    attempts = []

    def flaky_off():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("camera busy")
    r = Relay('Siren', lambda s: s, lambda: None, monostable=True, delay=10, expire_off=flaky_off)
    real_timer = threading.Timer
    monkeypatch.setattr(threading, 'Timer', lambda t, fn, args=(), **kw: real_timer(0.05, fn, args=args))
    r.set(True)
    assert wait_for(lambda: len(attempts) >= 1, 2)
    assert wait_for(lambda: not r.active, 2)
    assert len(attempts) == 2


# ------------------------------------------------------------- Reolink
def _client(upstream):
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 's3cretPW!')
    cl._schemes = ['http']
    return cl


def test_siren_off_retries_switch_zero(upstream, monkeypatch):
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    upstream.siren_off_failures = 2
    _client(upstream).siren_off()
    switches = [e[3]['param']['manual_switch'] for e in upstream.entries('AudioAlarmPlay')]
    assert switches == [1, 0, 0, 0]


def test_non_json_response_is_sanitised_error(upstream):
    cl = _client(upstream)
    cl.set_floodlight(True)
    upstream.garbage_cmds.add('GetWhiteLed')
    with pytest.raises(ReolinkError) as ei:
        cl.get_floodlight()
    assert cl._token not in str(ei.value)


def test_https_retried_after_an_hour(upstream):
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 's3cretPW!')
    cl.set_floodlight(True)
    assert cl._schemes == ['http']
    cl._http_since = time.time() - 3700
    assert cl.base.startswith('https://')
    cl.set_floodlight(False)                  # https fails again -> back to http, still works
    assert upstream.whiteled['state'] == 0


def test_state_key_is_not_plain_sha256(upstream):
    from app.device_control import state
    host = f'127.0.0.1:{upstream.port}'
    key = state.cred_key('reolink', host, 'admin', 'pw')
    plain = hashlib.sha256(f"reolink|{host}|admin|pw".encode()).hexdigest()[:24]
    assert plain not in key


def test_state_key_stable_when_secret_key_created_later(tmp_path, upstream, monkeypatch):
    """A persisted auth failure must still match after the app's secret key file appears."""
    from app.device_control import state
    from app import secrets_manager as sm
    keyfile = tmp_path / 'later.key'
    monkeypatch.setattr(sm, 'KEY_FILE', str(keyfile))
    before = state.cred_key('reolink', 'h', 'u', 'p')
    keyfile.write_bytes(b'generated-later')
    after = state.cred_key('reolink', 'h', 'u', 'p')
    assert before == after, "auth-failure flag is lost (one more login attempt) once the key file is created"


# -------------------------------------------------------- namespaces
def test_device_service_relay_replies_in_tds(make_camera, upstream, onvif_client):
    cam = make_camera(device_control={'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}'})
    c = onvif_client(cam)
    r = c.device('<tds:GetRelayOutputs/>')
    assert body_child(r).tag == f'{{{NS["tds"]}}}GetRelayOutputsResponse'
    r = c.deviceio('<tmd:GetRelayOutputs/>')
    child = body_child(r)
    assert child.tag == f'{{{NS["tmd"]}}}GetRelayOutputsResponse'
    assert all(e.tag == f'{{{NS["tmd"]}}}RelayOutputs' for e in child)
