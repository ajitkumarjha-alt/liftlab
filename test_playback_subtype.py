#!/usr/bin/env python3
"""Decisive playback-subtype test. Pulls ch1 vs ch29 across no-subtype / subtype=0
/ subtype=1 using the field-proven grammar (creds URL-encoded), and prints a
frame-difference metric so 'same camera' vs 'different cameras' is numeric.

Run with the B4 venv (needs cv2/numpy + ffmpeg):
    /home/askjitk/liftlab-b4/.venv/bin/python /tmp/test_playback_subtype.py
"""
import re
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/subtype_test")
WORK.mkdir(exist_ok=True)


def load_env(path):
    cfg = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


c = load_env(ENV)
USER, PW = c.get("NVR_USER", ""), c.get("NVR_PASS", "")
HOST, PORT = c.get("NVR_HOST", ""), c.get("NVR_PORT", "554")


def mask(u):
    m = re.match(r"^(\w+://)([^/]*)(/.*)?$", u or "")
    if not m:
        return u
    a = m.group(2)
    if "@" in a:
        a = "***:***@" + a.rsplit("@", 1)[1]
    return m.group(1) + a + (m.group(3) or "")


def build(channel, subtype):
    s = datetime.now() - timedelta(seconds=180)
    e = s + timedelta(seconds=3)
    st, et = s.strftime("%Y_%m_%d_%H_%M_%S"), e.strftime("%Y_%m_%d_%H_%M_%S")
    sub = "" if subtype is None else f"&subtype={subtype}"
    return (f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@{HOST}:{PORT}"
            f"/cam/playback?channel={channel}{sub}&starttime={st}&endtime={et}")


def grab(url, out):
    cmd = (f"ffmpeg -hide_banner -loglevel error -y -rtsp_transport tcp "
           f"-i {shlex.quote(url)} -ss 1.5 -frames:v 1 -vf scale=480:-1 {shlex.quote(str(out))}")
    try:
        subprocess.run(cmd, shell=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 1000


def diffmetric(a, b):
    ia = cv2.imread(str(a), cv2.IMREAD_GRAYSCALE)
    ib = cv2.imread(str(b), cv2.IMREAD_GRAYSCALE)
    if ia is None or ib is None:
        return None
    h, w = min(ia.shape[0], ib.shape[0]), min(ia.shape[1], ib.shape[1])
    return float(np.abs(ia[:h, :w].astype(int) - ib[:h, :w].astype(int)).mean())


print(f"NVR {HOST}:{PORT}  (creds url-encoded, masked below)\n")
for label, subtype in [("no-subtype (nvr.py proven)", None), ("subtype=0", 0), ("subtype=1 (survey)", 1)]:
    u1, u29 = build(1, subtype), build(29, subtype)
    o1, o29 = WORK / f"s{subtype}_ch01.jpg", WORK / f"s{subtype}_ch29.jpg"
    g1, g29 = grab(u1, o1), grab(u29, o29)
    d = diffmetric(o1, o29) if (g1 and g29) else None
    verdict = ("n/a" if d is None else
               "DIFFERENT cameras  <-- USE THIS" if d > 8 else
               "SAME camera (default fallback)")
    print(f"=== {label} ===")
    print(f"  ch1  : got={g1}  {mask(u1)}")
    print(f"  ch29 : got={g29}  {mask(u29)}")
    print(f"  ch1-vs-ch29 mean-abs-diff = {d if d is None else round(d,2)}  -> {verdict}\n")
print("Frames kept in /tmp/subtype_test/ for eyeballing.")
