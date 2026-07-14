#!/usr/bin/env python3
"""Dahua HTTP historical probe (Honeywell I-HNVR-1240, Dahua-OEM), ROBUST edition.
Every mechanism is wrapped — one failure never aborts the others. Capability-first:
we ask magicBox whether the Dahua HTTP API exists AT ALL before hunting endpoints.

  CAP   GET /cgi-bin/magicBox.cgi?action=getSystemInfo (Digest)  -> API present?
  PORTS status of candidate endpoints (find the layer that's actually there)
  M1    CGI  mediaFileFind.cgi (Digest)                          -> historical finder
  M2    RPC2 JSON login (/RPC2_Login then /RPC2) + mediaFileFind -> historical finder
  M3    CGI  loadfile.cgi?action=startLoad&channel&startTime&endTime
Then, if a finder works: two-window (ch27 @ 08:00 vs 18:00) OSD proof via the
validation viewer. Run with the B4 venv.
"""
import hashlib
import json
import re
import subprocess
import urllib.error
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
CH = 27


def digest_opener():
    pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pm.add_password(None, f"http://{HOST}", USER, PW)
    return urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(pm))


OP = digest_opener()


def hget(path, timeout=10, maxbytes=None):
    """Digest GET -> (status:int|None, body:bytes). Never raises."""
    try:
        r = OP.open(f"http://{HOST}/{path}", timeout=timeout)
        return getattr(r, "status", 200), (r.read(maxbytes) if maxbytes else r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as e:
        return None, str(e).encode()


def hpost(url, obj, timeout=12):
    """Plain JSON POST -> (status:int|None, body:bytes). Never raises. (RPC2 uses
    in-band session auth, not HTTP digest.)"""
    try:
        req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=timeout)
        return getattr(r, "status", 200), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception as e:
        return None, str(e).encode()


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
            return f"uploaded ({getattr(r,'status',200)})"
    except Exception as e:
        return f"upload failed: {str(e)[:60]}"


# ================= CAPABILITY =================
print(f"Dahua HTTP probe: NVR {HOST}\n")
print("=== CAPABILITY: is the Dahua HTTP API present? ===")
api_present = False
for path in ("cgi-bin/magicBox.cgi?action=getSystemInfo",
             "cgi-bin/magicBox.cgi?action=getDeviceType",
             "cgi-bin/global.cgi?action=getCurrentTime"):
    st, body = hget(path)
    txt = body.decode(errors="replace").replace("\r", " ").replace("\n", " ")[:110]
    print(f"  GET /{path.split('?')[0]:32} action={path.split('action=')[-1]:16} -> {st}  {txt.strip()}")
    if st == 200:
        api_present = True
print(f"  => Dahua CGI API present: {api_present}")

print("\n=== ENDPOINT PRESENCE (GET status; POST-only paths show 405 if they EXIST) ===")
for path in ("cgi-bin/mediaFileFind.cgi?action=factory.create",
             "cgi-bin/loadfile.cgi", "RPC2_Login", "RPC2", "RPC_Loadfile"):
    st, _ = hget(path, timeout=8)
    print(f"  /{path.split('?')[0]:34} -> {st}")


# ================= M1: CGI mediaFileFind =================
def cgi_find(ch, s, e):
    st, body = hget("cgi-bin/mediaFileFind.cgi?action=factory.create")
    if st != 200:
        return None, f"factory.create -> {st}"
    obj = body.decode(errors="replace").strip().split("=")[-1]
    hget(f"cgi-bin/mediaFileFind.cgi?action=findFile&object={obj}&condition.Channel={ch}"
         f"&condition.StartTime={quote(s)}&condition.EndTime={quote(e)}&condition.Types[0]=dav")
    st, body = hget(f"cgi-bin/mediaFileFind.cgi?action=findNextFile&object={obj}&count=100")
    files = []
    for line in body.decode(errors="replace").splitlines():
        m = re.match(r"items\[(\d+)\]\.(\w+)=(.*)", line)
        if m:
            i, k, v = int(m.group(1)), m.group(2), m.group(3)
            while len(files) <= i:
                files.append({})
            files[i][k] = v
    for a in ("close", "destroy"):
        hget(f"cgi-bin/mediaFileFind.cgi?action={a}&object={obj}")
    return [f for f in files if f.get("FilePath")], f"{st}"


print("\n=== M1: CGI mediaFileFind ===")
cgi_ok_ch = None
for ch in (CH, CH - 1):
    try:
        files, note = cgi_find(ch, WINDOWS[0][1], WINDOWS[0][2])
        if files is None:
            print(f"  Channel={ch}: FAIL ({note})")
        else:
            print(f"  Channel={ch}: {len(files)} file(s) [{note}]" + (f"  e.g. {files[0].get('FilePath')} @ {files[0].get('StartTime')}" if files else ""))
            if files and cgi_ok_ch is None:
                cgi_ok_ch = ch
    except Exception as ex:
        print(f"  Channel={ch}: EXC {type(ex).__name__}: {str(ex)[:70]}")


# ================= M2: RPC2 =================
def rpc2_login():
    for base in (f"http://{HOST}/RPC2_Login", f"http://{HOST}/RPC2"):
        st, body = hpost(base, {"method": "global.login",
                                "params": {"userName": USER, "password": "", "clientType": "Web3.0", "loginType": "Direct"},
                                "id": 1})
        if st != 200:
            print(f"  login step1 @ {base.rsplit('/',1)[-1]} -> {st} ({body.decode(errors='replace')[:60]})")
            continue
        try:
            r1 = json.loads(body)
        except Exception:
            print(f"  login step1 @ {base.rsplit('/',1)[-1]} -> non-JSON"); continue
        p = r1.get("params", {})
        realm, random, session = p.get("realm"), p.get("random"), r1.get("session")
        if not realm:
            print(f"  login step1 -> no realm ({str(r1)[:80]})"); continue
        a1 = hashlib.md5(f"{USER}:{realm}:{PW}".encode()).hexdigest().upper()
        pwh = hashlib.md5(f"{USER}:{random}:{a1}".encode()).hexdigest().upper()
        st2, body2 = hpost(base, {"method": "global.login", "session": session, "id": 2,
                                  "params": {"userName": USER, "password": pwh, "clientType": "Web3.0",
                                             "loginType": "Direct", "authorityType": "Default", "passwordType": "Default"}})
        try:
            r2 = json.loads(body2)
        except Exception:
            r2 = {}
        if r2.get("result"):
            return session, base
        print(f"  login step2 @ {base.rsplit('/',1)[-1]} -> {st2} ({str(r2)[:80]})")
    return None, None


print("\n=== M2: RPC2 JSON ===")
rpc_ok_ch = None
sess = rbase = None
try:
    sess, _ = rpc2_login()
    print(f"  login: {'ok' if sess else 'UNAVAILABLE'}")
    if sess:
        for ch in (CH, CH - 1):
            st, body = hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.factory.create", "session": sess, "id": 3})
            try:
                fac = json.loads(body).get("result")
            except Exception:
                fac = None
            if not fac:
                print(f"  Channel={ch}: factory.create -> {st}"); continue
            hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.findFile", "object": fac, "session": sess, "id": 4,
                                         "params": {"condition": {"Channel": ch, "StartTime": WINDOWS[0][1], "EndTime": WINDOWS[0][2], "Types": ["dav"]}}})
            st, body = hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.findNextFile", "object": fac, "session": sess, "id": 5, "params": {"count": 100}})
            try:
                infos = json.loads(body).get("params", {}).get("infos", []) or []
            except Exception:
                infos = []
            print(f"  Channel={ch}: {len(infos)} file(s)" + (f"  e.g. {infos[0].get('FilePath')} @ {infos[0].get('StartTime')}" if infos else ""))
            if infos and rpc_ok_ch is None:
                rpc_ok_ch = ch
except Exception as ex:
    print(f"  M2 EXC {type(ex).__name__}: {str(ex)[:80]}")


# ================= M3: loadfile by time =================
print("\n=== M3: loadfile.cgi by time ===")
try:
    st, body = hget(f"cgi-bin/loadfile.cgi?action=startLoad&channel={CH}"
                    f"&startTime={quote(WINDOWS[0][1])}&endTime={quote(WINDOWS[0][2])}&subtype=0",
                    timeout=45, maxbytes=8_000_000)
    if st == 200 and len(body) > 10000:
        dav = WORK / "m3.dav"; dav.write_bytes(body)
        f = WORK / "m3.jpg"
        ok = grab(dav, f)
        print(f"  ch{CH}: {len(body)} bytes, frame={ok}")
        if ok:
            print("  OSD:"); osd(f)
    else:
        print(f"  ch{CH}: -> {st} ({len(body)} bytes) — not a media stream")
except Exception as ex:
    print(f"  M3 EXC {type(ex).__name__}: {str(ex)[:80]}")


# ================= TWO-WINDOW PROOF =================
print("\n=== TWO-WINDOW PROOF (ch27 @ 08:00 vs 18:00) ===")
finder = None
if cgi_ok_ch is not None:
    finder = ("M1 CGI", cgi_ok_ch, lambda ch, s, e: cgi_find(ch, s, e)[0])
    dl = lambda fp, out: out.write_bytes(hget(f"cgi-bin/RPC_Loadfile{fp}", timeout=45, maxbytes=8_000_000)[1])
elif rpc_ok_ch is not None and sess:
    def _rpc_find(ch, s, e):
        st, body = hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.factory.create", "session": sess, "id": 7})
        fac = json.loads(body).get("result")
        hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.findFile", "object": fac, "session": sess, "id": 8,
                                     "params": {"condition": {"Channel": ch, "StartTime": s, "EndTime": e, "Types": ["dav"]}}})
        st, body = hpost(f"http://{HOST}/RPC2", {"method": "mediaFileFind.findNextFile", "object": fac, "session": sess, "id": 9, "params": {"count": 100}})
        return json.loads(body).get("params", {}).get("infos", []) or []
    finder = ("M2 RPC2", rpc_ok_ch, _rpc_find)
    dl = lambda fp, out: out.write_bytes(hget(f"cgi-bin/RPC_Loadfile{fp}", timeout=45, maxbytes=8_000_000)[1])

if finder:
    name, idx, find = finder
    print(f"  using {name} (Channel index {idx})")
    for tag, s, e in WINDOWS:
        try:
            files = find(idx, s, e)
            print(f"  {tag}: {len(files)} file(s)")
            if not files:
                print(f"    -> no footage at {s} (outside retention?)"); continue
            fp = files[0].get("FilePath")
            print(f"    file: {fp}  start={files[0].get('StartTime')}")
            dav, jpg = WORK / f"w{tag}.dav", WORK / f"w{tag}.jpg"
            dl(fp, dav)
            if grab(dav, jpg):
                print("    OSD:"); osd(jpg)
                print(f"    validation: {validate_upload(CH, f'20260712_{tag}_dahua', jpg)}")
        except Exception as ex:
            print(f"  {tag}: EXC {type(ex).__name__}: {str(ex)[:70]}")
else:
    print("  NO finder mechanism worked.")
    print("  If CAPABILITY above shows the Dahua CGI API ABSENT (all 404), this OEM stripped")
    print("  the HTTP API -> historical is an NVR-export/SDK/FM finding, not a code fix.")
    print("  If the API is present but no finder path matched, we hunt the OEM's finder next.")
