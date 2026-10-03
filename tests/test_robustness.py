"""Robustness probes for suspected defects (shared controllers, retries, Stop latency, events)."""

import threading
import time

import pytest

from app.device_control import CameraControl
from app.onvif_service import VirtualSubscription
from tests.helpers import wait_for, fault_codes

PROFILE = 'mainStream_7'


def test_shared_host_cameras_with_different_settings_share_one_controller(make_camera, upstream):
    """Tapo Zoom (PTZ on, explicit profile) and Tapo Wide (PTZ off) point at the same device.
    Alternating access must not rebuild the controller each time (thread leak, login churn)."""
    zoom = make_camera(cam_id=2, device_control={'backend': 'tapo', 'host': '127.0.0.1',
                                                 'ptz': {'enabled': True, 'profileToken': 'prof1'}, 'sirenSeconds': 10},
                       physical_onvif_port=upstream.port)
    wide = make_camera(cam_id=4, device_control={'backend': 'tapo', 'host': '127.0.0.1',
                                                 'ptz': {'enabled': False}, 'sirenSeconds': 20},
                       physical_onvif_port=upstream.port)
    a1 = CameraControl(zoom).ctl
    b1 = CameraControl(wide).ctl
    a2 = CameraControl(zoom).ctl
    threads_before = threading.active_count()
    for _ in range(5):
        CameraControl(zoom), CameraControl(wide)
    leaked = threading.active_count() - threads_before
    assert a1 is b1 is a2, "controller rebuilt on every alternate access"
    assert leaked <= 0, f"{leaked} threads leaked by controller rebuilds"


def test_reolink_shared_host_rebuild_causes_relogins(make_camera, upstream):
    host = f'127.0.0.1:{upstream.port}'
    a = make_camera(cam_id=3, device_control={'backend': 'reolink', 'host': host, 'sirenSeconds': 10})
    b = make_camera(cam_id=5, device_control={'backend': 'reolink', 'host': host, 'sirenSeconds': 30})
    for _ in range(4):
        CameraControl(a).relay('Floodlight').set(True)
        CameraControl(b).relay('Floodlight').set(False)
    time.sleep(0.5)
    logins = upstream.ops('reolink').count('Login')
    assert logins <= 2, f"{logins} logins for 8 commands: controller thrash re-logs in each time"


def test_relay_events_survive_controller_rebuild(make_camera, upstream, onvif_client):
    """Changing the camera's credentials rebuilds the controller lazily; relay events must still reach
    the NVR's pull-point (ControlServices subscribed to the old Relay objects)."""
    host = f'127.0.0.1:{upstream.port}'
    cam = make_camera(device_control={'backend': 'reolink', 'host': host})
    c = onvif_client(cam)
    c.service.controls.attach()
    sub = VirtualSubscription('s')
    c.service.subscriptions['s'] = sub
    cam.password = 's3cretPW!'          # unchanged
    cam.onvif_forwarding_username = 'other'   # any field in the signature -> new controller
    CameraControl(cam).relay('Floodlight').set(True)
    assert not sub.queue.empty(), "relay change after controller rebuild produced no ONVIF event"


def test_relative_move_not_resent_on_read_timeout(make_camera, upstream, onvif_client):
    """RelativeMove is not idempotent: a slow camera must not receive it twice."""
    cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}},
                      physical_onvif_port=upstream.port)
    c = onvif_client(cam)
    ptz = CameraControl(cam).ptz
    assert wait_for(lambda: ptz.probed, 5)
    upstream.log.clear()
    upstream.ptz_delay = 2.3             # longer than the 2 s upstream timeout
    c.ptz(f'<tptz:RelativeMove><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:Translation>'
          '<tt:PanTilt x="0.1" y="0"/></tptz:Translation></tptz:RelativeMove>')
    time.sleep(5.5)
    n = upstream.ops('ptz_service').count('RelativeMove')
    assert n == 1, f"RelativeMove reached the camera {n} times"


def test_stop_not_starved_by_queued_jobs(make_camera, upstream):
    cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}},
                      physical_onvif_port=upstream.port)
    ptz = CameraControl(cam).ptz
    assert wait_for(lambda: ptz.probed, 5)
    ptz.continuous_move(0.5, 0)
    assert wait_for(lambda: 'ContinuousMove' in upstream.ops('ptz_service'), 2)
    upstream.ptz_delay = 0.8

    def rel():
        try:
            ptz.relative_move(0.05, 0)
        except Exception:
            pass
    ts = [threading.Thread(target=rel) for _ in range(4)]
    [t.start() for t in ts]
    time.sleep(0.1)
    t0 = time.time()
    ptz.stop()
    assert wait_for(lambda: 'Stop' in upstream.ops('ptz_service'), 8)
    stop_t = upstream.entries('Stop')[0][0]
    [t.join() for t in ts]
    assert stop_t - t0 < 2.0, f"Stop reached the camera {stop_t - t0:.1f}s after it was requested"
