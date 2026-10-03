"""soap_op parsing, fault shape, capability advertisement, device-service regressions."""

import pytest

from app.onvif_control import soap_op, fault
from tests.conftest import soap, NS_PTZ, NS_TMD, NS_TEV
from tests.helpers import NS, parse, body_child, fault_codes, local, wait_for


# --------------------------------------------------------------- soap_op
@pytest.mark.parametrize("op", ["GetNode", "GetNodes", "GetRelayOutputs", "GetRelayOutputOptions",
                                "GetProfile", "GetProfiles", "GetConfiguration", "GetConfigurations",
                                "GetConfigurationOptions"])
def test_soap_op_exact_name(op):
    assert soap_op(soap(f'<tptz:{op}/>', NS_PTZ)) == op


def test_soap_op_ignores_header_and_unprefixed():
    body = ('<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope"><Header><Security>'
            '<UsernameToken><Username>GetNodes</Username></UsernameToken></Security></Header>'
            '<Body><GetNode xmlns="http://www.onvif.org/ver20/ptz/wsdl"><NodeToken>x</NodeToken></GetNode></Body></Envelope>')
    assert soap_op(body) == 'GetNode'


def test_soap_op_regex_fallback_on_malformed_xml():
    body = '<s:Envelope><s:Body><tds:GetRelayOutputOptions><broken></s:Body>'
    assert soap_op(body) == 'GetRelayOutputOptions'
    assert soap_op('') is None
    assert soap_op('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>') is None


def test_fault_shape_nested_subcode():
    r = fault('ter:InvalidArgVal', 'No such profile', 'ter:NoProfile')
    assert r.status_code == 400
    assert fault_codes(r) == ['SOAP-ENV:Sender', 'ter:InvalidArgVal', 'ter:NoProfile']
    reason = body_child(r).find('s:Reason/s:Text', NS)
    assert reason.text == 'No such profile'
    r = fault('ter:Action', 'x < y & z', receiver=True)
    assert r.status_code == 500
    assert fault_codes(r) == ['SOAP-ENV:Receiver', 'ter:Action']
    assert body_child(r).find('s:Reason/s:Text', NS).text == 'x < y & z'


# --------------------------------------------------- capability advertising
def caps(client):
    r = client.device('<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
    assert r.status_code == 200
    return parse(r)


def services(client, include=True):
    r = client.device(f'<tds:GetServices><tds:IncludeCapability>{str(include).lower()}</tds:IncludeCapability></tds:GetServices>')
    assert r.status_code == 200
    return [s.find('tds:Namespace', NS).text for s in parse(r).iter(f'{{{NS["tds"]}}}Service')], parse(r)


def profiles(client):
    r = client.media('<trt:GetProfiles/>')
    assert r.status_code == 200
    return parse(r).findall('.//trt:Profiles', NS)


def test_feature_off_advertises_nothing(make_camera, onvif_client):
    c = onvif_client(make_camera())
    root = caps(c)
    assert root.find('.//tt:PTZ', NS) is None
    assert root.find('.//tt:Imaging', NS) is None
    assert root.find('.//tt:DeviceIO', NS) is None
    assert root.find('.//tt:IO/tt:RelayOutputs', NS).text == '0'
    names, _ = services(c)
    assert not any(n.endswith(('ptz/wsdl', 'imaging/wsdl', 'deviceIO/wsdl')) for n in names)
    for p in profiles(c):
        assert p.find('tt:PTZConfiguration', NS) is None
    scopes = [e.text for e in parse(c.device('<tds:GetScopes/>')).iter(f'{{{NS["tt"]}}}ScopeItem')]
    assert 'onvif://www.onvif.org/type/ptz' not in scopes
    ev = parse(c.events('<tev:GetEventProperties/>'))
    assert b'Trigger' not in c.events('<tev:GetEventProperties/>').data
    # Feature-off PTZ/DeviceIO/Imaging services answer with ActionNotSupported
    for path in ('/onvif/ptz_service', '/onvif/imaging_service', '/onvif/deviceio_service'):
        r = c.post(path, '<x:GetServiceCapabilities xmlns:x="urn:x"/>')
        assert fault_codes(r)[:2] == ['SOAP-ENV:Sender', 'ter:ActionNotSupported']


def reolink_cam(make_camera, upstream, **dc):
    cfg = {'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}', 'webhookToken': 'tok-abc'}
    cfg.update(dc)
    return make_camera(device_control=cfg)


def tapo_cam(make_camera, upstream, ptz=True, **dc):
    cfg = {'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': ptz}, 'webhookToken': 'tok-abc'}
    cfg.update(dc)
    return make_camera(device_control=cfg, physical_onvif_port=upstream.port)


def test_reolink_on_advertises_relays_and_imaging_not_ptz(make_camera, upstream, onvif_client):
    c = onvif_client(reolink_cam(make_camera, upstream))
    root = caps(c)
    assert root.find('.//tt:PTZ', NS) is None
    assert root.find('.//tt:Imaging/tt:XAddr', NS).text.endswith('/onvif/imaging_service')
    assert root.find('.//tt:DeviceIO/tt:RelayOutputs', NS).text == '3'
    assert root.find('.//tt:IO/tt:RelayOutputs', NS).text == '3'
    names, sroot = services(c)
    assert 'http://www.onvif.org/ver10/deviceIO/wsdl' in names
    assert 'http://www.onvif.org/ver20/imaging/wsdl' in names
    assert 'http://www.onvif.org/ver20/ptz/wsdl' not in names
    tmd_caps = sroot.find('.//tmd:Capabilities', NS)
    assert tmd_caps is not None and tmd_caps.get('RelayOutputs') == '3'
    # relay subset filter
    c2 = onvif_client(reolink_cam(make_camera, upstream, relays=['Floodlight', 'Siren']))
    assert caps(c2).find('.//tt:DeviceIO/tt:RelayOutputs', NS).text == '2'
    # imaging off
    c3 = onvif_client(reolink_cam(make_camera, upstream, relays=['Floodlight'], imaging=False))
    assert caps(c3).find('.//tt:Imaging', NS) is None
    ev = c.events('<tev:GetEventProperties/>')
    evroot = parse(ev)
    assert evroot.find('.//{http://www.onvif.org/ver10/topics}Relay') is not None


def test_tapo_ptz_on_advertises_ptz_profiles_scope(make_camera, upstream, onvif_client):
    c = onvif_client(tapo_cam(make_camera, upstream))
    root = caps(c)
    assert root.find('.//tt:PTZ/tt:XAddr', NS).text == 'http://192.0.2.10:8007/onvif/ptz_service'
    assert root.find('.//tt:DeviceIO', NS) is None     # no cloud password -> no relays
    names, sroot = services(c, include=True)
    assert 'http://www.onvif.org/ver20/ptz/wsdl' in names
    assert sroot.find('.//tptz:Capabilities', NS) is not None
    names_nocaps, sroot2 = services(c, include=False)
    assert sroot2.find('.//tptz:Capabilities', NS) is None
    profs = profiles(c)
    assert len(profs) == 2
    for p in profs:
        cfg = p.find('tt:PTZConfiguration', NS)
        assert cfg is not None and cfg.find('tt:NodeToken', NS).text == 'PTZNode_7'
    # GetProfile (singular) includes it as well
    r = c.media('<trt:GetProfile><trt:ProfileToken>mainStream_7</trt:ProfileToken></trt:GetProfile>')
    assert parse(r).find('.//tt:PTZConfiguration', NS) is not None
    scopes = [e.text for e in parse(c.device('<tds:GetScopes/>')).iter(f'{{{NS["tt"]}}}ScopeItem')]
    assert 'onvif://www.onvif.org/type/ptz' in scopes


def test_tapo_ptz_disabled_not_advertised(make_camera, upstream, onvif_client):
    c = onvif_client(tapo_cam(make_camera, upstream, ptz=False))
    assert caps(c).find('.//tt:PTZ', NS) is None
    for p in profiles(c):
        assert p.find('tt:PTZConfiguration', NS) is None


def test_ptz_in_profile_false_omits_configuration(make_camera, upstream, onvif_client):
    cam = tapo_cam(make_camera, upstream)
    cam.device_control['ptz']['inProfile'] = False
    c = onvif_client(cam)
    assert caps(c).find('.//tt:PTZ', NS) is not None
    for p in profiles(c):
        assert p.find('tt:PTZConfiguration', NS) is None


# ------------------------------------------------- device service regressions
@pytest.mark.parametrize("op,resp", [
    ('<tds:GetDeviceInformation/>', 'GetDeviceInformationResponse'),
    ('<tds:GetCapabilities/>', 'GetCapabilitiesResponse'),
    ('<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>', 'GetServicesResponse'),
    ('<tds:GetSystemDateAndTime/>', 'GetSystemDateAndTimeResponse'),
    ('<tds:GetScopes/>', 'GetScopesResponse'),
    ('<tds:GetNetworkInterfaces/>', 'GetNetworkInterfacesResponse'),
    ('<tds:GetServiceCapabilities/>', 'GetServiceCapabilitiesResponse'),
])
def test_existing_device_ops_still_answer(make_camera, onvif_client, op, resp):
    c = onvif_client(make_camera())
    r = c.device(op)
    assert r.status_code == 200
    assert local(body_child(r).tag) == resp


def test_unknown_device_op_keeps_legacy_reply_and_is_logged(make_camera, onvif_client, capsys):
    # Deliberate: unknown device ops keep the historical GetDeviceInformation reply
    # so NVR adoption can't regress; they are logged so they can be implemented.
    c = onvif_client(make_camera())
    r = c.device('<tds:GetHostname/>')
    assert r.status_code == 200
    assert local(body_child(r).tag) == 'GetDeviceInformationResponse'
    assert "Unhandled device operation 'GetHostname'" in capsys.readouterr().out


def test_device_service_requires_auth(make_camera, onvif_client):
    c = onvif_client(make_camera())
    r = c.client.post('/onvif/ptz_service', data=soap('<tptz:GetNodes/>', NS_PTZ))
    assert r.status_code == 401
    r = c.client.post('/onvif/deviceio_service', data=soap('<tmd:GetRelayOutputs/>', NS_TMD))
    assert r.status_code == 401


def test_discovery_does_not_block_on_dead_camera(make_camera, onvif_client):
    """Capabilities/profiles/PTZ discovery answer immediately even when the physical camera is a black hole."""
    import socket, time
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(50)          # accepts connections, never answers
    port = srv.getsockname()[1]
    try:
        cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': True}},
                          physical_onvif_port=port)
        c = onvif_client(cam)
        t0 = time.time()
        caps(c)
        profiles(c)
        for op in ('<tptz:GetNodes/>', '<tptz:GetConfigurations/>',
                   '<tptz:GetConfigurationOptions><tptz:ConfigurationToken>PTZConfig_7</tptz:ConfigurationToken></tptz:GetConfigurationOptions>'):
            assert c.ptz(op).status_code == 200
        assert time.time() - t0 < 1.0
    finally:
        srv.close()
