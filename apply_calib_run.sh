#!/usr/bin/env bash
# Install /calib-run — the run buttons (wizard piece 4). calib_run_api.py spawns door_calib as a
# SUBPROCESS; it imports no cv2 and creates no tables, so the ingest process stays CV-free.
# Also ships the three wizard pages, which grow the button strip.
# FILES NEEDED IN /tmp: apply_calib_run.sh calib_run_api.py apply_calib_run_patch.py
#                       calib_roi_api.py calib_cells_api.py calib_label_api.py [door_calib.py gpu_door.py]
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_calib_run.sh calib_run_api.py apply_calib_run_patch.py calib_roi_api.py calib_cells_api.py calib_label_api.py door_calib.py gpu_door.py; do curl -fsSL -o /tmp/$f $B/$f; done
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
say "REV=run-buttons-3  (fix: chown calib+templates for the service user; write-checked)"
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
if [ -n "$CALIB_SCRIPT" ]; then
  CALIBDIR=$(dirname "$CALIB_SCRIPT")
  # door_calib does `import gpu_door as gd` at MODULE scope, so gpu_door.py must sit BESIDE it.
  # Refreshing door_calib alone left the old (or no) gpu_door there, and the first Build click died
  # with ModuleNotFoundError: gpu_door. Install every sibling door_calib imports, together.
  for f in door_calib.py gpu_door.py; do
    [ -f "/tmp/$f" ] || continue
    $PY -m py_compile "/tmp/$f" || { say "$f failed to compile — NOT installing it"; continue; }
    install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$CALIBDIR/$f" && say "refreshed $CALIBDIR/$f"
  done
  # Prove the import graph RESOLVES under the interpreter that will actually run it. A missing
  # sibling or a venv without cv2 must fail here, at deploy, not on the operator's first click.
  if [ -n "$CALIB_PY" ]; then
    if IMPERR=$(cd "$CALIBDIR" && "$CALIB_PY" -c "import door_calib" 2>&1); then
      say "import check: door_calib imports cleanly under $CALIB_PY"
    else
      say "WARNING: door_calib does NOT import under $CALIB_PY — Build/Fitcells WILL fail:"
      printf '%s\n' "$IMPERR" | tail -3 | sed 's/^/    /'
      say "  (usually a missing sibling — gpu_door.py — or cv2/numpy absent from that venv)"
    fi
  fi
fi
if [ -z "$CALIB_PY" ] || [ -z "$CALIB_SCRIPT" ]; then
  say "WARNING: no cv2-capable python and/or door_calib.py found."
  say "  python=${CALIB_PY:-NONE}  script=${CALIB_SCRIPT:-NONE}"
  say "  The pages will install and show the buttons DISABLED with the reason — they will not"
  say "  pretend to work. Set DOOR_CALIB_PY / DOOR_CALIB_SCRIPT in $SVC and restart to enable."
else
  say "runner: $CALIB_PY $CALIB_SCRIPT"
fi

# ---------- EVERY directory the runner writes, handed to the service user ----------
# The buttons run door_calib as the SERVICE user; the CLI era ran it under sudo. Anything root
# created back then is unwritable now, and it fails as a PermissionError deep inside a job rather
# than as anything the operator can act on. This is the same root cause as the labels.json 500.
#
# door_calib's own _chown_tree only helps when it runs AS ROOT, and only for the calib outdir it
# just wrote — it has never touched the templates tree. Sweep both here.
#   CALIB_DIR      crops, montages, *.json, roi.json, labels.json, _job.log
#   TEMPLATES_DIR  the --build output: <TEMPLATES_DIR>/<gw>/<cam>.npz (needs to CREATE <gw>/ too)
CALIB="${CALIB_DIR:-/var/lib/liftlab/calib}"
TEMPLATES="${TEMPLATES_DIR:-}"
if [ -z "$TEMPLATES" ]; then
  # honour a TEMPLATES_DIR already set on the unit before falling back to the default
  TEMPLATES=$(systemctl show -p Environment --value "$SVC" 2>/dev/null | tr ' ' '\n' \
              | sed -n 's/^TEMPLATES_DIR=//p' | head -1)
fi
TEMPLATES="${TEMPLATES:-/var/lib/liftlab/templates}"
for d in "$CALIB" "$TEMPLATES"; do
  mkdir -p "$d"
  chown -R "$OWNER:$OWNER" "$d" 2>/dev/null || true
  # u+rwX,g+rwX: the X only sets +x on DIRECTORIES, so .npz and .png do not become executable.
  chmod -R u+rwX,g+rwX "$d" 2>/dev/null || true
  say "owner: $d -> $OWNER (recursive, group-writable)"
done

# PROVE the service user can write them. chown reporting success is not the same as the runner
# being able to write — that gap is exactly what the drop-in bug taught, so check the effect.
WRITE_FAIL=0
for d in "$CALIB" "$TEMPLATES"; do
  if sudo -u "$OWNER" sh -c ": > '$d/.liftlab_write_test' && rm -f '$d/.liftlab_write_test'" 2>/dev/null; then
    say "write check OK: $OWNER can create files in $d"
  else
    say "WRITE CHECK FAILED: $OWNER cannot write $d"
    ls -ld "$d" | sed 's/^/    /'
    WRITE_FAIL=1
  fi
done
if [ "$WRITE_FAIL" = 1 ]; then
  say "ABORT: the Build button would PermissionError. Fix ownership and re-run:"
  say "  chown -R $OWNER:$OWNER $CALIB $TEMPLATES"
  exit 1
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
  # The drop-in directory is <UNIT FILENAME>.d — liftlab-cloud.SERVICE.d. Writing to "$SVC.d"
  # (liftlab-cloud.d) creates a directory systemd never reads: the env never reached the process,
  # and the buttons stayed disabled reporting "no python with cv2" until it was moved by hand.
  DROPIN="/etc/systemd/system/${SVC}.service.d"
  mkdir -p "$DROPIN"
  cat > "$DROPIN/calib-run.conf" <<EOF
[Service]
Environment=DOOR_CALIB_PY=$CALIB_PY
Environment=DOOR_CALIB_SCRIPT=$CALIB_SCRIPT
Environment=CALIB_DIR=$CALIB
Environment=TEMPLATES_DIR=$TEMPLATES
EOF
  say "wrote drop-in $DROPIN/calib-run.conf"
  # Remove the never-read directory the previous revision created, so a box that ran it is not left
  # with a decoy that looks like configuration.
  if [ -f "/etc/systemd/system/${SVC}.d/calib-run.conf" ]; then
    rm -f "/etc/systemd/system/${SVC}.d/calib-run.conf"
    rmdir "/etc/systemd/system/${SVC}.d" 2>/dev/null || true
    say "removed the stale (never-read) /etc/systemd/system/${SVC}.d/calib-run.conf"
  fi
  # And PROVE it took: after the restart below, the running process must actually have the var.
  VERIFY_DROPIN=1
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
# The drop-in is only real if the RUNNING process has it. systemd-show reads the merged unit, which
# is exactly what the wrong-directory bug got wrong — the file existed, the service never saw it.
if [ "${VERIFY_DROPIN:-0}" = 1 ]; then
  if systemctl show -p Environment --value "$SVC" 2>/dev/null | grep -q DOOR_CALIB_PY; then
    say "drop-in verified: DOOR_CALIB_PY is in the running unit environment"
  else
    say "RESULT: FAIL — the drop-in did NOT reach $SVC. The buttons will be disabled."
    say "  check: systemctl show -p Environment $SVC ; ls /etc/systemd/system/${SVC}.service.d/"; exit 1
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
fi
if [ "$ST" = 200 ] && [ "$ROI" = 200 ] && [ "$LBL" = 200 ]; then
  say "RESULT: PASS — buttons live at https://lift.gargi.online/calib-roi/site-A/$CAM"
  [ "$RUNOK" = '"ok":true' ] || say "  NOTE: runner reports NOT ok — buttons will be disabled with the reason shown on the page."
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
