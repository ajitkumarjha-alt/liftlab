#!/usr/bin/env bash
# ACCEPTANCE PROOF for /reports, run ON liftlab-cloud WITH the streams running.
#
# The bar that matters is item 2: a 30-day export must not degrade /dash or /ops. Everything else can
# pass and this feature is still wrong if a user pressing "Build workbook" makes the dashboard
# unusable for a minute — the instrument comes first.
#
#   sudo bash /tmp/prove_reports.sh
set -uo pipefail
BASE=http://127.0.0.1:9090
GW="${GW:-site-A}"
DB=/var/lib/liftlab/gateway.db
JOBS=/var/lib/liftlab/reports.db
OUT=/var/lib/liftlab/reports
PY=/opt/liftlab-b3/cloud/.venv/bin/python
PASS=0; FAIL=0
ok(){  echo "  PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
note(){ echo "  note  $1"; }

jq_(){ "$PY" -c "import sys,json;d=json.load(sys.stdin);print(d.get('$1',''))" 2>/dev/null; }
submit(){ curl -s --max-time 30 -X POST -H 'Content-Type: application/json' \
            -d "{\"from\":\"$1\",\"to\":\"$2\"${3:+,\"population\":$3}}" "$BASE/reports/submit"; }
poll(){   # $1=job  $2=max_s   -> echoes final status
  local t=0
  while [ "$t" -lt "${2:-900}" ]; do
    S=$(curl -s --max-time 10 "$BASE/reports/job/$1" | jq_ status)
    case "$S" in done|failed|expired) echo "$S"; return;; esac
    sleep 3; t=$((t+3))
  done; echo timeout
}
lat(){ curl -s -o /dev/null -w '%{time_total}' --max-time 180 "$1" 2>/dev/null || echo 999; }

echo "=================== BASELINE (no export running) ==================="
BD=""; BO=""
for i in 1 2 3 4 5; do BD="$BD $(lat "$BASE/dash/$GW/data")"; BO="$BO $(lat "$BASE/ops/$GW/data")"; done
echo "  /dash/$GW/data :$BD"
echo "  /ops/$GW/data  :$BO"
BD_MAX=$(printf '%s\n' $BD | sort -g | tail -1)
BO_MAX=$(printf '%s\n' $BO | sort -g | tail -1)
echo "  baseline worst: dash=${BD_MAX}s ops=${BO_MAX}s"

echo
echo "=================== 1. SEVEN-DAY EXPORT ==================="
F7=$(date -d '7 days ago' +%Y-%m-%d); T7=$(date +%Y-%m-%d)
J=$(submit "$F7" "$T7" | jq_ job_id)
[ -n "$J" ] && ok "7d submitted (job #$J)" || bad "7d submit failed"
T0=$(date +%s)
S=$(poll "$J" 900); T1=$(date +%s)
[ "$S" = done ] && ok "7d completed in $((T1-T0))s" || bad "7d ended as $S"
if [ "$S" = done ]; then
  C=$(curl -s -o /tmp/p7.xlsx -w '%{http_code}' --max-time 120 "$BASE/reports/download/$J")
  [ "$C" = 200 ] && ok "7d download 200, $(stat -c %s /tmp/p7.xlsx) bytes" || bad "7d download $C"
  "$PY" -c "
from openpyxl import load_workbook
wb=load_workbook('/tmp/p7.xlsx',read_only=True); print('    sheets:',len(wb.sheetnames)); wb.close()" \
    && ok "7d workbook opens" || bad "7d workbook does not open"
  RSS=$(sudo -u liftlab grep -h VmHWM /proc/*/status 2>/dev/null | head -0; echo)
  note "peak RSS is sampled during the 30d run below (the worker exits too fast to catch reliably here)"
fi
rm -f /tmp/p7.xlsx

echo
echo "=================== 2. THIRTY-DAY EXPORT + LIVE LATENCY (THE BAR) ==================="
F30=$(date -d '30 days ago' +%Y-%m-%d)
J=$(submit "$F30" "$T7" | jq_ job_id)
[ -n "$J" ] && ok "30d submitted (job #$J)" || bad "30d submit failed"
T0=$(date +%s); PEAK=0; DD=""; OO=""
for i in 1 2 3 4 5; do
  P=$(pgrep -f "report_runner.py $J" | head -1)
  if [ -n "$P" ]; then
    R=$(awk '/VmRSS/{print $2}' "/proc/$P/status" 2>/dev/null || echo 0)
    [ "${R:-0}" -gt "$PEAK" ] && PEAK=$R
    NI=$(ps -o ni= -p "$P" 2>/dev/null | tr -d ' ')
  fi
  DD="$DD $(lat "$BASE/dash/$GW/data")"
  OO="$OO $(lat "$BASE/ops/$GW/data")"
  sleep 4
done
S=$(poll "$J" 1200); T1=$(date +%s)
[ "$S" = done ] && ok "30d completed in $((T1-T0))s" || bad "30d ended as $S"
echo "  DURING the export:"
echo "    /dash/$GW/data :$DD"
echo "    /ops/$GW/data  :$OO"
DD_MAX=$(printf '%s\n' $DD | sort -g | tail -1)
OO_MAX=$(printf '%s\n' $OO | sort -g | tail -1)
echo "    worst during: dash=${DD_MAX}s ops=${OO_MAX}s   (baseline dash=${BD_MAX}s ops=${BO_MAX}s)"
echo "    worker peak RSS: $((PEAK/1024)) MB   nice=${NI:-?}"
# "within normal range": the whole point of nice+subprocess is that the request path stays usable.
# 2s is the /dash acceptance figure this project already set; allow 3x baseline or 2s, whichever is
# larger, and say plainly which one it hit.
JUDGE(){ "$PY" -c "
import sys
d,b=float('$1'),float('$2')
lim=max(2.0, b*3)
print(('OK' if d<=lim else 'DEGRADED'), round(d,3), 'vs limit', round(lim,3))"; }
R1=$(JUDGE "$DD_MAX" "$BD_MAX"); R2=$(JUDGE "$OO_MAX" "$BO_MAX")
case "$R1" in OK*) ok "/dash stayed within range during the 30d export ($R1)";; *) bad "/dash DEGRADED during the export ($R1)";; esac
case "$R2" in OK*) ok "/ops stayed within range during the 30d export ($R2)";; *) bad "/ops DEGRADED during the export ($R2)";; esac
J30=$J

echo
echo "=================== 3. QUEUEING AND THE CAP ==================="
A=$(submit "$F7" "$T7" | jq_ job_id); B=$(submit "$F7" "$T7" | jq_ job_id)
C=$(submit "$F7" "$T7" | jq_ job_id); D=$(submit "$F7" "$T7" | jq_ job_id)
BODY=$(submit "$F7" "$T7"); CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST \
  -H 'Content-Type: application/json' -d "{\"from\":\"$F7\",\"to\":\"$T7\"}" "$BASE/reports/submit")
NQ=$(sudo sqlite3 "$JOBS" "SELECT COUNT(*) FROM report_job WHERE status='queued';" 2>/dev/null)
NR=$(sudo sqlite3 "$JOBS" "SELECT COUNT(*) FROM report_job WHERE status='running';" 2>/dev/null)
echo "  after 6 submissions: running=$NR queued=$NQ  last HTTP=$CODE"
[ "${NR:-0}" -le 1 ] && ok "only one job runs at a time (running=$NR)" || bad "running=$NR — the lock is not holding"
[ "$CODE" = 429 ] && ok "beyond the cap: HTTP 429" || bad "beyond the cap: HTTP $CODE (expected 429)"
case "$BODY" in *"queue is full"*) ok "refusal explains itself";; *) bad "refusal has no message: $BODY";; esac
# drain
for j in $A $B $C $D; do [ -n "$j" ] && poll "$j" 900 >/dev/null; done

echo
echo "=================== 4. ERA-STRADDLING RANGE ==================="
RC=$(curl -s --max-time 20 "$BASE/reports/range-check?from_ts=2026-07-19&to_ts=2026-07-23")
case "$RC" in *"INSTRUMENT SPLIT"*) ok "era straddle warned BEFORE submission";; *) bad "no era warning: $(echo "$RC"|cut -c1-140)";; esac
J=$(submit 2026-07-19 2026-07-23 | jq_ job_id)
S=$(poll "$J" 900)
[ "$S" = done ] && ok "era-straddling export built (job #$J)" || bad "era-straddling export ended as $S"
if [ "$S" = done ]; then
  curl -s -o /tmp/pera.xlsx --max-time 120 "$BASE/reports/download/$J"
  "$PY" -c "
from openpyxl import load_workbook
wb=load_workbook('/tmp/pera.xlsx',read_only=True)
hit=[n for n in wb.sheetnames if 'ERA' in n.upper() or 'COVERAGE' in n.upper()]
print('    era/coverage sheets:', hit or '(none)')
wb.close()
raise SystemExit(0 if hit else 1)" && ok "workbook carries the era/coverage sheet" \
    || bad "workbook has no era/coverage sheet for a straddling range"
  rm -f /tmp/pera.xlsx
fi

echo
echo "=================== 5. EMPTY RANGE ==================="
J=$(submit 2020-01-01 2020-01-03 | jq_ job_id)
S=$(poll "$J" 900)
[ "$S" = done ] && ok "empty range completed without crashing (job #$J)" || bad "empty range ended as $S"
if [ "$S" = done ]; then
  curl -s -o /tmp/pempty.xlsx --max-time 120 "$BASE/reports/download/$J"
  "$PY" -c "
import re
from openpyxl import load_workbook
wb=load_workbook('/tmp/pempty.xlsx',read_only=True)
pat=re.compile(r'zero rows|no transits|no data|0 transits',re.I)
hits=[(n,c) for n in wb.sheetnames for row in wb[n].iter_rows(values_only=True)
      for c in row if isinstance(c,str) and pat.search(c)]
print('    zero-rows statements:', len(hits))
for n,c in hits[:2]: print('      [',n,']',c.strip()[:90])
wb.close()
raise SystemExit(0 if hits else 1)" && ok "empty workbook states the zero explicitly" \
    || bad "empty workbook does not explain the zero"
  rm -f /tmp/pempty.xlsx
fi

echo
echo "=================== 6. KILL THE WORKER MID-RUN ==================="
J=$(submit "$F30" "$T7" | jq_ job_id)
KP=""
for i in $(seq 1 40); do KP=$(pgrep -f "report_runner.py $J" | head -1); [ -n "$KP" ] && break; sleep 2; done
if [ -n "$KP" ]; then
  kill -9 "$KP"; ok "killed worker pid $KP mid-run"
  S=$(poll "$J" 300)
  [ "$S" = failed ] && ok "job marked failed after the kill (not stuck running)" || bad "job ended as $S"
  ET=$(sudo sqlite3 "$JOBS" "SELECT COALESCE(error_text,'') FROM report_job WHERE id=$J;" 2>/dev/null | head -c 110)
  [ -n "$ET" ] && ok "failure carries a reason: $ET" || bad "no error_text recorded for the killed job"
  UP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$BASE/dash")
  [ "$UP" = 200 ] && ok "gateway unaffected by the kill (/dash 200)" || bad "/dash -> $UP after the kill"
else
  bad "worker never appeared for job $J — could not test the kill"
fi

echo
echo "=================== 7. READ-ONLY DISCIPLINE ==================="
cd /opt/liftlab-b3/cloud && "$PY" -c "
from liftlab_report import reader
db = reader.open_ro('$DB')
assert db.execute('PRAGMA query_only').fetchone()[0] == 1
try:
    db.execute('CREATE TABLE _p (x INT)')
except Exception as e:
    print('    write refused:', str(e)[:60])
else:
    raise SystemExit('write succeeded')
db.close()" && ok "gateway.db opened read-only; writes refused" || bad "read-only discipline broken"
CP=/tmp/prove-static.db
sudo sqlite3 "$DB" ".backup '$CP'"; M1=$(md5sum "$CP" | cut -d' ' -f1)
sudo -u liftlab env GATEWAY_DB="$CP" REPORTS_DB=/tmp/prove-jobs.db REPORTS_DIR=/tmp/prove-out \
  "$PY" - <<PYEOF >/dev/null 2>&1
import sqlite3, time, os, subprocess, sys
os.makedirs('/tmp/prove-out', exist_ok=True)
db = sqlite3.connect('/tmp/prove-jobs.db')
db.execute("""CREATE TABLE IF NOT EXISTS report_job (id INTEGER PRIMARY KEY AUTOINCREMENT,
 requested_from TEXT, requested_to TEXT, population INTEGER, status TEXT, created_at REAL,
 started_at REAL, finished_at REAL, output_path TEXT, error_text TEXT, row_count INTEGER,
 requested_by TEXT, pid INTEGER)""")
db.execute("INSERT INTO report_job (requested_from,requested_to,status,created_at) VALUES (?,?,'queued',?)",
           ("$F7", "$T7", time.time())); db.commit()
jid = db.execute("SELECT MAX(id) FROM report_job").fetchone()[0]; db.close()
subprocess.run([sys.executable, "/opt/liftlab-b3/cloud/report_runner.py", str(jid)])
PYEOF
M2=$(md5sum "$CP" | cut -d' ' -f1)
[ "$M1" = "$M2" ] && ok "STATIC db md5 unchanged by a full export ($M1)" || bad "STATIC db md5 changed $M1 -> $M2"
sudo rm -rf "$CP" /tmp/prove-jobs.db /tmp/prove-out

echo
echo "=================== 8. UNAUTHENTICATED DOWNLOAD ==================="
# Auth is enforced by Caddy on the public host; localhost bypasses it by design, so this MUST be
# tested through the real URL or it proves nothing.
PUB="${PUBLIC_URL:-https://lift.gargi.online}"
UC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PUB/reports/download/${J30:-1}")
case "$UC" in 401|403) ok "unauthenticated download rejected ($UC) via $PUB";;
  000) bad "could not reach $PUB to test auth — UNPROVEN";;
  *) bad "unauthenticated download returned $UC — resident data is exposed";; esac
UP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$PUB/reports")
case "$UP" in 401|403) ok "unauthenticated /reports page rejected ($UP)";; *) bad "/reports page unauthenticated -> $UP";; esac

echo
echo "=================== 9. RETENTION SWEEP ==================="
OLD=$(sudo sqlite3 "$JOBS" "SELECT id FROM report_job WHERE status='done' ORDER BY id LIMIT 1;")
if [ -n "$OLD" ]; then
  OP=$(sudo sqlite3 "$JOBS" "SELECT output_path FROM report_job WHERE id=$OLD;")
  sudo sqlite3 "$JOBS" "UPDATE report_job SET finished_at=finished_at-8*86400 WHERE id=$OLD;"
  echo "  aged job #$OLD by 8 days; invoking the sweep directly ..."
  # Deliberately NOT `systemctl restart liftlab-cloud`. The sweep runs hourly in the supervisor and a
  # restart would force it — but restarting the gateway to test a housekeeping function interrupts
  # seven live streams to prove something about file retention. Call the same function the supervisor
  # calls; that is what is under test, and it costs the instrument nothing.
  cd /opt/liftlab-b3/cloud && sudo -u liftlab "$PY" -c "
import reports_api
db = reports_api._jobs()
reports_api._sweep(db)
db.close()
print('    _sweep() invoked')"
  ST=$(sudo sqlite3 "$JOBS" "SELECT status FROM report_job WHERE id=$OLD;")
  [ "$ST" = expired ] && ok "retention marked job #$OLD expired" || bad "job #$OLD is still '$ST' after the sweep"
  [ -f "$OP" ] && bad "the workbook is still on disk: $OP" || ok "workbook deleted from disk"
  DC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$BASE/reports/download/$OLD")
  [ "$DC" = 410 ] && ok "download of an expired job returns 410 with an explanation" || bad "expired download -> $DC"
  curl -s --max-time 30 "$BASE/reports/jobs?limit=10" | grep -q expired \
    && ok "the jobs list shows it as expired" || bad "the jobs list does not show the expired job"
else
  bad "no completed job to age for the retention test"
fi

echo
echo "=================== $PASS passed, $FAIL failed ==================="
[ "$FAIL" = 0 ]
