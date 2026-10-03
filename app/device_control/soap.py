"""Minimal raw SOAP 1.2 client for calling a physical camera's ONVIF services.

Used on the PTZ hot path instead of onvif-zeep: zeep's ONVIFCamera makes
network calls in its constructor, its clients are not documented as thread
safe, and its WS-Security timestamp breaks when the camera's clock is skewed.
Here a WS-UsernameToken digest is built per request with `Created` corrected
by a measured clock offset, and the response is parsed with ElementTree by
local element name.
"""

import base64
import hashlib
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import requests

NS = {
    'tptz': 'http://www.onvif.org/ver20/ptz/wsdl',
    'timg': 'http://www.onvif.org/ver20/imaging/wsdl',
    'tds': 'http://www.onvif.org/ver10/device/wsdl',
    'tt': 'http://www.onvif.org/ver10/schema',
}


class SoapFault(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def local(tag):
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def find_all(elem, name):
    return [e for e in elem.iter() if local(e.tag) == name]


def find_first(elem, name):
    for e in elem.iter():
        if local(e.tag) == name:
            return e
    return None


def parse_duration(text, default=None):
    """Parse an xs:duration like PT5S / PT1M30S / PT0.5S into seconds."""
    if not text:
        return default
    m = re.fullmatch(r'\s*P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?\s*', text)
    if not m:
        return default
    d, h, mi, s = m.groups()
    return (int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0))


class SoapClient:
    def __init__(self, username, password, timeout=2.5):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.clock_offset = timedelta(0)  # camera_time - local_time
        self._session = requests.Session()
        self.last_used = 0.0

    def reset_session(self):
        try:
            self._session.close()
        except Exception:
            pass
        self._session = requests.Session()

    def _security(self):
        if not self.username:
            return ""
        nonce = secrets.token_bytes(16)
        created = (datetime.now(timezone.utc) + self.clock_offset).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            '<wsse:UsernameToken>'
            f'<wsse:Username>{escape(self.username)}</wsse:Username>'
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f'{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f'<wsu:Created>{created}</wsu:Created>'
            '</wsse:UsernameToken></wsse:Security>'
        )

    def call(self, url, body_xml, timeout=None):
        """POST a SOAP request whose Body content is body_xml. Returns the Body element."""
        env = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:tptz="{NS["tptz"]}" xmlns:timg="{NS["timg"]}" xmlns:tds="{NS["tds"]}" xmlns:tt="{NS["tt"]}">'
            f'<s:Header>{self._security()}</s:Header><s:Body>{body_xml}</s:Body></s:Envelope>'
        )
        import time as _time
        self.last_used = _time.time()
        resp = self._session.post(url, data=env.encode('utf-8'), timeout=timeout or self.timeout,
                                  headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            raise SoapFault(f"HTTP {resp.status_code}: non-XML response")
        body = find_first(root, 'Body')
        if body is None:
            raise SoapFault(f"HTTP {resp.status_code}: no SOAP Body")
        fault = find_first(body, 'Fault')
        if fault is not None or resp.status_code >= 400:
            codes = [v.text for v in find_all(fault, 'Value')] if fault is not None else []
            text = find_first(fault, 'Text') if fault is not None else None
            reason = (text.text if text is not None else '') or f"HTTP {resp.status_code}"
            raise SoapFault(f"{reason} {codes}".strip(), code=codes[-1] if codes else None)
        return body

    def sync_clock(self, device_url):
        """Measure the camera clock offset from GetSystemDateAndTime (unauthenticated per spec)."""
        saved = self.username
        self.username = ''
        try:
            body = self.call(device_url, '<tds:GetSystemDateAndTime/>')
        finally:
            self.username = saved
        utc = find_first(body, 'UTCDateTime')
        if utc is None:
            return
        def num(name):
            e = find_first(utc, name)
            return int(e.text) if e is not None and e.text else 0
        try:
            cam = datetime(num('Year'), num('Month'), num('Day'), num('Hour'), num('Minute'), num('Second'),
                           tzinfo=timezone.utc)
            offset = cam - datetime.now(timezone.utc)
            # Only correct meaningful skew; sub-5s jitter is within every camera's tolerance.
            self.clock_offset = offset if abs(offset.total_seconds()) > 5 else timedelta(0)
        except ValueError:
            pass
