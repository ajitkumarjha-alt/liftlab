#!/usr/bin/env bash
# Hourly Litestream restore of gateway.db onto dev-box, for /reports.
#
# WHY A RESTORE AND NOT A COPY. Two jobs in one:
#   1. it gives /reports a database to read that costs the gateway nothing — the whole reason the
#      feature moved here after /ops degraded to 26s during an export on the e2-small;
#   2. it is a CONTINUOUS RESTORE TEST. On 2026-08-04 the backup chain was found unrestorable at
#      every timestamp, and nothing noticed because nobody had tried a restore in weeks. Now
#      something tries, every hour, and a failure is surfaced on the page as a BACKUP ALARM rather
#      than a stale report.
#
# WHAT IT WRITES. A state JSON the page reads. On success it carries the restore point; on failure
# it carries the error and the LAST GOOD restore point, and the previous database is left in place —
# a failed refresh must degrade to "old but honest data, loudly flagged", never to a missing file or
# a silently empty page.
#
# LOCKING. Takes an exclusive flock that report_runner.py also takes for the whole of a build. The
# database is replaced by atomic rename; a build that straddled the swap would keep reading the old
# inode and silently report a different restore point than the page claims.
set -uo pipefail

DB="${LIFTLAB_DB:-/var/lib/liftlab/gateway.db}"
LOCK="${LIFTLAB_DB_LOCK:-/var/lib/liftlab/db.lock}"
STATE="${LIFTLAB_RESTORE_STATE:-/var/lib/liftlab/restore_state.json}"
CONF="${LITESTREAM_CONFIG:-/etc/liftlab/litestream-restore.yml}"
LOCK_WAIT="${LOCK_WAIT_S:-1800}"          # a 30d export takes ~10 min; wait rather than skip
TMP="$DB.restoring.$$"
say(){ echo "[refresh] $*"; }

IST(){ TZ=Asia/Kolkata date -d "@$1" "+%Y-%m-%d %H:%M"; }

# Write the state file ATOMICALLY. The page polls it every 60s; a half-written file would render as
# "refresh broken" and cause a false alarm, which is the one thing an alarm must not do.
write_state(){   # $1=ok(true|false) $2=error $3=restored_at $4=data_max_ts $5=rows $6=generation
  local t="$STATE.tmp.$$"
  python3 - "$t" "$1" "$2" "$3" "$4" "$5" "$6" <<'PY'
import json, sys, time
p, ok, err, ra, dmax, rows, gen = sys.argv[1:8]
def ist(v):
    if not v or v == "-": return None
    import datetime, zoneinfo
    return datetime.datetime.fromtimestamp(float(v),
        zoneinfo.ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
d = {"ok": ok == "true", "error": (err or None) if ok != "true" else None,
     "restored_at": float(ra) if ra and ra != "-" else None,
     "restored_at_h": ist(ra), "data_max_ts": float(dmax) if dmax and dmax != "-" else None,
     "data_max_ts_h": ist(dmax), "rows": int(rows) if rows and rows != "-" else None,
     "generation": gen if gen and gen != "-" else None, "written_at": time.time()}
if d["restored_at"]:
    age = time.time() - d["restored_at"]
    h, m = int(age // 3600), int((age % 3600) // 60)
    d["age_h"] = (f"{h}h {m}m" if h else f"{m}m")
json.dump(d, open(p, "w"), indent=1)
PY
  mv -f "$t" "$STATE"
  chmod 644 "$STATE"
}

# Preserve the last good restore point across a failure, so the page can say "showing data from X,
# but the refresh has been failing since Y" instead of losing both facts.
prev(){ python3 -c "
import json,sys
try: print(json.load(open('$STATE')).get('$1') or '-')
except Exception: print('-')" 2>/dev/null || echo '-'; }

fail(){
  say "FAILED: $1"
  write_state false "$1" "$(prev restored_at)" "$(prev data_max_ts)" "$(prev rows)" "$(prev generation)"
  rm -f "$TMP"
  exit 1
}

mkdir -p "$(dirname "$DB")" "$(dirname "$STATE")"
touch "$LOCK"

command -v litestream >/dev/null || fail "litestream is not installed on this box"
[ -r "$CONF" ] || fail "litestream config $CONF is missing or unreadable"

# ── serialize against exports ────────────────────────────────────────────────
exec 9>"$LOCK"
say "waiting for the db lock (up to ${LOCK_WAIT}s; an export in progress holds it) ..."
if ! flock -x -w "$LOCK_WAIT" 9; then
  fail "could not get the db lock within ${LOCK_WAIT}s — an export has been running that long"
fi
say "lock acquired"

# ── restore ──────────────────────────────────────────────────────────────────
rm -f "$TMP"
say "restoring from the replica ..."
if ! litestream restore -config "$CONF" -o "$TMP" "$DB" >/tmp/.refresh_restore.log 2>&1; then
  fail "litestream restore failed: $(tail -2 /tmp/.refresh_restore.log | tr '\n' ' ' | cut -c1-300)"
fi
[ -s "$TMP" ] || fail "restore produced an empty file"

# ── verify BEFORE swapping in ────────────────────────────────────────────────
IC=$(sqlite3 "$TMP" "PRAGMA integrity_check;" 2>&1 | head -1)
[ "$IC" = ok ] || fail "restored copy failed integrity_check: $IC"

ROWS=$(sqlite3 "$TMP" "SELECT COUNT(*) FROM gw_door_event;" 2>/dev/null || echo 0)
[ "${ROWS:-0}" -gt 0 ] || fail "restored copy has no gw_door_event rows — refusing to publish it"
DMAX=$(sqlite3 "$TMP" "SELECT CAST(MAX(ts) AS INT) FROM gw_door_event;" 2>/dev/null || echo 0)

# Never go BACKWARDS. A restore that lands older data than we already have means the chain regressed;
# publishing it would silently rewind every figure on the page.
if [ -f "$DB" ]; then
  OLD=$(sqlite3 "$DB" "SELECT CAST(MAX(ts) AS INT) FROM gw_door_event;" 2>/dev/null || echo 0)
  if [ "${DMAX:-0}" -lt "$(( ${OLD:-0} - 300 ))" ]; then
    fail "restored data is OLDER than what is already here ($(IST "$DMAX") vs $(IST "$OLD")) — chain regressed"
  fi
fi

# ── publish atomically ───────────────────────────────────────────────────────
chmod 640 "$TMP"
mv -f "$TMP" "$DB"
NOW=$(date +%s)

# AFTER the rename, not before. `litestream generations` resolves the replica via the LOCAL db path
# in the config, so on a first run — when that path does not exist yet — it fails with "database
# path or replica URL required" and the field lands null. Harmless, but a null in a state file
# invites someone to go looking for a fault that is not there.
GEN=$(litestream generations -config "$CONF" "$DB" 2>/dev/null | awk 'NR==2{print $2}')
write_state true "" "$NOW" "$DMAX" "$ROWS" "${GEN:--}"
say "OK: data to $(IST "$DMAX") IST, $ROWS door rows, generation ${GEN:-?}"
say "  restore point published at $(IST "$NOW") IST"
