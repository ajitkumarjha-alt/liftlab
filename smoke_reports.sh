#!/usr/bin/env bash
# REPORTS GATE — does the export actually work, against the STAGED code, before install?
#
# WHY THIS SHAPE. Four incidents this week came from gates that checked the wrong layer:
#   `bash -n` passes      != the script starts        (relay_soak, unbound variable)
#   the module imports    != the handler runs         (ops_api, NameError on _f)
#   the endpoint 200s     != the row landed           (ingest gate)
# For an export, the equivalent trap is that POST /reports/submit returns 200 the instant it inserts
# a queued row — it 200s just as happily when the worker cannot start, when the build raises, and
# when the file is never written. So this gate does the whole round trip:
#
#   submit -> poll to a terminal state -> download the bytes -> OPEN the workbook
#
# and only then calls it a pass. It also runs a NEGATIVE CONTROL: an export pointed at a broken
# database must surface as a FAILED job carrying its real error text — not a job that hangs in
# 'running' forever, which is the failure mode that would quietly wedge the queue for everyone.
#
# Run: bash smoke_reports.sh <staged-dir> [app-dir]
set -uo pipefail
STAGED="${1:?usage: smoke_reports.sh <staged-dir> [app-dir]}"
APP="${2:-/opt/liftlab-b3/cloud}"
PY="$APP/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)
GDB="${GATEWAY_DB:-/var/lib/liftlab/gateway.db}"

TMP=$(mktemp -d); PIDS=""
# This gate builds a REAL workbook from the REAL database, so its scratch tree holds resident
# movement data for the length of the run. mktemp -d is 0700, but assert it rather than trust it —
# on a world-readable /tmp the difference between 0700 and 0755 is the whole protection.
chmod 700 "$TMP"
PERM=$(stat -c %a "$TMP")
[ "$PERM" = 700 ] || { echo "  FAIL  scratch dir is $PERM, refusing to build resident data in it"; exit 2; }
cleanup(){ for p in $PIDS; do kill -9 "$p" 2>/dev/null; done; rm -rf "$TMP"; }
trap cleanup EXIT
PASS=0; FAIL=0
ok(){  echo "  PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

echo "reports gate: staged=$STAGED  app=$APP  gateway_db=$GDB"
[ -r "$GDB" ] || { echo "  FAIL  cannot read $GDB — the gate needs a real DB to export from"; exit 2; }

cat > "$TMP/smoke_app.py" <<'PYAPP'
import os, sys
sys.path.insert(0, os.environ["SMOKE_STAGED"])
sys.path.insert(1, os.environ["SMOKE_APP"])
from fastapi import FastAPI
import reports_api, dash_api
app = FastAPI()
app.include_router(reports_api.reports_router)
app.include_router(dash_api.dash_router)
reports_api.start_supervisor()
sys.stderr.write("SMOKE-MODULE reports_api -> %s\n" % reports_api.__file__)
sys.stderr.write("SMOKE-MODULE runner      -> %s\n" % reports_api.RUNNER)
PYAPP

free_port(){ for p in $(seq "$1" $(( $1 + 40 ))); do
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && exec 3<&- || { echo "$p"; return; }; done; echo 0; }

# $1=port $2=jobsdir $3=gateway_db $4=queue_cap
start_app(){
  local port="$1" d="$2" gdb="$3" cap="$4"
  mkdir -p "$d/out"
  SMOKE_STAGED="$STAGED" SMOKE_APP="$APP" \
  GATEWAY_DB="$gdb" REPORTS_DB="$d/reports.db" REPORTS_DIR="$d/out" \
  REPORT_QUEUE_CAP="$cap" REPORT_TIMEOUT_S="${GATE_TIMEOUT_S:-240}" REPORT_MIN_FREE_GB=0.1 \
  DASH_GW="${DASH_GW:-site-A}" PYTHONPATH="$TMP:$STAGED:$APP" \
    "$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$port" --log-level warning \
    > "$d/uvicorn.log" 2>&1 &
  PIDS="$PIDS $!"
  for i in $(seq 1 60); do
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/openapi.json" && return 0
    sleep 0.5
  done
  return 1
}

MAIN_PORT=$(free_port 18200); [ "$MAIN_PORT" != 0 ] || { echo "  FAIL no free port"; exit 2; }
mkdir -p "$TMP/main"
if ! start_app "$MAIN_PORT" "$TMP/main" "$GDB" 3; then
  echo "  FAIL  scratch app never started — the staged code does not import."
  tail -25 "$TMP/main/uvicorn.log" | sed 's/^/    /'; exit 1
fi
grep '^SMOKE-MODULE' "$TMP/main/uvicorn.log" | sed 's/^/  /'
if grep -q "SMOKE-MODULE reports_api -> $STAGED" "$TMP/main/uvicorn.log"; then
  ok "modules loaded from the STAGED dir (not the installed copy)"
else
  bad "loaded the INSTALLED module — this gate would be testing the wrong code"
fi

MD5_BEFORE=$(md5sum "$GDB" | cut -d' ' -f1)

# ── the round trip ───────────────────────────────────────────────────────────
echo
echo "== full round trip: submit -> poll -> download -> open =="
# A short range keeps the gate fast; the 7d/30d runs are the acceptance proof, not the gate.
TO=$(date -u -d 'today' +%Y-%m-%d); FROM=$(date -u -d '1 day ago' +%Y-%m-%d)
RESP=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
        -d "{\"from\":\"$FROM\",\"to\":\"$TO\"}" "http://127.0.0.1:$MAIN_PORT/reports/submit")
JOB=$(printf '%s' "$RESP" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
if [ -n "$JOB" ]; then ok "submit accepted -> job #$JOB"; else bad "submit failed: $RESP"; fi

if [ -n "$JOB" ]; then
  ST=""; ELAPSED=0
  for i in $(seq 1 120); do
    J=$(curl -s --max-time 10 "http://127.0.0.1:$MAIN_PORT/reports/job/$JOB")
    ST=$(printf '%s' "$J" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    case "$ST" in done|failed|expired) break;; esac
    sleep 2; ELAPSED=$((ELAPSED+2))
  done
  if [ "$ST" = done ]; then ok "job reached done in ~${ELAPSED}s"
  else
    bad "job did not complete (status=$ST after ${ELAPSED}s)"
    printf '%s' "$J" | "$PY" -c "import sys,json;print('    error_text:',(json.load(sys.stdin).get('error_text') or '')[:600])" 2>/dev/null
    tail -20 "$TMP/main/out/job-$JOB.log" 2>/dev/null | sed 's/^/    /'
  fi

  if [ "$ST" = done ]; then
    CODE=$(curl -s -o "$TMP/dl.xlsx" -w '%{http_code}' --max-time 120 \
           "http://127.0.0.1:$MAIN_PORT/reports/download/$JOB")
    if [ "$CODE" = 200 ] && [ -s "$TMP/dl.xlsx" ]; then
      ok "download: HTTP 200, $(stat -c %s "$TMP/dl.xlsx") bytes"
    else
      bad "download: HTTP $CODE, $(stat -c %s "$TMP/dl.xlsx" 2>/dev/null || echo 0) bytes"
    fi
    # A 200 with bytes is still not proof it is a workbook. Open it.
    if "$PY" - "$TMP/dl.xlsx" <<'PYX'
import sys
from openpyxl import load_workbook
wb = load_workbook(sys.argv[1], read_only=True)
names = wb.sheetnames
assert names, "workbook has no sheets"
ws = wb[names[0]]
assert ws.max_row and ws.max_row > 1, f"first sheet {names[0]!r} has {ws.max_row} rows"
print("    sheets:", ", ".join(names[:8]), "..." if len(names) > 8 else "")
print("    first sheet rows:", ws.max_row)
PYX
    then ok "workbook OPENS and has populated sheets"
    else bad "downloaded bytes are not a readable workbook"
    fi
  fi
fi

MD5_AFTER=$(md5sum "$GDB" | cut -d' ' -f1)
if [ "$MD5_BEFORE" = "$MD5_AFTER" ]; then ok "gateway.db md5 UNCHANGED by the export ($MD5_BEFORE)"
else bad "gateway.db md5 CHANGED: $MD5_BEFORE -> $MD5_AFTER — the report path is not read-only"; fi

# ── negative control ─────────────────────────────────────────────────────────
echo
echo "== negative control: a broken export must FAIL loudly, not hang =="
NEG_PORT=$(free_port 18260); mkdir -p "$TMP/neg"
if start_app "$NEG_PORT" "$TMP/neg" "$TMP/neg/does-not-exist.db" 3; then
  R=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
      -d "{\"from\":\"$FROM\",\"to\":\"$TO\"}" "http://127.0.0.1:$NEG_PORT/reports/submit")
  NJ=$(printf '%s' "$R" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('job_id',''))" 2>/dev/null)
  NST=""; NERR=""
  for i in $(seq 1 60); do
    J=$(curl -s --max-time 10 "http://127.0.0.1:$NEG_PORT/reports/job/$NJ")
    NST=$(printf '%s' "$J" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
    case "$NST" in done|failed|expired) break;; esac
    sleep 2
  done
  NERR=$(curl -s "http://127.0.0.1:$NEG_PORT/reports/job/$NJ" | \
         "$PY" -c "import sys,json;print((json.load(sys.stdin).get('error_text') or '').strip()[:300])" 2>/dev/null)
  if [ "$NST" = failed ]; then ok "broken export -> status=failed (did not hang)"
  else bad "broken export -> status=$NST (expected failed; a hung job wedges the queue)"; fi
  if [ -n "$NERR" ]; then ok "failure carries real error text: $(printf '%s' "$NERR" | head -1 | cut -c1-90)"
  else bad "failed job has EMPTY error_text — the user would see 'failed' and nothing else"; fi
else
  bad "negative-control app did not start"
fi

# ── refusal path ─────────────────────────────────────────────────────────────
echo
echo "== refusals are explicit messages, never silent drops =="
CAP_PORT=$(free_port 18320); mkdir -p "$TMP/cap"
if start_app "$CAP_PORT" "$TMP/cap" "$GDB" 0; then
  BODY=$(curl -s --max-time 20 -X POST -H 'Content-Type: application/json' \
         -d "{\"from\":\"$FROM\",\"to\":\"$TO\"}" "http://127.0.0.1:$CAP_PORT/reports/submit")
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST \
         -H 'Content-Type: application/json' \
         -d "{\"from\":\"$FROM\",\"to\":\"$TO\"}" "http://127.0.0.1:$CAP_PORT/reports/submit")
  if [ "$CODE" = 429 ]; then ok "queue-full refusal: HTTP 429"; else bad "queue-full: HTTP $CODE (expected 429)"; fi
  case "$BODY" in *"queue is full"*) ok "refusal explains itself: $(printf '%s' "$BODY" | cut -c1-100)";;
    *) bad "refusal carries no explanation: $BODY";; esac
else bad "cap app did not start"; fi

echo
echo "== validation and range analysis =="
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST -H 'Content-Type: application/json' \
    -d "{\"from\":\"$TO\",\"to\":\"$FROM\"}" "http://127.0.0.1:$MAIN_PORT/reports/submit")
[ "$C" = 400 ] && ok "reversed range rejected (400)" || bad "reversed range -> $C (expected 400)"
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST -H 'Content-Type: application/json' \
    -d "{\"from\":\"$FROM\",\"to\":\"$TO\",\"population\":\"abc\"}" "http://127.0.0.1:$MAIN_PORT/reports/submit")
[ "$C" = 400 ] && ok "non-numeric population rejected (400)" || bad "bad population -> $C (expected 400)"
# the instrument split must be flagged for a range that straddles it
RC=$(curl -s --max-time 15 "http://127.0.0.1:$MAIN_PORT/reports/range-check?from_ts=2026-07-19&to_ts=2026-07-23")
case "$RC" in *"INSTRUMENT SPLIT"*) ok "era-straddling range is flagged pre-submission";;
  *) bad "era straddle NOT flagged: $(printf '%s' "$RC" | cut -c1-160)";; esac
case "$RC" in *"gap"*) ok "declared gaps are reported for the range";; *) echo "  note  no gap in that window";; esac

echo
echo "== the page renders and carries nav =="
P=$(curl -s --max-time 20 "http://127.0.0.1:$MAIN_PORT/reports")
case "$P" in *"Export a report"*) ok "/reports renders";; *) bad "/reports did not render";; esac
case "$P" in *"__NAV__"*) bad "nav placeholder left unsubstituted in the page";; *) ok "nav substituted";; esac

echo
echo "== output permissions (resident movement data) =="
OUTF=$(find "$TMP/main/out" -name '*.xlsx' 2>/dev/null | head -1)
if [ -n "$OUTF" ]; then
  DP=$(stat -c %a "$TMP/main/out"); FP=$(stat -c %a "$OUTF")
  case "$DP" in 750|700) ok "output dir is $DP (not world-readable)";; *) bad "output dir is $DP";; esac
  case "$FP" in 640|600) ok "workbook is $FP (not world-readable)";; *) bad "workbook is $FP";; esac
else
  bad "no workbook found to check permissions on"
fi

echo
echo "== reports gate: $PASS passed, $FAIL failed =="
[ "$FAIL" = 0 ]
