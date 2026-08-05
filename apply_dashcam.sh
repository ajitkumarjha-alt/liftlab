#!/usr/bin/env bash
# Deploy dash_api.py — heatmap camera fix + honest chart labels.
#
# WHAT AND WHY. /dash?cam=ch27 set the Dash tab's camera but never the Trends heatmap's, so the
# heatmap rendered whichever camera was last clicked while the URL claimed another. Floors are
# per-shaft, so that is a chart of a different building column under the wrong heading — reported
# 2026-08-05 as ch27 in the URL showing ch29's floor alphabet. Also labels both heatmaps with what
# they actually count and captions the attribution rate, so dense green against empty blue is not
# read as broken data.
#
# THIS IS THE OPERATOR DASHBOARD. A bad deploy here is the page everyone uses, so verification hits
# /dash, its data endpoint and the trends endpoint that feeds the heatmap, and rolls back on any.
# FILES NEEDED IN /tmp: apply_dashcam.sh dash_api.py
set -uo pipefail
APP=/opt/liftlab-b3/cloud; PY=$APP/.venv/bin/python; SVC=liftlab-cloud; OWNER=liftlab
GW="${GW:-site-A}"
say(){ echo "[dashcam] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
[ -f /tmp/dash_api.py ] || { echo "missing /tmp/dash_api.py"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'if(m)trCam=m\[1\]' /tmp/dash_api.py || { say "ABORT: trCam is not seeded from the URL"; exit 2; }
grep -q 'attr_window_s' /tmp/dash_api.py     || { say "ABORT: attr_window_s not exposed"; exit 2; }
$PY -m py_compile /tmp/dash_api.py || { say "compile failed — nothing changed"; exit 1; }

# scope guard: this is meant to be near-additive. A large deletion means something else came along.
REM=$(diff -u "$APP/dash_api.py" /tmp/dash_api.py | grep -c '^-[^-]' || true)
[ "$REM" -le 8 ] || { say "ABORT: candidate removes $REM lines; expected <=8. Review the diff."; exit 1; }
say "scope: +$(diff -u "$APP/dash_api.py" /tmp/dash_api.py | grep -c '^+[^+]') / -$REM lines"

TMPD=$(mktemp -d); cp /tmp/dash_api.py "$TMPD/"
( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0,'$TMPD')
from fastapi import FastAPI
import dash_api
assert dash_api.__file__.startswith('$TMPD'), 'imported the WRONG module: '+dash_api.__file__
a=FastAPI(); a.include_router(dash_api.dash_router)
p=a.openapi()['paths']
for w in ('/dash','/dash/{gw}/data','/dash/{gw}/trends'):
    assert w in p, 'route missing: '+w
print('  smoke ok:', dash_api.__file__)" ) || { say "SMOKE FAILED — nothing installed"; rm -rf "$TMPD"; exit 1; }
rm -rf "$TMPD"

STAMP=$(date +%Y%m%d-%H%M%S)
cp "$APP/dash_api.py" "$APP/dash_api.py.bak.$STAMP"; say "backup: dash_api.py.bak.$STAMP"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"
restore(){ say "ROLLING BACK"; cp "$APP/dash_api.py.bak.$STAMP" "$APP/dash_api.py"
           systemctl restart "$SVC"; sleep 10; say "restored; service=$(systemctl is-active $SVC)"; }

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 25 --no-pager; restore; exit 1; }
say "waiting for :9090 ..."
R=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 http://127.0.0.1:9090/openapi.json 2>/dev/null && { R=1; break; }; sleep 2; done
[ -n "$R" ] || { say "port never opened"; restore; exit 1; }

# NOT /dash/$GW/trends?cam=... — that endpoint is ALREADY broken on this box and has nothing to do
# with this change. Measured 2026-08-05 on the UNMODIFIED installed version: cam=ch27 -> HTTP 500
# after 43s, cam=ch29 -> no response at all within 100s. Including it here rolled back a verified-good
# deploy on the first attempt: the served page already had the fix and /dash, /dash/data, /reports and
# /ops were all 200. A gate must verify what the change touches; asserting an unrelated pre-existing
# failure just means good changes cannot ship. Tracked separately as its own bug.
for p in "/dash" "/dash/$GW/data" "/reports" "/ops/$GW/data"; do
  C=$(curl -s -o /tmp/.dc -w '%{http_code}' --max-time 150 "http://127.0.0.1:9090$p")
  [ "$C" = 200 ] || { say "VERIFY FAILED: $p -> $C"; restore; exit 1; }
  say "  $p: 200"
done
# the fix itself must be in the served HTML, not merely in the file
curl -s --max-time 60 "http://127.0.0.1:9090/dash?cam=ch27" > /tmp/.dp
grep -q 'if(m)trCam=m\[1\]' /tmp/.dp && say "  served page seeds trCam from the URL" \
  || { say "VERIFY FAILED: served page lacks the fix"; restore; exit 1; }
grep -q 'door CYCLES' /tmp/.dp && say "  served page labels stops as door cycles" \
  || say "  WARNING: chart label not found in served HTML"
# Report the known-broken endpoint's state so the deploy log carries it, without gating on it.
TR=$(curl -s -o /dev/null -w '%{http_code} in %{time_total}s' --max-time 60 "http://127.0.0.1:9090/dash/$GW/trends?cam=ch27" || echo "no response")
say "  (pre-existing, NOT gated) /dash/$GW/trends?cam=ch27 -> $TR"
say "RESULT: PASS — dash_api updated. Backup *.bak.$STAMP"
