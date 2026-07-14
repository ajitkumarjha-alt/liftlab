#!/usr/bin/env python3
"""Load probe: can ONE Pi analyse all marked cabins' ONVIF LIVE streams
CONCURRENTLY at a target fps without throttling? Decode + YOLO11n + a light
tracker on N streams for LOAD_SECONDS, telemetry every 10s (SoC temp,
get_throttled decoded, loadavg, mem, per-stream fps/decoded/dropped/events/
last-ts + timestamp monotonicity). Footage never stored. Run with the B4 venv.

Params (env):  LOAD_CHANNELS=27,28,29,30,32,33,34  LOAD_FPS=6  LOAD_SECONDS=1800
Smoke first:   LOAD_SECONDS=300 LOAD_FPS=6 ...   then the full 1800.
"""
import os
import re
import statistics
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

import av
import cv2
import numpy as np
import onnxruntime as ort

ENV = "/etc/liftlab-agent.env"
B4 = "/home/askjitk/liftlab-b4"
MODEL = f"{B4}/yolo11n.onnx"


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
CHANNELS = [int(x) for x in os.environ.get("LOAD_CHANNELS", "27,28,29,30,32,33,34").split(",") if x.strip()]
FPS = float(os.environ.get("LOAD_FPS", "6"))
SECONDS = int(os.environ.get("LOAD_SECONDS", "1800"))


def onvif_url(ch):
    import onvif_resolve  # in pi-agent/, add it to path
    cmap = onvif_resolve.resolve_map(HOST, USER, PW, cache_path=f"/tmp/onvif_map_{GWID}.json")
    raw = onvif_resolve.uri_for(cmap, ch, prefer=1)   # MAIN stream (analysis quality)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


def throttled():
    try:
        raw = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=3).strip()
        val = int(raw.split("=")[1], 16)
    except Exception:
        return "n/a", 0, []
    flags = []
    for bit, name in ((0, "UNDERVOLT-NOW"), (1, "FREQCAP-NOW"), (2, "THROTTLED-NOW"), (3, "TEMPLIMIT-NOW"),
                      (16, "undervolt-occurred"), (17, "freqcap-occurred"), (18, "throttled-occurred"), (19, "templimit-occurred")):
        if val & (1 << bit):
            flags.append(name)
    return raw.split("=")[1], val, flags


def soc_temp():
    try:
        return float(subprocess.check_output(["vcgencmd", "measure_temp"], text=True, timeout=3).split("=")[1].split("'")[0])
    except Exception:
        return None


def mem_avail_mb():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return None


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0, x2 - x1), max(0, y2 - y1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class Tracker:
    def __init__(self):
        self.tracks = []
        self.total = 0
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


STATS = {ch: {"decoded": 0, "infers": 0, "dropped": 0, "backjumps": 0, "gaps": 0,
              "last_ts": None, "infer_ms": 0.0, "err": None, "tracker": Tracker()} for ch in CHANNELS}
STOP = threading.Event()
T0 = time.time()


def preprocess(img):
    x = cv2.cvtColor(cv2.resize(img, (640, 640)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(x, (2, 0, 1))[None]


def worker(ch, url):
    st = STATS[ch]
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])
        cont = av.open(url, options={"rtsp_transport": "tcp", "stimeout": "5000000"})
        s = next(x for x in cont.streams if x.type == "video")
        s.thread_type = "AUTO"
        tb = float(s.time_base) if s.time_base else None
        interval = 1.0 / FPS
        last_infer, last_pts = 0.0, None
        deltas = []
        for frame in cont.decode(s):
            if STOP.is_set():
                break
            st["decoded"] += 1
            if frame.pts is not None and tb:
                t = frame.pts * tb
                if last_pts is not None:
                    d = t - last_pts
                    if d < 0:
                        st["backjumps"] += 1
                    elif d > 0:
                        deltas.append(d)
                        if len(deltas) > 400:
                            deltas.pop(0)
                st["last_ts"] = round(t, 2)
                last_pts = t
            now = time.time()
            if now - last_infer >= interval:
                last_infer = now
                t0 = time.time()
                out = sess.run(None, {"images": preprocess(frame.to_ndarray(format="bgr24"))})[0]
                st["infer_ms"] = round((time.time() - t0) * 1000, 1)
                st["infers"] += 1
                conf = out[0, 4, :]
                idx = np.where(conf > 0.4)[0]
                boxes = [(float(out[0, 0, i] - out[0, 2, i] / 2), float(out[0, 1, i] - out[0, 3, i] / 2),
                          float(out[0, 0, i] + out[0, 2, i] / 2), float(out[0, 1, i] + out[0, 3, i] / 2)) for i in idx]
                st["tracker"].update(boxes)
            else:
                st["dropped"] += 1
            if deltas:
                med = statistics.median(deltas)
                st["gaps"] = sum(1 for d in deltas if med > 0 and d > 2 * med)
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


# resolve + launch
import sys
sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")   # for onvif_resolve
print(f"LOAD PROBE: {len(CHANNELS)} cabins {CHANNELS} @ {FPS} fps for {SECONDS}s (Pi 4)\n")
urls = {}
for ch in CHANNELS:
    u = onvif_url(ch)
    print(f"  ch{ch}: {'resolved' if u else 'NO ONVIF URI'}")
    if u:
        urls[ch] = u
threads = [threading.Thread(target=worker, args=(ch, u), daemon=True) for ch, u in urls.items()]
for t in threads:
    t.start()

peak_temp, ever_throttled, series = 0.0, [], []
print("\n  t(s) temp throttled loadavg  memMB | per-cabin fps ...")
while time.time() - T0 < SECONDS:
    time.sleep(10)
    el = time.time() - T0
    temp = soc_temp() or 0
    peak_temp = max(peak_temp, temp)
    thex, tval, tflags = throttled()
    if tflags:
        ever_throttled = sorted(set(ever_throttled) | set(tflags))
    la = os.getloadavg()
    mem = mem_avail_mb()
    perc = {ch: {"fps": round(st["infers"] / el, 2), "decoded": st["decoded"], "dropped": st["dropped"],
                 "events": st["tracker"].total, "backjumps": st["backjumps"], "gaps": st["gaps"],
                 "last_ts": st["last_ts"], "err": st["err"]} for ch, st in STATS.items()}
    fps_str = " ".join(f"{ch}:{perc[ch]['fps']}" + ("!" if perc[ch]["err"] else "") for ch in urls)
    flag_str = ("THROTTLE:" + ",".join(tflags)) if tflags else "clean"
    print(f"  {int(el):4d} {temp:4.1f} {flag_str:20} {la[0]:.1f}/{la[1]:.1f} {mem}MB | {fps_str}")
    row = {"t": round(el), "temp": temp, "throttled_hex": thex, "throttle_flags": tflags,
           "load1": la[0], "load5": la[1], "mem_mb": mem, "streams": perc}
    series.append(row)
    upload(row)

STOP.set()
time.sleep(2)

# ---- summary / verdict ----
print("\n================= SUMMARY =================")
print(f"  duration {SECONDS}s @ target {FPS} fps, {len(urls)} cabins")
print(f"  peak SoC temp: {peak_temp:.1f} C")
print(f"  throttling ever: {ever_throttled or 'NONE (clean)'}")
target_ok, bad = True, []
for ch, st in STATS.items():
    el = time.time() - T0
    fps = st["infers"] / max(1, el)
    ok = (st["err"] is None) and fps >= 0.9 * FPS and st["backjumps"] == 0
    if not ok:
        target_ok = False
        bad.append(ch)
    print(f"  ch{ch}: fps={fps:.2f} decoded={st['decoded']} dropped={st['dropped']} "
          f"events={st['tracker'].total} backjumps={st['backjumps']} gaps={st['gaps']} err={st['err']}")

print("\n================= VERDICT =================")
if target_ok and not ever_throttled:
    print(f"  GO: {len(urls)} concurrent @ {FPS} fps SUSTAINABLE, throttle-clean, timestamp-monotonic.")
else:
    reasons = []
    if ever_throttled:
        reasons.append(f"THROTTLING occurred ({','.join(ever_throttled)}) — timestamp integrity at risk when hot")
    if bad:
        reasons.append(f"cabins under target fps / errored / non-monotonic: {bad}")
    print("  NO-GO as-is: " + "; ".join(reasons))
    print("  -> reduce fps, or round-robin M-at-a-time, or add active cooling. Re-run to find the sustainable N/fps.")
print("\n  10s series also POSTed to /api/gw/<gw>/loadtest (plot at /loadtest/<gw> if the cloud endpoint is deployed).")
