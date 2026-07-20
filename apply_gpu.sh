#!/usr/bin/env bash
# Install the GPU transit analyzer on liftlab-gpu (preemption-safe systemd). Auto-detects the ~/lab
# venv + user so it survives Spot preemption (Restart=always + enabled -> starts on boot).
# FILES NEEDED IN /tmp: apply_gpu.sh gpu_analyze.py counting.py gpu_door.py liftlab-gpu.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_gpu.sh gpu_analyze.py counting.py gpu_door.py liftlab-gpu.service; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON liftlab-gpu, passing the read-only analysis token:
#   sudo ANALYSIS_TOKEN=site-A:<read-only-token> bash /tmp/apply_gpu.sh
set -uo pipefail
say(){ echo "[apply-gpu] $*"; }
# --- staleness tell: the OLD script cannot print this. If you do NOT see this REV line and a
#     'MainPID X -> Y' line at the end, you ran a cached /tmp copy — re-curl apply_gpu.sh. ---
say "REV=restart-verify-4-envmerge+gpudoor  (restarts + asserts PID changed; MERGES env; installs gpu_door.py)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo ANALYSIS_TOKEN=... bash $0"; exit 2; }
for f in gpu_analyze.py counting.py gpu_door.py liftlab-gpu.service; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
: "${ANALYSIS_TOKEN:?pass ANALYSIS_TOKEN=site-A:<read-only-token> (minted on the cloud by apply_analysis.sh)}"
LABUSER="${LABUSER:-$(ls -d /home/*/lab 2>/dev/null | head -1 | cut -d/ -f3)}"
[ -n "$LABUSER" ] || { echo "could not find ~/lab; set LABUSER=<user>"; exit 2; }
LABDIR="/home/$LABUSER/lab"
VENVPY=$(ls "$LABDIR"/bin/python* 2>/dev/null | head -1 || ls "$LABDIR"/.venv/bin/python* 2>/dev/null | head -1)
[ -x "$VENVPY" ] || { echo "no venv python under $LABDIR (bin/ or .venv/bin/)"; exit 2; }
# MODEL: honor an explicit env, else PREFER yolo11m.pt (bench: 35fps vs 38.5 for 11n — ~9% cost, 10x
# the model; we're CPU-bound on ByteTrack, so the bigger model is nearly free), else fall back to 11n.
MODEL="${MODEL:-$(ls "/home/$LABUSER/yolo11m.pt" 2>/dev/null || ls "$LABDIR"/yolo11m.pt 2>/dev/null \
       || ls "/home/$LABUSER/yolo11n.pt" 2>/dev/null || ls "$LABDIR"/yolo11n.pt 2>/dev/null || echo "yolo11n.pt")}"
APPDIR=/opt/liftlab-gpu
say "user=$LABUSER venv=$VENVPY model=$MODEL"

"$VENVPY" -m py_compile /tmp/gpu_analyze.py /tmp/counting.py /tmp/gpu_door.py || { say "python compile failed"; exit 1; }
"$VENVPY" -c "import torch,ultralytics,av,cv2; print('  torch',torch.__version__,'cuda',torch.cuda.is_available(),'| ultralytics',ultralytics.__version__)" \
  || { say "deps missing in ~/lab venv (need torch/ultralytics/av/cv2)"; exit 1; }
install -d -o "$LABUSER" -g "$LABUSER" "$APPDIR"
install -o "$LABUSER" -g "$LABUSER" -m 755 /tmp/gpu_analyze.py "$APPDIR/gpu_analyze.py"
install -o "$LABUSER" -g "$LABUSER" -m 644 /tmp/counting.py "$APPDIR/counting.py"
install -o "$LABUSER" -g "$LABUSER" -m 644 /tmp/gpu_door.py "$APPDIR/gpu_door.py"   # GPU_DOOR pass — else ModuleNotFoundError crash-loop
# token env (root-only) — MERGE, don't clobber: refresh ANALYSIS_TOKEN but PRESERVE every other key the
# operator set (GPU_DOOR, DOOR_ROI_FRAME, PANEL_ROIS, DIGIT_CELLS, ARROW_CELL, ...). Atomic temp+mv.
ENVF=/etc/liftlab-gpu.env
umask 077
{ [ -f "$ENVF" ] && grep -v '^ANALYSIS_TOKEN=' "$ENVF" || true; printf 'ANALYSIS_TOKEN=%s\n' "${ANALYSIS_TOKEN#*:}"; } > "$ENVF.tmp"
mv "$ENVF.tmp" "$ENVF"
umask 022
KEPT=$(( $(wc -l < "$ENVF") - 1 ))
say "env: refreshed ANALYSIS_TOKEN; preserved $KEPT other key(s) (GPU_DOOR/geometry kept)"
# gpu_analyze imports counting from its own dir
sed -e "s|__USER__|$LABUSER|" -e "s|__VENVPY__|$VENVPY|" -e "s|__APPDIR__|$APPDIR|" -e "s|__MODEL__|$MODEL|" \
    /tmp/liftlab-gpu.service > /etc/systemd/system/liftlab-gpu.service
# ensure counting.py is importable: run from APPDIR
sed -i "s|ExecStart=$VENVPY $APPDIR/gpu_analyze.py|WorkingDirectory=$APPDIR\nExecStart=$VENVPY $APPDIR/gpu_analyze.py|" /etc/systemd/system/liftlab-gpu.service
OLDPID=$(systemctl show -p MainPID --value liftlab-gpu 2>/dev/null || echo 0)
systemctl daemon-reload
systemctl enable liftlab-gpu >/dev/null 2>&1 || true    # boot-persist; --now is a NO-OP if already running, so do NOT rely on it
systemctl restart liftlab-gpu                            # ALWAYS restart so the freshly-installed code actually loads
sleep 4
AC=$(systemctl is-active liftlab-gpu)
NEWPID=$(systemctl show -p MainPID --value liftlab-gpu 2>/dev/null || echo 0)
say "liftlab-gpu = $AC  (MainPID $OLDPID -> $NEWPID)"
if [ "$AC" != active ]; then
  say "RESULT: CHECK — service not active. journalctl -u liftlab-gpu -n 40  (token? model path? cuda?)"; exit 1
fi
# An install that doesn't take is WORSE than a failed one — it looks like success while the old code runs.
if [ -z "$NEWPID" ] || [ "$NEWPID" = 0 ] || [ "$NEWPID" = "$OLDPID" ]; then
  say "RESULT: FAIL — restart did NOT take (PID unchanged: $OLDPID -> $NEWPID). The OLD code is still running."
  say "  journalctl -u liftlab-gpu -n 40"; exit 1
fi
say "RESULT: PASS — new process $NEWPID is live (old $OLDPID replaced)."
say "  Follow: journalctl -u liftlab-gpu -f   (expect 'start:', 'frame WxH -> zones scaled', 'seg timing:', transit/REJECT lines)"
say "  Verify GPU: nvidia-smi  (python $NEWPID holding memory + non-zero GPU-Util = actually on the L4)"
say "  Counts + rejection histogram + per-segment timing on https://lift.gargi.online/ops/site-A"
