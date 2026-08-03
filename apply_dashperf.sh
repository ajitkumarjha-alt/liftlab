#!/usr/bin/env bash
# /dash PERFORMANCE. Two changes in dash_api.py, both pure optimisations — every number the page
# reports is byte-identical before and after. main.py is NOT touched (dash_router is already mounted).
#
#   1. _tier2: `for nxt in rows[idx+1:]` -> `itertools.islice(rows, idx+1, None)`.
#      The slice copied the ENTIRE remaining row list on every door-open transition, then broke out
#      of the loop after a handful of rows. Linear work, quadratic allocation: with ~60k era rows per
#      camera and thousands of cycles inside them, gigabytes of list churn per camera, seven cameras
#      per request. This is the single biggest cost on the page.
#
#   2. _transit_by_cam: counted 22k+ transit rows in Python to produce five numbers per camera.
#      Now a GROUP BY in SQL. The counting rule is preserved exactly ('in' boards, anything else
#      alights, last_ts ignores null timestamps).
#
# WHY NOT INDEXES: the DB was never unindexed. gw_door_event already has ix_door_event(gateway_id,
# cam,ts) and every hot /dash plan is already SEARCH, not SCAN. The /ops indexes are a separate,
# separately-justified migration — see apply_gwindex.sh.
#
# SAFETY: compile -> smoke-import -> install -> restart -> verify HTTP 200 -> auto-restore on any
# failure. The gateway ingest (segment PUT/DELETE, heartbeats) shares this process, so a bad deploy
# is an ingest outage; that is why the rollback is unconditional rather than advisory.
#
# ops_api.py is installed too when present in /tmp: it adds relay_status.gw_rss_mb, the gateway's own
# RSS sampled on every relay heartbeat, so the next memory event arrives as a curve rather than as a
# single number in the kernel log. The column is added by the existing ALTER-if-missing path in
# _db(); the table is already capped at 720 rows so it adds no storage growth.
#
# FILES NEEDED IN /tmp: apply_dashperf.sh dash_api.py [ops_api.py]
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_dashperf.sh dash_api.py ops_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_dashperf.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
say(){ echo "[dashperf] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/dash_api.py ] || { echo "missing /tmp/dash_api.py"; exit 2; }
[ -f "$APP/dash_api.py" ] || { echo "dash_api.py not installed at $APP — use apply_dash.sh first"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# The incoming file must actually contain both fixes, otherwise this is a no-op deploy that still
# costs a restart.
grep -q 'itertools.islice(rows, idx + 1, None)' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py does not contain the islice fix — wrong file?"; exit 2; }
grep -q 'GROUP BY cam' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py does not contain the _transit_by_cam SQL aggregation"; exit 2; }

FILES=(dash_api.py)
[ -f /tmp/ops_api.py ] && FILES+=(ops_api.py)
say "installing: ${FILES[*]}"

for f in "${FILES[@]}"; do
  $PY -m py_compile "/tmp/$f" || { say "compile failed on $f — aborting, nothing changed"; exit 1; }
done

# SMOKE-IMPORT the NEW files from a scratch dir, ahead of the live ones on PYTHONPATH, so a broken
# import or route fails HERE rather than after the ingest has already been taken down.
TMPD=$(mktemp -d)
for f in "${FILES[@]}"; do cp "/tmp/$f" "$TMPD/$f"; done
if ! ( cd "$APP" && PYTHONPATH="$TMPD:$APP" $PY -c "
from fastapi import FastAPI
import dash_api, ops_api
a=FastAPI()
a.include_router(dash_api.dash_router); a.include_router(ops_api.ops_router)
a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — not installing. Live app untouched."; rm -rf "$TMPD"; exit 1
fi
rm -rf "$TMPD"

STAMP=$(date +%Y%m%d-%H%M%S)
for f in "${FILES[@]}"; do
  cp "$APP/$f" "$APP/$f.bak.$STAMP"
  say "backup: $APP/$f.bak.$STAMP"
  install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"
done

restore(){
  say "RESTORING backups .bak.$STAMP"
  for f in "${FILES[@]}"; do
    cp "$APP/$f.bak.$STAMP" "$APP/$f"; chown "$OWNER:$OWNER" "$APP/$f"
  done
  systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"
}

systemctl restart "$SVC"
for i in $(seq 1 30); do
  [ "$(systemctl is-active "$SVC")" = active ] && break
  sleep 1
done
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "service did not come up"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1
fi

# Verify the page actually answers. Generous timeout: the FIRST request after a restart still walks
# the full era, and on a cold page cache that is not instant even with the fix in.
say "verifying /dash/$GW/data ..."
CODE=$(curl -s -o /tmp/dashperf_probe.json -w '%{http_code}' --max-time 240 \
        "http://127.0.0.1:9090/dash/$GW/data")
if [ "$CODE" != 200 ]; then
  say "VERIFY FAILED: /dash/$GW/data -> HTTP $CODE"; head -c 400 /tmp/dashperf_probe.json; echo; restore; exit 1
fi
say "verify OK: HTTP 200, $(stat -c%s /tmp/dashperf_probe.json) bytes"

# /ops must answer too — ops_api.py carries the relay ingest, so a break here is a heartbeat outage.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$CODE" = 200 ] || { say "VERIFY FAILED: /ops/$GW/data -> HTTP $CODE"; restore; exit 1; }
say "verify OK: /ops/$GW/data HTTP 200"

say "RESULT: PASS — ${FILES[*]} updated, $SVC active. Backups kept at *.bak.$STAMP"
say "  measure: for i in 1 2 3 4 5; do curl -s -o /dev/null -w '%{time_total}\n' http://127.0.0.1:9090/dash/$GW/data; done"
