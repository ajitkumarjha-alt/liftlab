#!/usr/bin/env python3
"""Find the concurrent-cabin CEILING on this Pi. Steps configs as 5-min smokes,
stops at the first FAIL (with a 4fps fallback), prints a table + the RECOMMENDED
continuous config = max cabins at >=4 fps that never throttles.

Sequence: A(2@6) -> B(3@6); if B fails at 6fps -> C(3@4). (7 already proved the
top is well below 4 cabins.) PASS requires ALL: per-cabin fps>=4, throttle CLEAN
the whole run, timestamp-monotonic (0 backjumps), no worker error/OOM.
Run with the B4 venv.  Override cabins: LOAD_CHANNELS=27,28,29,...
"""
import json
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

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")   # onvif_resolve
import onvif_resolve  # noqa: E402

ENV = "/etc/liftlab-agent.env"
MODEL = "/home/askjitk/liftlab-b4/yolo11n.onnx"
SMOKE = int(os.environ.get("LOAD_SECONDS", "300"))
FPS_FLOOR = 4.0


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
ALLCH = [int(x) for x in os.environ.get("LOAD_CHANNELS", "27,28,29,30,32,33,34").split(",") if x.strip()]

CACHE = {}


def onvif_url(ch):
    if not CACHE:
        CACHE.update(onvif_resolve.resolve_map(HOST, USER, PW, cache_path=f"/tmp/onvif_map_{GWID}.json"))
    raw = onvif_resolve.uri_for(CACHE, ch, prefer=1)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


def throttled():
    try:
        raw = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=3).strip().split("=")[1]
        val = int(raw, 16)
    except Exception:
        return "n/a", []
    names = {0: "UNDERVOLT", 1: "FREQCAP", 2: "THROTTLED", 3: "TEMPLIMIT",
             16: "undervolt-occurred", 17: "freqcap-occurred", 18: "throttled-occurred", 19: "templimit-occurred"}
    return raw, [n for b, n in names.items() if val & (1 << b)]


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


def worker(url, st, stop, fps):
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = so.inter_op_num_threads = 1
        sess = ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])
        cont = av.open(url, options={"rtsp_transport": "tcp", "stimeout": "5000000"})
        s = next(x for x in cont.streams if x.type == "video")
        s.thread_type = "AUTO"
        tb = float(s.time_base) if s.time_base else None
        interval, last_infer, last_pts, deltas = 1.0 / fps, 0.0, None, []
        for frame in cont.decode(s):
            if stop.is_set():
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
                        deltas[:] = deltas[-400:]
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
            else:
                st["dropped"] += 1
            if deltas:
                med = statistics.median(deltas)
                st["gaps"] = sum(1 for d in deltas if med > 0 and d > 2 * med)
        cont.close()
    except Exception as e:
        st["err"] = f"{type(e).__name__}: {str(e)[:80]}"


G0 = time.time()


def upload(row):
    if not (CLOUD and TOKEN):
        return
    try:
        req = urllib.request.Request(f"{CLOUD}/api/gw/{GWID}/loadtest", data=json.dumps(row).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def run_config(label, chans, fps):
    print(f"\n=== {label} === ({SMOKE}s smoke)")
    urls = {ch: onvif_url(ch) for ch in chans}
    stats = {ch: {"decoded": 0, "infers": 0, "dropped": 0, "backjumps": 0, "gaps": 0, "err": None, "tracker": Tracker()} for ch in chans}
    stop = threading.Event()
    threads = []
    for ch in chans:
        if not urls[ch]:
            stats[ch]["err"] = "no ONVIF URI"
            continue
        t = threading.Thread(target=worker, args=(urls[ch], stats[ch], stop, fps), daemon=True)
        t.start()
        threads.append(t)
    peak, flags, t0 = 0.0, set(), time.time()
    while time.time() - t0 < SMOKE:
        time.sleep(10)
        el = time.time() - t0
        temp = soc_temp()
        peak = max(peak, temp)
        thex, tf = throttled()
        flags |= set(tf)
        m = mem_mb()
        fstr = " ".join(f"{ch}:{round(stats[ch]['infers']/el,2)}" + ("!" if stats[ch]["err"] else "") for ch in chans)
        print(f"   {int(el):4d}s {temp:4.1f}C {'THR:'+','.join(tf) if tf else 'clean':22} L{os.getloadavg()[0]:.1f} {m}MB | {fstr}")
        upload({"t": round(time.time() - G0), "temp": temp, "throttle_flags": tf, "load1": os.getloadavg()[0], "mem_mb": m,
                "streams": {ch: {"fps": round(stats[ch]['infers']/el, 2), "decoded": stats[ch]['decoded'],
                                 "dropped": stats[ch]['dropped'], "events": stats[ch]['tracker'].total,
                                 "backjumps": stats[ch]['backjumps'], "err": stats[ch]['err'], "config": label} for ch in chans}})
    stop.set()
    for t in threads:
        t.join(timeout=8)
    el = max(1, time.time() - t0)
    fpss = {ch: stats[ch]["infers"] / el for ch in chans}
    minfps = min(fpss.values()) if fpss else 0
    backj = max((stats[ch]["backjumps"] for ch in chans), default=0)
    errs = [ch for ch in chans if stats[ch]["err"]]
    ok = (minfps >= FPS_FLOOR) and (not flags) and (backj == 0) and (not errs)
    res = {"label": label, "cabins": len(chans), "fps_target": fps, "min_fps": round(minfps, 2),
           "peak_temp": round(peak, 1), "throttled": bool(flags), "flags": sorted(flags), "backjumps": backj,
           "errs": errs, "pass": ok}
    print(f"   -> min_fps={res['min_fps']} peak={res['peak_temp']}C throttle={'YES '+','.join(res['flags']) if flags else 'clean'} backjumps={backj} errs={errs} => {'PASS' if ok else 'FAIL'}")
    time.sleep(5)   # let streams/threads fully release before next config
    return res


results = []
results.append(run_config("A: 2 cabins @ 6fps", ALLCH[:2], 6))
if results[-1]["pass"]:
    results.append(run_config("B: 3 cabins @ 6fps", ALLCH[:3], 6))
    if not results[-1]["pass"] and not results[-1]["throttled"]:
        pass  # failed for non-throttle reason; still try lower fps
    if not results[-1]["pass"]:
        results.append(run_config("C: 3 cabins @ 4fps (fallback)", ALLCH[:3], 4))

print("\n================= SWEEP TABLE =================")
print(f"  {'config':30} {'min fps/cabin':13} {'peak C':7} {'throttle':10} {'verdict'}")
for r in results:
    print(f"  {r['label']:30} {r['min_fps']:<13} {r['peak_temp']:<7} {'YES' if r['throttled'] else 'clean':10} {'PASS' if r['pass'] else 'FAIL'}")

passing = [r for r in results if r["pass"]]
print("\n================= RECOMMENDATION =================")
if passing:
    best = max(passing, key=lambda r: r["cabins"] * r["fps_target"])
    print(f"  RECOMMENDED continuous config: {best['cabins']} cabins @ {best['fps_target']} fps")
    print(f"    (min sustained {best['min_fps']} fps/cabin, peak {best['peak_temp']}C, throttle-clean, monotonic).")
    print(f"  With {len(ALLCH)} marked cabins, that is round-robin batches of {best['cabins']} "
          f"(~{-(-len(ALLCH)//best['cabins'])} batches to cover all).")
else:
    print("  Even 2 cabins @ 6fps did not pass. Fall back to 1 cabin (already proven fine),")
    print("  or retry lower fps / add active cooling. Continuous multi-cabin is not viable as-is.")
print("  Plot: /loadtest/<gw>")
