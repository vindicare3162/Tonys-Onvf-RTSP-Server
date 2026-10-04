"""PTZ service: synthesized discovery, ordered move/stop, dead-man refresh, presets, faults."""

import re
import socket
import threading
import time

import pytest

from app.device_control import CameraControl
from app.device_control.ptz import PtzController
from tests.helpers import NS, parse, body_child, fault_codes, local, wait_for

PROFILE = 'mainStream_7'


def cm(x, y, timeout=None, profile=PROFILE):
    t = f'<tptz:Timeout>{timeout}</tptz:Timeout>' if timeout else ''
    return (f'<tptz:ContinuousMove><tptz:ProfileToken>{profile}</tptz:ProfileToken><tptz:Velocity>'
            f'<tt:PanTilt x="{x}" y="{y}"/></tptz:Velocity>{t}</tptz:ContinuousMove>')


def stop(profile=PROFILE):
    return f'<tptz:Stop><tptz:ProfileToken>{profile}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt></tptz:Stop>'


@pytest.fixture
def ptz_setup(make_camera, upstream, onvif_client):
    def make(max_move=30, **dc):
        cfg = {'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True, 'maxMoveSeconds': max_move}}
        cfg.update(dc)
        cam = make_camera(device_control=cfg, physical_onvif_port=upstream.port)
        client = onvif_client(cam)
        ptz = CameraControl(cam).ptz
        assert wait_for(lambda: ptz.probed, 5), ptz.last_error
        upstream.log.clear()
        return client, ptz
    return make


def ptz_ops(upstream):
    return [op for op in upstream.ops('ptz_service') if op in ('ContinuousMove', 'Stop')]


def test_probe_discovers_profile_and_node(ptz_setup):
    _, ptz = ptz_setup()
    assert ptz.profile_token == 'prof1'
    assert ptz.node['max_presets'] == 8 and not ptz.node['zoom_continuous']
    assert ptz.home_supported


def test_discovery_ops_well_formed_and_local(ptz_setup, upstream):
    c, _ = ptz_setup()
    ops = {
        '<tptz:GetNodes/>': 'GetNodesResponse',
        '<tptz:GetNode><tptz:NodeToken>PTZNode_7</tptz:NodeToken></tptz:GetNode>': 'GetNodeResponse',
        '<tptz:GetConfigurations/>': 'GetConfigurationsResponse',
        '<tptz:GetConfiguration><tptz:PTZConfigurationToken>PTZConfig_7</tptz:PTZConfigurationToken></tptz:GetConfiguration>': 'GetConfigurationResponse',
        '<tptz:GetConfigurationOptions><tptz:ConfigurationToken>PTZConfig_7</tptz:ConfigurationToken></tptz:GetConfigurationOptions>': 'GetConfigurationOptionsResponse',
        '<tptz:GetServiceCapabilities/>': 'GetServiceCapabilitiesResponse',
    }
    for op, expected in ops.items():
        r = c.ptz(op)
        assert r.status_code == 200, r.data
        assert local(body_child(r).tag) == expected
    nodes = parse(c.ptz('<tptz:GetNodes/>')).findall('.//tptz:PTZNode', NS)
    assert len(nodes) == 1
    assert nodes[0].find('tt:MaximumNumberOfPresets', NS).text == '8'
    assert nodes[0].find('tt:HomeSupported', NS).text == 'true'   # emulated via preset
    assert nodes[0].find('.//tt:ContinuousPanTiltVelocitySpace', NS) is not None
    assert upstream.ops() == [], "discovery ops must never call the physical camera"
    # bad node token
    r = c.ptz('<tptz:GetNode><tptz:NodeToken>nope</tptz:NodeToken></tptz:GetNode>')
    assert fault_codes(r) == ['SOAP-ENV:Sender', 'ter:InvalidArgVal', 'ter:NoEntity']


def test_wrong_profile_token_is_noprofile(ptz_setup, upstream):
    c, _ = ptz_setup()
    r = c.ptz(cm(0.5, 0, profile='prof1'))
    assert fault_codes(r) == ['SOAP-ENV:Sender', 'ter:InvalidArgVal', 'ter:NoProfile']
    r = c.ptz('<tptz:GetPresets><tptz:ProfileToken>subStream_99</tptz:ProfileToken></tptz:GetPresets>')
    assert fault_codes(r)[-1] == 'ter:NoProfile'
    time.sleep(0.3)
    assert ptz_ops(upstream) == []


def test_continuous_move_then_stop_in_order(ptz_setup, upstream):
    c, _ = ptz_setup()
    assert c.ptz(cm(0.5, 0.25)).status_code == 200
    assert wait_for(lambda: 'ContinuousMove' in ptz_ops(upstream), 3)
    assert c.ptz(stop()).status_code == 200
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'], 3)
    assert not upstream.moving
    body = upstream.entries('ContinuousMove')[0][3]
    assert 'x="0.5000"' in body and 'y="0.2500"' in body and 'prof1' in body


def test_every_upstream_continuous_move_has_timeout(ptz_setup, upstream):
    c, _ = ptz_setup()
    c.ptz(cm(0.3, 0))
    time.sleep(2.6)
    c.ptz(stop())
    wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'], 3)
    moves = upstream.entries('ContinuousMove')
    assert moves
    for e in moves:
        m = re.search(r'Timeout>PT(\d+(?:\.\d+)?)S<', e[3])
        assert m, e[3]
        assert float(m.group(1)) <= 3


def test_burst_from_threads_ends_stopped(ptz_setup, upstream):
    c, ptz = ptz_setup()
    upstream.ptz_delay = 0.05      # slow camera makes reordering likely if unsynchronised
    errors = []

    def joystick(i):
        try:
            for k in range(10):
                ptz.continuous_move(0.1 * ((i + k) % 9 + 1), 0.0)
                time.sleep(0.001)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=joystick, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ptz.stop()
    assert not errors
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'] and not ptz._moving, 5)
    time.sleep(1.5)                # no refresh may resurrect the move after Stop
    assert ptz_ops(upstream)[-1] == 'Stop'
    assert not upstream.moving
    # latest-wins: far fewer upstream moves than the 80 requested
    assert ptz_ops(upstream).count('ContinuousMove') < 40


def test_burst_via_onvif_threads_ends_stopped(ptz_setup, upstream):
    c, ptz = ptz_setup()
    upstream.ptz_delay = 0.03
    results = []

    def worker():
        # each thread uses its own test client (Flask test client isn't thread-safe)
        cl = c.app.test_client()
        from tests.conftest import AUTH, soap, NS_PTZ
        for _ in range(5):
            results.append(cl.post('/onvif/ptz_service', data=soap(cm(0.7, -0.3), NS_PTZ), headers=AUTH).status_code)
        results.append(cl.post('/onvif/ptz_service', data=soap(stop(), NS_PTZ), headers=AUTH).status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c.ptz(stop())
    assert set(results) == {200}
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'] and not ptz._moving, 5)
    time.sleep(1.3)
    assert ptz_ops(upstream)[-1] == 'Stop'


def test_dead_man_refresh_and_max_move_seconds(ptz_setup, upstream):
    c, ptz = ptz_setup(max_move=2.5)
    t0 = time.time()
    c.ptz(cm(0.4, 0))
    assert wait_for(lambda: 'Stop' in ptz_ops(upstream), 6), ptz_ops(upstream)
    elapsed = time.time() - t0
    moves = upstream.entries('ContinuousMove')
    assert len(moves) >= 2, "move should be refreshed while held"
    assert 2.0 <= elapsed <= 4.5, elapsed
    stop_t = upstream.entries('Stop')[0][0]
    assert all(m[0] <= stop_t for m in moves)
    time.sleep(1.5)
    assert ptz_ops(upstream)[-1] == 'Stop'


def test_onvif_timeout_shortens_move(ptz_setup, upstream):
    c, ptz = ptz_setup(max_move=30)
    t0 = time.time()
    c.ptz(cm(0.4, 0, timeout='PT1S'))
    assert wait_for(lambda: 'Stop' in ptz_ops(upstream), 4)
    assert time.time() - t0 < 3


def test_zero_velocity_is_stop(ptz_setup, upstream):
    c, ptz = ptz_setup()
    c.ptz(cm(0.4, 0))
    wait_for(lambda: 'ContinuousMove' in ptz_ops(upstream), 2)
    c.ptz(cm(0, 0))
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'], 2)


def test_refresh_failure_does_not_kill_worker(ptz_setup, upstream):
    """A camera that faults one refresh must not leave the PTZ worker dead."""
    c, ptz = ptz_setup()
    c.ptz(cm(0.4, 0))
    assert wait_for(lambda: 'ContinuousMove' in ptz_ops(upstream), 2)
    upstream.ptz_fail_ops.add('ContinuousMove')       # the dead-man refresh now faults
    time.sleep(1.6)
    upstream.ptz_fail_ops.clear()
    c.ptz(stop())
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'], 3), \
        f"Stop never reached the camera after a refresh fault; ops={ptz_ops(upstream)}"
    # and the worker still serves synchronous jobs
    r = c.ptz(f'<tptz:GetPresets><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GetPresets>')
    assert r.status_code == 200, r.data


def test_presets_and_goto(ptz_setup, upstream):
    c, ptz = ptz_setup()
    r = c.ptz(f'<tptz:GetPresets><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GetPresets>')
    presets = parse(r).findall('.//tptz:Preset', NS)
    assert {p.get('token'): p.find('tt:Name', NS).text for p in presets} == {'1': 'Door', '2': 'Home'}
    r = c.ptz(f'<tptz:GotoPreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetToken>1</tptz:PresetToken></tptz:GotoPreset>')
    assert r.status_code == 200
    assert 'PresetToken>1<' in upstream.entries('GotoPreset')[-1][3]
    r = c.ptz(f'<tptz:GotoPreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetToken>99</tptz:PresetToken></tptz:GotoPreset>')
    assert fault_codes(r)[-1] == 'ter:NoToken'


def test_set_and_remove_preset(ptz_setup, upstream):
    c, ptz = ptz_setup()
    r = c.ptz(f'<tptz:SetPreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetName>Gate &amp; Path</tptz:PresetName></tptz:SetPreset>')
    assert r.status_code == 200, r.data
    tok = parse(r).find('.//tptz:PresetToken', NS).text
    assert tok == '3' and upstream.presets['3'] == 'Gate &amp; Path'
    r = c.ptz(f'<tptz:RemovePreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetToken>3</tptz:PresetToken></tptz:RemovePreset>')
    assert r.status_code == 200
    assert '3' not in upstream.presets
    r = c.ptz(f'<tptz:RemovePreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetToken>3</tptz:PresetToken></tptz:RemovePreset>')
    assert fault_codes(r)[-1] == 'ter:NoToken'


def test_home_emulated_via_preset_named_home(ptz_setup, upstream):
    c, ptz = ptz_setup()
    r = c.ptz(f'<tptz:GotoHomePosition><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GotoHomePosition>')
    assert r.status_code == 200
    assert 'PresetToken>2<' in upstream.entries('GotoPreset')[-1][3]
    assert not upstream.entries('GotoHomePosition')
    # SetHomePosition overwrites the existing Home preset (token 2), not a new slot
    r = c.ptz(f'<tptz:SetHomePosition><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:SetHomePosition>')
    assert r.status_code == 200
    sp = upstream.entries('SetPreset')[-1][3]
    assert 'PresetToken>2<' in sp and 'PresetName>Home<' in sp
    # No Home preset -> NoHomePosition fault
    del upstream.presets['2']
    r = c.ptz(f'<tptz:GotoHomePosition><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GotoHomePosition>')
    assert fault_codes(r)[0] == 'SOAP-ENV:Receiver'
    assert fault_codes(r)[-1] == 'ter:NoHomePosition'


def test_get_status(ptz_setup, upstream):
    c, _ = ptz_setup()
    r = c.ptz(f'<tptz:GetStatus><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GetStatus>')
    st = parse(r).find('.//tptz:PTZStatus', NS)
    assert st.find('tt:Position/tt:PanTilt', NS).get('x') == '0.1'
    assert st.find('tt:MoveStatus/tt:PanTilt', NS).text == 'IDLE'


def test_unreachable_upstream_receiver_fault_fast(make_camera, onvif_client):
    """A probed controller whose camera then stops answering: sync ops fault within ~3s."""
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(50)
    port = srv.getsockname()[1]
    try:
        cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}},
                          physical_onvif_port=port)
        c = onvif_client(cam)
        ptz = CameraControl(cam).ptz
        # pretend the probe succeeded earlier against this (now dead) camera
        ptz.ptz_url = f'http://127.0.0.1:{port}/onvif/ptz_service'
        ptz.profile_token = 'prof1'
        ptz.probed = True
        for op in (f'<tptz:GetPresets><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GetPresets>',
                   f'<tptz:GotoPreset><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:PresetToken>1</tptz:PresetToken></tptz:GotoPreset>',
                   f'<tptz:RelativeMove><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:Translation><tt:PanTilt x="0.1" y="0"/></tptz:Translation></tptz:RelativeMove>'):
            t0 = time.time()
            r = c.ptz(op)
            dt = time.time() - t0
            assert r.status_code == 500, r.data
            assert fault_codes(r)[0] == 'SOAP-ENV:Receiver'
            assert dt < 3.2, (op, dt)
    finally:
        srv.close()


def test_not_yet_probed_receiver_fault(make_camera, onvif_client):
    # Port 9 on localhost: nothing listening -> probe fails, ptz_url stays None
    cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}},
                      physical_onvif_port=9)
    c = onvif_client(cam)
    t0 = time.time()
    r = c.ptz(f'<tptz:GetPresets><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken></tptz:GetPresets>')
    assert fault_codes(r)[0] == 'SOAP-ENV:Receiver'
    assert time.time() - t0 < 3


def test_reolink_ptz_over_onvif_when_enabled(make_camera, upstream, onvif_client):
    """PTZ Reolinks (RLC-823S2) are driven over ONVIF on physical_onvif_port; the
    device_control host carries the HTTP API port, which must not leak into ONVIF."""
    cam = make_camera(device_control={'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}',
                                      'ptz': {'enabled': True}},
                      physical_onvif_port=upstream.port)
    c = onvif_client(cam)
    ptz = CameraControl(cam).ptz
    assert ptz is not None
    assert wait_for(lambda: ptz.probed, 5), ptz.last_error
    upstream.log.clear()
    assert c.ptz(cm(0.5, 0)).status_code == 200
    # wait for the move upstream first: a Stop queued behind it would supersede it (latest wins)
    assert wait_for(lambda: 'ContinuousMove' in ptz_ops(upstream), 3)
    assert c.ptz(stop()).status_code == 200
    assert wait_for(lambda: ptz_ops(upstream)[-1:] == ['Stop'], 3), ptz_ops(upstream)


def test_reolink_without_ptz_has_no_worker_and_enabling_rebuilds(make_camera, upstream):
    dc = {'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}'}
    cam = make_camera(device_control=dict(dc), physical_onvif_port=upstream.port)
    ctl = CameraControl(cam).ctl
    assert ctl.ptz is None and CameraControl(cam).ptz is None
    cam.device_control = dict(dc, ptz={'enabled': True})
    ctl2 = CameraControl(cam).ctl
    assert ctl2 is not ctl and ctl2.ptz is not None


def test_zoom_advertised_and_forwarded_when_node_has_zoom(ptz_setup, upstream):
    c, ptz = ptz_setup()
    # pan/tilt-only node (Tapo): no zoom spaces at all
    assert b'ZoomSpaces' not in c.ptz('<tptz:GetNodes/>').data
    ptz.node['zoom_continuous'] = True        # e.g. Reolink RLC-823S2
    for op in ('<tptz:GetNodes/>', '<tptz:GetConfigurations/>',
               '<tptz:GetConfigurationOptions><tptz:ConfigurationToken>PTZConfig_7</tptz:ConfigurationToken>'
               '</tptz:GetConfigurationOptions>'):
        assert b'tptz/ZoomSpaces/' in c.ptz(op).data, op
    assert b'<tt:ContinuousZoomVelocitySpace>' in c.ptz('<tptz:GetNodes/>').data
    assert b'<tt:DefaultContinuousZoomVelocitySpace>' in c.ptz('<tptz:GetConfigurations/>').data
    zoom_in = (f'<tptz:ContinuousMove><tptz:ProfileToken>{PROFILE}</tptz:ProfileToken><tptz:Velocity>'
               '<tt:Zoom x="0.5"/></tptz:Velocity></tptz:ContinuousMove>')
    assert c.ptz(zoom_in).status_code == 200
    assert wait_for(lambda: upstream.entries('ContinuousMove'), 3)
    assert '<tt:Zoom x="0.5000"' in upstream.entries('ContinuousMove')[0][3]
    c.ptz(stop())
