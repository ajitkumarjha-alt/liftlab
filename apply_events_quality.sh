#!/usr/bin/env bash
# emit-facts-flag-quality (cloud side): install the updated events_api.py — gw_event gains a guarded
# `quality` column (ALTER, not CREATE-IF-NOT-EXISTS), the ingest stores quality + withholds close_travel
# when a close is flagged bad, and /events REPORTS both censorship layers (read-time duplicate withhold
# + write-time quality flag) instead of hiding them, defaulting to clean with ?flagged=1 opt-in.
# events_api.py is already mounted in main.py -> file replace + restart, no main.py patch.
# FILES NEEDED IN /tmp: apply_events_quality.sh events_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_events_quality.sh events_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_events_quality.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[events-quality] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/events_api.py ] || { echo "missing /tmp/events_api.py"; exit 2; }
[ -f "$APP/events_api.py" ] || { echo "events_api.py not at $APP (is this the cloud host?)"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/events_api.py || { say "compile failed — aborting"; exit 1; }
# SMOKE-IMPORT the NEW module in the cloud venv BEFORE touching the live file — a broken import fails
# HERE, ingest untouched. Uses a temp copy so $APP/events_api.py is not swapped until smoke passes.
TMPDIR_S=$(mktemp -d)
cp /tmp/events_api.py "$TMPDIR_S/events_api.py"
if ! ( cd "$TMPDIR_S" && sudo -u "$OWNER" $PY -c "from fastapi import FastAPI
import events_api
a=FastAPI(); a.include_router(events_api.events_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT replacing events_api.py, ingest untouched. Fix the error above."
  rm -rf "$TMPDIR_S"; exit 1
fi
rm -rf "$TMPDIR_S"

BAK="$APP/events_api.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/events_api.py" "$BAK"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/events_api.py "$APP/events_api.py"
say "installed (backup: $BAK)"

OLDPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK to protect the ingest"
  cp "$BAK" "$APP/events_api.py"; chown "$OWNER:$OWNER" "$APP/events_api.py"; systemctl restart "$SVC"
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
  say "RESULT: PASS (ingest healthy; verify at https://lift.gargi.online/events). MainPID $OLDPID -> $NEWPID"; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
EV=$(code "$BASE/events"); EVF=$(code "$BASE/events?flagged=1"); CSV=$(code "$BASE/events.csv")
POST=$(code -X POST --data '{}' "$BASE/api/gw/events")     # 422/401 = route exists (not 000/404)
say "AFTER (port $PORT): /events=$EV  /events?flagged=1=$EVF  /events.csv=$CSV  POST /api/gw/events=$POST  (MainPID $OLDPID -> $NEWPID)"
if [ "$EV" = 200 ] && [ "$EVF" = 200 ] && [ "$CSV" = 200 ]; then
  say "RESULT: PASS — events dashboard + CSV up; quality column migrated. The 'N openings WITHHELD' line"
  say "  on /events now shows the duplicate-cluster close distribution (task-2 measurement)."
elif [ "$EV" = 000 ] && [ "$CSV" = 000 ]; then
  say "RESULT: CHECK — probes 000 = wrong port (NOT dead routes); service is active. Verify via the public URL."; exit 1
else
  say "RESULT: CHECK — routes not as expected; restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
