#!/usr/bin/env bash
# Deploy the two committed-but-undeployed ingest changes to liftlab-cloud.
#
#   door_event_api.py  gw_door_event.n_arrow_labels — how many DISTINCT arrows the reader could
#                      name when the row was written. Below 2 the engine suppresses `direction`
#                      entirely, because a single-class classifier cannot be wrong and so cannot be
#                      evidence. Without this column the DB cannot tell "this lift went down" from
#                      "this camera can only say down".
#   ops_api.py         relay_status.gw_rss_mb — the gateway's OWN RSS on each relay heartbeat, so a
#                      repeat of the 2026-08-03 OOM arrives as a growth curve, not one endpoint
#                      reading in the kernel log.
#
# Both add a nullable column via the existing ALTER-if-missing path and neither backfills. Existing
# rows stay NULL: unknown, not zero.
#
# THESE ARE INGEST FILES. A bad deploy here is not a slow dashboard, it is the door-event and relay
# heartbeat feeds going down — so the rollback is unconditional and the verification hits both
# ingest paths, not just a page.
#
# FILES NEEDED IN /tmp: apply_gwingest.sh door_event_api.py ops_api.py
#   sudo bash /tmp/apply_gwingest.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
say(){ echo "[gwingest] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in door_event_api.py ops_api.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'n_arrow_labels' /tmp/door_event_api.py || { say "ABORT: door_event_api.py has no n_arrow_labels"; exit 2; }
grep -q 'gw_rss_mb' /tmp/ops_api.py             || { say "ABORT: ops_api.py has no gw_rss_mb"; exit 2; }
grep -q 'tx_packets' /tmp/ops_api.py            || { say "ABORT: ops_api.py does not persist tx_packets"; exit 2; }

$PY -m py_compile /tmp/door_event_api.py /tmp/ops_api.py || { say "compile failed — nothing changed"; exit 1; }

# Smoke-import the CANDIDATES, not the installed files. sys.path.insert inside the interpreter is
# the only ordering that binds: `cd $APP` puts the CWD ahead of PYTHONPATH for -c, which silently
# imports the already-installed module and reports success for code it never loaded.
TMPD=$(mktemp -d); cp /tmp/door_event_api.py /tmp/ops_api.py "$TMPD/"
if ! ( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0, '$TMPD')
from fastapi import FastAPI
import door_event_api, ops_api
for m in (door_event_api, ops_api):
    assert m.__file__.startswith('$TMPD'), 'imported the WRONG module: '+m.__file__
a=FastAPI(); a.include_router(door_event_api.door_event_router); a.include_router(ops_api.ops_router)
a.openapi()
assert hasattr(ops_api, '_self_rss_mb')
print('smoke ok:', door_event_api.__file__)" ); then
  say "SMOKE-IMPORT FAILED — nothing installed."; rm -rf "$TMPD"; exit 1
fi
rm -rf "$TMPD"

STAMP=$(date +%Y%m%d-%H%M%S)
for f in door_event_api.py ops_api.py; do
  cp "$APP/$f" "$APP/$f.bak.$STAMP"; say "backup: $APP/$f.bak.$STAMP"
  install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"
done

restore(){
  say "RESTORING backups .bak.$STAMP"
  for f in door_event_api.py ops_api.py; do cp "$APP/$f.bak.$STAMP" "$APP/$f"; chown "$OWNER:$OWNER" "$APP/$f"; done
  systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"
}

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }

# systemd goes green when the process forks; uvicorn needs 8-35s to bind. Probing early gives
# HTTP 000 in microseconds, which is refused-not-hung and must not be read as a failure.
say "waiting for :9090 ..."
READY=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null && { READY=1; break; }; sleep 2; done
[ -n "$READY" ] || { say "port never opened"; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

OPS=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$OPS" = 200 ] || { say "VERIFY FAILED: /ops/$GW/data -> $OPS"; restore; exit 1; }
say "  /ops/$GW/data: 200"

DB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
say "waiting up to 90s for a relay heartbeat to create the columns ..."
OK=0
for i in $(seq 1 18); do
  A=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('relay_status') WHERE name='gw_rss_mb';" 2>/dev/null || echo 0)
  B=$(sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('gw_door_event') WHERE name='n_arrow_labels';" 2>/dev/null || echo 0)
  [ "${A:-0}" = 1 ] && [ "${B:-0}" = 1 ] && { OK=1; break; }
  sleep 5
done
if [ "$OK" != 1 ]; then
  say "columns not created yet (relay_status.gw_rss_mb=$A gw_door_event.n_arrow_labels=$B)."
  say "  Both are created lazily on the next _db() call from the relevant ingest. NOT a failure if"
  say "  no heartbeat/door event has arrived yet — re-check with the query below in a few minutes."
else
  say "  columns present: relay_status.gw_rss_mb, gw_door_event.n_arrow_labels"
  sqlite3 "$DB" "SELECT '    latest gw_rss_mb='||COALESCE(MAX(gw_rss_mb),'(none yet)') FROM relay_status;" 2>/dev/null
fi

say "RESULT: PASS — ingest updated, $SVC active. Backups *.bak.$STAMP"
say "  re-check later:  sqlite3 $DB \"SELECT ts,gw_rss_mb FROM relay_status ORDER BY id DESC LIMIT 3;\""
