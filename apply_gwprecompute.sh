#!/usr/bin/env bash
# STEP 2 FINAL: take /dash's per-camera walk OFF the request path entirely.
#
# door_gpu and tier2 are now PRECOMPUTED into door_aggregate by a scheduled job; /dash does indexed
# reads. This also folds (c) back in — the floor alphabet — as stage 1 of the same job, because
# stage 2 depends on it (_tier2 reads the stored alphabet to decide admissible floors). Two
# independent timers could compute an aggregate against a missing alphabet, so this is ONE ordered
# job and ONE timer, replacing the half-installed liftlab-alphabet units.
#
# WHY NOT MORE WINDOWING: gw_door_event spans ~15 days, so a 7-day bound is 87% of the table, and a
# sweep from days=7 to days=0.1 (70x less data) changed the outcome not at all. The flap/reopen
# state machine and the stop walk are sequential and do not reduce to SQL aggregates.
#
# ORDER IS ENFORCED. An empty door_aggregate makes /dash report "not yet computed" — correct, but
# useless. So the job runs and the table is verified populated BEFORE the new module serves. Job
# fails => nothing installed.
#
# FILES NEEDED IN /tmp: apply_gwprecompute.sh dash_api.py precompute_job.py
#   sudo bash /tmp/apply_gwprecompute.sh
#   sudo EVERY=15min bash /tmp/apply_gwprecompute.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
# CADENCE vs DUTY CYCLE. The job walks a 7-day window; recomputing it every few minutes buys no
# freshness a human could perceive and costs real CPU on a 2-vCPU box that has OOMed twice. At the
# first measured runtime (428s) a 20min timer ran ~35% of the time and visibly contended with the
# request path — /dash samples taken while it ran were 2-3.8s, and the job is the reason. 1h keeps
# the aggregates well inside their own staleness tolerance at a fraction of the load.
EVERY="${EVERY:-1h}"
say(){ echo "[precompute] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in dash_api.py precompute_job.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

for need in aggregate_refresh _aggregate_read _alphabet_read alphabet_refresh; do
  grep -q "$need" /tmp/dash_api.py || { say "ABORT: /tmp/dash_api.py lacks $need — wrong file?"; exit 2; }
done
# The request handler must contain NO live walk. If it still calls _tier2/_door_gpu_by_cam directly,
# the whole change is undone and the hang comes straight back.
if awk '/^def _dash_data_inner/,/^    return JSONResponse/' /tmp/dash_api.py \
     | grep -qE '_tier2\(|_door_gpu_by_cam\(|_transits_for_join\('; then
  say "ABORT: _dash_data_inner still performs a live walk — precompute would be bypassed"; exit 2
fi

$PY -m py_compile /tmp/dash_api.py /tmp/precompute_job.py || { say "compile failed"; exit 1; }

TMPD=$(mktemp -d); cp /tmp/dash_api.py "$TMPD/dash_api.py"
if ! ( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0, '$TMPD')
from fastapi import FastAPI
import dash_api
assert dash_api.__file__.startswith('$TMPD'), 'imported the WRONG dash_api: '+dash_api.__file__
a=FastAPI(); a.include_router(dash_api.dash_router); a.openapi()
for n in ('aggregate_refresh','_aggregate_read','alphabet_refresh','_alphabet_read'):
    assert hasattr(dash_api, n), n
print('smoke ok:', dash_api.__file__)" ); then
  say "SMOKE-IMPORT FAILED — nothing installed."; rm -rf "$TMPD"; exit 1
fi
rm -rf "$TMPD"

install -o "$OWNER" -g "$OWNER" -m 755 /tmp/precompute_job.py "$APP/precompute_job.py"
say "installed precompute_job.py"

DB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db

# ── populate first, with the NEW module, while the OLD one still serves ───────
STAGE=$(mktemp -d); cp /tmp/dash_api.py "$STAGE/dash_api.py"
say "running the precompute (alphabet -> aggregates), off the request path ..."
if ! ( cd "$APP" && PYTHONPATH="$STAGE:$APP" LIFTLAB_APP="$STAGE" \
       $(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^[A-Z_]+=' | sed 's/^/env /' | tr '\n' ' ') \
       $PY "$APP/precompute_job.py" "$GW" ); then
  say "PRECOMPUTE JOB FAILED — not installing dash_api.py. Live app untouched."; rm -rf "$STAGE"; exit 1
fi
rm -rf "$STAGE"

NA=$(sqlite3 "$DB" "SELECT COUNT(*) FROM door_aggregate WHERE gateway_id='$GW';" 2>/dev/null || echo 0)
if [ "${NA:-0}" -lt 1 ]; then
  say "ABORT: door_aggregate empty for $GW after the job — /dash would report 'not yet computed'"
  say "       for every camera. Nothing installed."; exit 1
fi
say "door_aggregate populated: $NA rows"
sqlite3 "$DB" "SELECT '    '||cam||': rows='||source_rows||' tier2='||(CASE WHEN tier2 IS NULL THEN 'none' ELSE 'yes' END)||' ms='||compute_ms||' dv='||COALESCE(door_version,'-') FROM door_aggregate WHERE gateway_id='$GW';"

STAMP=$(date +%Y%m%d-%H%M%S); BAK="$APP/dash_api.py.bak.$STAMP"
cp "$APP/dash_api.py" "$BAK"; say "backup: $BAK"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"

# ── COMPLETE rollback. The previous version of this script restored the module but LEFT the timer
# it had installed — a 35s job every 30min feeding a table nothing read. A rollback that leaves its
# own side effects running is not a rollback, so undo the units too.
restore(){
  say "RESTORING $BAK (and removing the units this script installed)"
  cp "$BAK" "$APP/dash_api.py"; chown "$OWNER:$OWNER" "$APP/dash_api.py"
  systemctl disable --now liftlab-precompute.timer >/dev/null 2>&1
  rm -f /etc/systemd/system/liftlab-precompute.timer /etc/systemd/system/liftlab-precompute.service
  systemctl daemon-reload
  systemctl restart "$SVC"; sleep 8
  say "restored; service=$(systemctl is-active $SVC); precompute timer=$(systemctl is-enabled liftlab-precompute.timer 2>/dev/null || echo removed)"
}

# retire the half-installed alphabet-only units — superseded by the ordered job
systemctl disable --now liftlab-alphabet.timer >/dev/null 2>&1
rm -f /etc/systemd/system/liftlab-alphabet.timer /etc/systemd/system/liftlab-alphabet.service

cat > /etc/systemd/system/liftlab-precompute.service <<UNIT
[Unit]
Description=liftlab /dash precompute (floor alphabet -> per-camera aggregates), off the request path
After=network.target
[Service]
Type=oneshot
User=$OWNER
WorkingDirectory=$APP
Nice=10
IOSchedulingClass=idle
$(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^[A-Z_]+=' | sed 's/^/Environment=/')
ExecStart=$PY $APP/precompute_job.py
UNIT
cat > /etc/systemd/system/liftlab-precompute.timer <<UNIT
[Unit]
Description=run the liftlab /dash precompute every $EVERY
[Timer]
OnBootSec=3min
OnUnitActiveSec=$EVERY
AccuracySec=1min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now liftlab-precompute.timer >/dev/null 2>&1
say "timer: $(systemctl is-active liftlab-precompute.timer), every $EVERY (niced, idle I/O)"

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }
say "waiting for :9090 ..."
READY=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null && { READY=1; break; }; sleep 2; done
[ -n "$READY" ] || { say "port never opened"; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

# ── ACCEPTANCE: 5 samples, all must be 200, and the bar is 2s ─────────────────
say "measuring /dash/$GW/data x5 (bar: 200 and under 2s) ..."
FAIL=0; SLOW=0
for i in 1 2 3 4 5; do
  O=$(curl -s -o /tmp/pre_probe.json -w '%{http_code} %{time_total}' --max-time 90 "http://127.0.0.1:9090/dash/$GW/data")
  C=${O%% *}; S=${O##* }
  say "  sample$i: HTTP $C in ${S}s"
  [ "$C" = 200 ] || FAIL=$((FAIL+1))
  awk "BEGIN{exit !($S > 2.0)}" && SLOW=$((SLOW+1))
done
if [ "$FAIL" -gt 0 ]; then
  say "VERIFY FAILED: $FAIL/5 non-200"; head -c 300 /tmp/pre_probe.json; echo; restore; exit 1
fi
SHELL_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 "http://127.0.0.1:9090/dash")
[ "$SHELL_CODE" = 200 ] || { say "VERIFY FAILED: /dash shell $SHELL_CODE"; restore; exit 1; }
OPS_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$OPS_CODE" = 200 ] || { say "VERIFY FAILED: /ops $OPS_CODE"; restore; exit 1; }
say "  /dash shell 200, /ops 200"
if [ "$SLOW" -gt 0 ]; then
  say "RESULT: PARTIAL — all 5 returned 200 but $SLOW/5 exceeded the 2s bar. Kept (200 beats 503),"
  say "        but the acceptance bar is NOT met. Investigate before calling this done."
else
  say "RESULT: PASS — 5/5 returned 200, all under 2s. Backup $BAK"
fi
