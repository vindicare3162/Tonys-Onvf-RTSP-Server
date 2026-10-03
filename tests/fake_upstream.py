"""A local HTTP server emulating a physical camera for device-control tests.

Emulates (a) an ONVIF device/media/PTZ/imaging service and (b) the Reolink
/cgi-bin/api.cgi JSON API. Every request is recorded so tests can assert on
exactly what reached the "camera".
"""

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ENV_OPEN = ('<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
            'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"><s:Body>')
ENV_CLOSE = '</s:Body></s:Envelope>'


def soap_fault_xml(reason="boom"):
    return (ENV_OPEN + '<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code><s:Reason><s:Text xml:lang="en">'
            f'{reason}</s:Text></s:Reason></s:Fault>' + ENV_CLOSE)


def body_op(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.rsplit('}', 1)[-1] == 'Body':
            for child in el:
                return child.tag.rsplit('}', 1)[-1]
    return None


class FakeCamera:
    def __init__(self):
        self.lock = threading.Lock()
        self.log = []                 # (t, kind, op, body/json)
        self.presets = {"1": "Door", "2": "Home"}
        self.next_preset = 3
        self.moving = False
        self.imaging = {"Brightness": 50.0, "ColorSaturation": 50.0, "Contrast": 50.0, "Sharpness": 50.0}
        # Reolink state
        self.reolink_user = "admin"
        self.reolink_password = "s3cretPW!"
        self.tokens = set()
        self.token_counter = 0
        self.whiteled = {"channel": 0, "state": 0, "mode": 3, "bright": 80,
                         "LightingSchedule": {"EndHour": 6}}
        self.ir = "Auto"
        self.isp = {"channel": 0, "dayNight": "Auto", "antiFlicker": "Outdoor", "exposure": "Auto",
                    "backLight": "Off", "mirroring": 0, "rotation": 0}
        self.image = {"channel": 0, "bright": 128, "contrast": 128, "saturation": 128, "sharpen": 128, "hue": 128}
        self.expire_tokens_once = False     # next non-login call answers rspCode -6
        self.ptz_fail_ops = set()           # ops answered with a SOAP fault
        self.ptz_delay = 0.0                # seconds to sleep before answering PTZ ops
        self.siren_off_failures = 0         # next N AudioAlarmPlay manual_switch=0 calls fail
        self.garbage_cmds = set()           # Reolink cmds answered with non-JSON
        self.server = None

    # ---------------------------------------------------------------- helpers
    def record(self, kind, op, payload):
        with self.lock:
            self.log.append((time.time(), kind, op, payload))

    def ops(self, kind=None):
        with self.lock:
            return [e[2] for e in self.log if kind is None or e[1] == kind]

    def entries(self, op):
        with self.lock:
            return [e for e in self.log if e[2] == op]

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        cam = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype):
                data = data.encode() if isinstance(data, str) else data
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                url = urlparse(self.path)
                if url.path == "/cgi-bin/api.cgi":
                    code, out = cam.reolink(parse_qs(url.query), raw)
                    if out == "GARBAGE":
                        return self._send(200, "<html>not json</html>", "text/html")
                    return self._send(code, json.dumps(out), "application/json")
                code, out = cam.onvif(url.path, raw.decode("utf-8", "replace"))
                return self._send(code, out, "application/soap+xml")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # ------------------------------------------------------------------ ONVIF
    def onvif(self, path, raw):
        op = body_op(raw)
        kind = path.rsplit("/", 1)[-1]
        self.record(kind, op, raw)
        b = self.base
        if kind == "device_service":
            if op == "GetSystemDateAndTime":
                n = datetime.now(timezone.utc)
                return 200, (ENV_OPEN + '<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>'
                             f'<tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute><tt:Second>{n.second}</tt:Second></tt:Time>'
                             f'<tt:Date><tt:Year>{n.year}</tt:Year><tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date>'
                             '</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>' + ENV_CLOSE)
            if op == "GetCapabilities":
                return 200, (ENV_OPEN + '<tds:GetCapabilitiesResponse><tds:Capabilities>'
                             f'<tt:Imaging><tt:XAddr>{b}/onvif/imaging_service</tt:XAddr></tt:Imaging>'
                             f'<tt:Media><tt:XAddr>{b}/onvif/media_service</tt:XAddr></tt:Media>'
                             f'<tt:PTZ><tt:XAddr>{b}/onvif/ptz_service</tt:XAddr></tt:PTZ>'
                             '</tds:Capabilities></tds:GetCapabilitiesResponse>' + ENV_CLOSE)
        if kind == "media_service" and op == "GetProfiles":
            return 200, (ENV_OPEN + '<trt:GetProfilesResponse><trt:Profiles token="prof1" fixed="true"><tt:Name>p1</tt:Name>'
                         '<tt:VideoSourceConfiguration token="vsc"><tt:SourceToken>vs1</tt:SourceToken></tt:VideoSourceConfiguration>'
                         '<tt:PTZConfiguration token="ptzc"><tt:NodeToken>n1</tt:NodeToken></tt:PTZConfiguration>'
                         '</trt:Profiles></trt:GetProfilesResponse>' + ENV_CLOSE)
        if kind == "ptz_service":
            if self.ptz_delay:
                time.sleep(self.ptz_delay)
            if op in self.ptz_fail_ops:
                return 500, soap_fault_xml(f"{op} refused")
            if op == "GetNodes":
                return 200, (ENV_OPEN + '<tptz:GetNodesResponse><tptz:PTZNode token="n1"><tt:SupportedPTZSpaces>'
                             '<tt:AbsolutePanTiltPositionSpace/><tt:RelativePanTiltTranslationSpace/>'
                             '<tt:ContinuousPanTiltVelocitySpace/></tt:SupportedPTZSpaces>'
                             '<tt:MaximumNumberOfPresets>8</tt:MaximumNumberOfPresets><tt:HomeSupported>false</tt:HomeSupported>'
                             '</tptz:PTZNode></tptz:GetNodesResponse>' + ENV_CLOSE)
            if op == "ContinuousMove":
                self.moving = True
                return 200, ENV_OPEN + '<tptz:ContinuousMoveResponse/>' + ENV_CLOSE
            if op == "Stop":
                self.moving = False
                return 200, ENV_OPEN + '<tptz:StopResponse/>' + ENV_CLOSE
            if op == "GetPresets":
                items = ''.join(f'<tptz:Preset token="{t}"><tt:Name>{n}</tt:Name></tptz:Preset>'
                                for t, n in sorted(self.presets.items()))
                return 200, ENV_OPEN + f'<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>' + ENV_CLOSE
            if op == "SetPreset":
                m = re.search(r'PresetToken>([^<]+)<', raw)
                name = re.search(r'PresetName>([^<]+)<', raw)
                tok = m.group(1) if m else str(self.next_preset)
                if not m:
                    self.next_preset += 1
                self.presets[tok] = name.group(1) if name else tok
                return 200, (ENV_OPEN + f'<tptz:SetPresetResponse><tptz:PresetToken>{tok}</tptz:PresetToken>'
                             '</tptz:SetPresetResponse>' + ENV_CLOSE)
            if op == "RemovePreset":
                m = re.search(r'PresetToken>([^<]+)<', raw)
                self.presets.pop(m.group(1), None)
                return 200, ENV_OPEN + '<tptz:RemovePresetResponse/>' + ENV_CLOSE
            if op in ("GotoPreset", "RelativeMove", "AbsoluteMove", "GotoHomePosition"):
                return 200, ENV_OPEN + f'<tptz:{op}Response/>' + ENV_CLOSE
            if op == "GetStatus":
                st = "MOVING" if self.moving else "IDLE"
                return 200, (ENV_OPEN + '<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position><tt:PanTilt x="0.1" y="-0.2"/>'
                             f'</tt:Position><tt:MoveStatus><tt:PanTilt>{st}</tt:PanTilt></tt:MoveStatus></tptz:PTZStatus>'
                             '</tptz:GetStatusResponse>' + ENV_CLOSE)
        if kind == "imaging_service":
            if op == "GetOptions":
                rng = '<tt:Min>0</tt:Min><tt:Max>100</tt:Max>'
                return 200, (ENV_OPEN + '<timg:GetOptionsResponse><timg:ImagingOptions>'
                             + ''.join(f'<tt:{k}>{rng}</tt:{k}>' for k in self.imaging)
                             + '</timg:ImagingOptions></timg:GetOptionsResponse>' + ENV_CLOSE)
            if op == "GetImagingSettings":
                return 200, (ENV_OPEN + '<timg:GetImagingSettingsResponse><timg:ImagingSettings>'
                             + ''.join(f'<tt:{k}>{v}</tt:{k}>' for k, v in self.imaging.items())
                             + '</timg:ImagingSettings></timg:GetImagingSettingsResponse>' + ENV_CLOSE)
            if op == "SetImagingSettings":
                for k in self.imaging:
                    m = re.search(rf'{k}>([^<]+)<', raw)
                    if m:
                        self.imaging[k] = float(m.group(1))
                return 200, ENV_OPEN + '<timg:SetImagingSettingsResponse/>' + ENV_CLOSE
        return 400, soap_fault_xml(f"unhandled {kind}.{op}")

    # ---------------------------------------------------------------- Reolink
    def reolink(self, query, raw):
        cmd = (query.get("cmd") or [""])[0]
        token = (query.get("token") or [None])[0]
        try:
            req = json.loads(raw or b"[]")
        except ValueError:
            return 400, {}
        entry = req[0] if req else {}
        self.record("reolink", cmd, entry)
        if cmd == "Login":
            u = entry["param"]["User"]
            if u["userName"] != self.reolink_user or u["password"] != self.reolink_password:
                return 200, [{"cmd": "Login", "code": 1, "error": {"detail": "invalid user", "rspCode": -27}}]
            self.token_counter += 1
            tok = f"tok{self.token_counter:04d}abcdef"
            self.tokens.add(tok)
            return 200, [{"cmd": "Login", "code": 0, "value": {"Token": {"leaseTime": 3600, "name": tok}}}]
        if cmd == "Logout":
            self.tokens.discard(token)
            return 200, [{"cmd": "Logout", "code": 0, "value": {"rspCode": 200}}]
        if token not in self.tokens or self.expire_tokens_once:
            self.expire_tokens_once = False
            self.tokens.discard(token)
            return 200, [{"cmd": cmd, "code": 1, "error": {"detail": "please login first", "rspCode": -6}}]
        if cmd in self.garbage_cmds:
            return 200, "GARBAGE"
        p = entry.get("param", {})
        ok = [{"cmd": cmd, "code": 0, "value": {"rspCode": 200}}]
        if cmd == "GetWhiteLed":
            return 200, [{"cmd": cmd, "code": 0, "value": {"WhiteLed": dict(self.whiteled)}}]
        if cmd == "SetWhiteLed":
            self.whiteled.update(p["WhiteLed"])
            return 200, ok
        if cmd == "GetIrLights":
            return 200, [{"cmd": cmd, "code": 0, "value": {"IrLights": {"state": self.ir}}}]
        if cmd == "SetIrLights":
            self.ir = p["IrLights"]["state"]
            return 200, ok
        if cmd == "GetIsp":
            return 200, [{"cmd": cmd, "code": 0, "value": {"Isp": dict(self.isp)}}]
        if cmd == "SetIsp":
            self.isp = dict(p["Isp"])
            return 200, ok
        if cmd == "GetImage":
            return 200, [{"cmd": cmd, "code": 0, "value": {"Image": dict(self.image)}}]
        if cmd == "SetImage":
            self.image = dict(p["Image"])
            return 200, ok
        if cmd == "AudioAlarmPlay":
            if p.get("manual_switch") == 0 and self.siren_off_failures > 0:
                self.siren_off_failures -= 1
                return 200, [{"cmd": cmd, "code": 1, "error": {"detail": "busy", "rspCode": -12}}]
            return 200, ok
        return 200, [{"cmd": cmd, "code": 1, "error": {"detail": "not supported", "rspCode": -9}}]
