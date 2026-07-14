#!/usr/bin/env python3
"""Replicate pull_playback's EXACT URL for ch27 @ 2026-07-12 08:00-08:05 and read
the burned-in OSD clock — settles historical-vs-live empirically.

pull_playback builds (agent playback_url):
   rtsp://user:pass@host:554/cam/playback?channel=N&subtype=1&starttime=..&endtime=..
which grammar_probe (channel ignored) + replay_probe (starttime ignored) already
falsified. If the OSD reads 2026-07-12 08:0x -> historical WORKS. If it reads the
CURRENT date/time -> starttime ignored, pull was live. Run with the B4 venv.
"""
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import cv2

ENV = "/etc/liftlab-agent.env"
WORK = Path("/tmp/osd_check")
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
PORT = c.get("NVR_PORT", "554")
CRED = f"{quote(USER, safe='')}:{quote(PW, safe='')}"

start = datetime(2026, 7, 12, 8, 0, 0)
end = datetime(2026, 7, 12, 8, 5, 0)
st, et = start.strftime("%Y_%m_%d_%H_%M_%S"), end.strftime("%Y_%m_%d_%H_%M_%S")
URL = f"rtsp://{CRED}@{HOST}:{PORT}/cam/playback?channel=27&subtype=1&starttime={st}&endtime={et}"


def mask(u):
    import re
    return re.sub(r"://[^/]*@", "://***:***@", u)


def grab(url, out, ss):
    cmd = (f"ffmpeg -hide_banner -loglevel error -y -rtsp_transport tcp -i {shlex.quote(url)} "
           f"-ss {ss} -frames:v 1 {shlex.quote(str(out))}")
    try:
        subprocess.run(cmd, shell=True, timeout=40)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 1000


def osd(jpg, band, cols=170, rows=12):
    img = cv2.imread(str(jpg), 0)
    if img is None:
        print("    (no frame)")
        return
    h, w = img.shape
    y0, y1 = (0, int(h * 0.11)) if band == "top" else (int(h * 0.89), h)
    strip = img[y0:y1, :]
    sh, sw = strip.shape
    ramp = " .:-=+*#%@"
    for ry in range(rows):
        yy = min(sh - 1, int((ry + 0.5) / rows * sh))
        print("    " + "".join(ramp[min(9, int(strip[yy, min(sw - 1, int((cx + 0.5) / cols * sw))]) * 10 // 256)] for cx in range(cols)))


print("pull_playback grammar, ch27, requested 2026-07-12 08:00:00 .. 08:05:00")
print(f"URL: {mask(URL)}")
print(f"NOW: {datetime.now():%Y-%m-%d %H:%M:%S}\n")

for ss, tag in ((1.5, "frame @ start (+1.5s)"), (75, "frame @ +75s")):
    out = WORK / f"osd_{int(ss)}.jpg"
    ok = grab(URL, out, ss)
    print(f"=== {tag}: got={ok} ===")
    if ok:
        print("  OSD top strip (read the burned-in date/time):")
        osd(out, "top")
        print("  OSD bottom strip (some Dahua burn the clock here):")
        osd(out, "bottom")
    print()

print("READ: if the burned-in clock shows 2026-07-12 08:0x  -> HISTORICAL works.")
print(f"      if it shows {datetime.now():%Y-%m-%d} (today/now) -> starttime ignored, this was LIVE.")
print("frames in /tmp/osd_check/ (scp/view for a clearer read than ASCII).")
