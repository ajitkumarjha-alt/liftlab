#!/usr/bin/env bash
# Install /calib-roi — the ROI drawing wizard (wizard piece 2). calib_roi_api.py reads/writes files
# only (roi.json + a JPEG header parse); NO cv2, NO tables, ingest untouched. Also ships the
# door_calib.py that READS roi.json when DOOR_ROI_FRAME/PANEL_ROIS are unset (env still wins), the
# /calib-label back-link, and the /dash setup links.
# FILES NEEDED IN /tmp: apply_calib_roi.sh calib_roi_api.py apply_calib_roi_patch.py
#                       calib_label_api.py dash_api.py door_calib.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_calib_roi.sh calib_roi_api.py apply_calib_roi_patch.py calib_label_api.py dash_api.py door_calib.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_calib_roi.sh
#
# CADDY: /calib-roi must sit behind the SAME basicauth as /calib and /calib-label — it is a human
# page that POSTs with no token. If your Caddy blanket-protects non-/api/ non-/live/ paths this is
# automatic; if it lists paths explicitly, add /calib-roi* to the matcher.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[calib-roi] $*"; }
say "REV=roi-wizard-1  (draws DOOR + PANEL boxes -> roi.json in FRAME px; door_calib reads it)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in calib_roi_api.py apply_calib_roi_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# The web app WRITES roi.json into the calib tree (same requirement calib-label already established).
CALIB=/var/lib/liftlab/calib
mkdir -p "$CALIB"
chown -R "$OWNER:$OWNER" "$CALIB" 2>/dev/null || true
chmod -R g+w "$CALIB" 2>/dev/null || true
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

$PY -m py_compile /tmp/calib_roi_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/calib_roi_api.py "$APP/calib_roi_api.py"
# Optional companions: the back-link, the dash setup links. Only installed if handed to us, and each
# is compile-checked first — a bad optional file must not take the app down with it.
for f in calib_label_api.py dash_api.py; do
  if [ -f "/tmp/$f" ]; then
    $PY -m py_compile "/tmp/$f" || { say "$f failed to compile — NOT installing it"; continue; }
    install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"; say "installed $f"
  fi
done
# door_calib.py is the CONSUMER of roi.json. It lives wherever calibration runs; install it beside the
# app if that copy exists, and say plainly when it does not — a wizard whose output nothing reads is
# worse than no wizard, because it looks finished.
if [ -f /tmp/door_calib.py ]; then
  $PY -m py_compile /tmp/door_calib.py || { say "door_calib.py failed to compile — NOT installing"; exit 1; }
  if [ -f "$APP/door_calib.py" ]; then
    install -o "$OWNER" -g "$OWNER" -m 644 /tmp/door_calib.py "$APP/door_calib.py"
    say "installed door_calib.py (reads roi.json when the envs are unset)"
  else
    say "NOTE: no $APP/door_calib.py — calibration runs elsewhere. Copy the new door_calib.py THERE,"
    say "  or roi.json will be written by the wizard and read by nothing."
  fi
fi

# SMOKE-IMPORT (route+openapi) BEFORE touching main.py — a broken import fails HERE, ingest safe.
if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import calib_roi_api
a=FastAPI(); a.include_router(calib_roi_api.calib_roi_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_calib_roi_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
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
  say "RESULT: PASS. Draw at https://lift.gargi.online/calib-roi/site-A/$CAM . MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
PAGE=$(code "$BASE/calib-roi/site-A/$CAM"); ST=$(code "$BASE/calib-roi/site-A/$CAM/state")
LBL=$(code "$BASE/calib-label/site-A/ch29"); OPS=$(code "$BASE/ops")
say "AFTER (port $PORT): /calib-roi=$PAGE  /state=$ST  /calib-label=$LBL  /ops=$OPS  (MainPID $OLDPID -> $NEWPID)"
if [ "$PAGE" = 200 ] && [ "$ST" = 200 ] && [ "$LBL" = 200 ]; then
  say "RESULT: PASS — draw ROIs at https://lift.gargi.online/calib-roi/site-A/$CAM"
  say "  Flow: draw 2 boxes -> Save -> door_calib --collect -> /calib-label -> anchor -> --build"
  say "  NOTE: for the most accurate boxes run 'door_calib --frames 1' on $CAM FIRST — that writes a"
  say "  NATIVE-resolution frame. The live-snapshot fallback is rescaled (~1.5 frame px per drawn px)."
elif [ "$PAGE" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); cloud active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
