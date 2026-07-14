#!/usr/bin/env python3
"""Dahua HTTP-CGI historical probe — the likely real way to get footage by
channel+time on this OEM (RTSP historical is dead here: channel and starttime
both ignored). Flow:
  mediaFileFind.cgi  -> AUTHORITATIVE per-channel files with real StartTime/EndTime
  RPC_Loadfile       -> download the file (Digest auth) -> grab a frame

Proof bar: for the REQUESTED channel + past window, mediaFileFind returns a file
under /<ch>/ whose StartTime is IN the window; we grab a frame and UPLOAD it to
the validation viewer so you read the burned-in OSD clock on the dashboard
(side-by-side with the broken pull_playback frame). Run with the B4 venv.
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
CLOUD = c.get("CLOUD_URL", "").rstrip("/")
GWID = c.get("GATEWAY_ID", "site-A")
TOKEN = c.get("GATEWAY_TOKEN", "")

_today = datetime.now().strftime("%Y-%m-%d")
TARGETS = [
    ("ch27 @ target (2026-07-12 08:00)", 27, "2026-07-12 08:00:00", "2026-07-12 08:05:00", True),
    ("ch1  @ target (channel contrast)", 1, "2026-07-12 08:00:00", "2026-07-12 08:05:00", False),
    ("ch27 @ today (retention fallback)", 27, f"{_today} 06:00:00", f"{_today} 06:05:00", True),
]


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
    cgi(f"mediaFileFind.cgi?action=findFile&object={obj}&condition.Channel={ch}"
        f"&condition.StartTime={quote(s)}&condition.EndTime={quote(e)}&condition.Types[0]=dav")
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
        dav.write_bytes(cgi(f"RPC_Loadfile{filepath}", timeout=45, maxbytes=maxbytes))
    except Exception as e:
        return False, f"loadfile: {type(e).__name__}: {str(e)[:70]}"
    subprocess.run(f"ffmpeg -hide_banner -loglevel error -y -i {dav} -frames:v 1 -vf scale=720:-1 {out}",
                   shell=True, timeout=30)
    return out.exists() and out.stat().st_size > 1000, f"{dav.stat().st_size} bytes"


def osd_ascii(jpg, cols=150, rows=7):
    img = cv2.imread(str(jpg), 0)
    if img is None:
        print("    (no frame)"); return
    h, w = img.shape
    strip = img[:max(1, int(h * 0.10)), :]
    sh, sw = strip.shape
    ramp = " .:-=+*#%@"
    for ry in range(rows):
        yy = min(sh - 1, int((ry + 0.5) / rows * sh))
        print("    " + "".join(ramp[min(9, int(strip[yy, min(sw - 1, int((cx + 0.5) / cols * sw))]) * 10 // 256)] for cx in range(cols)))


def upload_validation(ch, reqstart, jpg):
    if not (CLOUD and TOKEN):
        return "no CLOUD_URL/GATEWAY_TOKEN in env"
    url = f"{CLOUD}/api/gw/{GWID}/validation/{ch}?requested_start={quote(reqstart)}&mode=dahua-cgi"
    req = urllib.request.Request(url, data=Path(jpg).read_bytes(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "image/jpeg"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"uploaded ({r.status}) -> /validation/{GWID}"
    except Exception as e:
        return f"upload failed: {str(e)[:70]}"


print(f"Dahua HTTP-CGI probe: NVR {HOST}  (Digest auth)\n")
for label, ch, s, e, do_upload in TARGETS:
    print(f"=== {label}  (requested {s} .. {e}) ===")
    try:
        files = media_find(ch, s, e)
    except Exception as ex:
        print(f"  mediaFileFind FAILED: {type(ex).__name__}: {str(ex)[:100]}\n"); continue
    print(f"  mediaFileFind: {len(files)} file(s)  [AUTHORITATIVE channel+time from the NVR index]")
    for f in files[:3]:
        print(f"    ch={f.get('Channel')} start={f.get('StartTime')} end={f.get('EndTime')} path={f.get('FilePath')}")
    if not files:
        print("  -> no files (outside retention, or CGI channel indexing differs)\n"); continue
    out = WORK / (re.sub(r"[^0-9A-Za-z]", "_", label)[:24] + ".jpg")
    ok, info = load_grab(files[0]["FilePath"], out)
    print(f"  download+grab: {ok} ({info})")
    if ok:
        print("  OSD strip (top of frame — read the burned-in clock):")
        osd_ascii(out)
        if do_upload:
            reqtag = re.sub(r"[^0-9A-Za-z]", "", s)[:14] + "dahua"
            print(f"  validation upload: {upload_validation(ch, reqtag, out)}")
    print()

print("READ: a file under /<ch>/ (e.g. /27/) with StartTime in the window = channel+time")
print("      proven by the NVR's own index. Then confirm on the dashboard: the ch27 dahua-cgi")
print("      validation tile's OSD must read the PAST window (2026-07-12 08:0x), not now.")
