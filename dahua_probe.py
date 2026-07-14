#!/usr/bin/env python3
"""Dahua HTTP-CGI historical probe for the Honeywell I-HNVR-1240.

RTSP ignores starttime here and there's no ONVIF Replay, but the NVR indexes 40
per-channel recordings. The Dahua-native retrieval is the HTTP CGI:
  mediaFileFind.cgi  -> authoritative per-channel files with REAL StartTime/EndTime
  RPC_Loadfile       -> download the file (Digest auth)

Proof bar: for a requested past window, ch1 and ch29 must return DIFFERENT files
whose API StartTime lands IN the window (time proven by the NVR's own index, not
OSD). We then download+grab a frame and OSD-crop-and-print the in-frame clock as
human confirmation. Run with the B4 venv (cv2/numpy + ffmpeg).
"""
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import cv2

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/dahua_probe")
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
USER, PW, HOST = c.get("NVR_USER", ""), c.get("NVR_PASS", ""), c.get("NVR_HOST", "")

# two windows, today; adjust if outside retention
DAY = datetime.now().strftime("%Y-%m-%d")
T1s, T1e = f"{DAY} 08:00:00", f"{DAY} 08:30:00"
T2s, T2e = f"{DAY} 02:00:00", f"{DAY} 02:30:00"


def opener():
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pm.add_password(None, f"http://{HOST}", USER, PW)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))


OP = opener()


def cgi(path, timeout=15, maxbytes=None):
    r = OP.open(f"http://{HOST}/cgi-bin/{path}", timeout=timeout)
    return r.read(maxbytes) if maxbytes else r.read()


def media_find(ch, s, e):
    obj = cgi("mediaFileFind.cgi?action=factory.create").decode(errors="replace").strip().split("=")[-1]
    cond = (f"mediaFileFind.cgi?action=findFile&object={obj}"
            f"&condition.Channel={ch}&condition.StartTime={quote(s)}&condition.EndTime={quote(e)}"
            f"&condition.Types[0]=dav")
    cgi(cond)
    resp = cgi(f"mediaFileFind.cgi?action=findNextFile&object={obj}&count=100").decode(errors="replace")
    files = []
    for line in resp.splitlines():
        m = re.match(r"items\[(\d+)\]\.(\w+)=(.*)", line)
        if m:
            i, k, v = int(m.group(1)), m.group(2), m.group(3)
            while len(files) <= i:
                files.append({})
            files[i][k] = v
    for act in ("close", "destroy"):
        try:
            cgi(f"mediaFileFind.cgi?action={act}&object={obj}")
        except Exception:
            pass
    return [f for f in files if f.get("FilePath")]


def load_grab(filepath, out, maxbytes=8_000_000):
    dav = WORK / (out.stem + ".dav")
    try:
        data = cgi(f"RPC_Loadfile{filepath}", timeout=40, maxbytes=maxbytes)
        dav.write_bytes(data)
    except Exception as e:
        return False, f"loadfile: {type(e).__name__}: {str(e)[:60]}"
    cmd = f"ffmpeg -hide_banner -loglevel error -y -i {dav} -frames:v 1 -vf scale=640:-1 {out}"
    subprocess.run(cmd, shell=True, timeout=30)
    return out.exists() and out.stat().st_size > 1000, f"{len(data)} bytes"


def diffm(a, b):
    import numpy as np
    ia, ib = cv2.imread(str(a), 0), cv2.imread(str(b), 0)
    if ia is None or ib is None:
        return None
    h, w = min(ia.shape[0], ib.shape[0]), min(ia.shape[1], ib.shape[1])
    return float(np.abs(ia[:h, :w].astype(int) - ib[:h, :w].astype(int)).mean())


def osd_ascii(jpg, rows_frac=0.09, cols=150):
    img = cv2.imread(str(jpg), 0)
    if img is None:
        print("    (no frame to OSD-render)")
        return
    h, w = img.shape
    strip = img[:max(1, int(h * rows_frac)), :]
    ramp = " .:-=+*#%@"
    sh, sw = strip.shape
    for ry in range(7):
        y = min(sh - 1, int((ry + 0.5) / 7 * sh))
        print("    " + "".join(ramp[min(9, int(strip[y, min(sw - 1, int((cx + 0.5) / cols * sw))]) * 10 // 256)] for cx in range(cols)))


print(f"Dahua HTTP-CGI probe: NVR {HOST}  (Digest auth)\n")
grabbed = {}
for tag, ch, s, e in (("ch1@T1", 1, T1s, T1e), ("ch29@T1", 29, T1s, T1e), ("ch1@T2", 1, T2s, T2e)):
    print(f"=== {tag}  (requested {s} .. {e}) ===")
    try:
        files = media_find(ch, s, e)
    except Exception as ex:
        print(f"  mediaFileFind FAILED: {type(ex).__name__}: {str(ex)[:90]}\n")
        continue
    print(f"  mediaFileFind: {len(files)} file(s)")
    for f in files[:3]:
        print(f"    ch={f.get('Channel')} start={f.get('StartTime')} end={f.get('EndTime')} path={f.get('FilePath')}")
    if not files:
        print("  (no files in window — outside retention? adjust T1/T2)\n")
        continue
    out = WORK / f"{tag.replace('@','_')}.jpg"
    ok, info = load_grab(files[0]["FilePath"], out)
    print(f"  download+grab: {ok} ({info})")
    if ok:
        grabbed[tag] = out
        print("  OSD strip (top of frame — read the burned-in clock):")
        osd_ascii(out)
    print()

ch_d = diffm(grabbed["ch1@T1"], grabbed["ch29@T1"]) if {"ch1@T1", "ch29@T1"} <= grabbed.keys() else None
tm_d = diffm(grabbed["ch1@T1"], grabbed["ch1@T2"]) if {"ch1@T1", "ch1@T2"} <= grabbed.keys() else None
print("=== VERDICT ===")
print(f"  channel diff (ch1 vs ch29 @T1) = {ch_d if ch_d is None else round(ch_d,2)}")
print(f"  time    diff (ch1 @T1 vs @T2)  = {tm_d if tm_d is None else round(tm_d,2)}")
print("  NOTE: the AUTHORITATIVE time proof is each file's API StartTime above landing")
print("        in the requested window + a per-channel FilePath (/1/ vs /29/).")
print("  frames + .dav in /tmp/dahua_probe/")
