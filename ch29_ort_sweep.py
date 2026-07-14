#!/usr/bin/env python3
"""READ-ONLY onnxruntime thread sweep. Sizes the inference fix. Changes NOTHING
(no agent/pipeline/controller/Gargi writes). Run with the B4 venv.

Confirmed upstream: decode is 25fps hardware (hevc_v4l2m2m), NOT the wall. The wall
is yolo11n.onnx CPU inference — 0.97 fps at 1 thread. Pi 4 has 4 cores, 3 idle
during single-cabin inference. This measures inference throughput vs thread count.

One static in-memory ch29 frame is reused; NO capture in the timing loop. Per
intra_op_num_threads in [1,2,3,4]: fresh InferenceSession, warm up 10, time N=100.
Plus a 5th row: intra_op=4 with graph_optimization_level=ORT_ENABLE_ALL.
"""
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")
import onvif_resolve  # noqa: E402

ENV = "/etc/liftlab-agent.env"
CH = 29
MODEL_CANDIDATES = ["/home/askjitk/liftlab-b4/yolo11n.onnx", "/home/askjitk/yolo11n.onnx"]
WARMUP = 10
N = 100
TARGET_FPS = 6.0


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
GWID = c.get("GATEWAY_ID", "site-A")
MODEL = next((m for m in MODEL_CANDIDATES if Path(m).exists()), MODEL_CANDIDATES[0])


def ch29_url():
    cm = onvif_resolve.resolve_map(HOST, USER, PW, cache_path=f"/tmp/onvif_map_{GWID}.json")
    raw = onvif_resolve.uri_for(cm, CH, prefer=1)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


import cv2  # noqa: E402
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402

try:
    import os
    ncpu = os.cpu_count()
except Exception:
    ncpu = "?"
print(f"READ-ONLY onnxruntime thread sweep — model={MODEL}, cores={ncpu}, ort={ort.__version__}\n")

# ---- grab ONE static frame (no capture in the timing loop) ----
URL = ch29_url()
frame_bgr = None
cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
for _ in range(10):
    ok, f = cap.read()
    if ok and f is not None:
        frame_bgr = f
        break
cap.release()
if frame_bgr is None:
    print("FATAL: could not grab a ch29 frame"); raise SystemExit(1)
print(f"  static frame: {frame_bgr.shape[1]}x{frame_bgr.shape[0]}")


def preprocess(img, size=640):
    x = cv2.cvtColor(cv2.resize(img, (size, size)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(x, (2, 0, 1))[None]


BLOB = preprocess(frame_bgr)


def bench(intra, all_opt=False):
    so = ort.SessionOptions()
    so.intra_op_num_threads = intra
    so.inter_op_num_threads = 1
    if all_opt:
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    for _ in range(WARMUP):
        sess.run(None, {name: BLOB})
    t0 = time.time()
    for _ in range(N):
        sess.run(None, {name: BLOB})
    dt = time.time() - t0
    del sess
    return round(N / dt, 2), round(dt / N * 1000, 1)


rows = []
for intra in [1, 2, 3, 4]:
    fps, ms = bench(intra)
    rows.append((f"{intra}", fps, ms))
    print(f"  threads={intra}: {fps} fps ({ms} ms/inf)")
fps4a, ms4a = bench(4, all_opt=True)
rows.append(("4+ALLOPT", fps4a, ms4a))
print(f"  threads=4 +ORT_ENABLE_ALL: {fps4a} fps ({ms4a} ms/inf)")

base = rows[0][1] or 1e-9
print("\n=== TABLE ===")
print(f"  {'threads':10} {'fps':8} {'ms/inf':8} x_vs_1")
for label, fps, ms in rows:
    print(f"  {label:10} {str(fps):8} {str(ms):8} {round(fps/base,2)}x")

# knee: first thread count whose gain over the previous is <10%
core_rows = rows[:4]  # 1..4 threads
best = max(core_rows, key=lambda r: r[1])
knee = core_rows[0]
for i in range(1, len(core_rows)):
    prev, cur = core_rows[i - 1][1], core_rows[i][1]
    if prev and (cur - prev) / prev < 0.10:
        knee = core_rows[i - 1]
        break
    knee = core_rows[i]

best_overall = max(rows, key=lambda r: r[1])
print("\n=== READ ===")
print(f"  knee: ~{knee[1]} fps at {knee[0]} thread(s) (more threads stop helping past here)")
print(f"  best overall: {best_overall[1]} fps at {best_overall[0]}")
print(f"  clears {TARGET_FPS} fps target? {'YES' if best_overall[1] >= TARGET_FPS else 'NO'}"
      f"  (headroom {round(best_overall[1] - TARGET_FPS, 2)} fps)")
if best_overall[1] < 3.0:
    print(f"  FLAG: best CPU config {best_overall[1]} fps < 3 fps — YOLO11n-CPU may need a lighter")
    print("        path (smaller imgsz + zone re-cal, or a Hailo accelerator). NUMBER ONLY, no hw recommendation.")
elif best_overall[1] < TARGET_FPS:
    print(f"  NOTE: clears 3 fps but short of {TARGET_FPS} — usable at reduced analysis fps, or pair")
    print("        with a lighter path to reach target. Reporting the number; no hardware call.")
else:
    print(f"  Thread un-pin alone reaches the {TARGET_FPS} fps target — no lighter path / hardware needed.")
