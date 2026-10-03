"""Relay outputs (DeviceIO + device service), relay events, Reolink client."""

import queue
import time

import pytest

from app.device_control import CameraControl
from app.device_control.reolink import ReolinkClient, ReolinkAuthError, ReolinkError
from app.onvif_service import VirtualSubscription
from tests.helpers import NS, parse, body_child, fault_codes, local, wait_for


@pytest.fixture
def reolink_setup(make_camera, upstream, onvif_client):
    def make(**dc):
        cfg = {'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}', 'webhookToken': 'tok-abc'}
        cfg.update(dc)
        cam = make_camera(device_control=cfg)
        client = onvif_client(cam)
        client.service.controls.attach()
        ctl = CameraControl(cam).ctl
        # let the initial-state seed thread (https probe -> http fallback) finish first
        if not getattr(ctl, '_test_seen', False):     # shared controller: seed runs once
            assert wait_for(lambda: 'GetIrLights' in upstream.ops('reolink'), 5)
            ctl._test_seen = True
        time.sleep(0.1)
        upstream.log.clear()
        return client, ctl
    return make


def set_relay(token, state):
    return (f'<tds:SetRelayOutputState><tds:RelayOutputToken>{token}</tds:RelayOutputToken>'
            f'<tds:LogicalState>{state}</tds:LogicalState></tds:SetRelayOutputState>')


def reolink_calls(upstream, cmd):
    return [e[3] for e in upstream.entries(cmd) if e[1] == 'reolink']


# -------------------------------------------------------------- listing
def test_get_relay_outputs_device_and_deviceio(reolink_setup):
    c, _ = reolink_setup()
    for resp in (c.device('<tds:GetRelayOutputs/>'), c.deviceio('<tmd:GetRelayOutputs/>')):
        assert resp.status_code == 200
        relays = [e for e in body_child(resp) if local(e.tag) == 'RelayOutputs']
        by_tok = {r.get('token'): r for r in relays}
        assert set(by_tok) == {'Floodlight', 'Siren', 'IR_Illuminator'}
        siren = by_tok['Siren'].find('tt:Properties', NS)
        assert siren.find('tt:Mode', NS).text == 'Monostable'
        assert siren.find('tt:DelayTime', NS).text == 'PT10S'
        assert siren.find('tt:IdleState', NS).text == 'open'
        assert by_tok['Floodlight'].find('tt:Properties/tt:Mode', NS).text == 'Bistable'


def test_deviceio_responses_use_deviceio_namespace(reolink_setup):
    """DeviceIO (tmd) operations must answer in the tmd namespace, not tds."""
    c, _ = reolink_setup()
    r = c.deviceio('<tmd:GetRelayOutputs/>')
    assert body_child(r).tag == f'{{{NS["tmd"]}}}GetRelayOutputsResponse', body_child(r).tag
    r = c.deviceio(set_relay('Floodlight', 'active').replace('tds:', 'tmd:'))
    assert body_child(r).tag == f'{{{NS["tmd"]}}}SetRelayOutputStateResponse', body_child(r).tag


def test_get_relay_output_options(reolink_setup):
    c, _ = reolink_setup()
    r = c.deviceio('<tmd:GetRelayOutputOptions/>')
    assert local(body_child(r).tag) == 'GetRelayOutputOptionsResponse'
    opts = {o.get('token'): o for o in body_child(r)}
    assert opts['Siren'].find('tmd:Mode', NS).text == 'Monostable'
    assert opts['Siren'].find('tmd:DelayTimes', NS).text == '1 60'


# ---------------------------------------------------------- floodlight
def test_floodlight_active_inactive(reolink_setup, upstream):
    c, ctl = reolink_setup()
    r = c.device(set_relay('Floodlight', 'active'))
    assert r.status_code == 200, r.data
    r = c.deviceio(set_relay('Floodlight', 'inactive').replace('tds:', 'tmd:'))
    assert r.status_code == 200, r.data
    calls = reolink_calls(upstream, 'SetWhiteLed')
    assert [x['param'] for x in calls] == [{'WhiteLed': {'channel': 0, 'state': 1}},
                                           {'WhiteLed': {'channel': 0, 'state': 0}}]
    assert upstream.whiteled['mode'] == 3 and 'LightingSchedule' in upstream.whiteled


def test_unknown_relay_and_bad_state(reolink_setup):
    c, _ = reolink_setup()
    r = c.device(set_relay('Nope', 'active'))
    assert fault_codes(r) == ['SOAP-ENV:Sender', 'ter:InvalidArgVal', 'ter:RelayToken']
    r = c.device(set_relay('Floodlight', 'maybe'))
    assert fault_codes(r)[-1] == 'ter:ModeError'
    # relay filtered out by config is unknown too
    c2, _ = reolink_setup(relays=['Siren'])
    r = c2.device(set_relay('Floodlight', 'active'))
    assert fault_codes(r)[-1] == 'ter:RelayToken'


# --------------------------------------------------------------- siren
def test_siren_monostable_times_mode_capped_and_auto_revert(reolink_setup, upstream):
    c, ctl = reolink_setup(sirenSeconds=60)
    sub = VirtualSubscription('s1')
    c.service.subscriptions['s1'] = sub
    siren = ctl.relays['Siren']
    siren.delay = 60
    # shorten the auto-revert for the test while keeping the request of 60s
    import threading
    real_timer = threading.Timer
    holds = []

    def fast_timer(hold, fn, *args, **kwargs):
        holds.append(hold)
        return real_timer(0.3, fn, *args, **kwargs)
    threading.Timer = fast_timer
    try:
        r = c.device(set_relay('Siren', 'active'))
    finally:
        threading.Timer = real_timer
    assert r.status_code == 200, r.data
    call = reolink_calls(upstream, 'AudioAlarmPlay')[-1]
    assert call['param'] == {'alarm_mode': 'times', 'times': 12, 'channel': 0}
    assert holds == [60.0]
    assert siren.active
    assert wait_for(lambda: not siren.active, 3)
    # relay events: active then inactive
    evts = [sub.queue.get(timeout=1), sub.queue.get(timeout=1)]
    assert [(e['relay_token'], e['value']) for e in evts] == [('Siren', 'active'), ('Siren', 'inactive')]
    for e in evts:
        sub.queue.put(e)
    r = c.post('/onvif/subscription/s1', '<wsnt:PullMessages xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">'
               '<wsnt:Timeout>PT1S</wsnt:Timeout><wsnt:MessageLimit>10</wsnt:MessageLimit></wsnt:PullMessages>')
    root = parse(r)
    msgs = root.findall('.//wsnt:NotificationMessage', NS)
    assert len(msgs) == 2
    for m, state in zip(msgs, ('active', 'inactive')):
        assert m.find('wsnt:Topic', NS).text == 'tns1:Device/Trigger/Relay'
        src = m.find('.//tt:Source/tt:SimpleItem', NS)
        data = m.find('.//tt:Data/tt:SimpleItem', NS)
        assert (src.get('Name'), src.get('Value')) == ('RelayToken', 'Siren')
        assert (data.get('Name'), data.get('Value')) == ('LogicalState', state)


@pytest.mark.parametrize("seconds,plays", [(1, 1), (5, 1), (6, 2), (12, 3), (60, 12), (999, 12)])
def test_siren_pulse_rounding(upstream, seconds, plays):
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 's3cretPW!')
    cl._schemes = ['http']
    assert cl.siren_pulse(seconds) == plays * 5
    assert reolink_calls(upstream, 'AudioAlarmPlay')[-1]['param']['times'] == plays


def test_siren_off_sends_switch_1_then_0(reolink_setup, upstream):
    c, ctl = reolink_setup()
    c.device(set_relay('Siren', 'active'))
    upstream.log.clear()
    r = c.device(set_relay('Siren', 'inactive'))
    assert r.status_code == 200
    calls = [x['param'] for x in reolink_calls(upstream, 'AudioAlarmPlay')]
    assert calls == [{'alarm_mode': 'manul', 'manual_switch': 1, 'channel': 0},
                     {'alarm_mode': 'manul', 'manual_switch': 0, 'channel': 0}]
    assert not ctl.relays['Siren'].active


def test_relay_settings_delay(reolink_setup):
    c, ctl = reolink_setup()
    r = c.deviceio('<tmd:SetRelayOutputSettings><tmd:RelayOutput token="Siren"><tt:Properties><tt:Mode>Monostable</tt:Mode>'
                   '<tt:DelayTime>PT90S</tt:DelayTime><tt:IdleState>open</tt:IdleState></tt:Properties></tmd:RelayOutput>'
                   '</tmd:SetRelayOutputSettings>')
    assert r.status_code == 200, r.data
    assert ctl.relays['Siren'].delay == 60
    r = c.device('<tds:SetRelayOutputSettings><tds:RelayOutputToken>Siren</tds:RelayOutputToken><tds:Properties>'
                 '<tt:Mode>Bistable</tt:Mode><tt:DelayTime>PT0S</tt:DelayTime><tt:IdleState>open</tt:IdleState>'
                 '</tds:Properties></tds:SetRelayOutputSettings>')
    assert fault_codes(r)[-1] == 'ter:ModeError'


# -------------------------------------------------------- Reolink client
def test_relogin_once_on_rsp_minus_6(upstream):
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 's3cretPW!')
    cl._schemes = ['http']
    cl.set_floodlight(True)
    upstream.expire_tokens_once = True
    cl.set_floodlight(False)
    assert upstream.ops('reolink').count('Login') == 2
    assert upstream.whiteled['state'] == 0


def test_https_falls_back_to_http(upstream):
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 's3cretPW!')
    cl.set_floodlight(True)
    assert cl._schemes == ['http']
    assert upstream.whiteled['state'] == 1


def test_credential_error_persists_and_blocks(upstream):
    host = f'127.0.0.1:{upstream.port}'
    cl = ReolinkClient(host, 'admin', 'wrong')
    cl._schemes = ['http']
    with pytest.raises(ReolinkAuthError) as ei:
        cl.set_floodlight(True)
    with pytest.raises(ReolinkAuthError):
        cl.set_floodlight(True)
    assert upstream.ops('reolink').count('Login') == 1
    # a new client (e.g. after a restart) with the same credentials does not try again
    cl2 = ReolinkClient(host, 'admin', 'wrong')
    cl2._schemes = ['http']
    with pytest.raises(ReolinkAuthError):
        cl2.get_floodlight()
    assert upstream.ops('reolink').count('Login') == 1
    assert 'wrong' not in str(ei.value)
    # corrected credentials clear the block (different key)
    cl3 = ReolinkClient(host, 'admin', 's3cretPW!')
    cl3._schemes = ['http']
    cl3.set_floodlight(True)
    assert upstream.whiteled['state'] == 1


def test_errors_never_leak_token_or_password(upstream):
    host = f'127.0.0.1:{upstream.port}'
    cl = ReolinkClient(host, 'admin', 's3cretPW!')
    cl._schemes = ['http']
    cl.set_floodlight(True)
    token = cl._token
    assert token
    msgs = []
    # unsupported command -> ReolinkError
    try:
        cl.call('Bogus', {})
    except ReolinkError as e:
        msgs.append(str(e))
    # camera goes away -> connection error
    upstream.stop()
    import requests
    cl._session = requests.Session()     # drop the kept-alive connection to the old server
    try:
        cl.set_floodlight(False)
    except ReolinkError as e:
        msgs.append(str(e))
    assert len(msgs) == 2
    for m in msgs:
        assert token not in m and 's3cretPW!' not in m, m


def test_state_file_does_not_contain_password(upstream):
    from app.device_control import state
    cl = ReolinkClient(f'127.0.0.1:{upstream.port}', 'admin', 'wrong-pass-123')
    cl._schemes = ['http']
    with pytest.raises(ReolinkAuthError):
        cl.get_floodlight()
    with open(state.STATE_FILE) as f:
        assert 'wrong-pass-123' not in f.read()
