#!/usr/bin/env python3
"""Dahua HTTP historical probe (Honeywell I-HNVR-1240, Dahua-OEM). RTSP historical
is dead here (channel + starttime both ignored). Try the Dahua HTTP API,
authoritative-first, for ch27 over 2026-07-12 08:00-08:05 (and 18:00 for a
time-selection contrast):

  M1  CGI   GET /cgi-bin/mediaFileFind.cgi   (Digest)
  M2  RPC2  POST /RPC2  (JSON-RPC, global.login session + mediaFileFind.*)
  M3  CGI   GET /cgi-bin/loadfile.cgi?action=startLoad&channel&startTime&endTime

Channel indexing tested BOTH N and N-1 (Dahua RPC is often 0-indexed). For the
first mechanism that yields a file, we download one frame and UPLOAD it to the
validation viewer (ch27, mode=dahua-cgi) for OSD-clock verification on the
dashboard. PROOF: right cabin (not 42B-REFUGE) at the requested PAST window, and
the 08:00 vs 18:00 OSD clocks must DIFFER. Run with the B4 venv.
"""
import hashlib
import json
import re
import subprocess
import urllib.request
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

WINDOWS = [("0800", "2026-07-12 08:00:00", "2026-07-12 08:05:00"),
           ("1800", "2026-07-12 18:00:00", "2026-07-12 18:05:00")]
DISP_CH = 27


def digest_opener():
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pm.add_password(None, f"http://{HOST}", USER, PW)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))


OP = digest_opener()


def cgi(path, timeout=15, maxbytes=None):
    r = OP.open(f"http://{HOST}/cgi-bin/{path}", timeout=timeout)
    return r.read(maxbytes) if maxbytes else r.read()


def grab(dav, out):
    subprocess.run(f"ffmpeg -hide_banner -loglevel error -y -i {dav} -frames:v 1 -vf scale=720:-1 {out}",
                   shell=True, timeout=30)
    return out.exists() and out.stat().st_size > 1000


def osd(jpg, cols=150, rows=7):
    img = cv2.imread(str(jpg), 0)
    if img is None:
        print("      (no frame)"); return
    h, w = img.shape
    strip = img[:max(1, int(h * 0.10)), :]
    sh, sw = strip.shape
    ramp = " .:-=+*#%@"
    for ry in range(rows):
        yy = min(sh - 1, int((ry + 0.5) / rows * sh))
        print("      " + "".join(ramp[min(9, int(strip[yy, min(sw - 1, int((cx + 0.5) / cols * sw))]) * 10 // 256)] for cx in range(cols)))


def validate_upload(ch, tag, jpg):
    if not (CLOUD and TOKEN):
        return "no CLOUD_URL/GATEWAY_TOKEN"
    url = f"{CLOUD}/api/gw/{GWID}/validation/{ch}?requested_start={quote(tag)}&mode=dahua-cgi"
    req = urllib.request.Request(url, data=Path(jpg).read_bytes(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "image/jpeg"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return f"uploaded ({r.status})"
    except Exception as e:
        return f"upload failed: {str(e)[:60]}"


# ---------------- M1: CGI mediaFileFind ----------------
def cgi_find(ch, s, e):
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
    for a in ("close", "destroy"):
        try:
            cgi(f"mediaFileFind.cgi?action={a}&object={obj}")
        except Exception:
            pass
    return [f for f in files if f.get("FilePath")]


def cgi_download(filepath, out_dav, maxbytes=8_000_000):
    out_dav.write_bytes(cgi(f"RPC_Loadfile{filepath}", timeout=45, maxbytes=maxbytes))


# ---------------- M2: RPC2 JSON login + mediaFileFind ----------------
def rpc(url, body, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode(errors="replace"))


def rpc2_login():
    base = f"http://{HOST}/RPC2_Login"
    r1 = rpc(base, {"method": "global.login",
                    "params": {"userName": USER, "password": "", "clientType": "Web3.0", "loginType": "Direct"},
                    "id": 1})
    p = r1.get("params", {})
    realm, random, session = p.get("realm"), p.get("random"), r1.get("session")
    if not realm:
        return None, f"no realm (step1={str(r1)[:120]})"
    a1 = hashlib.md5(f"{USER}:{realm}:{PW}".encode()).hexdigest().upper()
    pwh = hashlib.md5(f"{USER}:{random}:{a1}".encode()).hexdigest().upper()
    r2 = rpc(base, {"method": "global.login", "session": session, "id": 2,
                    "params": {"userName": USER, "password": pwh, "clientType": "Web3.0",
                               "loginType": "Direct", "authorityType": "Default", "passwordType": "Default"}})
    if r2.get("result"):
        return session, "ok"
    return None, f"login failed (step2={str(r2)[:120]})"


def rpc2_find(session, ch, s, e):
    rurl = f"http://{HOST}/RPC2"
    fac = rpc(rurl, {"method": "mediaFileFind.factory.create", "session": session, "id": 3})["result"]
    rpc(rurl, {"method": "mediaFileFind.findFile", "object": fac, "session": session, "id": 4,
               "params": {"condition": {"Channel": ch, "StartTime": s, "EndTime": e, "Types": ["dav"]}}})
    nf = rpc(rurl, {"method": "mediaFileFind.findNextFile", "object": fac, "session": session, "id": 5,
                    "params": {"count": 100}})
    try:
        rpc(rurl, {"method": "mediaFileFind.destroy", "object": fac, "session": session, "id": 6})
    except Exception:
        pass
    return nf.get("params", {}).get("infos", []) or []


# ---------------- M3: loadfile.cgi by time ----------------
def loadfile_time(ch, s, e, out_dav, maxbytes=8_000_000):
    r = OP.open(f"http://{HOST}/cgi-bin/loadfile.cgi?action=startLoad&channel={ch}"
                f"&startTime={quote(s)}&endTime={quote(e)}&subtype=0", timeout=45)
    out_dav.write_bytes(r.read(maxbytes))


print(f"Dahua HTTP probe: NVR {HOST}\n")
s0, e0 = WINDOWS[0][1], WINDOWS[0][2]

# ---- M1 CGI find, ch27 and ch26 ----
print("=== M1: CGI mediaFileFind /cgi-bin/mediaFileFind.cgi (Digest) ===")
cgi_ok = None
for ch in (DISP_CH, DISP_CH - 1):
    try:
        files = cgi_find(ch, s0, e0)
        print(f"  Channel={ch}: {len(files)} file(s)" + (f"  e.g. {files[0].get('FilePath')} @ {files[0].get('StartTime')}" if files else ""))
        if files and cgi_ok is None:
            cgi_ok = ch
    except Exception as ex:
        print(f"  Channel={ch}: FAILED {type(ex).__name__}: {str(ex)[:70]}")

# ---- M2 RPC2 ----
print("\n=== M2: RPC2 JSON (POST /RPC2, global.login) ===")
rpc_ok = None
sess, note = rpc2_login()
print(f"  login: {note}")
if sess:
    for ch in (DISP_CH, DISP_CH - 1):
        try:
            infos = rpc2_find(sess, ch, s0, e0)
            print(f"  Channel={ch}: {len(infos)} file(s)" + (f"  e.g. {infos[0].get('FilePath')} @ {infos[0].get('StartTime')}" if infos else ""))
            if infos and rpc_ok is None:
                rpc_ok = ch
        except Exception as ex:
            print(f"  Channel={ch}: FAILED {type(ex).__name__}: {str(ex)[:70]}")

# ---- M3 loadfile by time (quick grab) ----
print("\n=== M3: loadfile.cgi by time (Digest) ===")
m3_ok = False
try:
    dav = WORK / "m3.dav"
    loadfile_time(DISP_CH, s0, e0, dav)
    f = WORK / "m3.jpg"
    m3_ok = grab(dav, f)
    print(f"  ch{DISP_CH}: {dav.stat().st_size} bytes, frame={m3_ok}")
    if m3_ok:
        print("  OSD:"); osd(f)
except Exception as ex:
    print(f"  FAILED {type(ex).__name__}: {str(ex)[:80]}")

# ---- pick a finder+downloader and do the two-window OSD proof ----
print("\n=== TWO-WINDOW PROOF (ch27 @ 08:00 vs 18:00) ===")
finder = downloader = None
if cgi_ok is not None:
    finder = lambda ch, s, e: cgi_find(cgi_ok, s, e)
    downloader = cgi_download
    print(f"  using M1 CGI mediaFileFind (Channel index {cgi_ok})")
elif rpc_ok is not None and sess:
    finder = lambda ch, s, e: rpc2_find(sess, rpc_ok, s, e)
    downloader = lambda fp, out, maxbytes=8_000_000: cgi_download(fp, out, maxbytes)
    print(f"  using M2 RPC2 (Channel index {rpc_ok})")

if finder:
    for tag, s, e in WINDOWS:
        try:
            files = finder(DISP_CH, s, e)
        except Exception as ex:
            print(f"  {tag}: find FAILED {str(ex)[:70]}"); continue
        print(f"  {tag}: {len(files)} file(s)")
        if not files:
            print(f"    -> no footage at {s} (outside retention?)"); continue
        fp = files[0].get("FilePath")
        print(f"    file: {fp} start={files[0].get('StartTime')}")
        dav = WORK / f"w{tag}.dav"
        jpg = WORK / f"w{tag}.jpg"
        try:
            downloader(fp, dav)
        except Exception as ex:
            print(f"    download FAILED {str(ex)[:70]}"); continue
        if grab(dav, jpg):
            print("    OSD (read the burned-in clock):"); osd(jpg)
            print(f"    validation: {validate_upload(DISP_CH, f'20260712_{tag}_dahua', jpg)}")
elif m3_ok:
    print("  (M1/M2 found nothing, but M3 loadfile grabbed a frame — check its OSD above)")
else:
    print("  NONE of CGI/RPC2/loadfile returned historical footage for ch27.")
    print("  => This NVR does not serve historical by channel+time over the network.")
    print("     Retrieval is an NVR-export/SDK/FM question, not a code fix.")

print("\nCheck /validation/site-A: the two dahua-cgi ch27 tiles must show a CABIN with OSD")
print("clocks 2026-07-12 08:0x and 18:0x (different) — that proves channel AND time.")
