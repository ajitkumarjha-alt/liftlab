#!/usr/bin/env bash
# Strip the fabricated compliance verdict from /events -> FACTS ONLY. RUN AS ROOT ON THE VM:
#   sudo bash /tmp/apply_events_facts.sh   (needs /tmp/apply_events_strip_patch.py)
# Backup-first + pre-write compile check live in the patch; this does a RIGOROUS verify:
# discovers the real port, treats an unreachable page as an ERROR (never "0 markers = clean"),
# and asserts HTTP 200 + facts marker present + ZERO verdict strings, against a pre-patch baseline.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
say(){ echo "[events-facts] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/apply_events_strip_patch.py ] || { echo "missing /tmp/apply_events_strip_patch.py — curl it first"; exit 2; }
[ -f "$APP/events_api.py" ] || { echo "events_api.py not at $APP"; exit 2; }

# --- 1) discover the real port from the unit (never hardcode) ---
PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl show "$SVC" -p ExecStart --value 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || { say "ERROR: could not parse the port from $SVC — set it: PORT=<n>"; exit 2; }
URL="http://127.0.0.1:$PORT/events"
say "service=$SVC port=$PORT url=$URL"

# probe -> "CODE<TAB>BYTES<TAB>VERDICTS<TAB>FACTS"  (CODE=000 if unreachable)
probe(){
  local raw code body
  raw=$(curl -s --max-time 8 -w $'\n%{http_code}' "$URL" 2>/dev/null || true)
  code=$(printf '%s' "$raw" | tail -n1)
  body=$(printf '%s' "$raw" | sed '$d')
  printf '%s\t%s\t%s\t%s' \
    "${code:-000}" \
    "$(printf '%s' "$body" | wc -c)" \
    "$(printf '%s' "$body" | grep -cE 'NON-COMPLIANT|COMPLIANT|Decision line|DECISION_LINE_S' || true)" \
    "$(printf '%s' "$body" | grep -cE '[0-9]+ opens|openings' || true)"
}

# --- 4) BASELINE before patching: page MUST be reachable; record the verdict count (expect 3) ---
IFS=$'\t' read -r B_CODE B_LEN B_VERD B_FACTS <<<"$(probe)"
say "BEFORE: http=$B_CODE bytes=$B_LEN verdict_strings=$B_VERD facts_markers=$B_FACTS"
if [ "$B_CODE" != 200 ]; then
  say "ERROR: /events NOT reachable at :$PORT before patching (http=$B_CODE, ${B_LEN}B). That's a dead URL, NOT a clean page — wrong port or service down. Aborting, no change."
  exit 1
fi

# --- apply the strip + compile ---
sudo -u liftlab "$PY" /tmp/apply_events_strip_patch.py || { say "patch step failed — see message above"; exit 1; }
"$PY" -m py_compile "$APP/events_api.py" || { say "events_api.py does NOT compile — RESTORE newest $APP/events_api.py.bak.*"; exit 1; }
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

systemctl restart "$SVC"; sleep 3
AC=$(systemctl is-active "$SVC")

# --- 2,3) AFTER: distinguish clean page from NO page; assert all three separately ---
IFS=$'\t' read -r A_CODE A_LEN A_VERD A_FACTS <<<"$(probe)"
say "AFTER: service=$AC http=$A_CODE bytes=$A_LEN verdict_strings=$A_VERD facts_markers=$A_FACTS"
if [ "$A_CODE" != 200 ]; then
  say "RESULT: CHECK — /events UNREACHABLE after restart (http=$A_CODE). NOT a clean strip. Restore newest $APP/events_api.py.bak.* and: systemctl restart $SVC"; exit 1
fi
if [ "$A_FACTS" -lt 1 ]; then
  say "RESULT: CHECK — page 200 but MISSING the facts distribution marker (opens/openings). Strip may not have rendered. Restore + investigate."; exit 1
fi
if [ "$A_VERD" -ne 0 ]; then
  say "RESULT: CHECK — page 200 but still has $A_VERD verdict string(s) (expected 0). Strip incomplete — restore + send the ANCHOR(S) NOT FOUND line."; exit 1
fi
say "RESULT: PASS — 200 + facts marker present + verdict_strings ${B_VERD}->0 (real delta). /events is facts-only."
