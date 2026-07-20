#!/usr/bin/env bash
# Install /calib-label — the labeling wizard. calib_label_api.py reads/writes files only (labels.json +
# lists crops); NO cv2, NO tables, ingest untouched. Deps-free -> smoke-import -> patch main.py as root
# -> restart -> verify-or-restore. Crop images are served by the existing ops_api /calib route.
# FILES NEEDED IN /tmp: apply_calib_label.sh calib_label_api.py apply_calib_label_patch.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_calib_label.sh calib_label_api.py apply_calib_label_patch.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_calib_label.sh
#
# CADDY: /calib-label must be behind the SAME basicauth as /calib. If your Caddy default already puts
# every non-/api/, non-/live/ path behind basicauth, this is automatic. If it lists paths explicitly,
# add /calib-label* to the basicauth matcher (it's a human page — it POSTs labels with no token).
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[calib-label] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in calib_label_api.py apply_calib_label_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/calib_label_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/calib_label_api.py "$APP/calib_label_api.py"
# SMOKE-IMPORT (route+openapi) BEFORE touching main.py — a broken import/route fails HERE, ingest safe.
if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import calib_label_api
a=FastAPI(); a.include_router(calib_label_api.calib_label_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_calib_label_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
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
if [ -z "$PORT" ]; then
  say "AFTER: cloud=active (came-up verified); port unknown -> routes not HTTP-probed. NOT a failure."
  say "RESULT: PASS. Label at https://lift.gargi.online/calib-label/site-A/ch29 . MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
PAGE=$(code "$BASE/calib-label/site-A/ch29"); ST=$(code "$BASE/calib-label/site-A/ch29/state")
OPS=$(code "$BASE/ops")     # confirm the operator surface still answers
say "AFTER (port $PORT): /calib-label=$PAGE  /state=$ST  /ops=$OPS  (MainPID $OLDPID -> $NEWPID)"
if [ "$PAGE" = 200 ] && [ "$ST" = 200 ]; then
  say "RESULT: PASS — label at https://lift.gargi.online/calib-label/site-A/ch29 (Enter=save+next)."
elif [ "$PAGE" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); cloud active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
