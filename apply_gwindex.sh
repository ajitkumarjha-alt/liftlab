#!/usr/bin/env bash
# INDEXES FOR /ops. Derived from EXPLAIN QUERY PLAN against the live schema, not from guesswork.
#
# WHAT THIS IS NOT: it is NOT a blanket index sweep, and it does NOT fix /dash. The gateway DB was
# never unindexed — gw_door_event already has ix_door_event(gateway_id,cam,ts) and transit_event has
# ux_transit(...). A `SELECT name,(SELECT COUNT(*) FROM pragma_index_list(name)) FROM sqlite_master`
# reports 0 for every table because the correlated argument does not reach the table-valued pragma;
# use `SELECT tbl_name,COUNT(*) FROM sqlite_master WHERE type='index' GROUP BY tbl_name` instead.
#
# WHAT IT FIXES: the /ops queries whose plans show the index being entered on gateway_id ALONE and
# then fetching every row of the table to evaluate a ts range. Each index below names the query it
# was derived from. /dash is a separate problem (unbounded per-request materialisation) and is fixed
# in dash_api.py, not here.
#
# LIVE-SAFE BY CONSTRUCTION: CREATE INDEX IF NOT EXISTS, one statement at a time, each in its own
# implicit transaction, with a busy timeout so it yields to the ingest instead of erroring. Litestream
# replicates the resulting WAL frames like any other write — an index build is ordinary page writes,
# so there is no restore gap. The WAL is checkpointed and reported after each one.
#
# FILES NEEDED IN /tmp: apply_gwindex.sh
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; curl -fsSL -o /tmp/apply_gwindex.sh $B/apply_gwindex.sh
#   sudo bash /tmp/apply_gwindex.sh            # apply
#   sudo DRYRUN=1 bash /tmp/apply_gwindex.sh   # report only, create nothing
set -uo pipefail
say(){ echo "[gwindex] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }

DB="${DB:-$(systemctl show liftlab-cloud -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)}"
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
[ -f "$DB" ] || { say "gateway.db not found at '$DB' — pass DB=/path/to/gateway.db"; exit 2; }
command -v sqlite3 >/dev/null || { say "sqlite3 not installed"; exit 2; }
say "database: $DB"

# 30s busy timeout: an index build takes a write lock at the end. The ingest is writing continuously,
# so without this a contended moment is an immediate SQLITE_BUSY and a half-applied migration.
SQ(){ sqlite3 -cmd ".timeout 30000" "$DB" "$@"; }

wal_mb(){ local w="$DB-wal"; [ -f "$w" ] && echo "$(( $(stat -c%s "$w") / 1048576 ))MB" || echo "none"; }
report(){ say "    WAL now $(wal_mb)  checkpoint(PASSIVE) -> $(SQ 'PRAGMA wal_checkpoint(PASSIVE);')"; }

say "BEFORE: db $(( $(stat -c%s "$DB") / 1048576 ))MB  wal $(wal_mb)  journal_mode=$(SQ 'PRAGMA journal_mode;')"
say "existing indexes:"
SQ "SELECT '    '||name||'  ON '||tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"

# ── the indexes, each with the query that justifies it ────────────────────────
# 1. ops_api floor-collapse detector, BOTH windows (base 7d, recent 6h):
#      SELECT cam, COUNT(*), SUM(floor IS NOT NULL) FROM gw_door_event
#      WHERE gateway_id=? AND ts>=? AND ts<? GROUP BY cam
#    Plan before: SEARCH gw_door_event USING INDEX ix_door_event (gateway_id=?)  <- gateway_id ONLY,
#    so it walks all 428k index entries and fetches all 428k table rows to test ts and read floor.
#    ix_door_event is (gateway_id,cam,ts): with cam unconstrained the ts range cannot be used.
#    This index leads with ts so the window is a range scan, and carries cam+floor so the scan is
#    COVERING — no table fetch at all.
#    Write cost: ts is monotonic, so inserts append to the right edge of the b-tree (no page splits
#    mid-tree). This is CHEAPER per insert than the existing ix_door_event, whose (cam,ts) prefix
#    scatters writes across seven camera groups.
IDX1_NAME=ix_door_event_ts
IDX1_SQL="CREATE INDEX IF NOT EXISTS ix_door_event_ts ON gw_door_event (gateway_id, ts, cam, floor)"

# 2. ops_api per-camera transit recency + the trailing-14d hour-of-day rate:
#      SELECT cam, MAX(ts) FROM transit_event WHERE gateway_id=? GROUP BY cam
#      SELECT cam, COUNT(*), MIN(ts) FROM transit_event WHERE gateway_id=? AND ts>=? AND <hour> GROUP BY cam
#    and dash_api._transit_by_cam's SQL aggregation.
#    Plan before: SEARCH transit_event USING INDEX ux_transit (gateway_id=?) — grouping rides the
#    index, but ts is NOT in ux_transit (ts_bucket is), so every one of the 22k rows is fetched from
#    the table. Adding ts+direction makes all three queries index-only.
IDX2_NAME=ix_transit_cam_ts
IDX2_SQL="CREATE INDEX IF NOT EXISTS ix_transit_cam_ts ON transit_event (gateway_id, cam, ts, direction)"

# NOT CREATED, and why — this is the "do not blanket-index" half of the job:
#   transit_event(gateway_id, ts, direction) for `WHERE gateway_id=? AND ts>=? GROUP BY direction`.
#     IDX2 already makes that query index-only; it scans all 22k entries instead of range-scanning
#     the last 24h, but on 22k covering entries that is sub-millisecond. A second index on the same
#     table to save that is write cost for no measurable read.
#   gw_event / gw_source: _floor_coverage plans as SCAN e + rowid lookup, but gw_event is 4,242 rows.
#     Indexing a 4k table that is scanned once per request buys nothing measurable.
#   watch_status(gateway_id): _latest plans as SCAN, but it is ORDER BY id DESC LIMIT 1 — it stops
#     at the first row. O(1) already.

apply_one(){
  local name="$1" sql="$2"
  if SQ "SELECT 1 FROM sqlite_master WHERE type='index' AND name='$name';" | grep -q 1; then
    say "  $name: already exists — skip"; return 0
  fi
  if [ -n "${DRYRUN:-}" ]; then say "  $name: DRYRUN, would run: $sql"; return 0; fi
  say "  $name: creating ..."
  local t0 t1
  t0=$(date +%s)
  if ! SQ "$sql"; then say "  $name: FAILED (see error above) — stopping, nothing further applied"; return 1; fi
  t1=$(date +%s)
  say "  $name: created in $((t1-t0))s"
  report
  return 0
}

say "applying, one at a time:"
apply_one "$IDX1_NAME" "$IDX1_SQL" || exit 1
apply_one "$IDX2_NAME" "$IDX2_SQL" || exit 1

say "AFTER: db $(( $(stat -c%s "$DB") / 1048576 ))MB  wal $(wal_mb)"
say "integrity_check: $(SQ 'PRAGMA integrity_check;')"
say "indexes now:"
SQ "SELECT '    '||name||'  ON '||tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"

# Litestream: an index build is ordinary page writes in the WAL. Confirm the replica is still moving
# rather than assuming it — a generation change here would mean a restore gap.
if systemctl is-active --quiet litestream; then
  say "litestream: active"
  litestream snapshots "$DB" 2>&1 | tail -3 | sed 's/^/    /'
  say "  recent errors (should be none):"
  journalctl -u litestream --since "10 minutes ago" 2>/dev/null | grep -iE 'error|fatal' | tail -5 | sed 's/^/    /' || say "    none"
else
  say "litestream: NOT active — replication is not running, index build is local only"
fi
say "done."
