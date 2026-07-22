#!/usr/bin/env bash
# Derive close_travel_s at gateway ingest so the durable store never holds NULL
# close_travel_s while it has both close timestamps. RUN AS ROOT ON THE VM:
#   sudo bash /tmp/apply_close_travel.sh   (needs /tmp/apply_close_travel_patch.py)
# Backup-first, pre-write compile check in the patch, restart + verify here.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[close-travel] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/apply_close_travel_patch.py ] || { echo "missing /tmp/apply_close_travel_patch.py — curl it first"; exit 2; }
[ -f "$APP/events_api.py" ] || { echo "events_api.py not at $APP"; exit 2; }

say "BEFORE: service=$(systemctl is-active liftlab-cloud) /events=$(code http://127.0.0.1:9090/events)"
sudo -u liftlab $PY /tmp/apply_close_travel_patch.py || { say "patch step failed — see message above"; exit 1; }
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

$PY -m py_compile "$APP/events_api.py" || { say "events_api.py does NOT compile — RESTORE newest $APP/events_api.py.bak.*"; exit 1; }

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
say "AFTER: service=$AC /events=$(code http://127.0.0.1:9090/events)"
if [ "$AC" = active ]; then
  say "RESULT: PASS — ingest derives close_travel_s. Watch a NEW cycle land non-NULL, THEN backfill the old rows."
else
  say "RESULT: CHECK — restore newest $APP/events_api.py.bak.*, systemctl restart liftlab-cloud"
fi
