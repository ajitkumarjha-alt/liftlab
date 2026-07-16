#!/usr/bin/env bash
# Stand up the VM ANALYSIS stack in a SEPARATE venv (does NOT touch liftlab-cloud's venv/service).
# onnxruntime + opencv + PyAV + numpy, plus yolo11n.onnx (from the Pi).
# FILES NEEDED IN /tmp: setup_analysis_venv.sh bench_analysis.py  AND the model at /tmp/yolo11n.onnx
#   Get the model onto the VM — on the PI:
#     scp /home/askjitk/liftlab-b4/yolo11n.onnx <this-vm-host>:/tmp/yolo11n.onnx
# CURL (scripts): B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#     for f in setup_analysis_venv.sh bench_analysis.py; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON THE VM: sudo bash /tmp/setup_analysis_venv.sh
set -uo pipefail
DIR=/opt/liftlab-analysis
VENV="$DIR/.venv"
say(){ echo "[analysis-setup] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/bench_analysis.py ] || { echo "missing /tmp/bench_analysis.py"; exit 2; }
command -v python3 >/dev/null || { echo "python3 missing"; exit 2; }
command -v ffmpeg >/dev/null || say "note: ffmpeg CLI not found (PyAV bundles its own libs, still fine)"
mkdir -p "$DIR"

# ---- model ----
if [ -f "$DIR/yolo11n.onnx" ]; then say "model already at $DIR/yolo11n.onnx"
elif [ -f /tmp/yolo11n.onnx ]; then install -m 644 /tmp/yolo11n.onnx "$DIR/yolo11n.onnx"; say "model installed from /tmp"
else
  say "MISSING yolo11n.onnx. On the PI run:"
  say "    scp /home/askjitk/liftlab-b4/yolo11n.onnx <this-vm-host>:/tmp/yolo11n.onnx"
  say "then re-run this. Aborting."; exit 1
fi
SZ=$(stat -c%s "$DIR/yolo11n.onnx"); say "model size: $SZ bytes (Pi's is 10741392 — should match)"
[ "$SZ" -gt 1000000 ] || { say "model looks too small ($SZ) — re-copy it"; exit 1; }

# ---- venv (isolated) ----
say "creating venv $VENV (separate from liftlab-cloud) ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
say "installing numpy onnxruntime opencv-python-headless av (this takes a minute) ..."
"$VENV/bin/pip" install --quiet numpy onnxruntime opencv-python-headless av || { say "pip install FAILED"; exit 1; }
install -m 755 /tmp/bench_analysis.py "$DIR/bench_analysis.py"

# ---- verify ----
"$VENV/bin/python" - <<PY || { say "import/model check FAILED"; exit 1; }
import numpy, cv2, av, onnxruntime as o
print("  numpy", numpy.__version__, "| cv2", cv2.__version__, "| av", av.__version__, "| ort", o.__version__)
s = o.InferenceSession("$DIR/yolo11n.onnx", providers=["CPUExecutionProvider"])
print("  model loads; input", s.get_inputs()[0].shape, "output", s.get_outputs()[0].shape)
PY
say "STACK READY (isolated from liftlab-cloud)."
say "BENCHMARK (relay must be running so ch29 segments exist):"
say "    sudo $VENV/bin/python $DIR/bench_analysis.py"
say "    (report the DECODE / INFER 1t+2t / END-TO-END / INGEST PROBE / VERDICT block)"
