#!/usr/bin/env bash
# Install the gw_door_event stream + /floorcheck. door_event_api.py CREATEs its own tables in gateway.db
# (gw_door_event, floor_sample) on first use; the existing gw_event/transit ingest is untouched. Bearer
# ingest for the GPU; /floorcheck is a human page (Caddy basicauth). Deps-free -> smoke-import -> patch
# main.py as root -> restart -> verify-or-restore.
# FILES NEEDED IN /tmp: apply_door_event.sh door_event_api.py apply_door_event_patch.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_door_event.sh door_event_api.py apply_door_event_patch.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_door_event.sh
# CADDY: /floorcheck behind the SAME basicauth as /ops (human page). /api/gw/*/door_event and
# /api/gw/*/floorcheck are token endpoints (GPU) — leave under the token path, not basicauth.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[door-event] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in door_event_api.py apply_door_event_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# nav_common.py is a HARD dependency of every page module (the shared header). Installing a page
# module without it is an ImportError that takes the ENTIRE operator UI down, not just this page.
if [ -f /tmp/nav_common.py ]; then
  $PY -m py_compile /tmp/nav_common.py || { say "nav_common.py failed to compile — aborting"; exit 1; }
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/nav_common.py "$APP/nav_common.py"
  say "installed nav_common.py (shared header)"
elif [ ! -f "$APP/nav_common.py" ]; then
  say "ABORT: nav_common.py is neither in /tmp nor installed. Every page imports it; continuing"
  say "  would ImportError the whole UI. Re-curl nav_common.py and run again."; exit 1
fi

$PY -m py_compile /tmp/door_event_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/door_event_api.py "$APP/door_event_api.py"
if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import door_event_api
a=FastAPI(); a.include_router(door_event_api.door_event_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_door_event_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
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
  say "AFTER: cloud=active; port unknown -> not HTTP-probed. NOT a failure."
  say "RESULT: PASS. Spot-check at https://lift.gargi.online/floorcheck/site-A/ch29 . MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
PAGE=$(code "$BASE/floorcheck/site-A/ch29"); DATA=$(code "$BASE/floorcheck/site-A/ch29/data"); OPS=$(code "$BASE/ops")
say "AFTER (port $PORT): /floorcheck=$PAGE  /data=$DATA  /ops=$OPS  (MainPID $OLDPID -> $NEWPID)"
if [ "$PAGE" = 200 ] && [ "$DATA" = 200 ]; then
  say "RESULT: PASS — /floorcheck live. Set GPU_DOOR=1 + geometry on the GPU to start the stream."
elif [ "$PAGE" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); cloud active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
