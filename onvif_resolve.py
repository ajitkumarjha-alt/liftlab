"""
ONVIF per-channel RTSP URI resolver (stdlib only; safe to import in the
import-light agent). This NVR selects channels by PATH (/<channel>/<stream>),
never by ?channel=N, so we ask ONVIF for the authoritative URIs.

GetProfiles + GetStreamUri per profile, then parse the returned path to learn the
REAL (channel, stream) — we do NOT assume profile_index == channel. Result:
  {channel:int -> {stream:int -> uri}}   (uri has NO credentials; caller injects)
Cached per gateway (stable per NVR; refresh on demand).
"""
import base64
import hashlib
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ONVIF_PORTS = (80, 8000, 8899, 8080)


def _wssec(user, pw):
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + pw.encode()).digest()).decode()
    return (f'<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f'<UsernameToken><Username>{user}</Username>'
            f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            f'</UsernameToken></Security></s:Header>')


def _soap(url, body, user, pw, timeout=8):
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'{_wssec(user, pw)}<s:Body>{body}</s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=env.encode(),
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def _media_url(host, user, pw, timeout):
    for port in ONVIF_PORTS:
        dev = f"http://{host}:{port}/onvif/device_service"
        try:
            xml = _soap(dev, '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl">'
                             '<IncludeCapability>false</IncludeCapability></GetServices>',
                        user, pw, timeout)
        except Exception:
            continue
        media = f"http://{host}:{port}/onvif/media"     # sensible default
        try:
            root = ET.fromstring(xml)
            for svc in root.iter():
                if svc.tag.endswith("Service"):
                    ns = xa = None
                    for ch in svc:
                        if ch.tag.endswith("Namespace"):
                            ns = ch.text
                        if ch.tag.endswith("XAddr"):
                            xa = ch.text
                    if ns and "ver10/media" in ns and xa:
                        pp = urlparse(xa)
                        media = f"http://{host}:{pp.port or port}{pp.path}"   # force real host
        except Exception:
            pass
        return media
    return None


def _profiles(media, user, pw, timeout):
    root = ET.fromstring(_soap(media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>',
                               user, pw, timeout))
    return [el.get("token") for el in root.iter()
            if el.tag.rsplit("}", 1)[-1] in ("Profiles", "Profile") and el.get("token")]


def _stream_uri(media, token, user, pw, timeout):
    body = ('<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
            '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
            '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
            f'<ProfileToken>{token}</ProfileToken></GetStreamUri>')
    root = ET.fromstring(_soap(media, body, user, pw, timeout))
    for el in root.iter():
        if el.tag.endswith("Uri") and el.text and el.text.startswith("rtsp"):
            return el.text
    return None


def resolve_map(host, user, pw, *, cache_path=None, refresh=False, timeout=8):
    """-> {channel:int -> {stream:int -> uri (no creds)}}. Cached per host."""
    if cache_path and not refresh:
        try:
            d = json.loads(Path(cache_path).read_text())
            if d.get("host") == host and d.get("map"):
                return {int(k): {int(s): u for s, u in v.items()} for k, v in d["map"].items()}
        except Exception:
            pass
    media = _media_url(host, user, pw, timeout)
    if not media:
        raise RuntimeError("no ONVIF device service reachable")
    m = {}
    for tok in _profiles(media, user, pw, timeout):
        try:
            uri = _stream_uri(media, tok, user, pw, timeout)
        except Exception:
            continue
        if not uri:
            continue
        parts = urlparse(uri).path.strip("/").split("/")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            m.setdefault(int(parts[0]), {})[int(parts[1])] = uri
    if not m:
        raise RuntimeError("ONVIF returned no channel/stream URIs")
    if cache_path:
        try:
            Path(cache_path).write_text(json.dumps(
                {"host": host, "ts": time.time(),
                 "map": {str(k): {str(s): u for s, u in v.items()} for k, v in m.items()}}))
        except Exception:
            pass
    return m


def uri_for(m, channel, prefer=2):
    """URI for a channel; prefer stream `prefer` (2=sub, good for thumbnails),
    falling back to stream 1 (main) then any."""
    streams = m.get(int(channel))
    if not streams:
        return None
    return streams.get(prefer) or streams.get(1) or next(iter(streams.values()))
