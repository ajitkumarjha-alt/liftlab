#!/usr/bin/env python3
"""Historical/per-channel-playback probe for the Honeywell I-HNVR-1240.

Two routes, both proving ch1 != ch29 (channel selection) AND t1 != t2 (time
selection) before we wire analyze_local:

  A) ONVIF Replay (authoritative): GetServices -> GetRecordings -> GetReplayUri.
     Prints the returned URIs (this reveals the NVR's real historical grammar,
     like GetStreamUri revealed the live one) and grabs ch1/ch29 to diff.
  B) Path+starttime: the proven live path /<ch>/<stream> plus starttime params,
     a few format variants, hashing ch1-vs-ch29 and t1-vs-t2.

Run with the B4 venv (cv2/numpy + ffmpeg). Windows default to ~2h and ~6h ago
(recent, within retention, different scenes). Frames kept in /tmp/replay_probe/.
"""
import base64
import hashlib
import os
import re
import shlex
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import cv2
import numpy as np

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/replay_probe")
WORK.mkdir(exist_ok=True)
ONVIF_PORTS = (80, 8000, 8899, 8080)


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
HOST, RTSP_PORT = c.get("NVR_HOST", ""), c.get("NVR_PORT", "554")
CRED = f"{quote(USER, safe='')}:{quote(PW, safe='')}"

# two distinct past windows
NOW = datetime.now()
T1s, T1e = NOW - timedelta(hours=2), NOW - timedelta(hours=2) + timedelta(minutes=5)
T2s, T2e = NOW - timedelta(hours=6), NOW - timedelta(hours=6) + timedelta(minutes=5)


def mask(u):
    m = re.match(r"^(\w+://)([^/]*)(/.*)?$", u or "")
    if not m:
        return u
    a = m.group(2)
    if "@" in a:
        a = "***:***@" + a.rsplit("@", 1)[1]
    return m.group(1) + a + (m.group(3) or "")


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


def grab(url, out, ss=1.5):
    cmd = (f"ffmpeg -hide_banner -loglevel error -y -rtsp_transport tcp -i {shlex.quote(url)} "
           f"-ss {ss} -frames:v 1 -vf scale=480:-1 {shlex.quote(str(out))}")
    try:
        subprocess.run(cmd, shell=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 1000


def diffm(a, b):
    ia, ib = cv2.imread(str(a), 0), cv2.imread(str(b), 0)
    if ia is None or ib is None:
        return None
    h, w = min(ia.shape[0], ib.shape[0]), min(ia.shape[1], ib.shape[1])
    return float(np.abs(ia[:h, :w].astype(int) - ib[:h, :w].astype(int)).mean())


def services():
    for port in ONVIF_PORTS:
        dev = f"http://{HOST}:{port}/onvif/device_service"
        try:
            xml = soap(dev, '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl"><IncludeCapability>false</IncludeCapability></GetServices>')
        except Exception as e:
            print(f"  device_service :{port} -> {type(e).__name__}: {str(e)[:60]}")
            continue
        out = {}
        root = ET.fromstring(xml)
        for svc in root.iter():
            if svc.tag.endswith("Service"):
                ns = xa = None
                for ch in svc:
                    if ch.tag.endswith("Namespace"):
                        ns = ch.text
                    if ch.tag.endswith("XAddr"):
                        xa = ch.text
                if ns and xa:
                    key = ns.rsplit("/", 1)[-1]
                    out[key] = f"http://{HOST}:{port}{urlparse(xa).path}"
        return out
    return {}


# ---------------- ROUTE A: ONVIF Replay ----------------
print(f"replay probe: NVR {HOST}\n--- ONVIF services ---")
svc = services()
for k, v in svc.items():
    print(f"  {k}: {v}")
rec_url = svc.get("wsdl") or svc.get("recording") or next((v for k, v in svc.items() if "recording" in k.lower()), None)
rep_url = next((v for k, v in svc.items() if "replay" in k.lower()), None)
print(f"\n  recording svc: {rec_url}\n  replay svc   : {rep_url}")

recs = []
if rec_url:
    try:
        root = ET.fromstring(soap(rec_url, '<GetRecordings xmlns="http://www.onvif.org/ver10/recording/wsdl"/>'))
        for item in root.iter():
            if item.tag.endswith("RecordingItem"):
                tok = None
                name = None
                for el in item.iter():
                    if el.tag.endswith("RecordingToken"):
                        tok = el.text
                    if el.tag.endswith("Name") and name is None:
                        name = el.text
                if tok:
                    recs.append((tok, name))
        print(f"\n--- GetRecordings: {len(recs)} recording(s) ---")
        for i, (t, n) in enumerate(recs[:6]):
            print(f"  [{i}] token={t} name={n}")
        if len(recs) > 6:
            print(f"  ... ({len(recs)} total)")
    except Exception as e:
        print(f"  GetRecordings failed: {type(e).__name__}: {str(e)[:100]}")

if rep_url and len(recs) >= 2:
    def replay_uri(token):
        body = ('<GetReplayUri xmlns="http://www.onvif.org/ver10/replay/wsdl">'
                '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
                f'<RecordingToken>{token}</RecordingToken></GetReplayUri>')
        root = ET.fromstring(soap(rep_url, body))
        for el in root.iter():
            if el.tag.endswith("Uri") and el.text and el.text.startswith("rtsp"):
                return el.text
        return None

    i1, i29 = 0, min(28, len(recs) - 1)
    print("\n--- GetReplayUri (reveals historical grammar) ---")
    for tag, idx in (("rec1", i1), ("rec29", i29)):
        try:
            u = replay_uri(recs[idx][0])
        except Exception as e:
            print(f"  {tag}: GetReplayUri failed: {str(e)[:80]}"); u = None
        print(f"  {tag} (token={recs[idx][0]}): {mask(u)}")
        if u:
            withc = u if urlparse(u).username else u.replace("rtsp://", f"rtsp://{CRED}@", 1)
            print(f"    grab: {grab(withc, WORK / f'replay_{tag}.jpg')}")
    d = diffm(WORK / "replay_rec1.jpg", WORK / "replay_rec29.jpg") \
        if (WORK / "replay_rec1.jpg").exists() and (WORK / "replay_rec29.jpg").exists() else None
    print(f"  replay rec1-vs-rec29 diff = {d if d is None else round(d, 2)} -> "
          f"{'DIFFERENT cameras' if (d and d > 8) else 'same/na'}")
else:
    print("\n  Replay route unavailable (no replay service or <2 recordings).")


# ---------------- ROUTE B: path + starttime variants ----------------
def base_path(ch):
    return f"rtsp://{CRED}@{HOST}:{RTSP_PORT}/{ch}/1"


def fmt_dahua(dt):
    return dt.strftime("%Y_%m_%d_%H_%M_%S")


def fmt_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


VARIANTS = [
    ("path + starttime (Y_M_D_H_M_S)",
     lambda ch, s, e: base_path(ch) + f"?transmode=unicast&profile=vam&starttime={fmt_dahua(s)}&endtime={fmt_dahua(e)}"),
    ("path + starttime (ISO Z)",
     lambda ch, s, e: base_path(ch) + f"?transmode=unicast&profile=vam&starttime={fmt_iso(s)}&endtime={fmt_iso(e)}"),
]
print("\n--- ROUTE B: path + starttime variants ---")
for label, build in VARIANTS:
    u1_t1, u29_t1, u1_t2 = build(1, T1s, T1e), build(29, T1s, T1e), build(1, T2s, T2e)
    o1, o29, o1b = WORK / "b_ch1_t1.jpg", WORK / "b_ch29_t1.jpg", WORK / "b_ch1_t2.jpg"
    g1, g29, g1b = grab(u1_t1, o1), grab(u29_t1, o29), grab(u1_t2, o1b)
    ch_d = diffm(o1, o29) if (g1 and g29) else None
    tm_d = diffm(o1, o1b) if (g1 and g1b) else None
    print(f"\n=== {label} ===")
    print(f"  ch1@T1 : got={g1}  {mask(u1_t1)}")
    print(f"  ch29@T1: got={g29}")
    print(f"  ch1@T2 : got={g1b}")
    print(f"  channel diff (ch1 vs ch29) = {ch_d if ch_d is None else round(ch_d,2)}")
    print(f"  time    diff (T1 vs T2)    = {tm_d if tm_d is None else round(tm_d,2)}")
    win = (ch_d and ch_d > 8 and tm_d and tm_d > 8)
    print(f"  -> {'HISTORICAL PER-CHANNEL WORKS (both diffs high)' if win else 'not proven (need both diffs high)'}")

print(f"\nWindows: T1={T1s:%Y-%m-%d %H:%M} T2={T2s:%Y-%m-%d %H:%M}. "
      "Eyeball OSD clocks in /tmp/replay_probe/ to confirm the frames are from the PAST windows (not live).")
