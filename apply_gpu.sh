#!/usr/bin/env bash
# Install the GPU transit analyzer on liftlab-gpu (preemption-safe systemd). Auto-detects the ~/lab
# venv + user so it survives Spot preemption (Restart=always + enabled -> starts on boot).
# FILES NEEDED IN /tmp: apply_gpu.sh gpu_analyze.py counting.py liftlab-gpu.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_gpu.sh gpu_analyze.py counting.py liftlab-gpu.service; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON liftlab-gpu, passing the read-only analysis token:
#   sudo ANALYSIS_TOKEN=site-A:<read-only-token> bash /tmp/apply_gpu.sh
set -uo pipefail
say(){ echo "[apply-gpu] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo ANALYSIS_TOKEN=... bash $0"; exit 2; }
for f in gpu_analyze.py counting.py liftlab-gpu.service; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
: "${ANALYSIS_TOKEN:?pass ANALYSIS_TOKEN=site-A:<read-only-token> (minted on the cloud by apply_analysis.sh)}"
LABUSER="${LABUSER:-$(ls -d /home/*/lab 2>/dev/null | head -1 | cut -d/ -f3)}"
[ -n "$LABUSER" ] || { echo "could not find ~/lab; set LABUSER=<user>"; exit 2; }
LABDIR="/home/$LABUSER/lab"
VENVPY=$(ls "$LABDIR"/bin/python* 2>/dev/null | head -1 || ls "$LABDIR"/.venv/bin/python* 2>/dev/null | head -1)
[ -x "$VENVPY" ] || { echo "no venv python under $LABDIR (bin/ or .venv/bin/)"; exit 2; }
MODEL=$(ls "$LABDIR"/yolo11n.pt 2>/dev/null || ls "$LABDIR"/**/yolo11n.pt 2>/dev/null | head -1 || echo "yolo11n.pt")
APPDIR=/opt/liftlab-gpu
say "user=$LABUSER venv=$VENVPY model=$MODEL"

"$VENVPY" -m py_compile /tmp/gpu_analyze.py /tmp/counting.py || { say "python compile failed"; exit 1; }
"$VENVPY" -c "import torch,ultralytics,av,cv2; print('  torch',torch.__version__,'cuda',torch.cuda.is_available(),'| ultralytics',ultralytics.__version__)" \
  || { say "deps missing in ~/lab venv (need torch/ultralytics/av/cv2)"; exit 1; }
install -d -o "$LABUSER" -g "$LABUSER" "$APPDIR"
install -o "$LABUSER" -g "$LABUSER" -m 755 /tmp/gpu_analyze.py "$APPDIR/gpu_analyze.py"
install -o "$LABUSER" -g "$LABUSER" -m 644 /tmp/counting.py "$APPDIR/counting.py"
# token env (root-only)
umask 077; printf 'ANALYSIS_TOKEN=%s\n' "${ANALYSIS_TOKEN#*:}" > /etc/liftlab-gpu.env; umask 022
# gpu_analyze imports counting from its own dir
sed -e "s|__USER__|$LABUSER|" -e "s|__VENVPY__|$VENVPY|" -e "s|__APPDIR__|$APPDIR|" -e "s|__MODEL__|$MODEL|" \
    /tmp/liftlab-gpu.service > /etc/systemd/system/liftlab-gpu.service
# ensure counting.py is importable: run from APPDIR
sed -i "s|ExecStart=$VENVPY $APPDIR/gpu_analyze.py|WorkingDirectory=$APPDIR\nExecStart=$VENVPY $APPDIR/gpu_analyze.py|" /etc/systemd/system/liftlab-gpu.service
systemctl daemon-reload
systemctl enable --now liftlab-gpu >/dev/null 2>&1 || systemctl restart liftlab-gpu
sleep 4
AC=$(systemctl is-active liftlab-gpu)
say "liftlab-gpu = $AC (enabled -> survives preemption/reboot)"
if [ "$AC" = active ]; then
  say "RESULT: PASS. Follow: journalctl -u liftlab-gpu -f   (expect 'start:' then 'frame WxH -> zones scaled' then transit lines)"
  say "Counts appear on https://lift.gargi.online/ops/site-A (Transit card) as riders cross."
else
  say "RESULT: CHECK — journalctl -u liftlab-gpu -n 40  (token? model path? cuda?)"; exit 1
fi
