#!/usr/bin/env bash
# Install /dash — the ONE aggregated page. dash_api.py is READ-ONLY (writes nothing, creates no
# tables); it just reads the existing gateway.db and renders. Deps-first -> smoke-import -> patch
# main.py as root -> restart -> verify-or-restore. /ops /events /validate /pihealth are untouched.
# FILES NEEDED IN /tmp: apply_dash.sh dash_api.py apply_dash_patch.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_dash.sh dash_api.py apply_dash_patch.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_dash.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[dash] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in dash_api.py apply_dash_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

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

$PY -m py_compile /tmp/dash_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"
# SMOKE-IMPORT (route+openapi) BEFORE touching main.py — a broken import/route fails HERE, ingest safe.
if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import dash_api
a=FastAPI(); a.include_router(dash_api.dash_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_dash_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }

OLDPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
# ROOT ROUTE CONFLICT. dash_api now serves "/" as a 307 to /dash. FastAPI matches the FIRST
# registered route, so if main.py defines its own "/" it keeps it and the redirect is DEAD CODE —
# silently, which is the worst outcome. Say so here rather than let it look deployed.
if grep -qE '^\s*@app\.(get|route)\("/"' "$APP/main.py"; then
  say "NOTE: main.py defines its own \"/\" route, which WINS over dash_api's redirect."
  say "  The / -> /dash redirect will NOT take effect until that handler is changed or removed."
  say "  Whatever it serves today (the Pi fleet overview) should move to its own path, and"
  say "  DASH_FLEET_URL set to it so the \"Pi fleet\" link on /dash points there."
else
  say "root route: dash_api serves / -> /dash (main.py defines no competing \"/\")"
fi
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi

# port off the RUNNING MainPID's listening socket (bind-agnostic), then unit --port
PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
if [ -n "$MPID" ] && [ "$MPID" != 0 ]; then
  PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)
fi
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
NEWPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
if [ -z "$PORT" ]; then
  say "AFTER: cloud=active (came-up verified); port unknown -> routes not HTTP-probed. NOT a failure."
  say "RESULT: PASS (verify at https://lift.gargi.online/dash). MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
PAGE=$(code "$BASE/dash"); DATA=$(code "$BASE/dash/site-A/data")
EV=$(code "$BASE/events")     # confirm the deep views still answer (ingest surface healthy)
say "AFTER (port $PORT): /dash=$PAGE  /dash/site-A/data=$DATA  /events=$EV  (MainPID $OLDPID -> $NEWPID)"
if [ "$PAGE" = 200 ] && [ "$DATA" = 200 ]; then
  say "RESULT: PASS — /dash live at https://lift.gargi.online/dash (tabbed by camera, 15s refresh)."
elif [ "$PAGE" = 000 ] && [ "$DATA" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); cloud active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
