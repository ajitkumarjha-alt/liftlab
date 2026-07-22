#!/usr/bin/env bash
# Install /calib-run — the run buttons (wizard piece 4). calib_run_api.py spawns door_calib as a
# SUBPROCESS; it imports no cv2 and creates no tables, so the ingest process stays CV-free.
# Also ships the three wizard pages, which grow the button strip.
# FILES NEEDED IN /tmp: apply_calib_run.sh calib_run_api.py apply_calib_run_patch.py
#                       calib_roi_api.py calib_cells_api.py calib_label_api.py [door_calib.py]
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_calib_run.sh calib_run_api.py apply_calib_run_patch.py calib_roi_api.py calib_cells_api.py calib_label_api.py door_calib.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_calib_run.sh
#
# THE ONE THING TO GET RIGHT: door_calib needs cv2 + numpy and this app's venv does not have them.
# The script probes for a usable interpreter and REFUSES to finish quietly if it cannot find one —
# buttons that 503 on every press are worse than no buttons, because they look deployed.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[calib-run] $*"; }
say "REV=run-buttons-1  (Collect / Top-up / Build / Fitcells / Refresh frame as buttons)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in calib_run_api.py apply_calib_run_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# ---------- find the interpreter that can actually run door_calib ----------
CALIB_PY="${DOOR_CALIB_PY:-}"
if [ -z "$CALIB_PY" ]; then
  for c in /opt/liftlab-b3/calib/.venv/bin/python "$PY" /usr/bin/python3; do
    [ -x "$c" ] || continue
    if "$c" -c "import cv2, numpy" >/dev/null 2>&1; then CALIB_PY="$c"; break; fi
  done
fi
CALIB_SCRIPT="${DOOR_CALIB_SCRIPT:-}"
if [ -z "$CALIB_SCRIPT" ]; then
  for c in "$APP/door_calib.py" /opt/liftlab-b3/calib/door_calib.py /opt/liftlab-gpu/door_calib.py; do
    [ -f "$c" ] && { CALIB_SCRIPT="$c"; break; }
  done
fi
if [ -f /tmp/door_calib.py ] && [ -n "$CALIB_SCRIPT" ]; then
  $PY -m py_compile /tmp/door_calib.py \
    && install -o "$OWNER" -g "$OWNER" -m 644 /tmp/door_calib.py "$CALIB_SCRIPT" \
    && say "refreshed $CALIB_SCRIPT"
fi
if [ -z "$CALIB_PY" ] || [ -z "$CALIB_SCRIPT" ]; then
  say "WARNING: no cv2-capable python and/or door_calib.py found."
  say "  python=${CALIB_PY:-NONE}  script=${CALIB_SCRIPT:-NONE}"
  say "  The pages will install and show the buttons DISABLED with the reason — they will not"
  say "  pretend to work. Set DOOR_CALIB_PY / DOOR_CALIB_SCRIPT in $SVC and restart to enable."
else
  say "runner: $CALIB_PY $CALIB_SCRIPT"
fi

$PY -m py_compile /tmp/calib_run_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/calib_run_api.py "$APP/calib_run_api.py"
for f in calib_roi_api.py calib_cells_api.py calib_label_api.py; do
  if [ -f "/tmp/$f" ]; then
    $PY -m py_compile "/tmp/$f" || { say "$f failed to compile — NOT installing it"; continue; }
    install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"; say "installed $f"
  fi
done

# Persist the runner paths into the unit so a restart keeps them (a drop-in, not an edit of the unit).
if [ -n "$CALIB_PY" ] && [ -n "$CALIB_SCRIPT" ]; then
  mkdir -p "/etc/systemd/system/$SVC.d"
  cat > "/etc/systemd/system/$SVC.d/calib-run.conf" <<EOF
[Service]
Environment=DOOR_CALIB_PY=$CALIB_PY
Environment=DOOR_CALIB_SCRIPT=$CALIB_SCRIPT
EOF
  say "wrote drop-in /etc/systemd/system/$SVC.d/calib-run.conf"
fi

if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import calib_run_api
a=FastAPI(); a.include_router(calib_run_api.calib_run_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_calib_run_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }

OLDPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
systemctl daemon-reload
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi
PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
[ -n "$MPID" ] && [ "$MPID" != 0 ] && PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
NEWPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
CAM="${CAM:-ch16}"
if [ -z "$PORT" ]; then
  say "AFTER: cloud=active; port unknown -> not HTTP-probed. NOT a failure. MainPID $OLDPID -> $NEWPID"
  say "RESULT: PASS. Buttons at https://lift.gargi.online/calib-roi/site-A/$CAM"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
ST=$(code "$BASE/calib-run/site-A/$CAM/status"); ROI=$(code "$BASE/calib-roi/site-A/$CAM")
LBL=$(code "$BASE/calib-label/site-A/$CAM"); OPS=$(code "$BASE/ops")
RUNOK=$(curl -s --max-time 8 "$BASE/calib-run/site-A/$CAM/status" | grep -o '"ok":[a-z]*' | head -1)
say "AFTER (port $PORT): /calib-run/status=$ST  /calib-roi=$ROI  /calib-label=$LBL  /ops=$OPS  runner $RUNOK"
if [ "$ST" = 200 ] && [ "$ROI" = 200 ] && [ "$LBL" = 200 ]; then
  say "RESULT: PASS — buttons live at https://lift.gargi.online/calib-roi/site-A/$CAM"
  [ "$RUNOK" = '"ok":true' ] || say "  NOTE: runner reports NOT ok — buttons will be disabled with the reason shown on the page."
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
