#!/usr/bin/env bash
# Strip the fabricated compliance verdict from /events -> FACTS ONLY. RUN AS ROOT ON THE VM:
#   sudo bash /tmp/apply_events_facts.sh   (needs /tmp/apply_events_facts_patch.py)
# Backup-first + pre-write compile check in the patch; restart + verify here.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[events-facts] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/apply_events_facts_patch.py ] || { echo "missing /tmp/apply_events_facts_patch.py — curl it first"; exit 2; }
[ -f "$APP/events_api.py" ] || { echo "events_api.py not at $APP"; exit 2; }

say "BEFORE: service=$(systemctl is-active liftlab-cloud) /events=$(code http://127.0.0.1:9090/events)"
sudo -u liftlab $PY /tmp/apply_events_facts_patch.py || { say "patch step failed — see message above"; exit 1; }
$PY -m py_compile "$APP/events_api.py" || { say "events_api.py does NOT compile — RESTORE newest $APP/events_api.py.bak.*"; exit 1; }

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
H=$(curl -s --max-time 8 http://127.0.0.1:9090/events)
FACTS=$(printf '%s' "$H" | grep -c "events &middot; facts" || true)
VERDICT=$(printf '%s' "$H" | grep -cE "COMPLIANT|2\.31|Decision line" || true)
say "AFTER: service=$AC /events=$(code http://127.0.0.1:9090/events) facts_marker=$FACTS verdict_strings=$VERDICT"
if [ "$AC" = active ] && [ "$FACTS" -ge 1 ] && [ "$VERDICT" = 0 ]; then
  say "RESULT: PASS — /events is facts-only: per-camera opens/closes + close-travel distribution, no verdict/threshold."
else
  say "RESULT: CHECK — restore newest $APP/events_api.py.bak.*, systemctl restart liftlab-cloud"
fi
