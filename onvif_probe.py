#!/usr/bin/env python3
"""ONVIF probe for the Honeywell I-HNVR-1240 (Dahua-OEM). Raw SOAP (no onvif-zeep):
GetProfiles -> count is decisive; GetStreamUri per profile -> authoritative
per-channel RTSP URIs; hash-diff ch1 vs ch29 to PROVE they differ before wiring
anything. Run with the B4 venv (cv2/numpy + ffmpeg).

Decision on GetProfiles count:
  ~40 profiles -> per-channel streams exist; use their real URIs.
  <=1 profile  -> NVR serves ONE stream only; per-channel RTSP is off. That is an
                  NVR-config/site issue, NOT a code issue.
"""
import base64
import hashlib
import os
import re
import shlex
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import cv2
import numpy as np

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/onvif_probe")
WORK.mkdir(exist_ok=True)


def load_env(p):
    cfg = {}
    for line in Path(p).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


c = load_env(ENV)
USER, PW = c.get("NVR_USER", ""), c.get("NVR_PASS", "")
HOST = c.get("NVR_HOST", "")
CRED = f"{quote(USER, safe='')}:{quote(PW, safe='')}"


def _wssec():
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + PW.encode()).digest()).decode()
    return (f'<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f'<UsernameToken><Username>{USER}</Username>'
            f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            f'</UsernameToken></Security></s:Header>')


def soap(url, body, timeout=8):
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'{_wssec()}<s:Body>{body}</s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=env.encode(),
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace")


def find_device_service():
    for port in (80, 8000, 8899, 8080, 8080):
        url = f"http://{HOST}:{port}/onvif/device_service"
        try:
            xml = soap(url, '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl"><IncludeCapability>false</IncludeCapability></GetServices>')
            print(f"  device_service :{port} -> responded ({len(xml)} bytes)")
            return url, xml
        except Exception as e:
            print(f"  device_service :{port} -> {type(e).__name__}: {str(e)[:70]}")
    return None, None


def media_xaddr(services_xml, device_url):
    try:
        root = ET.fromstring(services_xml)
        for svc in root.iter():
            if svc.tag.endswith("Service"):
                ns = xaddr = None
                for ch in svc:
                    if ch.tag.endswith("Namespace"):
                        ns = ch.text
                    if ch.tag.endswith("XAddr"):
                        xaddr = ch.text
                if ns and "ver10/media" in ns and xaddr:
                    return xaddr
    except Exception:
        pass
    p = urlparse(device_url)
    return f"http://{p.hostname}:{p.port}/onvif/media_service"


def _real_host(url):
    # ONVIF XAddr can carry an internal/NAT ip; force the NVR's real host + path
    p = urlparse(url)
    return f"http://{HOST}:{p.port or 80}{p.path or '/onvif/media_service'}"


def get_profiles(media_url):
    xml = soap(media_url, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>')
    root = ET.fromstring(xml)
    profs = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] in ("Profiles", "Profile") and el.get("token"):
            name = None
            for ch in el:
                if ch.tag.endswith("Name"):
                    name = ch.text
            profs.append((el.get("token"), name))
    return profs


def get_stream_uri(media_url, token):
    body = ('<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
            '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
            '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
            f'<ProfileToken>{token}</ProfileToken></GetStreamUri>')
    root = ET.fromstring(soap(media_url, body))
    for el in root.iter():
        if el.tag.endswith("Uri") and el.text and el.text.startswith("rtsp"):
            return el.text
    return None


def inject_creds(uri):
    return uri if urlparse(uri).username else uri.replace("rtsp://", f"rtsp://{CRED}@", 1)


def mask(u):
    m = re.match(r"^(\w+://)([^/]*)(/.*)?$", u or "")
    if not m:
        return u
    a = m.group(2)
    if "@" in a:
        a = "***:***@" + a.rsplit("@", 1)[1]
    return m.group(1) + a + (m.group(3) or "")


def grab(url, out):
    cmd = (f"ffmpeg -hide_banner -loglevel error -y -rtsp_transport tcp -i {shlex.quote(url)} "
           f"-ss 1.0 -frames:v 1 -vf scale=480:-1 {shlex.quote(str(out))}")
    try:
        subprocess.run(cmd, shell=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 1000


def diffm(a, b):
    ia, ib = cv2.imread(str(a), 0), cv2.imread(str(b), 0)
    if ia is None or ib is None:
        return None
    h, w = min(ia.shape[0], ib.shape[0]), min(ia.shape[1], ib.shape[1])
    return float(np.abs(ia[:h, :w].astype(int) - ib[:h, :w].astype(int)).mean())


print(f"ONVIF probe: NVR {HOST}\n--- discover device service ---")
dev_url, services = find_device_service()
if not dev_url:
    print("\nNO ONVIF device service on :80/:8000/:8899/:8080 — ONVIF may be disabled on this NVR.")
    sys.exit(1)

media = _real_host(media_xaddr(services, dev_url))
print(f"media service : {media}")
profs = None
for target in (media, dev_url):
    try:
        profs = get_profiles(target)
        media = target
        break
    except Exception as e:
        print(f"  GetProfiles @ {target} -> {type(e).__name__}: {str(e)[:80]}")
if profs is None:
    print("GetProfiles failed on both media and device endpoints — see errors above.")
    sys.exit(1)

print(f"\n=== GetProfiles: {len(profs)} profile(s) ===")
for i, (tok, name) in enumerate(profs[:60]):
    print(f"  [{i}] token={tok}  name={name}")

if len(profs) <= 1:
    print("\nDECISION: <=1 media profile -> the NVR exposes only ONE RTSP stream.")
    print("Per-channel RTSP is disabled/unavailable on this NVR — an NVR CONFIG/SITE")
    print("issue, NOT a code issue. Next: enable multi-channel RTSP/ONVIF on the NVR")
    print("(or connect per-camera directly). No survey code change will fix a 1-stream NVR.")
    sys.exit(0)

i1, i29 = 0, min(28, len(profs) - 1)
uris = {}
for tag, idx in (("ch1", i1), ("ch29", i29)):
    tok, name = profs[idx]
    try:
        uri = get_stream_uri(media, tok)
    except Exception as e:
        print(f"\n{tag}: GetStreamUri FAILED: {type(e).__name__}: {str(e)[:80]}")
        uri = None
    uris[tag] = uri
    print(f"\n{tag}: profile[{idx}] token={tok} name={name}")
    print(f"   ONVIF URI: {mask(uri) if uri else None}")
    if uri:
        print(f"   grab: {grab(inject_creds(uri), WORK / f'{tag}.jpg')}")

d = (diffm(WORK / "ch1.jpg", WORK / "ch29.jpg")
     if (WORK / "ch1.jpg").exists() and (WORK / "ch29.jpg").exists() else None)
print(f"\n=== ch1 vs ch29 mean-abs-diff = {d if d is None else round(d, 2)} -> "
      f"{'DIFFERENT cameras — ONVIF WORKS' if (d and d > 8) else 'same camera' if d is not None else 'n/a'}")
print("frames + URIs in /tmp/onvif_probe/")
