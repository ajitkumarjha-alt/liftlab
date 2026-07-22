#!/usr/bin/env bash
# Install watch_status ingest + light /pihealth monitoring (survey_api.py wholesale).
# RUN AS ROOT ON THE VM: sudo bash /tmp/apply_watchstatus.sh   (needs /tmp/survey_api.py)
# Backup-first, compile-check, restart, verify /pihealth + /watchstatus + ingest-exists.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
say(){ echo "[watchstatus] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/survey_api.py ] || { echo "missing /tmp/survey_api.py — curl it first"; exit 2; }
[ -f "$APP/survey_api.py" ] || { echo "survey_api.py not at $APP"; exit 2; }

PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=9090
BASE="http://127.0.0.1:$PORT"
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }

# the shared-header guard needs these; this script did not define them
OWNER=liftlab
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

$PY -m py_compile /tmp/survey_api.py || { say "new survey_api.py does NOT compile — aborting, no change"; exit 1; }
cp "$APP/survey_api.py" "$APP/survey_api.py.bak.$(date +%Y%m%d-%H%M%S)"
install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py (backup written)"

systemctl restart "$SVC"; sleep 3
AC=$(systemctl is-active "$SVC")
PH=$(code "$BASE/pihealth/site-A")
WS=$(code "$BASE/watchstatus/site-A")
ING=$(code -X POST "$BASE/api/gw/site-A/watch_status")   # no token: 401 if route EXISTS, 404 if missing
say "AFTER: service=$AC /pihealth=$PH /watchstatus=$WS POST/watch_status(no-token)=$ING"
if [ "$AC" = active ] && [ "$PH" = 200 ] && [ "$WS" = 200 ] && [ "$ING" = 401 ]; then
  say "RESULT: PASS — watch_status ingest live (401 w/o token = route exists), /watchstatus JSON + light /pihealth up."
  say "The Pi's reader already POSTs here every ~15s — data should start appearing on /pihealth/site-A shortly."
else
  say "RESULT: CHECK — restore newest $APP/survey_api.py.bak.* and: systemctl restart $SVC  (ING=404 => route didn't register)"
  exit 1
fi
