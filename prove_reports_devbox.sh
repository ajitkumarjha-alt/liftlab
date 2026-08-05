#!/usr/bin/env bash
# ACCEPTANCE PROOF for /reports on dev-box — the five items the gateway run never reached, plus the
# two that only exist here because the data source is a restore.
#
# Ordered so the destructive ones come last and each restores good state behind it. The final act is
# always a real refresh, so the box is left serving current data whatever happened in between.
#
#   sudo bash /tmp/prove_reports_devbox.sh
set -uo pipefail
BASE=http://127.0.0.1:9091
PUB="${PUBLIC_URL:-https://dev.gargi.online}"
VAR=/var/lib/liftlab
JOBS=$VAR/reports.db
OUT=$VAR/reports
STATE=$VAR/restore_state.json
LOCK=$VAR/db.lock
APP=/opt/liftlab-reports
PY=$APP/.venv/bin/python
RUN_USER="${RUN_USER:-ajit_kumarjha_lodhagroup_com}"
PASS=0; FAIL=0
ok(){  echo "  PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

jq_(){ "$PY" -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }
submit(){ curl -s --max-time 30 -X POST -H 'Content-Type: application/json' \
            -d "{\"from\":\"$1\",\"to\":\"$2\"}" "$BASE/reports/submit"; }
poll(){ local t=0
  while [ "$t" -lt "${2:-1200}" ]; do
    S=$(curl -s --max-time 10 "$BASE/reports/job/$1" | jq_ status)
    case "$S" in done|failed|expired) echo "$S"; return;; esac
    sleep 3; t=$((t+3))
  done; echo timeout; }
run_refresh(){  # $1 = config path
  sudo -u "$RUN_USER" env HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)" \
    LIFTLAB_DB=$VAR/gateway.db LIFTLAB_DB_LOCK=$LOCK LIFTLAB_RESTORE_STATE=$STATE \
    LITESTREAM_CONFIG="$1" "$APP/refresh_reports_db.sh"; }

echo "=================== 1. ERA-STRADDLING RANGE ==================="
RC=$(curl -s --max-time 20 "$BASE/reports/range-check?from_ts=2026-07-19&to_ts=2026-07-23")
case "$RC" in *"INSTRUMENT SPLIT"*) ok "era straddle warned BEFORE submission";;
  *) bad "no era warning: $(echo "$RC" | cut -c1-140)";; esac
J=$(submit 2026-07-19 2026-07-23 | jq_ job_id); S=$(poll "$J" 1200)
[ "$S" = done ] && ok "era-straddling export built (job #$J)" || bad "era-straddling export -> $S"
if [ "$S" = done ]; then
  curl -s -o /tmp/pera.xlsx --max-time 180 "$BASE/reports/download/$J"
  "$PY" - /tmp/pera.xlsx <<'PY' && ok "workbook separates the eras (does not pool across the split)" \
    || bad "workbook has no era/coverage sheet for a straddling range"
import sys
from openpyxl import load_workbook
wb = load_workbook(sys.argv[1], read_only=True)
hits = [n for n in wb.sheetnames if "ERA" in n.upper() or "COVERAGE" in n.upper()]
print("    era/coverage sheets:", hits or "(none)")
wb.close(); raise SystemExit(0 if hits else 1)
PY
  rm -f /tmp/pera.xlsx
fi

echo
echo "=================== 2. EMPTY RANGE ==================="
J=$(submit 2020-01-01 2020-01-03 | jq_ job_id); S=$(poll "$J" 900)
[ "$S" = done ] && ok "empty range completed, no crash (job #$J)" || bad "empty range -> $S"
if [ "$S" = done ]; then
  curl -s -o /tmp/pempty.xlsx --max-time 180 "$BASE/reports/download/$J"
  "$PY" - /tmp/pempty.xlsx <<'PY' && ok "empty workbook states the zero explicitly" \
    || bad "empty workbook does not explain the zero"
import re, sys
from openpyxl import load_workbook
wb = load_workbook(sys.argv[1], read_only=True)
pat = re.compile(r"zero rows|no transits|no data|0 transits", re.I)
hits = [(n, c) for n in wb.sheetnames for row in wb[n].iter_rows(values_only=True)
        for c in row if isinstance(c, str) and pat.search(c)]
print("    zero-rows statements:", len(hits))
for n, c in hits[:2]: print("      [", n, "]", c.strip()[:88])
wb.close(); raise SystemExit(0 if hits else 1)
PY
  rm -f /tmp/pempty.xlsx
fi

echo
echo "=================== 3. REFRESH-vs-EXPORT LOCKING ==================="
# Start a long export, then run a refresh against it. The refresh must WAIT — publishing by atomic
# rename under a running build would leave that build reading the old inode while the page advertises
# a new restore point.
F30=$(date -d '30 days ago' +%Y-%m-%d); TODAY=$(date +%Y-%m-%d)
J=$(submit "$F30" "$TODAY" | jq_ job_id)
WPID=""
for i in $(seq 1 60); do WPID=$(pgrep -f "report_runner.py $J" | head -1); [ -n "$WPID" ] && break; sleep 2; done
if [ -n "$WPID" ]; then
  ok "export job #$J running (pid $WPID) — now racing a refresh against it"
  T0=$(date +%s)
  run_refresh /etc/liftlab/litestream-restore.yml > /tmp/.lockproof 2>&1 &
  RPID=$!
  sleep 12
  if kill -0 "$RPID" 2>/dev/null && grep -q "waiting for the db lock" /tmp/.lockproof; then
    ok "refresh is BLOCKED on the lock while the export runs (did not publish under it)"
  else
    grep -q "lock acquired" /tmp/.lockproof && bad "refresh acquired the lock DURING an export — the swap can race a build" \
      || bad "refresh neither waited nor ran: $(tail -1 /tmp/.lockproof)"
  fi
  EXP_END=""; S=$(poll "$J" 1800); EXP_END=$(date +%s)
  wait "$RPID" 2>/dev/null; REF_END=$(date +%s)
  echo "    export ended at +$((EXP_END-T0))s, refresh completed at +$((REF_END-T0))s"
  [ "$REF_END" -ge "$EXP_END" ] && ok "refresh completed only AFTER the export finished" \
    || bad "refresh finished before the export — serialization did not hold"
  [ "$S" = done ] && ok "the export itself still completed correctly under contention" \
    || bad "export ended as $S while contending with the refresh"
else
  bad "export worker never appeared — could not test locking"
fi

echo
echo "=================== 4. KILL THE WORKER MID-RUN ==================="
J=$(submit "$F30" "$TODAY" | jq_ job_id); KP=""
for i in $(seq 1 60); do KP=$(pgrep -f "report_runner.py $J" | head -1); [ -n "$KP" ] && break; sleep 2; done
if [ -n "$KP" ]; then
  kill -9 "$KP"; ok "killed worker pid $KP mid-run"
  S=$(poll "$J" 400)
  [ "$S" = failed ] && ok "job marked failed after the kill (not stuck running)" || bad "job -> $S"
  ET=$(sudo sqlite3 "$JOBS" "SELECT COALESCE(error_text,'') FROM report_job WHERE id=$J;" | head -c 100)
  [ -n "$ET" ] && ok "failure carries a reason: $ET" || bad "no error_text for the killed job"
  UP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$BASE/reports")
  [ "$UP" = 200 ] && ok "service unaffected by the kill (/reports 200)" || bad "/reports -> $UP after kill"
  # a killed job must not wedge the queue
  NJ=$(submit "$(date -d '2 days ago' +%Y-%m-%d)" "$TODAY" | jq_ job_id)
  NS=$(poll "$NJ" 900)
  [ "$NS" = done ] && ok "queue still works after a kill (job #$NJ completed)" || bad "next job -> $NS"
else
  bad "worker never appeared — could not test the kill"
fi

echo
echo "=================== 5. RETENTION SWEEP ==================="
OLD=$(sudo sqlite3 "$JOBS" "SELECT id FROM report_job WHERE status='done' ORDER BY id LIMIT 1;")
if [ -n "$OLD" ]; then
  OP=$(sudo sqlite3 "$JOBS" "SELECT output_path FROM report_job WHERE id=$OLD;")
  sudo test -e "$OP" && ok "job #$OLD has a workbook on disk to sweep" || bad "no file at $OP"
  sudo sqlite3 "$JOBS" "UPDATE report_job SET finished_at=finished_at-8*86400 WHERE id=$OLD;"
  cd "$APP" && sudo -u "$RUN_USER" env REPORTS_DB="$JOBS" REPORTS_DIR="$OUT" "$PY" -c "
import reports_api
db = reports_api._jobs(); reports_api._sweep(db); db.close(); print('    _sweep() invoked')"
  ST=$(sudo sqlite3 "$JOBS" "SELECT status FROM report_job WHERE id=$OLD;")
  [ "$ST" = expired ] && ok "job #$OLD marked expired" || bad "job #$OLD is '$ST' after the sweep"
  # THE POINT: existence, not an exit code. `rm -f` cannot fail, so it is never evidence.
  if sudo test -e "$OP"; then bad "workbook STILL ON DISK: $OP"; else ok "workbook is GONE from disk (sudo test -e)"; fi
  DC=$(curl -s -o /tmp/.exp -w '%{http_code}' --max-time 30 "$BASE/reports/download/$OLD")
  [ "$DC" = 410 ] && ok "expired download -> 410" || bad "expired download -> $DC"
  grep -qi retention /tmp/.exp && ok "410 explains why" || bad "410 carries no explanation"
  curl -s --max-time 30 "$BASE/reports/jobs?limit=10" | grep -q expired \
    && ok "the jobs list shows it as expired" || bad "jobs list does not show expired"
else
  bad "no completed job to age"
fi

echo
echo "=================== 6. UNAUTHENTICATED ACCESS ==================="
# Auth is Caddy's, so this MUST go through the public host — localhost bypasses it by design.
for p in /reports "/reports/jobs" "/reports/download/1" "/reports/data-as-of"; do
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PUB$p")
  case "$C" in 401|403) ok "unauthenticated $p -> $C";;
    000) bad "could not reach $PUB$p — UNPROVEN";;
    *) bad "unauthenticated $p -> $C — resident data is exposed";; esac
done
# and the pre-existing app on this host must still be reachable, unauthenticated, as before
C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PUB/")
case "$C" in 2*|3*) ok "the existing dev app is untouched ($C)";; *) bad "existing app -> $C";; esac

echo
echo "=================== 7. REFRESH FAILURE SURFACES ==================="
GOOD_ROWS=$(sudo sqlite3 $VAR/gateway.db "SELECT COUNT(*) FROM gw_door_event;" 2>/dev/null)
GOOD_MAX=$(sudo sqlite3 $VAR/gateway.db "SELECT CAST(MAX(ts) AS INT) FROM gw_door_event;" 2>/dev/null)
PREV_AT=$("$PY" -c "import json;print(json.load(open('$STATE')).get('restored_at_h'))" 2>/dev/null)
cat > /tmp/broken-litestream.yml <<YML
dbs:
  - path: $VAR/gateway.db
    replicas:
      - type: gcs
        bucket: liftlab-backup-lodha-DOES-NOT-EXIST
        path: liftlab/gateway.db
YML
chmod 644 /tmp/broken-litestream.yml
echo "  running a refresh against a bucket that does not exist ..."
run_refresh /tmp/broken-litestream.yml >/tmp/.brokenrefresh 2>&1 && bad "broken refresh exited 0" \
  || ok "broken refresh exited non-zero"
OKF=$("$PY" -c "import json;print(json.load(open('$STATE'))['ok'])" 2>/dev/null)
ERR=$("$PY" -c "import json;print((json.load(open('$STATE')).get('error') or '')[:90])" 2>/dev/null)
[ "$OKF" = False ] && ok "state file records ok=false" || bad "state ok=$OKF (expected False)"
[ -n "$ERR" ] && ok "state carries the real error: $ERR" || bad "state has no error text"
KEPT=$("$PY" -c "import json;print(json.load(open('$STATE')).get('restored_at_h'))" 2>/dev/null)
[ "$KEPT" = "$PREV_AT" ] && ok "last good restore point preserved across the failure ($KEPT)" \
  || bad "last good restore point lost (was $PREV_AT, now $KEPT)"
NOW_ROWS=$(sudo sqlite3 $VAR/gateway.db "SELECT COUNT(*) FROM gw_door_event;" 2>/dev/null)
[ "$NOW_ROWS" = "$GOOD_ROWS" ] && ok "the previous database is INTACT ($NOW_ROWS rows) — no data lost" \
  || bad "database changed under a failed refresh: $GOOD_ROWS -> $NOW_ROWS"
# the API must say so, and say so as an alarm
AS=$(curl -s --max-time 20 "$BASE/reports/data-as-of")
case "$AS" in *'"ok": false'*|*'"ok":false'*) ok "the API reports the failure to the page";;
  *) bad "data-as-of does not report the failure: $(echo "$AS" | cut -c1-120)";; esac
curl -s --max-time 30 "$BASE/reports" | grep -q "loadAsOf" && ok "the page fetches and renders that state" \
  || bad "the page has no banner hook"
rm -f /tmp/broken-litestream.yml

echo
echo "  restoring good state (a proof must not leave the box degraded) ..."
if run_refresh /etc/liftlab/litestream-restore.yml >/tmp/.finalrefresh 2>&1; then
  FOK=$("$PY" -c "import json;print(json.load(open('$STATE'))['ok'])" 2>/dev/null)
  [ "$FOK" = True ] && ok "recovered: a good refresh clears the alarm" || bad "still ok=$FOK after a good refresh"
else
  bad "the recovery refresh FAILED — the box is left on stale data: $(tail -2 /tmp/.finalrefresh)"
fi

echo
echo "=================== $PASS passed, $FAIL failed ==================="
[ "$FAIL" = 0 ]
