#!/usr/bin/env bash
# /events dashboard upgrade (DESC + latest-100 cap + 30s refresh) + /events.csv download.
# RUN AS ROOT ON THE VM: sudo bash /tmp/apply_events_dashboard.sh
#   (needs /tmp/apply_events_dashboard_patch.py)
# Discovers the port; unreachable = ERROR (never "0 markers = clean"); asserts /events
# 200 + facts marker + ZERO verdict strings, AND /events.csv 200 + header row — all
# against a pre-patch baseline.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
say(){ echo "[events-dash] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/apply_events_dashboard_patch.py ] || { echo "missing /tmp/apply_events_dashboard_patch.py"; exit 2; }
[ -f "$APP/events_api.py" ] || { echo "events_api.py not at $APP"; exit 2; }

PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl show "$SVC" -p ExecStart --value 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || { say "ERROR: could not parse port from $SVC — set PORT=<n>"; exit 2; }
BASE="http://127.0.0.1:$PORT"
say "service=$SVC port=$PORT"

httpcode(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$1" 2>/dev/null || echo 000; }
verdicts(){ curl -s --max-time 8 "$1" 2>/dev/null | grep -cE 'NON-COMPLIANT|COMPLIANT|2\.31|Decision line' || true; }
facts(){    curl -s --max-time 8 "$1" 2>/dev/null | grep -cE '[0-9]+ opens|openings' || true; }
csvhdr(){   curl -s --max-time 8 "$1" 2>/dev/null | head -1 | grep -c '^id,gateway_id,camera' || true; }

B_CODE=$(httpcode "$BASE/events")
say "BEFORE: /events http=$B_CODE verdict_strings=$(verdicts "$BASE/events")"
[ "$B_CODE" = 200 ] || { say "ERROR: /events unreachable at :$PORT before patching (http=$B_CODE) — dead URL, NOT a clean page. Aborting, no change."; exit 1; }

sudo -u liftlab "$PY" /tmp/apply_events_dashboard_patch.py || { say "patch step failed"; exit 1; }
"$PY" -m py_compile "$APP/events_api.py" || { say "events_api.py does NOT compile — RESTORE newest $APP/events_api.py.bak.*"; exit 1; }
systemctl restart "$SVC"; sleep 3
AC=$(systemctl is-active "$SVC")

E_CODE=$(httpcode "$BASE/events"); E_VERD=$(verdicts "$BASE/events"); E_FACT=$(facts "$BASE/events")
C_CODE=$(httpcode "$BASE/events.csv"); C_HDR=$(csvhdr "$BASE/events.csv")
say "AFTER: service=$AC /events http=$E_CODE facts=$E_FACT verdict=$E_VERD | /events.csv http=$C_CODE header=$C_HDR"
FAIL=""
[ "$E_CODE" = 200 ] || FAIL="$FAIL /events-unreachable(http=$E_CODE)"
[ "${E_FACT:-0}" -ge 1 ] || FAIL="$FAIL /events-missing-facts"
[ "${E_VERD:-1}" -eq 0 ] || FAIL="$FAIL /events-has-$E_VERD-verdict-strings"
[ "$C_CODE" = 200 ] || FAIL="$FAIL /events.csv-unreachable(http=$C_CODE)"
[ "${C_HDR:-0}" -ge 1 ] || FAIL="$FAIL /events.csv-missing-header"
if [ -z "$FAIL" ]; then
  say "RESULT: PASS — facts page (DESC, latest-100, 30s refresh, no verdict) + /events.csv download live."
else
  say "RESULT: CHECK —$FAIL. Restore newest $APP/events_api.py.bak.* and: systemctl restart $SVC"
  exit 1
fi
