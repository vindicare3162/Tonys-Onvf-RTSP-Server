"""Per-camera `reportedMac`: the MAC the proxy reports as its ONVIF identity.

UniFi Protect keys a third-party camera on the first enabled interface's
GetNetworkInterfaces HwAddress. `reportedMac` lets a proxied camera present the
physical camera's MAC (so Protect keeps the existing record) while the macvlan
vNIC keeps using `nicMac` on the wire. Unset -> identity is nicMac as before.
"""

import pytest

from tests.helpers import NS, body_child, parse

NIC_MAC = '02:aa:bb:cc:dd:01'
PHYS_MAC = 'EC:71:DB:12:34:56'


def make_real_camera(**extra):
    from app.camera import VirtualONVIFCamera
    cfg = {
        'id': 9, 'name': 'Back Gate', 'uuid': '11111111-2222-3333-4444-555555555555',
        'mainStreamUrl': 'rtsp://admin:pw@127.0.0.1:554/s1',
        'subStreamUrl': 'rtsp://admin:pw@127.0.0.1:554/s2',
        'onvifUsername': 'onvifuser', 'onvifPassword': 'onvifpass',
        'nicMac': NIC_MAC,
    }
    cfg.update(extra)
    return VirtualONVIFCamera(cfg)


def hw_address(client):
    resp = client.device('<tds:GetNetworkInterfaces/>')
    assert resp.status_code == 200, resp.data
    return parse(resp).find('.//tt:HwAddress', NS).text


def test_network_interfaces_report_reported_mac(onvif_client):
    client = onvif_client(make_real_camera(reportedMac=PHYS_MAC))
    assert hw_address(client) == 'ec:71:db:12:34:56'


def test_network_interfaces_report_nic_mac_when_unset(onvif_client):
    client = onvif_client(make_real_camera())
    assert hw_address(client) == NIC_MAC


def device_info(client):
    resp = client.device('<tds:GetDeviceInformation/>')
    assert resp.status_code == 200, resp.data
    el = body_child(resp)
    return el.find('tds:SerialNumber', NS).text, el.find('tds:HardwareId', NS).text


def scopes(client):
    resp = client.device('<tds:GetScopes/>')
    assert resp.status_code == 200, resp.data
    return [e.text for e in parse(resp).iter('{%s}ScopeItem' % NS['tt'])]


def test_device_information_reports_reported_mac(onvif_client):
    client = onvif_client(make_real_camera(reportedMac=PHYS_MAC))
    assert device_info(client) == ('EC71DB123456', 'EC71DB123456')


def test_device_information_reports_nic_mac_when_unset(onvif_client):
    client = onvif_client(make_real_camera())
    assert device_info(client) == ('02AABBCCDD01', '02AABBCCDD01')


def test_hardware_scope_reports_reported_mac(onvif_client):
    client = onvif_client(make_real_camera(reportedMac=PHYS_MAC))
    assert 'onvif://www.onvif.org/hardware/EC71DB123456' in scopes(client)
    assert not any('02AABBCCDD01' in s for s in scopes(client))


def test_hardware_scope_reports_nic_mac_when_unset(onvif_client):
    client = onvif_client(make_real_camera())
    assert 'onvif://www.onvif.org/hardware/02AABBCCDD01' in scopes(client)


def test_reported_mac_persists_in_config_and_round_trips():
    from app.camera import VirtualONVIFCamera
    cam = make_real_camera(reportedMac='ec71db123456')
    cfg = cam.to_config_dict()
    assert cfg['reportedMac'] == 'ec:71:db:12:34:56'
    assert cfg['nicMac'] == NIC_MAC
    assert VirtualONVIFCamera(cfg).identity_mac == 'ec:71:db:12:34:56'


def test_unset_reported_mac_persists_as_empty():
    assert make_real_camera().to_config_dict()['reportedMac'] == ''


class RecordingNetworkManager:
    def __init__(self):
        self.macvlans = []

    def create_macvlan(self, parent, name, mac):
        self.macvlans.append((parent, name, mac))
        return True

    def setup_ip(self, *a, **kw):
        return None  # no IP -> no keepalive thread


@pytest.mark.parametrize('extra', [{}, {'reportedMac': PHYS_MAC}])
def test_vnic_is_created_with_nic_mac_regardless_of_reported_mac(extra, monkeypatch):
    cam = make_real_camera(useVirtualNic=True, parentInterface='bond0.2', **extra)
    net = RecordingNetworkManager()
    cam.network_mgr = net
    for side_effect in ('_start_onvif_service', '_probe_source_streams', 'start_snapshot_cache'):
        monkeypatch.setattr(cam, side_effect, lambda: None)
    monkeypatch.setattr('app.camera.time.sleep', lambda s: None)
    cam.start()
    cam.status = 'stopped'
    assert [m for _p, _n, m in net.macvlans] == [NIC_MAC]


# --- Camera add/update API -------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    """The real /api/cameras routes over a CameraManager with no side effects
    (no MediaMTX, analytics or config file). Needs pyyaml + psutil."""
    pytest.importorskip('yaml')
    pytest.importorskip('psutil')
    from app.manager import CameraManager
    from app.web import create_web_app
    mgr = CameraManager.__new__(CameraManager)
    mgr.cameras, mgr.next_id, mgr.next_onvif_port = [], 1, 8001
    mgr.global_username, mgr.global_password = 'onvifuser', 'onvifpass'
    mgr.auth_enabled, mgr.debug_mode, mgr.server_ip = False, False, 'localhost'
    monkeypatch.setattr(CameraManager, 'save_config', lambda self: None)
    monkeypatch.setattr(CameraManager, 'is_port_available', lambda self, *a, **kw: True)
    app = create_web_app(mgr)
    app.config['TESTING'] = True
    return app.test_client(), mgr


def camera_body(**extra):
    body = {'name': 'Back Gate', 'host': '192.168.5.10', 'rtspPort': 554,
            'username': 'admin', 'password': 'pw', 'mainPath': '/main', 'subPath': '/sub',
            'nicMac': NIC_MAC}
    body.update(extra)
    return body


@pytest.mark.parametrize('given', ['EC:71:DB:12:34:56', 'ec71db123456', 'EC71DB123456'])
def test_add_camera_accepts_and_normalises_reported_mac(web, given):
    client, mgr = web
    resp = client.post('/api/cameras', json=camera_body(reportedMac=given))
    assert resp.status_code == 201, resp.data
    assert resp.get_json()['reportedMac'] == 'ec:71:db:12:34:56'
    assert mgr.cameras[0].to_config_dict()['reportedMac'] == 'ec:71:db:12:34:56'


def test_add_camera_without_reported_mac_leaves_identity_on_nic_mac(web):
    client, mgr = web
    resp = client.post('/api/cameras', json=camera_body())
    assert resp.status_code == 201, resp.data
    assert resp.get_json()['reportedMac'] == ''
    assert mgr.cameras[0].identity_mac == NIC_MAC


@pytest.mark.parametrize('bad', ['EC:71:DB:12:34', 'zz71db123456', 'EC-71-DB-12-34-56x', 'hello'])
def test_add_camera_rejects_malformed_reported_mac(web, bad):
    client, mgr = web
    resp = client.post('/api/cameras', json=camera_body(reportedMac=bad))
    assert resp.status_code == 400
    assert 'MAC' in resp.get_json()['error']
    assert mgr.cameras == []


def test_update_camera_sets_and_clears_reported_mac(web):
    client, mgr = web
    cid = client.post('/api/cameras', json=camera_body()).get_json()['id']
    resp = client.put(f'/api/cameras/{cid}', json=camera_body(reportedMac='EC71DB123456'))
    assert resp.status_code == 200, resp.data
    assert mgr.get_camera(cid).identity_mac == 'ec:71:db:12:34:56'
    assert mgr.get_camera(cid).nic_mac == NIC_MAC

    # An update that omits the field (older UI / API client) keeps it.
    client.put(f'/api/cameras/{cid}', json=camera_body())
    assert mgr.get_camera(cid).reported_mac == 'ec:71:db:12:34:56'

    # An explicit empty value clears it.
    client.put(f'/api/cameras/{cid}', json=camera_body(reportedMac=''))
    assert mgr.get_camera(cid).reported_mac == ''
    assert mgr.get_camera(cid).identity_mac == NIC_MAC


def test_update_camera_rejects_malformed_reported_mac(web):
    client, mgr = web
    cid = client.post('/api/cameras', json=camera_body(reportedMac=PHYS_MAC)).get_json()['id']
    resp = client.put(f'/api/cameras/{cid}', json=camera_body(reportedMac='nope'))
    assert resp.status_code == 400
    assert mgr.get_camera(cid).reported_mac == 'ec:71:db:12:34:56'


def test_reported_mac_must_not_collide_with_another_cameras_identity(web):
    client, _mgr = web
    assert client.post('/api/cameras', json=camera_body(reportedMac=PHYS_MAC)).status_code == 201
    resp = client.post('/api/cameras', json=camera_body(name='Other', nicMac='02:aa:bb:cc:dd:02',
                                                        reportedMac='ec71db123456'))
    assert resp.status_code == 400
    assert 'already' in resp.get_json()['error']


@pytest.mark.parametrize('is_linux', [True, False])
def test_settings_ui_has_reported_mac_field_and_sends_it(is_linux):
    from app.web_template import get_web_ui_html
    html = get_web_ui_html({}, boot_id='x', is_linux=is_linux)
    assert 'id="reportedMac"' in html
    assert "reportedMac: document.getElementById('reportedMac').value" in html
    assert "getElementById('reportedMac').value = camera.reportedMac || ''" in html
