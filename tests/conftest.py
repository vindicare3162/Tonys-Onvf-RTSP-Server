"""Shared fixtures for the device-control test suite.

- `app.config` is replaced before anything imports it so DATA_DIR (state file,
  secret key) lives in a temp dir instead of the repo's data/ directory.
- FakeVirtualCamera has only the attributes ONVIFService / CameraControl read.
- `upstream` is a FakeCamera HTTP server on 127.0.0.1 (never a real camera).
"""

import base64
import os
import sys
import tempfile
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="devctl-tests-")


def _install_fake_config():
    """Exec the real config.py with __file__ under a temp root, so its
    os.makedirs(DATA_DIR) and constants all point at the temp dir."""
    if 'app.config' in sys.modules:
        return
    import app  # namespace package
    src_path = os.path.join(ROOT, 'app', 'config.py')
    fake_file = os.path.join(_TMP, 'app', 'config.py')
    os.makedirs(os.path.dirname(fake_file), exist_ok=True)
    mod = types.ModuleType('app.config')
    mod.__file__ = fake_file
    mod.__package__ = 'app'
    with open(src_path, 'r', encoding='utf-8') as f:
        code = compile(f.read(), src_path, 'exec')
    exec(code, mod.__dict__)
    sys.modules['app.config'] = mod
    app.config = mod


_install_fake_config()

from tests.fake_upstream import FakeCamera  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Fresh device-control state file and secret key per test, fresh registry."""
    from app.device_control import state, controller
    monkeypatch.setattr(state, 'STATE_FILE', str(tmp_path / 'device_control_state.json'))
    from app import secrets_manager as sm
    monkeypatch.setattr(sm.secrets_manager, 'key_file', str(tmp_path / '.secret.key'))
    monkeypatch.setattr(sm.secrets_manager, '_fernet', None)
    old = dict(controller.registry._controllers)
    controller.registry._controllers.clear()
    yield
    for _sig, ctl in list(controller.registry._controllers.values()):
        if ctl.ptz:
            ctl.ptz._stop_worker = True
            ctl.ptz._wake.set()
    controller.registry._controllers.clear()
    controller.registry._controllers.update(old)


@pytest.fixture
def upstream():
    cam = FakeCamera().start()
    yield cam
    cam.stop()


class FakeVirtualCamera:
    def __init__(self, cam_id=7, device_control=None, **kw):
        self.id = cam_id
        self.uuid = f"uuid-{cam_id}"
        self.name = f"Cam {cam_id}"
        self.onvif_port = 8000 + cam_id
        self.rtsp_port = 8554
        self.path_name = f"cam{cam_id}"
        self.main_width, self.main_height = 1920, 1080
        self.sub_width, self.sub_height = 640, 360
        self.main_framerate, self.sub_framerate = 20, 10
        self.disable_substream = False
        self.enable_audio = False
        self.onvif_username, self.onvif_password = "onvifuser", "onvifpass"
        self.manager = None
        self.mac_address = "02:00:00:00:00:07"
        self.device_control = device_control or {}
        self.username, self.password = "admin", "s3cretPW!"
        self.main_stream_url = "rtsp://127.0.0.1:554/stream1"
        self.sub_stream_url = "rtsp://127.0.0.1:554/stream2"
        self.onvif_forwarding_username = ""
        self.onvif_forwarding_password = ""
        self.physical_onvif_port = 2020
        self.debug_mode = False
        self.status = "running"
        self.assigned_ip = None
        self.ip_mode = "dhcp"
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def identity_mac(self):
        return getattr(self, 'reported_mac', '') or self.mac_address

    def get_effective_ip(self):
        return "192.0.2.10"


@pytest.fixture
def make_camera():
    return FakeVirtualCamera


AUTH = {"Authorization": "Basic " + base64.b64encode(b"onvifuser:onvifpass").decode()}


def soap(op_xml, ns='xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'):
    return ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:tt="http://www.onvif.org/ver10/schema" {ns}><s:Header/><s:Body>{op_xml}</s:Body></s:Envelope>')


NS_PTZ = 'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
NS_IMG = 'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"'
NS_TMD = 'xmlns:tmd="http://www.onvif.org/ver10/deviceIO/wsdl"'
NS_TRT = 'xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
NS_TEV = 'xmlns:tev="http://www.onvif.org/ver10/events/wsdl"'


class OnvifClient:
    """Flask test client wrapper for one virtual camera's ONVIF app."""

    def __init__(self, cam):
        from app.onvif_service import ONVIFService
        self.cam = cam
        self.service = ONVIFService(cam)
        self.app = self.service.create_app()
        self.client = self.app.test_client()

    def post(self, path, op_xml, ns='xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'):
        return self.client.post(path, data=soap(op_xml, ns), headers=AUTH,
                                content_type='application/soap+xml')

    def device(self, op_xml):
        return self.post('/onvif/device_service', op_xml)

    def ptz(self, op_xml):
        return self.post('/onvif/ptz_service', op_xml, NS_PTZ)

    def imaging(self, op_xml):
        return self.post('/onvif/imaging_service', op_xml, NS_IMG)

    def deviceio(self, op_xml):
        return self.post('/onvif/deviceio_service', op_xml, NS_TMD)

    def media(self, op_xml):
        return self.post('/onvif/media_service', op_xml, NS_TRT)

    def events(self, op_xml):
        return self.post('/onvif/events_service', op_xml, NS_TEV)


@pytest.fixture
def onvif_client():
    return OnvifClient
