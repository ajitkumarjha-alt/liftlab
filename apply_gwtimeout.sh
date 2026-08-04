#!/usr/bin/env bash
# STEP 1 OF 3: a request time budget on /dash/{gw}/data. dash_api.py ONLY. Nothing else changes.
#
# WHY THIS SHIPS FIRST, ALONE. /dash/{gw}/data does not terminate — measured 2026-08-03, curl
# --max-time 600 returned 000 with 0 bytes while the page shell returned 200 in 6.5s. The dashboard
# auto-refreshes, so an open tab strands one non-terminating request after another until the kernel
# reaps the gateway: two OOM kills in ~13h at anon-rss 1.48/1.49 GB, on unmodified code. The budget
# does not make the endpoint fast. It converts an OUTAGE into an ERROR, which is the thing standing
# between a slow page and a dead ingest. The SQL rewrite is step 2 and ships separately.
#
# WHAT "PASS" MEANS HERE — READ THIS BEFORE PANICKING AT A 503.
# Until step 2 bounds the query, /dash/{gw}/data CANNOT complete, so the correct post-deploy result
# is a prompt 503, not a 200. This script therefore verifies TERMINATION, not success:
#     200 -> fine (it finished inside the budget)
#     503 -> PASS, this is the budget doing its job
#     000 -> FAIL, still hanging; the budget did not take effect -> auto-restore
# The page shell (/dash) and /ops must both still answer 200.
#
# FILES NEEDED IN /tmp: apply_gwtimeout.sh dash_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/gw-timeout; \
#       for f in apply_gwtimeout.sh dash_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_gwtimeout.sh
#   sudo BUDGET=15 bash /tmp/apply_gwtimeout.sh     # override the default 25s budget
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
say(){ echo "[gwtimeout] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/dash_api.py ] || { echo "missing /tmp/dash_api.py"; exit 2; }
[ -f "$APP/dash_api.py" ] || { echo "dash_api.py not installed at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'DashTimeout' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py has no DashTimeout — wrong file?"; exit 2; }
grep -q 'set_progress_handler' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py has no progress handler — running SQL would be uninterruptible"; exit 2; }
grep -q 'if(inflight) return;' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py has no client in-flight guard — the page would still stack requests"; exit 2; }
grep -q 'setInterval(load' /tmp/dash_api.py && ! grep -q 'This was .load(); setInterval(load' /tmp/dash_api.py \
  && { say "ABORT: /tmp/dash_api.py still arms setInterval(load, ...)"; exit 2; }

$PY -m py_compile /tmp/dash_api.py || { say "compile failed — nothing changed"; exit 1; }

# SMOKE-IMPORT THE CANDIDATE, NOT THE INSTALLED FILE.
# `cd $APP` makes Python prepend the CWD to sys.path for -c, and the CWD wins over PYTHONPATH — so
# the obvious `cd $APP && PYTHONPATH=$TMPD:$APP python -c 'import dash_api'` silently imports the
# ALREADY-INSTALLED module and reports "smoke ok" for code it never loaded. sys.path.insert(0,...)
# inside the interpreter is the only ordering that actually binds. The assertions below exist to
# make a wrong-module import fail loudly instead of passing vacuously.
TMPD=$(mktemp -d); cp /tmp/dash_api.py "$TMPD/dash_api.py"
if ! ( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0, '$TMPD')
from fastapi import FastAPI
import dash_api
assert dash_api.__file__.startswith('$TMPD'), 'imported the WRONG dash_api: '+dash_api.__file__
a=FastAPI(); a.include_router(dash_api.dash_router); a.openapi()
assert hasattr(dash_api,'DashTimeout') and hasattr(dash_api,'_budget_arm')
print('smoke ok:', dash_api.__file__, '| budget default', dash_api.DATA_BUDGET_S, 's')" ); then
  say "SMOKE-IMPORT FAILED — not installing. Live app untouched."; rm -rf "$TMPD"; exit 1
fi
rm -rf "$TMPD"

# Optional budget override, applied as a systemd env drop-in so it survives restarts.
if [ -n "${BUDGET:-}" ]; then
  mkdir -p /etc/systemd/system/$SVC.service.d
  printf '[Service]\nEnvironment=DASH_DATA_BUDGET_S=%s\n' "$BUDGET" \
    > /etc/systemd/system/$SVC.service.d/dashbudget.conf
  systemctl daemon-reload
  say "budget override: DASH_DATA_BUDGET_S=$BUDGET"
fi
EFFBUDGET="${BUDGET:-25}"

STAMP=$(date +%Y%m%d-%H%M%S)
BAK="$APP/dash_api.py.bak.$STAMP"
cp "$APP/dash_api.py" "$BAK"; say "backup: $BAK"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"

restore(){
  say "RESTORING $BAK"
  cp "$BAK" "$APP/dash_api.py"; chown "$OWNER:$OWNER" "$APP/dash_api.py"
  rm -f /etc/systemd/system/$SVC.service.d/dashbudget.conf; systemctl daemon-reload
  systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"
}

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "service did not come up"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1
fi

# READINESS GATE. `systemctl is-active` goes green the moment the process forks, but uvicorn takes
# 8-35s to import the app and bind :9090 (observed in this unit's own journal). Probing before it
# listens gives curl 000 in ~0.2ms — connection refused, which is NOT the hang we are testing for.
# Distinguishing those two is the whole point of this gate: without it a healthy deploy fails
# verification and gets rolled back for the wrong reason.
say "waiting for :9090 to accept connections ..."
READY=""
for i in $(seq 1 60); do
  if curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ -n "$READY" ] || { say "port 9090 never accepted a connection after 120s"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

# ── VERIFY TERMINATION ────────────────────────────────────────────────────────
# Ceiling = budget + 40s slack. The slack matters: the progress handler only fires between SQLite VM
# steps, so an abort lands shortly AFTER the deadline, not exactly on it.
CEIL=$(( EFFBUDGET + 40 ))
say "verifying /dash/$GW/data terminates within ${CEIL}s (budget ${EFFBUDGET}s) ..."
OUT=$(curl -s -o /tmp/gwtimeout_probe.json -w '%{http_code} %{time_total}' --max-time "$CEIL" \
      "http://127.0.0.1:9090/dash/$GW/data")
CODE=${OUT%% *}; SECS=${OUT##* }
say "  -> HTTP $CODE in ${SECS}s"

case "$CODE" in
  200) say "  200: completed inside the budget." ;;
  503) say "  503: the budget aborted it — THIS IS THE EXPECTED RESULT until step 2 lands."
       head -c 400 /tmp/gwtimeout_probe.json; echo ;;
  000) # 000 is ambiguous: a hang exhausts --max-time, a refused connection returns in microseconds.
       if awk "BEGIN{exit !($SECS < 5)}"; then
         say "VERIFY FAILED: connection refused after ${SECS}s — the app is not serving, not hanging."
       else
         say "VERIFY FAILED: still hanging at ${CEIL}s. The budget did not take effect."
       fi
       journalctl -u "$SVC" -n 25 --no-pager; restore; exit 1 ;;
  *)   say "VERIFY FAILED: unexpected HTTP $CODE"; head -c 400 /tmp/gwtimeout_probe.json; echo; restore; exit 1 ;;
esac

# The page shell and /ops share the process — a regression there is an operator-visible outage.
SHELL_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 "http://127.0.0.1:9090/dash")
[ "$SHELL_CODE" = 200 ] || { say "VERIFY FAILED: /dash shell -> HTTP $SHELL_CODE"; restore; exit 1; }
say "  /dash shell: 200"
OPS_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$OPS_CODE" = 200 ] || { say "VERIFY FAILED: /ops/$GW/data -> HTTP $OPS_CODE"; restore; exit 1; }
say "  /ops/$GW/data: 200"

say "RESULT: PASS — budget live, /dash/$GW/data TERMINATES. Backup at $BAK"
say "  next: watch RSS. A stranded request can no longer hold rows past ${EFFBUDGET}s."
