"""Imaging service: Reolink mapping (sliders + IrCutFilter<->dayNight) and ONVIF passthrough."""

import time

import pytest

from app.device_control import CameraControl
from tests.helpers import NS, parse, body_child, fault_codes, local, wait_for

VS = 'VideoSource_7'


@pytest.fixture
def reolink_img(make_camera, upstream, onvif_client):
    cam = make_camera(device_control={'backend': 'reolink', 'host': f'127.0.0.1:{upstream.port}'})
    c = onvif_client(cam)
    CameraControl(cam)
    assert wait_for(lambda: 'GetIrLights' in upstream.ops('reolink'), 5)
    time.sleep(0.05)
    upstream.log.clear()
    return c


def set_img(inner, vs=VS):
    return (f'<timg:SetImagingSettings><timg:VideoSourceToken>{vs}</timg:VideoSourceToken><timg:ImagingSettings>'
            f'{inner}</timg:ImagingSettings></timg:SetImagingSettings>')


def sets(upstream):
    return [op for op in upstream.ops('reolink') if op.startswith('Set')]


def test_get_options_reolink(reolink_img):
    r = reolink_img.imaging(f'<timg:GetOptions><timg:VideoSourceToken>{VS}</timg:VideoSourceToken></timg:GetOptions>')
    opts = parse(r).find('.//timg:ImagingOptions', NS)
    assert [e.text for e in opts.findall('tt:IrCutFilterModes', NS)] == ['ON', 'OFF', 'AUTO']
    b = opts.find('tt:Brightness', NS)
    assert (b.find('tt:Min', NS).text, b.find('tt:Max', NS).text) == ('0', '255')


def test_get_imaging_settings_reolink(reolink_img, upstream):
    upstream.isp['dayNight'] = 'Black&White'
    upstream.image['bright'] = 140
    r = reolink_img.imaging(f'<timg:GetImagingSettings><timg:VideoSourceToken>{VS}</timg:VideoSourceToken></timg:GetImagingSettings>')
    s = parse(r).find('.//timg:ImagingSettings', NS)
    assert s.find('tt:Brightness', NS).text == '140'
    assert s.find('tt:IrCutFilter', NS).text == 'OFF'


@pytest.mark.parametrize("ir,daynight", [('ON', 'Color'), ('OFF', 'Black&White'), ('AUTO', 'Auto')])
def test_ircut_maps_to_isp_daynight_full_object(reolink_img, upstream, ir, daynight):
    if ir == 'AUTO':
        upstream.isp['dayNight'] = 'Color'
    before = dict(upstream.isp)
    r = reolink_img.imaging(set_img(f'<tt:IrCutFilter>{ir}</tt:IrCutFilter>'))
    assert r.status_code == 200, r.data
    isp_calls = [e[3] for e in upstream.entries('SetIsp')]
    assert len(isp_calls) == 1
    sent = isp_calls[0]['param']['Isp']
    expected = dict(before, dayNight=daynight)
    assert sent == expected            # whole object round-tripped, only dayNight changed
    assert 'SetIrLights' not in upstream.ops('reolink')


def test_identical_set_imaging_is_noop_upstream(reolink_img, upstream):
    body = set_img('<tt:Brightness>200</tt:Brightness><tt:IrCutFilter>ON</tt:IrCutFilter>')
    assert reolink_img.imaging(body).status_code == 200
    assert sorted(sets(upstream)) == ['SetImage', 'SetIsp']
    assert upstream.image['bright'] == 200 and upstream.image['hue'] == 128
    upstream.log.clear()
    for _ in range(3):
        assert reolink_img.imaging(body).status_code == 200
    assert sets(upstream) == []


@pytest.mark.parametrize("inner", ['<tt:Brightness>300</tt:Brightness>', '<tt:Contrast>-1</tt:Contrast>',
                                   '<tt:Brightness>abc</tt:Brightness>', '<tt:IrCutFilter>BOGUS</tt:IrCutFilter>'])
def test_invalid_settings(reolink_img, upstream, inner):
    r = reolink_img.imaging(set_img(inner))
    assert fault_codes(r) == ['SOAP-ENV:Sender', 'ter:InvalidArgVal', 'ter:SettingsInvalid']
    assert sets(upstream) == []


def test_wrong_video_source(reolink_img):
    r = reolink_img.imaging(set_img('<tt:Brightness>10</tt:Brightness>', vs='VideoSource_99'))
    assert fault_codes(r)[-1] == 'ter:NoSource'


def test_onvif_passthrough_imaging_tapo(make_camera, upstream, onvif_client):
    cam = make_camera(device_control={'backend': 'tapo', 'host': '127.0.0.1', 'ptz': {'enabled': False}},
                      physical_onvif_port=upstream.port)
    c = onvif_client(cam)
    img = CameraControl(cam).imaging
    assert wait_for(lambda: img.sliders, 5)
    r = c.imaging(f'<timg:GetOptions><timg:VideoSourceToken>{VS}</timg:VideoSourceToken></timg:GetOptions>')
    opts = parse(r).find('.//timg:ImagingOptions', NS)
    assert opts.find('tt:IrCutFilterModes', NS) is None          # no cloud password -> no IR control
    assert opts.find('tt:Brightness/tt:Max', NS).text == '100'
    upstream.log.clear()
    r = c.imaging(set_img('<tt:Brightness>70</tt:Brightness>'))
    assert r.status_code == 200, r.data
    sent = upstream.entries('SetImagingSettings')
    assert len(sent) == 1 and 'vs1' in sent[0][3] and upstream.imaging['Brightness'] == 70
    r = c.imaging(set_img('<tt:IrCutFilter>AUTO</tt:IrCutFilter>'))
    assert fault_codes(r)[-1] == 'ter:SettingsInvalid'
