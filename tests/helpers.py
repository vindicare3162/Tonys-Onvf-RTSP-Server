import time
import xml.etree.ElementTree as ET

NS = {
    's': 'http://www.w3.org/2003/05/soap-envelope',
    'tt': 'http://www.onvif.org/ver10/schema',
    'tds': 'http://www.onvif.org/ver10/device/wsdl',
    'trt': 'http://www.onvif.org/ver10/media/wsdl',
    'tptz': 'http://www.onvif.org/ver20/ptz/wsdl',
    'timg': 'http://www.onvif.org/ver20/imaging/wsdl',
    'tmd': 'http://www.onvif.org/ver10/deviceIO/wsdl',
    'wsnt': 'http://docs.oasis-open.org/wsn/b-2',
    'tet': 'http://www.onvif.org/ver10/events/wsdl',
}


def parse(resp):
    """Parse a Flask response as XML (fails the test if not well-formed)."""
    return ET.fromstring(resp.data)


def local(tag):
    return tag.rsplit('}', 1)[-1]


def body_child(resp):
    root = parse(resp)
    body = root.find('s:Body', NS)
    assert body is not None, resp.data
    return list(body)[0]


def fault_codes(resp):
    """Return [Code/Value, Subcode/Value, nested Subcode/Value...] of a SOAP 1.2 fault."""
    f = body_child(resp)
    assert local(f.tag) == 'Fault', resp.data
    code = f.find('s:Code', NS)
    out = [code.find('s:Value', NS).text]
    sub = code.find('s:Subcode', NS)
    while sub is not None:
        out.append(sub.find('s:Value', NS).text)
        sub = sub.find('s:Subcode', NS)
    return out


def wait_for(pred, timeout=5.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()
