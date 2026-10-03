"""Webhook API (/api/control/...), settings API, and deviceControl persistence."""

import json
import time

import pytest
from flask import Flask

from app import control_api
from tests.helpers import wait_for


class FakeManager:
    def __init__(self, cams):
        self.cams = {c.id: c for c in cams}
        self.saves = 0

    def get_camera(self, cid):
        return self.cams.get(cid)

    def save_config(self):
        self.saves += 1


def make_real_camera(cam_id, upstream_port=None, dc=None):
    from app.camera import VirtualONVIFCamera
    return VirtualONVIFCamera({
        'id': cam_id, 'name': f'Cam {cam_id}', 'mainStreamUrl': 'rtsp://admin:s3cretPW!@127.0.0.1:554/s1',
        'subStreamUrl': 'rtsp://admin:s3cretPW!@127.0.0.1:554/s2', 'username': 'admin', 'password': 's3cretPW!',
        'deviceControl': dc or {},
    })


@pytest.fixture
def api(upstream):
    def make(dc=None, cam_id=3):
        cam = make_real_camera(cam_id, dc=dc)
        mgr = FakeManager([cam])
        app = Flask('t')
        app.config['TESTING'] = True
        control_api.register(app, mgr, lambda f: f)
        control_api._rate.clear()
        return app.test_client(), cam, mgr
    return make


def reolink_dc(upstream, **kw):
    d = {'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}', 'webhookToken': 'hook-123'}
    d.update(kw)
    return d


def settle(upstream):
    assert wait_for(lambda: 'GetIrLights' in upstream.ops('reolink'), 5)
    time.sleep(0.05)
    upstream.log.clear()


@pytest.mark.parametrize("q", ['', '?token=', '?token=wrong', '?token=hook-12'])
def test_bad_token_404(api, upstream, q):
    c, cam, _ = api(reolink_dc(upstream))
    r = c.get(f'/api/control/3/light_on{q}')
    assert r.status_code == 404
    assert upstream.entries('SetWhiteLed') == []


def test_non_ascii_token_is_404_not_500(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    r = c.get('/api/control/3/light_on?token=%C3%A9')
    assert r.status_code == 404


def test_feature_off_404(api):
    c, cam, _ = api({})
    assert c.get('/api/control/3/light_on?token=').status_code == 404
    assert c.get('/api/control/99/light_on?token=x').status_code == 404


def test_light_on_off_and_header_token(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    r = c.get('/api/control/3/light_on?token=hook-123')
    assert r.status_code == 200, r.json
    assert r.json == {'ok': True, 'relay': 'Floodlight', 'active': True}
    time.sleep(0.3)
    r = c.post('/api/control/3/light_off', headers={'X-Token': 'hook-123'})
    assert r.status_code == 200 and r.json['active'] is False
    sets = [e[3]['param'] for e in upstream.entries('SetWhiteLed')]
    assert sets[-2:] == [{'WhiteLed': {'channel': 0, 'state': 1}}, {'WhiteLed': {'channel': 0, 'state': 0}}]


def test_siren_seconds_capped(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    r = c.get('/api/control/3/siren_on?token=hook-123&seconds=999')
    assert r.status_code == 200, r.json
    play = [e[3]['param'] for e in upstream.entries('AudioAlarmPlay')][-1]
    assert play == {'alarm_mode': 'times', 'times': 12, 'channel': 0}
    time.sleep(0.3)
    r = c.get('/api/control/3/siren_off?token=hook-123')
    assert r.status_code == 200
    plays = [e[3]['param'] for e in upstream.entries('AudioAlarmPlay')]
    assert plays[-2:] == [{'alarm_mode': 'manul', 'manual_switch': 1, 'channel': 0},
                          {'alarm_mode': 'manul', 'manual_switch': 0, 'channel': 0}]


def test_unavailable_action_409_and_unknown_400(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    assert c.get('/api/control/3/privacy_on?token=hook-123').status_code == 409
    time.sleep(0.3)
    assert c.get('/api/control/3/ptz_stop?token=hook-123').status_code == 409
    time.sleep(0.3)
    assert c.get('/api/control/3/explode?token=hook-123').status_code == 400


def test_rate_limit(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    codes = [c.get('/api/control/3/help?token=hook-123').status_code for _ in range(6)]
    assert codes[:4] == [200] * 4 and 429 in codes


def test_daynight_webhook(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    from app.device_control import CameraControl
    CameraControl(cam)          # build the controller so its seed read runs before we act
    settle(upstream)
    r = c.get('/api/control/3/daynight_night?token=hook-123')
    assert r.status_code == 200, r.json
    assert upstream.isp['dayNight'] == 'Black&White'


def test_state_does_not_leak_secrets(api, upstream):
    c, cam, _ = api(reolink_dc(upstream))
    r = c.get('/api/control/3/state?token=hook-123')
    assert r.status_code == 200
    assert 's3cretPW!' not in r.get_data(as_text=True)


# ------------------------------------------------------------ settings API
def test_settings_put_encrypts_cloud_password(api, upstream):
    c, cam, mgr = api({})
    r = c.put('/api/cameras/3/device-control', json={
        'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True, 'maxMoveSeconds': 500},
        'sirenSeconds': 999, 'tapoCloudPassword': 'Cl0udPass!'})
    assert r.status_code == 200, r.json
    text = r.get_data(as_text=True)
    assert 'Cl0udPass!' not in text and 'tapoCloudPasswordEnc' not in text
    assert r.json['config']['tapoCloudConfigured'] is True
    assert r.json['config']['webhookToken']
    enc = cam.device_control['tapoCloudPasswordEnc']
    assert enc and 'Cl0udPass!' not in enc
    from app.secrets_manager import secrets_manager
    assert secrets_manager.decrypt(enc) == 'Cl0udPass!'
    assert cam.device_control['ptz']['maxMoveSeconds'] == 120
    assert cam.device_control['sirenSeconds'] == 60
    assert mgr.saves == 1
    g = c.get('/api/cameras/3/device-control')
    assert 'Cl0udPass!' not in g.get_data(as_text=True) and 'tapoCloudPasswordEnc' not in g.get_data(as_text=True)
    # to_dict (the /api/cameras view) never carries the encrypted value either
    assert 'tapoCloudPasswordEnc' not in json.dumps(cam.to_dict(), default=str)
    # empty password clears it
    r = c.put('/api/cameras/3/device-control', json={'tapoCloudPassword': ''})
    assert 'tapoCloudPasswordEnc' not in cam.device_control
    assert r.json['config']['tapoCloudConfigured'] is False


def test_settings_bad_backend_and_bad_numbers(api):
    c, cam, _ = api({})
    assert c.put('/api/cameras/3/device-control', json={'backend': 'hikvision'}).status_code == 400
    r = c.put('/api/cameras/3/device-control', json={'backend': 'reolink', 'sirenSeconds': 'loud'})
    assert r.status_code == 400, f"non-numeric sirenSeconds should be a 400, got {r.status_code}"


def test_settings_backend_none_strips_config(api, upstream):
    c, cam, _ = api(reolink_dc(upstream, sirenSeconds=20))
    r = c.put('/api/cameras/3/device-control', json={'backend': 'none'})
    assert r.status_code == 200
    assert cam.device_control == {'backend': 'none', 'webhookToken': 'hook-123'}


def test_to_config_dict_round_trip(upstream):
    from app.camera import VirtualONVIFCamera
    dc = {'backend': 'tapo', 'host': '10.0.0.5', 'ptz': {'enabled': True, 'profileToken': 'p2'},
          'relays': ['Siren'], 'sirenSeconds': 15, 'tapoCloudPasswordEnc': 'gAAAAenc', 'webhookToken': 'w'}
    cam = make_real_camera(5, dc=dc)
    cfg = cam.to_config_dict()
    assert cfg['deviceControl'] == dc
    cam2 = VirtualONVIFCamera(json.loads(json.dumps(cfg)))
    assert cam2.device_control == dc
    pub = cam2._device_control_public()
    assert 'tapoCloudPasswordEnc' not in pub and pub['tapoCloudConfigured'] is True
    # absent -> feature off, persisted as {}
    cam3 = make_real_camera(6)
    assert cam3.to_config_dict()['deviceControl'] == {}
