#!/usr/bin/env bash
# STEP A — can torch+ultralytics install AND run on the 4GB Pi 4, and at what fps?
# Decides Step 2 by itself. RUN ON THE PI (root not required). ISOLATED throwaway venv
# in /tmp — does NOT touch B4 or the running door pipeline. Uses a SYNTHETIC 1080p frame
# (inference cost is fixed by imgsz, content-independent) so NO RTSP contention with
# liftlab-watch. Benchmarks yolo11n.pt at torch threads 1-4, vs onnx's 1.97 fps @ 3.
set -uo pipefail
VENV=/tmp/ultra_probe
B4PY=/home/askjitk/liftlab-b4/.venv/bin/python
say(){ echo "[step-a] $*"; }

say "creating throwaway venv $VENV (B4 untouched)"
python3 -m venv "$VENV" || { say "venv create FAILED — python3-venv missing?"; exit 1; }
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true

say "pip install ultralytics (pulls torch — THE feasibility question on a 4GB Pi)..."
t0=$(date +%s)
if "$VENV/bin/pip" install --quiet ultralytics 2> "$VENV/pip_err.log"; then
  say "install OK in $(( $(date +%s) - t0 ))s"
else
  say "install FAILED in $(( $(date +%s) - t0 ))s — torch likely has no ARM wheel / OOM:"
  tail -5 "$VENV/pip_err.log" | sed 's/^/[step-a]   /'
  say "RESULT: (a) ultralytics-on-Pi is DEAD at the install step. Decision: onnx path (b), OR Hailo."
  rm -rf "$VENV"; exit 0
fi

say "benchmarking (synthetic 1080p frame; imgsz 640; N=50; predict = inference-only, track = +ByteTrack)"
"$VENV/bin/python" - <<'PY'
import time, numpy as np, torch
from ultralytics import YOLO
frame = (np.random.rand(1080,1920,3)*255).astype('uint8')
m = YOLO("yolo11n.pt")
def bench(fn, n=50):
    for _ in range(8): fn()            # warm
    t=time.time()
    for _ in range(n): fn()
    return round(n/(time.time()-t),2)
print(f"  torch {torch.__version__}  cpu_threads_max={torch.get_num_threads()}")
print(f"  {'threads':>7} {'predict_fps':>12} {'track_fps':>10}")
for th in (1,2,3,4):
    torch.set_num_threads(th)
    pf = bench(lambda: m.predict(frame, imgsz=640, classes=[0], conf=0.35, verbose=False))
    m2 = YOLO("yolo11n.pt")   # fresh tracker state
    tf = bench(lambda: m2.track(frame, imgsz=640, classes=[0], conf=0.35, persist=True, tracker="bytetrack.yaml", verbose=False))
    print(f"  {th:>7} {pf:>12} {tf:>10}")
print("\n  onnx reference (earlier sweep): 1.97 fps @ 3 threads (inference-only, mem-bandwidth capped)")
print("  READ: predict_fps < ~1.97 => torch CPU slower than onnx -> (a) has no fps advantage.")
print("        track_fps is the REAL occupancy cost (adds ByteTrack). If << 2 fps, Pi 4 CPU can't")
print("        do live occupancy in torch either -> Hailo is the measured path.")
PY

say "verifying B4 is UNAFFECTED (separate venv):"
( cd /home/askjitk/liftlab-b4 && PYTHONPATH=/home/askjitk/liftlab-b4 "$B4PY" -c "import liftlab.doors, numpy, av; print('  B4 imports liftlab.doors + deps OK')" ) || say "  B4 import CHECK FAILED (unexpected — venv is isolated)"

say "cleanup: rm -rf $VENV"
rm -rf "$VENV"
say "done. Report the threads table + install time."
