#!/usr/bin/env bash
# Install the GPU FLEET supervisor (wizard piece 5) on liftlab-gpu: one gpu_analyze worker per
# camera ENABLED in the cloud registry. Adding a camera becomes a toggle on /dash.
# FILES NEEDED IN /tmp: apply_gpu_fleet.sh gpu_fleet.py gpu_analyze.py counting.py gpu_door.py
#                       gpu_watchdog.py liftlab-gpu-fleet.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_gpu_fleet.sh gpu_fleet.py gpu_analyze.py counting.py gpu_door.py gpu_watchdog.py liftlab-gpu-fleet.service; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo ANALYSIS_TOKEN=site-A:<token> bash /tmp/apply_gpu_fleet.sh
#
# THIS REPLACES liftlab-gpu (the single-camera unit) WITH liftlab-gpu-fleet. Both running would put
# two workers on the same camera, double-POSTing every transit. The script refuses to leave that
# state: it disables the old unit, and it will not start the fleet until the registry says which
# cameras to run — SEEDED FROM THE CAMERA THE OLD UNIT WAS ALREADY ANALYSING, so the switchover does
# not silently stop collecting.
set -uo pipefail
say(){ echo "[gpu-fleet] $*"; }
say "REV=fleet-1  (registry-driven workers; /dash toggle replaces the CAM= env edit)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo ANALYSIS_TOKEN=... bash $0"; exit 2; }
for f in gpu_fleet.py gpu_analyze.py liftlab-gpu-fleet.service; do
  [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
: "${ANALYSIS_TOKEN:?pass ANALYSIS_TOKEN=site-A:<read-only-token> (same one apply_gpu.sh uses)}"
CLOUD="${CLOUD_URL:-https://lift.gargi.online}"
GW="${GW:-site-A}"
TOK="${ANALYSIS_TOKEN#*:}"

LABUSER="${LABUSER:-$(ls -d /home/*/lab 2>/dev/null | head -1 | cut -d/ -f3)}"
[ -n "$LABUSER" ] || { echo "could not find ~/lab; set LABUSER=<user>"; exit 2; }
LABDIR="/home/$LABUSER/lab"
VENVPY=$(ls "$LABDIR"/bin/python* 2>/dev/null | head -1 || ls "$LABDIR"/.venv/bin/python* 2>/dev/null | head -1)
[ -x "$VENVPY" ] || { echo "no venv python under $LABDIR"; exit 2; }
MODEL="${MODEL:-$(ls "/home/$LABUSER/yolo11m.pt" 2>/dev/null || ls "/home/$LABUSER/yolo11n.pt" 2>/dev/null || echo yolo11n.pt)}"
APPDIR=/opt/liftlab-gpu
say "user=$LABUSER venv=$VENVPY model=$MODEL"

"$VENVPY" -m py_compile /tmp/gpu_fleet.py /tmp/gpu_analyze.py || { say "compile failed"; exit 1; }
install -d -o "$LABUSER" -g "$LABUSER" "$APPDIR"
for f in gpu_fleet.py gpu_analyze.py counting.py gpu_door.py gpu_watchdog.py; do
  [ -f "/tmp/$f" ] && install -o "$LABUSER" -g "$LABUSER" -m 644 "/tmp/$f" "$APPDIR/$f"
done
chmod 755 "$APPDIR/gpu_fleet.py" "$APPDIR/gpu_analyze.py"

# ---------- what is the old unit analysing? that camera must stay enabled ----------
OLDCAM=""
if [ -f /etc/systemd/system/liftlab-gpu.service ]; then
  OLDCAM=$(grep -oP '^Environment=CAM=\K\S+' /etc/systemd/system/liftlab-gpu.service | head -1)
fi
OLDCAM="${SEED_CAM:-$OLDCAM}"
say "camera the single-unit fleet was running: ${OLDCAM:-none found}"

# ---------- seed the registry so the switchover does not stop collection ----------
if [ -n "$OLDCAM" ]; then
  CODE=$(curl -s -o /tmp/_seed.json -w '%{http_code}' --max-time 10 -X POST \
    -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
    -d "{\"enabled\":true,\"stride\":2,\"note\":\"seeded from liftlab-gpu at fleet switchover\"}" \
    "$CLOUD/api/gw/$GW/cameras/$OLDCAM" 2>/dev/null)
  if [ "$CODE" = 200 ]; then
    say "registry seeded: $OLDCAM enabled"
  else
    say "ABORT: could not seed the registry ($OLDCAM -> HTTP $CODE). Starting the fleet now would"
    say "  run NO cameras and stop collection. Deploy camera_registry_api on the cloud first"
    say "  (apply_camera_registry.sh), then re-run this."; exit 1
  fi
fi
# Confirm the fleet will actually have something to run — refuse to switch over into silence.
ENABLED=$(curl -s --max-time 10 -H "Authorization: Bearer $TOK" "$CLOUD/api/gw/$GW/cameras" 2>/dev/null \
          | grep -o '"enabled_count":[0-9]*' | grep -oE '[0-9]+' | head -1)
if [ -z "$ENABLED" ] || [ "$ENABLED" = 0 ]; then
  say "ABORT: the registry reports ${ENABLED:-no} enabled cameras. The fleet would start and analyse"
  say "  nothing. Enable at least one on /dash (GPU analysis -> Enable), then re-run."; exit 1
fi
say "registry reports $ENABLED enabled camera(s) — safe to switch over"

sed -e "s|__USER__|$LABUSER|" -e "s|__VENVPY__|$VENVPY|" -e "s|__APPDIR__|$APPDIR|" -e "s|__MODEL__|$MODEL|" \
    /tmp/liftlab-gpu-fleet.service > /etc/systemd/system/liftlab-gpu-fleet.service

# ---------- stop the single-camera unit: two workers on one camera would double-count ----------
if systemctl is-enabled liftlab-gpu >/dev/null 2>&1 || [ "$(systemctl is-active liftlab-gpu 2>/dev/null)" = active ]; then
  systemctl disable --now liftlab-gpu >/dev/null 2>&1 || true
  say "liftlab-gpu STOPPED and DISABLED (the fleet owns the workers now; both would double-POST transits)"
fi

systemctl daemon-reload
systemctl enable liftlab-gpu-fleet >/dev/null 2>&1 || true
systemctl restart liftlab-gpu-fleet
sleep 8
AC=$(systemctl is-active liftlab-gpu-fleet)
say "liftlab-gpu-fleet = $AC | liftlab-gpu = $(systemctl is-active liftlab-gpu 2>/dev/null || echo inactive)"
if [ "$AC" != active ]; then
  say "RESULT: FAIL — fleet not active. journalctl -u liftlab-gpu-fleet -n 40"
  say "  The old unit is disabled; re-enable it to restore collection: systemctl enable --now liftlab-gpu"
  exit 1
fi
sleep 12
WORKERS=$(pgrep -fc "gpu_analyze.py" 2>/dev/null || echo 0)
say "gpu_analyze workers running: $WORKERS (expect $ENABLED)"
journalctl -u liftlab-gpu-fleet --since -30s --no-pager | tail -8 | sed 's/^/    /'
if [ "$WORKERS" -ge 1 ]; then
  say "RESULT: PASS — fleet is running $WORKERS worker(s)."
  say "  Add a camera: /dash -> the camera's tab -> GPU analysis -> Enable. Takes effect in ~30s."
  say "  Watch: journalctl -u liftlab-gpu-fleet -f"
else
  say "RESULT: CHECK — fleet is active but no workers started. journalctl -u liftlab-gpu-fleet -n 40"
  say "  Collection is STOPPED. To roll back now: systemctl disable --now liftlab-gpu-fleet"
  say "                                            systemctl enable --now liftlab-gpu"
  exit 1
fi
