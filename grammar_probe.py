#!/usr/bin/env python3
"""Channel-selection grammar probe for the Honeywell I-HNVR-1240 (Dahua-OEM).
For each candidate grammar, grab a frame for display-ch1 and display-ch29 and
print the ch1-vs-ch29 mean-abs-diff. WINNER = whichever makes them DIFFER (>8).
Priority per field guidance: LIVE realmonitor first (a commissioning thumbnail
only needs a current frame), then zero-indexed, then path/other.
Run with the B4 venv (cv2/numpy + ffmpeg)."""
import re
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/grammar_probe")
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
HOST, PORT = c.get("NVR_HOST", ""), c.get("NVR_PORT", "554")
CRED = f"{quote(USER, safe='')}:{quote(PW, safe='')}"


def mask(u):
    m = re.match(r"^(\w+://)([^/]*)(/.*)?$", u or "")
    if not m:
        return u
    a = m.group(2)
    if "@" in a:
        a = "***:***@" + a.rsplit("@", 1)[1]
    return m.group(1) + a + (m.group(3) or "")


def _win():
    s = datetime.now() - timedelta(seconds=180)
    e = s + timedelta(seconds=3)
    return s.strftime("%Y_%m_%d_%H_%M_%S"), e.strftime("%Y_%m_%d_%H_%M_%S")


def realmon(disp, subtype, zero=False):
    ch = disp - 1 if zero else disp
    return f"rtsp://{CRED}@{HOST}:{PORT}/cam/realmonitor?channel={ch}&subtype={subtype}"


def playbk(disp, subtype, zero=False):
    ch = disp - 1 if zero else disp
    st, et = _win()
    sub = "" if subtype is None else f"&subtype={subtype}"
    return f"rtsp://{CRED}@{HOST}:{PORT}/cam/playback?channel={ch}{sub}&starttime={st}&endtime={et}"


def path_pb(disp):   # Dahua alt: channel as a path segment
    st, et = _win()
    return f"rtsp://{CRED}@{HOST}:{PORT}/cam/playback/{disp}?starttime={st}&endtime={et}"


GRAMMARS = [
    ("LIVE realmonitor ch=N   subtype=0", lambda d: realmon(d, 0)),
    ("LIVE realmonitor ch=N   subtype=1", lambda d: realmon(d, 1)),
    ("LIVE realmonitor ch=N-1 subtype=0 (0-idx)", lambda d: realmon(d, 0, zero=True)),
    ("PLAYBACK ch=N-1 subtype=0 (0-idx)", lambda d: playbk(d, 0, zero=True)),
    ("PLAYBACK ch=N-1 no-subtype (0-idx)", lambda d: playbk(d, None, zero=True)),
    ("PLAYBACK channel in path /cam/playback/N", path_pb),
]


def grab(url, out):
    cmd = (f"ffmpeg -hide_banner -loglevel error -y -rtsp_transport tcp -i {shlex.quote(url)} "
           f"-ss 1.5 -frames:v 1 -vf scale=480:-1 {shlex.quote(str(out))}")
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


print(f"NVR {HOST}:{PORT}\n")
winner = None
for i, (label, b) in enumerate(GRAMMARS):
    u1, u29 = b(1), b(29)
    o1, o29 = WORK / f"g{i}_ch01.jpg", WORK / f"g{i}_ch29.jpg"
    g1, g29 = grab(u1, o1), grab(u29, o29)
    d = diffm(o1, o29) if (g1 and g29) else None
    verdict = ("n/a (grab failed)" if d is None
               else "DIFFERENT cameras  <== WINNER" if d > 8 else "same camera")
    if d is not None and d > 8 and winner is None:
        winner = label
    print(f"=== {label} ===")
    print(f"  ch1  got={g1}: {mask(u1)}")
    print(f"  ch29 got={g29}: {mask(u29)}")
    print(f"  diff={d if d is None else round(d, 2)} -> {verdict}\n")

print("WINNER:", winner or "NONE — RTSP grammars exhausted; next step is ONVIF GetStreamUri (authoritative per-channel URLs)")
print("frames in /tmp/grammar_probe/")
