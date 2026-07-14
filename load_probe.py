#!/usr/bin/env python3
"""Load probe (single- or multi-cabin) with LIVE-vs-STICKY throttle decoding and
DECODE-vs-ANALYZE fps so we can tell a stalled STREAM from slow ANALYSIS.

get_throttled: live bits 0-3 (undervolt/freqcap/throttled/templimit NOW) are the
real-time integrity guard; sticky bits 16-19 are "since boot" (set by any earlier
heavy run, clear only on reboot) — informational, NOT a verdict input.

Per cabin we log decode_fps (frames received from the stream) AND analyze_fps
(frames run through YOLO) + last-frame age: decode≈0 => stream starved; decode ok
but analyze low => compute-bound.

Params (env): LOAD_CHANNELS=29  LOAD_FPS=6  LOAD_SECONDS=1800  LOAD_STREAM=1  (1=main,2=sub)
"""
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import av
import cv2
import numpy as np
import onnxruntime as ort

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")
import onvif_resolve  # noqa: E402

ENV = "/etc/liftlab-agent.env"
MODEL = "/home/askjitk/liftlab-b4/yolo11n.onnx"


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
CLOUD, GWID, TOKEN = c.get("CLOUD_URL", "").rstrip("/"), c.get("GATEWAY_ID", "site-A"), c.get("GATEWAY_TOKEN", "")
CHANNELS = [int(x) for x in os.environ.get("LOAD_CHANNELS", "29").split(",") if x.strip()]
FPS = float(os.environ.get("LOAD_FPS", "6"))
SECONDS = int(os.environ.get("LOAD_SECONDS", "1800"))
STREAM = int(os.environ.get("LOAD_STREAM", "1"))   # 1=main, 2=sub


def onvif_url(ch):
    cm = onvif_resolve.resolve_map(HOST, USER, PW, cache_path=f"/tmp/onvif_map_{GWID}.json")
    raw = onvif_resolve.uri_for(cm, ch, prefer=STREAM)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


def throttled():
    """-> (hex, live_flags, sticky_flags). Live = currently happening (integrity
    guard); sticky = has-happened-since-boot (informational)."""
    try:
        raw = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=3).strip().split("=")[1]
        val = int(raw, 16)
    except Exception:
        return "n/a", [], []
    live = {0: "undervolt-NOW", 1: "freqcap-NOW", 2: "throttled-NOW", 3: "templimit-NOW"}
    stky = {16: "undervolt-since-boot", 17: "freqcap-since-boot", 18: "throttled-since-boot", 19: "templimit-since-boot"}
    return raw, [n for b, n in live.items() if val & (1 << b)], [n for b, n in stky.items() if val & (1 << b)]


def soc_temp():
    try:
        return float(subprocess.check_output(["vcgencmd", "measure_temp"], text=True, timeout=3).split("=")[1].split("'")[0])
    except Exception:
        return 0.0


def mem_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None


def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class Tracker:
    def __init__(self):
        self.tracks, self.total = [], 0
    def update(self, dets):
        matched = set()
        for tr in self.tracks:
            best, bi = 0.3, -1
            for i, d in enumerate(dets):
                if i in matched:
                    continue
                io = iou(tr["box"], d)
                if io > best:
                    best, bi = io, i
            if bi >= 0:
                tr["box"], tr["miss"] = dets[bi], 0
                matched.add(bi)
            else:
                tr["miss"] += 1
        for i, d in enumerate(dets):
            if i not in matched:
                self.tracks.append({"box": d, "miss": 0})
                self.total += 1
        self.tracks = [t for t in self.tracks if t["miss"] < 10]


def preprocess(img):
    x = cv2.cvtColor(cv2.resize(img, (640, 640)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(x, (2, 0, 1))[None]


STATS = {ch: {"decoded": 0, "infers": 0, "backjumps": 0, "last_decode": None, "err": None, "tracker": Tracker()} for ch in CHANNELS}
STOP = threading.Event()
T0 = time.time()


def worker(ch, url):
    st = STATS[ch]
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = so.inter_op_num_threads = 1
        sess = ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])
        cont = av.open(url, options={"rtsp_transport": "tcp", "stimeout": "5000000"})
        s = next(x for x in cont.streams if x.type == "video")
        s.thread_type = "AUTO"
        tb = float(s.time_base) if s.time_base else None
        interval, last_infer, last_pts = 1.0 / FPS, 0.0, None
        for frame in cont.decode(s):
            if STOP.is_set():
                break
            st["decoded"] += 1
            st["last_decode"] = time.time()
            if frame.pts is not None and tb:
                t = frame.pts * tb
                if last_pts is not None and t < last_pts:
                    st["backjumps"] += 1
                last_pts = t
            now = time.time()
            if now - last_infer >= interval:
                last_infer = now
                out = sess.run(None, {"images": preprocess(frame.to_ndarray(format="bgr24"))})[0]
                st["infers"] += 1
                idx = np.where(out[0, 4, :] > 0.4)[0]
                boxes = [(float(out[0, 0, i] - out[0, 2, i] / 2), float(out[0, 1, i] - out[0, 3, i] / 2),
                          float(out[0, 0, i] + out[0, 2, i] / 2), float(out[0, 1, i] + out[0, 3, i] / 2)) for i in idx]
                st["tracker"].update(boxes)
        cont.close()
    except Exception as e:
        st["err"] = f"{type(e).__name__}: {str(e)[:90]}"


def upload(row):
    if not (CLOUD and TOKEN):
        return
    try:
        import json
        req = urllib.request.Request(f"{CLOUD}/api/gw/{GWID}/loadtest", data=json.dumps(row).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


print(f"LOAD PROBE: cabins {CHANNELS} @ {FPS} fps for {SECONDS}s, stream={'main' if STREAM==1 else 'sub'}\n")
urls = {}
for ch in CHANNELS:
    u = onvif_url(ch)
    print(f"  ch{ch}: {'resolved '+u.rsplit('@',1)[-1] if u else 'NO ONVIF URI'}")
    if u:
        urls[ch] = u
for ch, u in urls.items():
    threading.Thread(target=worker, args=(ch, u), daemon=True).start()

peak, ever_live, sticky_seen = 0.0, set(), set()
print("\n  t(s) temp throttle                     mem | per-cabin decode/analyze fps")
while time.time() - T0 < SECONDS:
    time.sleep(10)
    el = time.time() - T0
    temp = soc_temp()
    peak = max(peak, temp)
    thex, live_f, sticky_f = throttled()
    ever_live |= set(live_f)
    sticky_seen |= set(sticky_f)
    m = mem_mb()
    now = time.time()
    perc = {ch: {"dfps": round(st["decoded"] / el, 2), "afps": round(st["infers"] / el, 2),
                 "decoded": st["decoded"], "infers": st["infers"], "events": st["tracker"].total,
                 "age": round(now - st["last_decode"], 1) if st["last_decode"] else None,
                 "backjumps": st["backjumps"], "err": st["err"]} for ch, st in STATS.items()}
    fstr = " ".join(f"{ch}:d{perc[ch]['dfps']}/a{perc[ch]['afps']}" + ("!" if perc[ch]["err"] else "") for ch in urls)
    thr = ("LIVE:" + ",".join(live_f)) if live_f else ("clean" + (" +sticky" if sticky_f else ""))
    print(f"  {int(el):4d} {temp:4.1f} {thr:30} {m}MB | {fstr}")
    upload({"t": round(el), "temp": temp, "throttled_hex": thex, "throttle_flags": live_f, "sticky_flags": sticky_f,
            "load1": os.getloadavg()[0], "mem_mb": m, "streams": perc})

STOP.set()
time.sleep(2)
el = max(1, time.time() - T0)
print("\n================= SUMMARY =================")
print(f"  peak SoC temp: {peak:.1f} C")
print(f"  LIVE throttling during run: {sorted(ever_live) or 'NONE (clean)'}   <- integrity verdict input")
print(f"  sticky (since boot, informational): {sorted(sticky_seen) or 'none'}")
target_ok, bad = True, []
for ch, st in STATS.items():
    dfps, afps = st["decoded"] / el, st["infers"] / el
    age = (time.time() - st["last_decode"]) if st["last_decode"] else None
    stalled = age is not None and age > 30
    if afps >= 4:
        diag = "ok"
    elif dfps < 2:
        diag = "STREAM STARVED (decode also ~0 -> stream stalled/trickling, NOT the Pi)"
    else:
        diag = "compute-bound (decode ok, YOLO too slow)"
    ok = (st["err"] is None) and afps >= 4 and st["backjumps"] == 0 and not stalled and not ever_live
    if not ok:
        target_ok = False
        bad.append(ch)
    print(f"  ch{ch}: decode_fps={dfps:.2f} analyze_fps={afps:.2f} last_frame_age={None if age is None else round(age,1)}s "
          f"decoded={st['decoded']} infers={st['infers']} events={st['tracker'].total} backjumps={st['backjumps']} err={st['err']}")
    print(f"        -> {diag}" + ("  [STALLED]" if stalled else ""))

print("\n================= VERDICT =================")
if target_ok:
    print(f"  GO: {CHANNELS} @ {FPS} fps sustained (analyze>=4fps), LIVE-throttle-clean, monotonic.")
else:
    if ever_live:
        print(f"  NO-GO: LIVE throttling occurred ({sorted(ever_live)}) — timestamp integrity at risk.")
    starved = [ch for ch in bad if STATS[ch]["decoded"] / el < 2]
    if starved:
        print(f"  STREAM problem (not thermal): cabins {starved} starved — decode fps ~0 while Pi cool/idle.")
        print("  Investigate the ONVIF stream: try LOAD_STREAM=2 (sub), check RTSP-over-TCP stalls / NVR main-stream bitrate.")
    slow = [ch for ch in bad if ch not in starved and STATS[ch]["infers"] / el < 4]
    if slow:
        print(f"  COMPUTE-bound: cabins {slow} decode fine but YOLO < 4 fps — lower fps or lighter model.")
print("  Plot: /pihealth/<gw> (live throttle only) and /loadtest/<gw>.")
