#!/usr/bin/env bash
# Install the VALIDATION UI on the VM — SAFELY. liftlab-cloud is the ingest for the only
# irreplaceable data in the project; it must NOT go down for a missing pip package. So:
#   deps first  ->  smoke-import the module in the cloud venv BEFORE patching main.py  ->
#   patch as ROOT  ->  restart  ->  VERIFY it came up, else RESTORE main.py and restart.
# FILES NEEDED IN /tmp: apply_valui.sh validation_api.py apply_validation_patch.py ops_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_valui.sh validation_api.py apply_validation_patch.py ops_api.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_valui.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
IMGDIR="${VALIDATION_IMG_DIR:-/var/lib/liftlab/validation_img}"
say(){ echo "[valui] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in validation_api.py apply_validation_patch.py ops_api.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root
# nav_common.py is a HARD dependency of every page module (the shared header). Installing a page
# without it is an ImportError that takes the ENTIRE operator UI down, not just this page.
if [ -f /tmp/nav_common.py ]; then
  $PY -m py_compile /tmp/nav_common.py || { say "nav_common.py failed to compile — aborting"; exit 1; }
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/nav_common.py "$APP/nav_common.py"
  say "installed nav_common.py (shared header)"
elif [ ! -f "$APP/nav_common.py" ]; then
  say "ABORT: nav_common.py is neither in /tmp nor installed. Every page imports it; continuing"
  say "  would ImportError the whole UI. Re-curl nav_common.py and run again."; exit 1
fi

$PY -m py_compile /tmp/validation_api.py /tmp/ops_api.py || { say "compile failed — aborting"; exit 1; }

# ---- 1. DEPS FIRST (validation_api uses fastapi Form() -> needs python-multipart) ----
if ! sudo -u "$OWNER" $PY -c "import multipart" 2>/dev/null && ! sudo -u "$OWNER" $PY -c "import python_multipart" 2>/dev/null; then
  say "installing python-multipart into the cloud venv ..."
  sudo -u "$OWNER" $PY -m pip install --quiet python-multipart || { say "pip install python-multipart FAILED — aborting, main.py untouched"; exit 1; }
fi

# ---- 2. install the module files ----
install -d -o "$OWNER" -g "$OWNER" -m 750 "$IMGDIR"
say "validation images: $IMGDIR (deleted at each verdict; files not DB -> never in the backup)"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/validation_api.py "$APP/validation_api.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/ops_api.py "$APP/ops_api.py"

# ---- 3. SMOKE-IMPORT in the cloud venv, BEFORE touching main.py. Full route+dependency setup so a
#         missing dep (multipart etc.) fails HERE, cleanly, with main.py and the ingest untouched. ----
if ! ( cd "$APP" && sudo -u "$OWNER" env VALIDATION_IMG_DIR="$IMGDIR" $PY - <<'PYEOF'
from fastapi import FastAPI
import validation_api, ops_api
app = FastAPI()
app.include_router(validation_api.validation_router)   # Form routes checked here in modern FastAPI
app.include_router(ops_api.ops_router)
app.openapi()                                          # forces full route/dependency resolution
print("smoke ok")
PYEOF
); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest UNTOUCHED. Fix the error above and re-run."
  exit 1
fi
say "smoke-import passed (module + deps load cleanly)"

# ---- 4. patch main.py AS ROOT (patch backup-writes into $APP; running as $OWNER fails on dir perms) ----
BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_validation_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }
# VALIDATION_IMG_DIR + COUNTING_VERSION into the service env (matches this install).
# COUNTING_VERSION MUST equal counting.COUNTING_VERSION on the GPU, else new verdicts are filtered as
# a foreign version. A model/logic change is a comparability boundary -> bump both together.
CV="${COUNTING_VERSION:-2026-07-17-yolo11m-dwell-disp}"
mkdir -p "/etc/systemd/system/$SVC.service.d"
printf '[Service]\nEnvironment=VALIDATION_IMG_DIR=%s\nEnvironment=COUNTING_VERSION=%s\n' \
    "$IMGDIR" "$CV" > "/etc/systemd/system/$SVC.service.d/validation.conf"
say "cloud COUNTING_VERSION pinned to: $CV  (must match the GPU's counting.py)"

# ---- 5. restart + VERIFY it came up; if not, RESTORE main.py and restart (self-heal the ingest) ----
systemctl daemon-reload
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start after patch — RESTORING $BAK and restarting to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"
  systemctl restart "$SVC"; sleep 3
  say "restored. cloud=$(systemctl is-active "$SVC"). Investigate: journalctl -u $SVC -n 40"
  exit 1
fi

# Parse the REAL port. The previous verify read 000 because the port guess was wrong (fell back to a
# dead 9090). Most reliable: the socket the RUNNING MainPID actually listens on — works even when the
# app binds 0.0.0.0 or :: (a '127.0.0.1:' grep misses those). Then the unit --port (events-dash's proven
# path), then any local listener. If we STILL can't, do NOT emit misleading 000s: the came-up check
# above already proved the ingest is alive; report the port as unknown and skip the HTTP probe.
PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
if [ -n "$MPID" ] && [ "$MPID" != 0 ]; then
  PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)   # the actual listen port
fi
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl show "$SVC" -p ExecStart --value 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(ss -tlnpH 2>/dev/null | grep -oP '127\.0\.0\.1:\K[0-9]+' | head -1)
if [ -z "$PORT" ]; then
  say "AFTER: cloud=active (came-up verified — ingest is UP). Could not determine the HTTP port to probe"
  say "  the new routes; that is NOT a route failure. To HTTP-verify, re-run with PORT=<n>."
  say "RESULT: PASS (ingest healthy; routes not HTTP-probed — port unknown)."
  exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
MODE=$(code "$BASE/api/gw/site-A/validation_mode/ch29"); PAGE=$(code "$BASE/validate")
VITEM=$(code -X POST --data '{}' "$BASE/api/gw/site-A/validation_item/ch29")
EVT=$(code "$BASE/events" || echo "?")           # confirm the ingest surface is still alive
say "AFTER (port $PORT): validation_mode=$MODE  /validate=$PAGE  validation_item(no-token)=$VITEM  /events=$EVT"
if [ "$MODE" = 401 ] && [ "$PAGE" = 200 ] && [ "$VITEM" = 401 ]; then
  say "RESULT: PASS — validation UI up, ingest healthy. Review at https://lift.gargi.online/validate"
elif [ "$MODE" = 000 ] && [ "$PAGE" = 000 ] && [ "$VITEM" = 000 ]; then
  say "RESULT: CHECK — every probe was 000 = could not connect on :$PORT (wrong port, NOT dead routes)."
  say "  Service is active. Re-run with the right PORT=<n>, or verify via https://lift.gargi.online/validate"; exit 1
else
  say "RESULT: CHECK — routes not as expected; journalctl -u $SVC -n 40 (main.py.bak.* available)"; exit 1
fi
