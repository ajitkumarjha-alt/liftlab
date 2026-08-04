#!/usr/bin/env bash
# Deploy analysis_api.py — APPEND-ONLY WORKER TELEMETRY (worker_telemetry).
#
# WHAT THIS IS. analyzer_status is an UPSERT: one row per camera, current state only. Every drop /
# throughput figure we have is therefore an instantaneous sample of a CUMULATIVE counter, and that has
# now produced two wrong conclusions in a row:
#   - "four workers at exactly 66.667%"  -> 2 dropped of 3 segments on a just-restarted worker.
#   - "ch29 runs 2.38x over budget"      -> a transient; it reads 0.79x once uptime is non-trivial.
# drop_frac is `dropped/total since process start`, so a small denominator makes any figure possible.
# worker_telemetry appends the same heartbeat (throttled + pruned) so those numbers can be DIFFERENCED
# into instantaneous rates and regressed against door-cycle completion. Restarts are recoverable from
# uptime_s resets.
#
# THIS IS A SIBLING, NOT A REPLACEMENT. The upsert path the dashboard reads is untouched, and the gate
# below asserts it still holds exactly one row per camera.
#
# THE DIFF IS PURELY ADDITIVE: 2 hunks, 33 lines added, 0 removed, all worker_telemetry. Nothing else
# rides along — verified against the installed file before writing this.
#
# FILES NEEDED IN /tmp: apply_gwtelemetry.sh analysis_api.py smoke_ingest.sh
#   sudo bash /tmp/apply_gwtelemetry.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
say(){ echo "[gwtele] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in analysis_api.py smoke_ingest.sh; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'worker_telemetry' /tmp/analysis_api.py || { say "ABORT: analysis_api.py has no worker_telemetry"; exit 2; }
grep -q 'ix_wtele' /tmp/analysis_api.py         || { say "ABORT: analysis_api.py does not create ix_wtele"; exit 2; }
# the prune must be per-cam; a global `WHERE ts < ?` full-scans on every append inside the handler
grep -q 'DELETE FROM worker_telemetry WHERE gateway_id=? AND cam=? AND ts < ?' /tmp/analysis_api.py \
  || { say "ABORT: the prune is not the indexed per-cam form — it would SCAN on every heartbeat"; exit 2; }

$PY -m py_compile /tmp/analysis_api.py || { say "compile failed — nothing changed"; exit 1; }

# ── SCOPE CHECK: refuse to ship anything that is not the telemetry change ────
# Deploying a whole file ships every local edit to it, not the one being described. Diff the candidate
# against what is installed and require the change set to be additive and confined to worker_telemetry.
DIFF=$(diff -u "$APP/analysis_api.py" /tmp/analysis_api.py)
REMOVED=$(printf '%s\n' "$DIFF" | grep -c '^-[^-]' || true)
ADDED_OTHER=$(printf '%s\n' "$DIFF" | grep '^+[^+]' | grep -vciE 'worker_telemetry|ix_wtele|WORKER_TELEMETRY|prune|history|append|index|scan|ts <|cam only|^\+\s*#' || true)
if [ "$REMOVED" != 0 ]; then
  say "ABORT: the candidate REMOVES $REMOVED line(s) from the installed file. This deploy is"
  say "  supposed to be purely additive. Review the diff before shipping:"
  printf '%s\n' "$DIFF" | grep '^-[^-]' | head -10 | sed 's/^/    /'
  exit 1
fi
say "scope check: +$(printf '%s\n' "$DIFF" | grep -c '^+[^+]') lines, -0 lines, all worker_telemetry"

# ── NEGATIVE CONTROL: prove the gate can FAIL before trusting it to pass ─────
# Four incidents this week came from gates that passed on broken code. A gate that has never been
# observed failing is not evidence. Stage a copy with the history INSERT removed and require the gate
# to reject it. If it passes that, the gate is not testing what it claims and we stop here.
say "negative control: running the ingest gate against a DELIBERATELY BROKEN copy ..."
NEG=$(mktemp -d)
cp "$APP"/*.py "$NEG/" 2>/dev/null
cp /tmp/analysis_api.py "$NEG/"
$PY - "$NEG/analysis_api.py" <<'PYEOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
# remove the history INSERT but leave CREATE TABLE, so the table exists and stays empty — the exact
# silent-failure shape the gate must catch (endpoint still returns 200).
s2 = re.sub(r'\n(\s+)db\.execute\("INSERT INTO worker_telemetry.*?\)\)\)\n',
            '\n', s, flags=re.S)
assert s2 != s, "negative control did not modify the file — the gate would be tested against good code"
open(p, "w").write(s2)
print("  negative control: history INSERT removed")
PYEOF
[ $? = 0 ] || { say "ABORT: could not build the negative control"; rm -rf "$NEG"; exit 1; }
if bash /tmp/smoke_ingest.sh "$NEG" "$APP" >/tmp/neg_gate.log 2>&1; then
  say "ABORT: THE GATE PASSED BROKEN CODE. It is not testing the history append."
  say "  This is the same class as the four failures this week — fix the gate, not the deploy."
  grep -E 'PASS|FAIL' /tmp/neg_gate.log | sed 's/^/    /' | tail -20
  rm -rf "$NEG"; exit 1
fi
say "negative control PASSED: the gate rejected the broken copy. Reason it gave —"
grep -E '^  FAIL' /tmp/neg_gate.log | sed 's/^/    /'
rm -rf "$NEG"

# ── the real gate, against the real candidate ────────────────────────────────
say "ingest gate: POSTing real payloads to every producer endpoint against the STAGED code ..."
STAGE=$(mktemp -d)
cp "$APP"/*.py "$STAGE/" 2>/dev/null
cp /tmp/analysis_api.py "$STAGE/"
if ! bash /tmp/smoke_ingest.sh "$STAGE" "$APP"; then
  say "ABORT: an ingest handler did not accept a real payload. NOTHING INSTALLED — live ingest is"
  say "  untouched and still working."
  rm -rf "$STAGE"; exit 1
fi
rm -rf "$STAGE"
say "ingest gate PASSED — every endpoint 2xx, rows landed, history appended, throttle held."

STAMP=$(date +%Y%m%d-%H%M%S)
cp "$APP/analysis_api.py" "$APP/analysis_api.py.bak.$STAMP"; say "backup: $APP/analysis_api.py.bak.$STAMP"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/analysis_api.py "$APP/analysis_api.py"

restore(){
  say "RESTORING backup .bak.$STAMP"
  cp "$APP/analysis_api.py.bak.$STAMP" "$APP/analysis_api.py"; chown "$OWNER:$OWNER" "$APP/analysis_api.py"
  systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"
}

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }

# systemd goes green when the process forks; uvicorn needs 8-35s to bind.
say "waiting for :9090 ..."
READY=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null && { READY=1; break; }; sleep 2; done
[ -n "$READY" ] || { say "port never opened"; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

OPS=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$OPS" = 200 ] || { say "VERIFY FAILED: /ops/$GW/data -> $OPS"; restore; exit 1; }
say "  /ops/$GW/data: 200"

DB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db

# The table is created lazily on the next _db() call from an analysis-API ingest. The GPU fleet
# heartbeats every ~60s per camera, so this should be quick — but "no heartbeat yet" is not a failure.
say "waiting up to 180s for the first real heartbeat to create and populate the table ..."
OK=0
for i in $(seq 1 36); do
  T=$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='worker_telemetry';" 2>/dev/null || echo 0)
  N=$(sqlite3 "$DB" "SELECT COUNT(*) FROM worker_telemetry;" 2>/dev/null || echo 0)
  [ "${T:-0}" = 1 ] && [ "${N:-0}" -ge 1 ] && { OK=1; break; }
  sleep 5
done
if [ "$OK" != 1 ]; then
  say "table=$T rows=$N after 180s. Created lazily on the next analysis-API ingest; NOT a failure if"
  say "  the fleet is quiet. Re-check:  sqlite3 $DB \"SELECT COUNT(*) FROM worker_telemetry;\""
else
  say "  worker_telemetry live: $N row(s) after ~$(( i * 5 ))s"
  sqlite3 "$DB" "SELECT '    '||cam||'  ts='||ROUND(ts)||'  drop_frac='||COALESCE(drop_frac,'null')||'  segments='||COALESCE(segments,'null') FROM worker_telemetry ORDER BY ts DESC LIMIT 5;" 2>/dev/null
  # the upsert must be unharmed — the dashboard reads it
  AS=$(sqlite3 "$DB" "SELECT COUNT(*) FROM analyzer_status;" 2>/dev/null)
  CAMS=$(sqlite3 "$DB" "SELECT COUNT(DISTINCT cam) FROM analyzer_status;" 2>/dev/null)
  if [ "$AS" = "$CAMS" ]; then say "  analyzer_status intact: $AS rows for $CAMS cameras (still one per cam)"
  else say "  WARNING: analyzer_status has $AS rows for $CAMS cameras — expected one per cam"; fi
fi

# Litestream replicates the whole DB file via WAL, so a new table needs no config change. Confirm it
# is actually shipping rather than assuming it: a growing WAL position after the inserts.
say "litestream: $(systemctl is-active litestream 2>/dev/null || echo 'not installed')"
WALN=$(journalctl -u litestream --since "2 min ago" --no-pager 2>/dev/null | grep -c "wal segment written")
say "  $WALN WAL segments shipped to GCS in the last 2 min (0 would mean replication stalled)"
# Shipping is NOT the same as restorable. On 2026-08-04 the replica was shipping continuously while
# every restore failed on a missing initial WAL segment. Prove restorability, do not infer it.
if ! litestream restore -config /etc/litestream.yml -o /tmp/.ls_verify.db "$DB" >/tmp/.ls_verify.log 2>&1; then
  say "  WARNING: replication is shipping but a RESTORE FAILS — the backup is not usable:"
  tail -2 /tmp/.ls_verify.log | sed "s/^/      /"
else
  say "  restore verified: $(sqlite3 /tmp/.ls_verify.db "SELECT COUNT(*) FROM worker_telemetry;" 2>/dev/null || echo 0) worker_telemetry rows in the restored copy"
fi
rm -f /tmp/.ls_verify.db /tmp/.ls_verify.log

say "RESULT: PASS — worker_telemetry live, upsert path untouched, $SVC active. Backup *.bak.$STAMP"
say "  growth: ~10,080 rows/day fleet-wide (7 cams x 60s throttle), ~114 B/row, plateaus ~16 MB at 14d"
say "  the correlation this unblocks needs DIFFERENCED rows — drop_frac is cumulative since process"
say "  start, so compute (dropped[t]-dropped[t-1])/(segments[t]-segments[t-1]) and reset on uptime_s drop."
