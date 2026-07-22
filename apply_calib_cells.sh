#!/usr/bin/env bash
# Install /calib-cells — the cell drawing wizard (wizard piece 3). calib_cells_api.py reads/writes
# files only (roi.json + a PNG header parse); NO cv2, NO tables, ingest untouched.
#
# ALSO ships calib_roi_api.py, which is REQUIRED alongside it: both pages share roi.json, and the
# ROI page's save is only merge-safe from this rev on. An older calib_roi_api.py replaces the whole
# file, so redrawing ROIs would silently delete the cells this page just wrote.
# FILES NEEDED IN /tmp: apply_calib_cells.sh calib_cells_api.py apply_calib_cells_patch.py
#                       calib_roi_api.py door_calib.py [dash_api.py]
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_calib_cells.sh calib_cells_api.py apply_calib_cells_patch.py calib_roi_api.py door_calib.py dash_api.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_calib_cells.sh
#
# CADDY: /calib-cells must sit behind the SAME basicauth as /calib, /calib-label and /calib-roi.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[calib-cells] $*"; }
say "REV=cells-wizard-1  (drag TENS/UNITS/ARROW[/HUNDREDS] -> cells into roi.json; door_calib reads them)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in calib_cells_api.py apply_calib_cells_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
[ -f "$APP/calib_roi_api.py" ] || { echo "calib_roi_api.py not installed — run apply_calib_roi.sh first"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

CALIB=/var/lib/liftlab/calib
mkdir -p "$CALIB"; chown -R "$OWNER:$OWNER" "$CALIB" 2>/dev/null || true; chmod -R g+w "$CALIB" 2>/dev/null || true
say "calib tree owned by $OWNER (roi.json writable by the web app)"

# nav_common.py is a HARD dependency of every page module (the shared header). Installing a page
# without it is an ImportError that takes the ENTIRE operator UI down, not just this page — so it is
# installed here unconditionally and the deploy refuses to continue without it.
if [ -f /tmp/nav_common.py ]; then
  $PY -m py_compile /tmp/nav_common.py || { say "nav_common.py failed to compile — aborting"; exit 1; }
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/nav_common.py "$APP/nav_common.py"
  say "installed nav_common.py (shared header)"
elif [ ! -f "$APP/nav_common.py" ]; then
  say "ABORT: nav_common.py is neither in /tmp nor installed. Every page imports it; continuing"
  say "  would ImportError the whole UI. Re-curl nav_common.py and run again."; exit 1
fi

$PY -m py_compile /tmp/calib_cells_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/calib_cells_api.py "$APP/calib_cells_api.py"

# calib_roi_api.py is NOT optional here — see the header. Refuse rather than deploy a pair that can
# eat its own data.
if [ -f /tmp/calib_roi_api.py ]; then
  $PY -m py_compile /tmp/calib_roi_api.py || { say "calib_roi_api.py failed to compile — aborting"; exit 1; }
  if ! grep -q "MERGE, never replace" /tmp/calib_roi_api.py; then
    say "ABORT: /tmp/calib_roi_api.py is an OLD copy whose save REPLACES roi.json — it would delete"
    say "  the cells this page writes. Re-curl calib_roi_api.py from pi-scripts."; exit 1
  fi
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/calib_roi_api.py "$APP/calib_roi_api.py"
  say "installed calib_roi_api.py (merge-safe roi.json writes)"
else
  if ! grep -q "MERGE, never replace" "$APP/calib_roi_api.py"; then
    say "ABORT: the INSTALLED calib_roi_api.py replaces roi.json wholesale — redrawing ROIs would"
    say "  delete the cells. Fetch calib_roi_api.py from pi-scripts and re-run."; exit 1
  fi
  say "installed calib_roi_api.py is already merge-safe"
fi

for f in dash_api.py calib_label_api.py; do
  if [ -f "/tmp/$f" ]; then
    $PY -m py_compile "/tmp/$f" || { say "$f failed to compile — NOT installing it"; continue; }
    install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"; say "installed $f"
  fi
done
if [ -f /tmp/door_calib.py ]; then
  $PY -m py_compile /tmp/door_calib.py || { say "door_calib.py failed to compile — NOT installing"; exit 1; }
  if [ -f "$APP/door_calib.py" ]; then
    install -o "$OWNER" -g "$OWNER" -m 644 /tmp/door_calib.py "$APP/door_calib.py"
    say "installed door_calib.py (reads cells from roi.json when the envs are unset)"
  else
    say "NOTE: no $APP/door_calib.py — calibration runs elsewhere. Copy the new door_calib.py THERE,"
    say "  or the cells will be drawn and saved and read by nothing at --build time."
  fi
fi

if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import calib_cells_api
a=FastAPI(); a.include_router(calib_cells_api.calib_cells_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_calib_cells_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }

OLDPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi

PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
if [ -n "$MPID" ] && [ "$MPID" != 0 ]; then
  PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)
fi
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
NEWPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
CAM="${CAM:-ch16}"
if [ -z "$PORT" ]; then
  say "AFTER: cloud=active (came-up verified); port unknown -> routes not HTTP-probed. NOT a failure."
  say "RESULT: PASS. Draw cells at https://lift.gargi.online/calib-cells/site-A/$CAM . MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
PAGE=$(code "$BASE/calib-cells/site-A/$CAM"); ST=$(code "$BASE/calib-cells/site-A/$CAM/state")
ROI=$(code "$BASE/calib-roi/site-A/$CAM"); OPS=$(code "$BASE/ops")
say "AFTER (port $PORT): /calib-cells=$PAGE  /state=$ST  /calib-roi=$ROI  /ops=$OPS  (MainPID $OLDPID -> $NEWPID)"
if [ "$PAGE" = 200 ] && [ "$ST" = 200 ] && [ "$ROI" = 200 ]; then
  say "RESULT: PASS — draw cells at https://lift.gargi.online/calib-cells/site-A/$CAM"
  say "  Per-camera setup is now: draw ROIs -> --collect -> label -> draw cells -> --build."
  say "  The page needs COLLECTED CROPS: run door_calib --collect for $CAM before drawing cells."
elif [ "$PAGE" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); cloud active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
