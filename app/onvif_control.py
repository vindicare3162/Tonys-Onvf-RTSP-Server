"""ONVIF PTZ, Imaging and DeviceIO services for a virtual camera.

These let an NVR (UniFi Protect) drive the physical camera behind a virtual
camera: pan/tilt and presets (PTZ ver20), image sliders and day/night
(Imaging ver20 IrCutFilter), and floodlight / siren / IR / spotlight /
privacy toggles exposed as relay outputs (DeviceIO ver10 + the legacy device
service relay operations), with relay state changes published as
tns1:Device/Trigger/Relay events.

Every discovery-type response (capabilities, nodes, configurations, options,
relay lists) is built from config and cached probe results and never waits
on the physical camera. Only commands and live reads go upstream, with
bounded waits; when the camera is unreachable they return an ONVIF
env:Receiver fault rather than hanging.
"""

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape, unescape

from flask import Response, request

from .device_control import CameraControl, registry
from .device_control.controller import ControlError, SLIDERS
from .device_control.ptz import PtzUnavailable, GENERIC
from .device_control.soap import parse_duration

NS_DECL = ('xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
           'xmlns:tt="http://www.onvif.org/ver10/schema" '
           'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
           'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" '
           'xmlns:tmd="http://www.onvif.org/ver10/deviceIO/wsdl" '
           'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
           'xmlns:ter="http://www.onvif.org/ver10/error"')


def soap_op(body):
    """Local name of the first element inside the SOAP Body."""
    try:
        root = ET.fromstring(body.encode('utf-8') if isinstance(body, str) else body)
        for el in root.iter():
            if el.tag.rsplit('}', 1)[-1] == 'Body':
                for child in el:
                    return child.tag.rsplit('}', 1)[-1]
                return None
    except ET.ParseError:
        pass
    m = re.search(r'<(?:[\w-]+:)?Body[^>]*>\s*<(?:[\w-]+:)?([\w]+)', body or '')
    return m.group(1) if m else None


def envelope(inner):
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n<SOAP-ENV:Envelope {NS_DECL}><SOAP-ENV:Body>'
                    f'{inner}</SOAP-ENV:Body></SOAP-ENV:Envelope>', mimetype='application/soap+xml')


def fault(subcode='ter:ActionNotSupported', reason='Action not supported', detail_code=None, receiver=False):
    code = 'SOAP-ENV:Receiver' if receiver else 'SOAP-ENV:Sender'
    sub = f'<SOAP-ENV:Subcode><SOAP-ENV:Value>{detail_code}</SOAP-ENV:Value></SOAP-ENV:Subcode>' if detail_code else ''
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<SOAP-ENV:Envelope {NS_DECL}><SOAP-ENV:Body><SOAP-ENV:Fault>'
           f'<SOAP-ENV:Code><SOAP-ENV:Value>{code}</SOAP-ENV:Value><SOAP-ENV:Subcode><SOAP-ENV:Value>{subcode}</SOAP-ENV:Value>{sub}'
           f'</SOAP-ENV:Subcode></SOAP-ENV:Code><SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">{escape(reason)}</SOAP-ENV:Text>'
           f'</SOAP-ENV:Reason></SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>')
    return Response(xml, mimetype='application/soap+xml', status=500 if receiver else 400)


def _text(body, name):
    m = re.search(rf'<(?:[\w-]+:)?{name}\b[^>]*>([^<]*)</', body)
    return unescape(m.group(1).strip(), {'&quot;': '"', '&apos;': "'"}) if m else None


def _vector(body, parent, child):
    """Read x/y attributes of e.g. <Velocity><PanTilt x= y=/></Velocity>."""
    pm = re.search(rf'<(?:[\w-]+:)?{parent}\b[^>]*>(.*?)</(?:[\w-]+:)?{parent}>', body, re.S)
    if not pm:
        return None
    cm = re.search(rf'<(?:[\w-]+:)?{child}\b([^>]*)/?>', pm.group(1))
    if not cm:
        return None
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', cm.group(1)))
    def f(k):
        try:
            return float(attrs[k]) if k in attrs else None
        except ValueError:
            return None
    return {'x': f('x'), 'y': f('y')}


def _speed(body):
    v = _vector(body, 'Speed', 'PanTilt')
    if v and v['x'] is not None:
        return abs(v['x'])
    return None


def _space(kind, xr=(-1, 1), yr=(-1, 1)):
    uri = {'abs': 'PositionGenericSpace', 'rel': 'TranslationGenericSpace', 'cont': 'VelocityGenericSpace'}[kind]
    return (f'<tt:URI>{GENERIC}/{uri}</tt:URI><tt:XRange><tt:Min>{xr[0]}</tt:Min><tt:Max>{xr[1]}</tt:Max></tt:XRange>'
            f'<tt:YRange><tt:Min>{yr[0]}</tt:Min><tt:Max>{yr[1]}</tt:Max></tt:YRange>')


ZOOM_GENERIC = "http://www.onvif.org/ver10/tptz/ZoomSpaces"


def _zoom_space(uri, xr=(-1, 1)):
    return (f'<tt:URI>{ZOOM_GENERIC}/{uri}</tt:URI>'
            f'<tt:XRange><tt:Min>{xr[0]}</tt:Min><tt:Max>{xr[1]}</tt:Max></tt:XRange>')


class ControlServices:
    """Handlers bound to one ONVIFService / virtual camera."""

    def __init__(self, service):
        self.svc = service
        self.cam = service.camera
        self._logged_reads = set()

    # ------------------------------------------------------------ helpers
    @property
    def control(self):
        return CameraControl(self.cam)

    @property
    def cid(self):
        return self.cam.id

    @property
    def node_token(self):
        return f"PTZNode_{self.cid}"

    @property
    def config_token(self):
        return f"PTZConfig_{self.cid}"

    def valid_profile(self, token):
        if token is None:
            return True
        ok = {f"mainStream_{self.cid}"}
        if not getattr(self.cam, 'disable_substream', False):
            ok.add(f"subStream_{self.cid}")
        return token in ok

    def log(self, service, op):
        # Commands are always logged; read-only operations (GetStatus polling etc.)
        # only the first time each, so the log shows what the NVR uses without flooding.
        if op.startswith('Get'):
            key = (service, op)
            if key in self._logged_reads:
                return
            self._logged_reads.add(key)
            print(f"  [ONVIF Control ({self.cam.name})] {service}.{op} from {request.remote_addr} (first call; further reads not logged)")
            return
        print(f"  [ONVIF Control ({self.cam.name})] {service}.{op} from {request.remote_addr}")

    def ptz_in_profile(self):
        c = self.control
        return bool(c.ptz) and (c.dc.get('ptz') or {}).get('inProfile', True)

    # -------------------------------------------------- capability snippets
    def capabilities_ptz_xml(self, base):
        if not self.control.ptz:
            return ''
        return f'<tt:PTZ><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>'

    def has_imaging(self):
        return self.control.imaging is not None

    def relay_count(self):
        return len(self.control.relays)

    def services_xml(self, base, include_caps=False):
        c = self.control
        out = ''
        ver = ('<tds:Version><tt:Major xmlns:tt="http://www.onvif.org/ver10/schema">2</tt:Major>'
               '<tt:Minor xmlns:tt="http://www.onvif.org/ver10/schema">5</tt:Minor></tds:Version>')
        if c.ptz:
            caps = ('<tds:Capabilities><tptz:Capabilities xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" EFlip="false" '
                    'Reverse="false" GetCompatibleConfigurations="true" MoveStatus="true" StatusPosition="true"/></tds:Capabilities>'
                    if include_caps else '')
            out += (f'<tds:Service><tds:Namespace>http://www.onvif.org/ver20/ptz/wsdl</tds:Namespace>'
                    f'<tds:XAddr>{base}/onvif/ptz_service</tds:XAddr>{caps}{ver}</tds:Service>')
        if c.imaging is not None:
            caps = ('<tds:Capabilities><timg:Capabilities xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" '
                    'ImageStabilization="false" Presets="false" AdaptablePreset="false"/></tds:Capabilities>' if include_caps else '')
            out += (f'<tds:Service><tds:Namespace>http://www.onvif.org/ver20/imaging/wsdl</tds:Namespace>'
                    f'<tds:XAddr>{base}/onvif/imaging_service</tds:XAddr>{caps}{ver}</tds:Service>')
        if c.relays:
            caps = (f'<tds:Capabilities><tmd:Capabilities xmlns:tmd="http://www.onvif.org/ver10/deviceIO/wsdl" VideoSources="1" '
                    f'VideoOutputs="0" AudioSources="0" AudioOutputs="0" RelayOutputs="{len(c.relays)}" DigitalInputs="0" '
                    f'SerialPorts="0"/></tds:Capabilities>' if include_caps else '')
            out += (f'<tds:Service><tds:Namespace>http://www.onvif.org/ver10/deviceIO/wsdl</tds:Namespace>'
                    f'<tds:XAddr>{base}/onvif/deviceio_service</tds:XAddr>{caps}{ver}</tds:Service>')
        return out

    def profile_ptz_xml(self):
        if not self.ptz_in_profile():
            return ''
        return f'<tt:PTZConfiguration token="{self.config_token}">{self._ptz_config_body()}</tt:PTZConfiguration>'

    def _ptz_config_body(self):
        ptz = self.control.ptz
        node = ptz.node
        uc = 1 if getattr(self.cam, 'disable_substream', False) else 2
        parts = [f'<tt:Name>PTZ</tt:Name><tt:UseCount>{uc}</tt:UseCount><tt:NodeToken>{self.node_token}</tt:NodeToken>']
        if node['pan_tilt_absolute']:
            parts.append(f'<tt:DefaultAbsolutePantTiltPositionSpace>{GENERIC}/PositionGenericSpace</tt:DefaultAbsolutePantTiltPositionSpace>')
        if node['pan_tilt_relative']:
            parts.append(f'<tt:DefaultRelativePanTiltTranslationSpace>{GENERIC}/TranslationGenericSpace</tt:DefaultRelativePanTiltTranslationSpace>')
        if node['pan_tilt_continuous']:
            parts.append(f'<tt:DefaultContinuousPanTiltVelocitySpace>{GENERIC}/VelocityGenericSpace</tt:DefaultContinuousPanTiltVelocitySpace>')
        if node['zoom_continuous']:
            parts.append(f'<tt:DefaultContinuousZoomVelocitySpace>{ZOOM_GENERIC}/VelocityGenericSpace</tt:DefaultContinuousZoomVelocitySpace>')
        zoom_speed = f'<tt:Zoom x="1" space="{ZOOM_GENERIC}/ZoomGenericSpeedSpace"/>' if node['zoom_continuous'] else ''
        parts.append(f'<tt:DefaultPTZSpeed><tt:PanTilt x="1" y="1" space="{GENERIC}/GenericSpeedSpace"/>{zoom_speed}</tt:DefaultPTZSpeed>')
        parts.append('<tt:DefaultPTZTimeout>PT5S</tt:DefaultPTZTimeout>')
        if node['pan_tilt_absolute']:
            parts.append(f'<tt:PanTiltLimits><tt:Range>{_space("abs")}</tt:Range></tt:PanTiltLimits>')
        return ''.join(parts)

    def _spaces_xml(self):
        node = self.control.ptz.node
        s = ''
        if node['pan_tilt_absolute']:
            s += f'<tt:AbsolutePanTiltPositionSpace>{_space("abs")}</tt:AbsolutePanTiltPositionSpace>'
        if node['pan_tilt_relative']:
            s += f'<tt:RelativePanTiltTranslationSpace>{_space("rel")}</tt:RelativePanTiltTranslationSpace>'
        if node['pan_tilt_continuous']:
            s += f'<tt:ContinuousPanTiltVelocitySpace>{_space("cont")}</tt:ContinuousPanTiltVelocitySpace>'
        if node['zoom_continuous']:
            s += f'<tt:ContinuousZoomVelocitySpace>{_zoom_space("VelocityGenericSpace")}</tt:ContinuousZoomVelocitySpace>'
        s += (f'<tt:PanTiltSpeedSpace><tt:URI>{GENERIC}/GenericSpeedSpace</tt:URI>'
              '<tt:XRange><tt:Min>0</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:PanTiltSpeedSpace>')
        if node['zoom_continuous']:
            s += f'<tt:ZoomSpeedSpace>{_zoom_space("ZoomGenericSpeedSpace", (0, 1))}</tt:ZoomSpeedSpace>'
        return s

    def _node_xml(self, tag):
        ptz = self.control.ptz
        return (f'<{tag} token="{self.node_token}" FixedHomePosition="false">'
                f'<tt:Name>PTZNode</tt:Name><tt:SupportedPTZSpaces>{self._spaces_xml()}</tt:SupportedPTZSpaces>'
                f'<tt:MaximumNumberOfPresets>{ptz.node["max_presets"]}</tt:MaximumNumberOfPresets>'
                f'<tt:HomeSupported>{"true" if ptz.home_supported else "false"}</tt:HomeSupported></{tag}>')

    # ---------------------------------------------------------------- PTZ
    def handle_ptz(self, body):
        op = soap_op(body) or ''
        self.log('PTZ', op)
        ptz = self.control.ptz
        if not ptz:
            return fault('ter:ActionNotSupported', 'PTZ is not enabled for this camera')
        profile = _text(body, 'ProfileToken')
        if op in ('ContinuousMove', 'Stop', 'RelativeMove', 'AbsoluteMove', 'GetStatus', 'GetPresets',
                  'GotoPreset', 'SetPreset', 'RemovePreset', 'GotoHomePosition', 'SetHomePosition') \
                and not self.valid_profile(profile):
            return fault('ter:InvalidArgVal', 'No such profile', 'ter:NoProfile')
        try:
            if op == 'GetServiceCapabilities':
                return envelope('<tptz:GetServiceCapabilitiesResponse><tptz:Capabilities EFlip="false" Reverse="false" '
                                'GetCompatibleConfigurations="true" MoveStatus="true" StatusPosition="true"/>'
                                '</tptz:GetServiceCapabilitiesResponse>')
            if op == 'GetNodes':
                return envelope(f'<tptz:GetNodesResponse>{self._node_xml("tptz:PTZNode")}</tptz:GetNodesResponse>')
            if op == 'GetNode':
                if _text(body, 'NodeToken') not in (None, self.node_token):
                    return fault('ter:InvalidArgVal', 'No such node', 'ter:NoEntity')
                return envelope(f'<tptz:GetNodeResponse>{self._node_xml("tptz:PTZNode")}</tptz:GetNodeResponse>')
            if op in ('GetConfigurations', 'GetCompatibleConfigurations'):
                return envelope(f'<tptz:{op}Response><tptz:PTZConfiguration token="{self.config_token}">'
                                f'{self._ptz_config_body()}</tptz:PTZConfiguration></tptz:{op}Response>')
            if op == 'GetConfiguration':
                if _text(body, 'PTZConfigurationToken') not in (None, self.config_token):
                    return fault('ter:InvalidArgVal', 'No such configuration', 'ter:NoConfig')
                return envelope(f'<tptz:GetConfigurationResponse><tptz:PTZConfiguration token="{self.config_token}">'
                                f'{self._ptz_config_body()}</tptz:PTZConfiguration></tptz:GetConfigurationResponse>')
            if op == 'GetConfigurationOptions':
                return envelope('<tptz:GetConfigurationOptionsResponse><tptz:PTZConfigurationOptions>'
                                f'<tt:Spaces>{self._spaces_xml()}</tt:Spaces>'
                                '<tt:PTZTimeout><tt:Min>PT1S</tt:Min><tt:Max>PT60S</tt:Max></tt:PTZTimeout>'
                                '</tptz:PTZConfigurationOptions></tptz:GetConfigurationOptionsResponse>')
            if op == 'SetConfiguration':
                return envelope('<tptz:SetConfigurationResponse/>')
            if op == 'ContinuousMove':
                v = _vector(body, 'Velocity', 'PanTilt') or {}
                z = _vector(body, 'Velocity', 'Zoom') or {}
                timeout = parse_duration(_text(body, 'Timeout'))
                ptz.continuous_move(v.get('x') or 0.0, v.get('y') or 0.0, z.get('x') or 0.0, timeout)
                return envelope('<tptz:ContinuousMoveResponse/>')
            if op == 'Stop':
                ptz.stop()
                return envelope('<tptz:StopResponse/>')
            if op == 'RelativeMove':
                t = _vector(body, 'Translation', 'PanTilt') or {}
                z = _vector(body, 'Translation', 'Zoom') or {}
                ptz.relative_move(t.get('x') or 0.0, t.get('y') or 0.0, z.get('x') or 0.0, _speed(body))
                return envelope('<tptz:RelativeMoveResponse/>')
            if op == 'AbsoluteMove':
                p = _vector(body, 'Position', 'PanTilt') or {}
                z = _vector(body, 'Position', 'Zoom') or {}
                ptz.absolute_move(p.get('x'), p.get('y'), z.get('x'), _speed(body))
                return envelope('<tptz:AbsoluteMoveResponse/>')
            if op == 'GetStatus':
                st = ptz.get_status()
                pos = ''
                if st.get('pan') is not None and st.get('tilt') is not None:
                    pos = (f'<tt:Position><tt:PanTilt x="{st["pan"]}" y="{st["tilt"]}" '
                           f'space="{GENERIC}/PositionGenericSpace"/></tt:Position>')
                now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                return envelope(f'<tptz:GetStatusResponse><tptz:PTZStatus>{pos}<tt:MoveStatus><tt:PanTilt>'
                                f'{st.get("move_status", "IDLE")}</tt:PanTilt></tt:MoveStatus><tt:UtcTime>{now}</tt:UtcTime>'
                                '</tptz:PTZStatus></tptz:GetStatusResponse>')
            if op == 'GetPresets':
                items = ''
                for p in ptz.get_presets():
                    pos = (f'<tt:PTZPosition><tt:PanTilt x="{p["pan"]}" y="{p["tilt"]}" space="{GENERIC}/PositionGenericSpace"/>'
                           '</tt:PTZPosition>') if p.get('pan') is not None and p.get('tilt') is not None else ''
                    items += f'<tptz:Preset token="{escape(str(p["token"]))}"><tt:Name>{escape(str(p["name"]))}</tt:Name>{pos}</tptz:Preset>'
                return envelope(f'<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>')
            if op == 'GotoPreset':
                token = _text(body, 'PresetToken')
                if not token or token not in {p['token'] for p in ptz.get_presets()}:
                    return fault('ter:InvalidArgVal', 'No such preset', 'ter:NoToken')
                ptz.goto_preset(token, _speed(body))
                return envelope('<tptz:GotoPresetResponse/>')
            if op == 'SetPreset':
                token = ptz.set_preset(_text(body, 'PresetName'), _text(body, 'PresetToken'))
                return envelope(f'<tptz:SetPresetResponse><tptz:PresetToken>{escape(str(token or ""))}</tptz:PresetToken>'
                                '</tptz:SetPresetResponse>')
            if op == 'RemovePreset':
                token = _text(body, 'PresetToken')
                if not token or token not in {p['token'] for p in ptz.get_presets()}:
                    return fault('ter:InvalidArgVal', 'No such preset', 'ter:NoToken')
                ptz.remove_preset(token)
                return envelope('<tptz:RemovePresetResponse/>')
            if op == 'GotoHomePosition':
                if not ptz.goto_home(_speed(body)):
                    return fault('ter:Action', 'No home position set (save a preset named "Home")', 'ter:NoHomePosition',
                                 receiver=True)
                return envelope('<tptz:GotoHomePositionResponse/>')
            if op == 'SetHomePosition':
                ptz.set_home()
                return envelope('<tptz:SetHomePositionResponse/>')
            return fault()
        except PtzUnavailable as e:
            return fault('ter:Action', f'Camera unavailable: {e}', receiver=True)
        except Exception as e:
            return fault('ter:Action', f'PTZ command failed: {type(e).__name__}', receiver=True)

    # ------------------------------------------------------------ Imaging
    def handle_imaging(self, body):
        op = soap_op(body) or ''
        self.log('Imaging', op)
        img = self.control.imaging
        if img is None:
            return fault('ter:ActionNotSupported', 'Imaging is not enabled for this camera')
        vs = _text(body, 'VideoSourceToken')
        if op not in ('GetServiceCapabilities',) and vs not in (None, f"VideoSource_{self.cid}"):
            return fault('ter:InvalidArgVal', 'No such video source', 'ter:NoSource')
        lo, hi = img.slider_range
        sliders = getattr(img, 'sliders', None)
        sliders = list(SLIDERS) if sliders is None else sliders
        try:
            if op == 'GetServiceCapabilities':
                return envelope('<timg:GetServiceCapabilitiesResponse><timg:Capabilities ImageStabilization="false" '
                                'Presets="false" AdaptablePreset="false"/></timg:GetServiceCapabilitiesResponse>')
            if op == 'GetImagingSettings':
                cur = img.get()
                parts = ''
                for name in ('Brightness', 'ColorSaturation', 'Contrast'):
                    if name in sliders and name in cur:
                        parts += f'<tt:{name}>{cur[name]:g}</tt:{name}>'
                if img.ir_modes and cur.get('IrCutFilter'):
                    parts += f'<tt:IrCutFilter>{cur["IrCutFilter"]}</tt:IrCutFilter>'
                if 'Sharpness' in sliders and 'Sharpness' in cur:
                    parts += f'<tt:Sharpness>{cur["Sharpness"]:g}</tt:Sharpness>'
                return envelope(f'<timg:GetImagingSettingsResponse><timg:ImagingSettings>{parts}</timg:ImagingSettings>'
                                '</timg:GetImagingSettingsResponse>')
            if op == 'GetOptions':
                rng = f'<tt:Min>{lo:g}</tt:Min><tt:Max>{hi:g}</tt:Max>'
                parts = ''.join(f'<tt:{n}>{rng}</tt:{n}>' for n in ('Brightness', 'ColorSaturation', 'Contrast') if n in sliders)
                parts += ''.join(f'<tt:IrCutFilterModes>{m}</tt:IrCutFilterModes>' for m in img.ir_modes)
                if 'Sharpness' in sliders:
                    parts += f'<tt:Sharpness>{rng}</tt:Sharpness>'
                return envelope(f'<timg:GetOptionsResponse><timg:ImagingOptions>{parts}</timg:ImagingOptions></timg:GetOptionsResponse>')
            if op == 'SetImagingSettings':
                values = {}
                for name in SLIDERS:
                    t = _text(body, name)
                    if t is not None and t != '':
                        try:
                            v = float(t)
                        except ValueError:
                            return fault('ter:InvalidArgVal', f'Bad {name}', 'ter:SettingsInvalid')
                        if not (lo <= v <= hi):
                            return fault('ter:InvalidArgVal', f'{name} out of range', 'ter:SettingsInvalid')
                        if name in sliders:
                            values[name] = v
                ir = _text(body, 'IrCutFilter')
                if ir:
                    ir = ir.upper()
                    if ir not in img.ir_modes:
                        return fault('ter:InvalidArgVal', f'IrCutFilter {ir} not supported', 'ter:SettingsInvalid')
                    values['IrCutFilter'] = ir
                img.set(values)
                return envelope('<timg:SetImagingSettingsResponse/>')
            if op == 'GetMoveOptions':
                return envelope('<timg:GetMoveOptionsResponse><timg:MoveOptions/></timg:GetMoveOptionsResponse>')
            if op == 'GetStatus':
                return envelope('<timg:GetStatusResponse><timg:Status/></timg:GetStatusResponse>')
            if op in ('Move', 'Stop'):
                return fault('ter:ActionNotSupported', 'No focus control')
            return fault()
        except ControlError as e:
            return fault('ter:Action', str(e)[:200], receiver=True)
        except Exception as e:
            return fault('ter:Action', f'Imaging failed: {type(e).__name__}', receiver=True)

    # ------------------------------------------------------------ DeviceIO
    def _relay_xml(self, r, tag):
        delay = f"PT{r.delay}S" if r.monostable else "PT0S"
        return (f'<{tag} token="{r.token}"><tt:Properties><tt:Mode>{r.mode}</tt:Mode><tt:DelayTime>{delay}</tt:DelayTime>'
                f'<tt:IdleState>open</tt:IdleState></tt:Properties></{tag}>')

    def handle_relay_op(self, op, body, service):
        """Relay operations shared by the device service and the DeviceIO service.
        Returns a Response, or None if op is not a relay operation."""
        c = self.control
        # Answer in the namespace of the endpoint that was called: tds on the
        # device service, tmd on the DeviceIO service.
        ns = 'tmd' if service == 'deviceio' else 'tds'
        if op == 'GetRelayOutputs':
            return envelope(f'<{ns}:GetRelayOutputsResponse>' + ''.join(self._relay_xml(r, f'{ns}:RelayOutputs') for r in c.relays)
                            + f'</{ns}:GetRelayOutputsResponse>')
        if op == 'SetRelayOutputState':
            token = _text(body, 'RelayOutputToken')
            state = (_text(body, 'LogicalState') or '').lower()
            r = c.relay(token)
            if not r:
                return fault('ter:InvalidArgVal', 'No such relay output', 'ter:RelayToken')
            if state not in ('active', 'inactive'):
                return fault('ter:InvalidArgVal', 'LogicalState must be active or inactive', 'ter:ModeError')
            try:
                r.set(state == 'active')
            except ControlError as e:
                return fault('ter:Action', str(e)[:200], receiver=True)
            print(f"  [ONVIF Control ({self.cam.name})] Relay {token} -> {state}")
            return envelope(f'<{ns}:SetRelayOutputStateResponse/>')
        if op == 'SetRelayOutputSettings':
            token = _text(body, 'RelayOutputToken') or (re.search(r'RelayOutput\b[^>]*token="([^"]+)"', body) or [None, None])[1]
            r = c.relay(token)
            if not r:
                return fault('ter:InvalidArgVal', 'No such relay output', 'ter:RelayToken')
            try:
                r.configure(_text(body, 'Mode'), parse_duration(_text(body, 'DelayTime')))
            except ControlError as e:
                return fault('ter:InvalidArgVal', str(e), 'ter:ModeError')
            return envelope(f'<{ns}:SetRelayOutputSettingsResponse/>')
        return None

    def handle_deviceio(self, body):
        op = soap_op(body) or ''
        self.log('DeviceIO', op)
        c = self.control
        if not c.relays:
            return fault('ter:ActionNotSupported', 'No relay outputs on this camera')
        resp = self.handle_relay_op(op, body, 'deviceio')
        if resp is not None:
            return resp
        if op == 'GetServiceCapabilities':
            return envelope(f'<tmd:GetServiceCapabilitiesResponse><tmd:Capabilities VideoSources="1" VideoOutputs="0" '
                            f'AudioSources="0" AudioOutputs="0" RelayOutputs="{len(c.relays)}" DigitalInputs="0" '
                            'SerialPorts="0"/></tmd:GetServiceCapabilitiesResponse>')
        if op == 'GetRelayOutputOptions':
            out = ''
            for r in c.relays:
                if r.monostable:
                    out += (f'<tmd:RelayOutputOptions token="{r.token}"><tmd:Mode>Monostable</tmd:Mode>'
                            f'<tmd:DelayTimes>1 {r.max_delay}</tmd:DelayTimes></tmd:RelayOutputOptions>')
                else:
                    out += f'<tmd:RelayOutputOptions token="{r.token}"><tmd:Mode>Bistable</tmd:Mode></tmd:RelayOutputOptions>'
            return envelope(f'<tmd:GetRelayOutputOptionsResponse>{out}</tmd:GetRelayOutputOptionsResponse>')
        if op == 'GetDigitalInputs':
            return envelope('<tmd:GetDigitalInputsResponse/>')
        if op in ('GetVideoSources', 'GetAudioSources', 'GetAudioOutputs', 'GetVideoOutputs', 'GetSerialPorts'):
            return envelope(f'<tmd:{op}Response/>')
        return fault()

    # -------------------------------------------------------- relay events
    def attach(self):
        """Build the device controller (starting its probes) and subscribe to its
        relay state changes. The subscription is held by the registry per camera,
        so it survives the controller being rebuilt."""
        self.control  # builds / reuses the controller
        registry.add_listener(self.cam, self._relay_changed)

    def detach(self):
        registry.remove_listener(self.cam)

    def _relay_changed(self, token, active):
        if not self.control.relay(token):
            return  # this virtual camera doesn't expose that relay
        evt = {'topic': 'Device/Trigger/Relay', 'relay_token': token,
               'value': 'active' if active else 'inactive', 'data_name': 'LogicalState',
               'timestamp': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
               'camera_id': self.cid, 'camera_name': self.cam.name, 'type': 'relay', 'tags': [token.lower()]}
        import queue
        for sub in list(self.svc.subscriptions.values()):
            try:
                sub.queue.put_nowait(evt)
            except queue.Full:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(evt)
                except Exception:
                    pass

    def event_topics_xml(self):
        if not self.control.relays:
            return ''
        return ('<tns1:Device xmlns:tns1="http://www.onvif.org/ver10/topics"><tns1:Trigger><tns1:Relay wstop:topic="true" '
                'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"><tt:MessageDescription IsProperty="true"><tt:Source>'
                '<tt:SimpleItemDescription Name="RelayToken" Type="tt:ReferenceToken"/></tt:Source><tt:Data>'
                '<tt:SimpleItemDescription Name="LogicalState" Type="tt:RelayLogicalState"/></tt:Data>'
                '</tt:MessageDescription></tns1:Relay></tns1:Trigger></tns1:Device>')
