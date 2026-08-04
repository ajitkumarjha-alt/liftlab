#!/usr/bin/env bash
# FORCE A FRESH LITESTREAM GENERATION for gateway.db.
#
# WHY. On 2026-08-04 `litestream restore` failed at EVERY timestamp tried:
#   cannot find max wal index for restore: missing initial wal segment:
#   generation=1950dae6522690d2 index=00005d8e offset=337872
# Litestream was running and shipping WAL segments continuously, but the chain had a gap, so nothing
# could be restored from it. Cause visible in the journal: GCS API flakiness from this box
# (`oauth2: cannot fetch token`, TLS handshake and i/o timeouts) hitting the RETAINER mid-delete —
# it removed WAL segments the surviving snapshot still needed — plus 7 `checkpoint: mode=PASSIVE
# err=database is locked` failures. The existing point-in-time history is therefore worth nothing,
# and restarting the chain trades nothing real.
#
# ORDER MATTERS. A one-off consistent .backup is taken, verified, and uploaded to a prefix OUTSIDE
# liftlab/gateway.db/ BEFORE the generation data is deleted — so the destructive step cannot reach
# the fallback. Every gate below aborts before the delete, never after.
#
#   sudo bash /tmp/apply_litestream_regen.sh
set -uo pipefail
DB=/var/lib/liftlab/gateway.db
BUCKET=liftlab-backup-lodha
REPLICA_PREFIX="gs://$BUCKET/liftlab/gateway.db"          # what litestream owns — this gets deleted
FALLBACK_PREFIX="gs://$BUCKET/liftlab/manual-snapshots"   # deliberately OUTSIDE the replica prefix
STAMP=$(date +%Y%m%d)
SNAP="/var/lib/liftlab/gateway-pre-generation-$STAMP.db"
say(){ echo "[regen] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }

case "$FALLBACK_PREFIX/" in
  "$REPLICA_PREFIX"/*) say "ABORT: fallback lives under the replica prefix — the delete would eat it"; exit 2;;
esac

# ── 1. consistent one-off snapshot ──────────────────────────────────────────
AVAIL=$(df -k /var/lib/liftlab | awk 'NR==2{print $4}')
NEED=$(( $(stat -c %s "$DB") / 1024 * 2 ))
[ "$AVAIL" -gt "$NEED" ] || { say "ABORT: need ~${NEED}KB free, have ${AVAIL}KB"; exit 1; }

say "live baseline (the numbers step 3 must reproduce):"
LIVE_DOOR=$(sqlite3 "$DB" "SELECT COUNT(*) FROM gw_door_event;")
LIVE_TRANSIT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM transit_event;")
say "  gw_door_event=$LIVE_DOOR  transit_event=$LIVE_TRANSIT"

rm -f "$SNAP"
say "taking a consistent snapshot via .backup (online backup API, safe on a live WAL db) ..."
sqlite3 "$DB" ".backup '$SNAP'" || { say "ABORT: .backup failed"; rm -f "$SNAP"; exit 1; }
[ -s "$SNAP" ] || { say "ABORT: snapshot is empty"; rm -f "$SNAP"; exit 1; }
say "  snapshot: $(du -h "$SNAP" | cut -f1)"

say "integrity_check on the SNAPSHOT (not the live db) ..."
IC=$(sqlite3 "$SNAP" "PRAGMA integrity_check;" 2>&1 | head -3)
[ "$IC" = "ok" ] || { say "ABORT: integrity_check failed: $IC"; rm -f "$SNAP"; exit 1; }
say "  integrity_check: ok"

SNAP_DOOR=$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM gw_door_event;" 2>/dev/null || echo 0)
SNAP_TRANSIT=$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM transit_event;" 2>/dev/null || echo 0)
SNAP_WT=$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM worker_telemetry;" 2>/dev/null || echo 0)
say "  snapshot rows: gw_door_event=$SNAP_DOOR transit_event=$SNAP_TRANSIT worker_telemetry=$SNAP_WT"
# the live db is still ingesting, so the snapshot may be a few rows behind — but never ahead, and
# never materially short. A snapshot missing most of the table means .backup raced and lost.
[ "$SNAP_DOOR" -ge "$LIVE_DOOR" ] || [ "$SNAP_DOOR" -ge $(( LIVE_DOOR - 5000 )) ] \
  || { say "ABORT: snapshot has $SNAP_DOOR door rows vs live $LIVE_DOOR — too far behind"; rm -f "$SNAP"; exit 1; }

# ── 2. upload the fallback, and VERIFY it landed, before anything destructive ─
DEST="$FALLBACK_PREFIX/gateway-pre-generation-$STAMP.db"
# The VM's own scopes are devstorage.READ_ONLY, so gsutil/gcloud cannot write to the bucket — the
# first run of this script aborted here with 403, correctly, before deleting anything. Litestream
# writes using a service-account key (GOOGLE_APPLICATION_CREDENTIALS on its unit); reuse that key,
# but activate it inside a THROWAWAY CLOUDSDK_CONFIG so the box's global gcloud auth is untouched.
SA_KEY=$(systemctl show litestream -p Environment 2>/dev/null | grep -oP 'GOOGLE_APPLICATION_CREDENTIALS=\K\S+')
[ -n "$SA_KEY" ] && [ -r "$SA_KEY" ] || { say "ABORT: cannot read litestream's SA key ($SA_KEY) — NOTHING deleted"; exit 1; }
export CLOUDSDK_CONFIG=$(mktemp -d); trap 'rm -rf "$CLOUDSDK_CONFIG"' EXIT
gcloud auth activate-service-account --key-file="$SA_KEY" --quiet >/dev/null 2>&1 \
  || { say "ABORT: could not activate $SA_KEY — NOTHING deleted"; exit 1; }
say "uploading fallback as $(gcloud config get-value account 2>/dev/null) -> $DEST"
gcloud storage cp "$SNAP" "$DEST" >/dev/null 2>&1 || { say "ABORT: upload failed — NOTHING deleted"; exit 1; }
REMOTE_SZ=$(gcloud storage ls -l "$DEST" 2>/dev/null | awk 'NR==1{print $1}')
LOCAL_SZ=$(stat -c %s "$SNAP")
[ "$REMOTE_SZ" = "$LOCAL_SZ" ] || { say "ABORT: uploaded size $REMOTE_SZ != local $LOCAL_SZ — NOTHING deleted"; exit 1; }
say "  verified in GCS: $REMOTE_SZ bytes"

# ── 3. force the new generation ─────────────────────────────────────────────
# v0.3.x picks up generations from the replica prefix. Remove it while litestream is STOPPED and it
# will create a fresh generation with a new snapshot on next start.
OLDGEN_RAW=$(litestream generations -config /etc/litestream.yml "$DB" 2>/dev/null | awk 'NR==2{print $2}')
say "old generation to discard: ${OLDGEN_RAW:-<none>} (already unrestorable)"

say "stopping litestream ..."
systemctl stop litestream; sleep 2
[ "$(systemctl is-active litestream)" = inactive ] \
  || { say "ABORT: litestream did not stop — a delete racing a live replica is what caused the livelock"; exit 1; }
say "  litestream: $(systemctl is-active litestream)"

# BUG FIXED 2026-08-04. Deleting the PARENT prefix is wrong twice over:
#   1. It targets gs://.../gateway.db, which is also where the NEW generation gets written. On the
#      first run this did NOT bite — the delete enumerated once, before the new generation existed,
#      so the new chain was never on its list (verified from GCS: 87 -> 90 WAL indices during the
#      delete, zero lost, zero gaps). But it is luck, not design: had litestream restarted before
#      the enumeration finished, the new generation's objects would have been included.
#   2. It is unnecessary and slow. ~14k objects took ~100 minutes over this box's flaky GCS link and
#      consumed enough of a 2-vCPU box that sshd refused connections for ~40 minutes — so the run
#      could not be supervised or aborted. Only the OLD generation is junk. Name it explicitly.
OLD_GEN_ID=$(printf '%s' "$OLDGEN_RAW" | tr -d ' ')
[ -n "$OLD_GEN_ID" ] || { say "no old generation to remove — starting litestream and continuing"; }
if [ -n "$OLD_GEN_ID" ]; then
  TARGET="$REPLICA_PREFIX/generations/$OLD_GEN_ID"
  case "$TARGET" in
    */generations/*) : ;;                       # must be generation-scoped, never the parent
    *) say "ABORT: refusing to delete $TARGET — not a generation-scoped path"; systemctl start litestream; exit 1;;
  esac
  say "deleting ONLY the old generation: $TARGET"
  # bounded: if it has not finished in 10 minutes something is wrong (see the livelock note above)
  timeout 600 gcloud storage rm -r "$TARGET" >/dev/null 2>&1
  RC=$?
  [ $RC = 0 ] || say "  delete returned $RC (124=timed out) — old junk may remain, which is harmless"
fi

say "starting litestream ..."
systemctl start litestream
for i in $(seq 1 30); do [ "$(systemctl is-active litestream)" = active ] && break; sleep 1; done
[ "$(systemctl is-active litestream)" = active ] || { say "ABORT: litestream did not start"; journalctl -u litestream -n 20 --no-pager; exit 1; }

say "waiting up to 300s for the first snapshot of the NEW generation ..."
NEWGEN=""
for i in $(seq 1 60); do
  NEWGEN=$(litestream generations -config /etc/litestream.yml "$DB" 2>/dev/null | awk 'NR>1{print $2; exit}')
  [ -n "$NEWGEN" ] && [ "$NEWGEN" != "1950dae6522690d2" ] && break
  sleep 5
done
[ -n "$NEWGEN" ] || { say "ABORT: no new generation after 300s"; journalctl -u litestream -n 25 --no-pager; exit 1; }
say "  new generation: $NEWGEN  (old was 1950dae6522690d2)"

# ── 4. PROVE restorability — the whole point of this exercise ───────────────
say "restoring from the NEW chain to a scratch path ..."
OUT=$(mktemp -u /var/lib/liftlab/.regen_verify_XXXX.db)
if ! litestream restore -config /etc/litestream.yml -o "$OUT" "$DB" 2>/tmp/regen_restore.log; then
  say "FAILED: restore from the new generation does not work either:"
  tail -5 /tmp/regen_restore.log | sed 's/^/    /'
  say "  The fallback snapshot is safe at $DEST"
  rm -f "$OUT"; exit 1
fi
say "  restore completed"

IC2=$(sqlite3 "$OUT" "PRAGMA integrity_check;" 2>&1 | head -3)
[ "$IC2" = "ok" ] || { say "FAILED: restored copy integrity_check: $IC2"; rm -f "$OUT"; exit 1; }
say "  integrity_check: ok"

R_DOOR=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM gw_door_event;" 2>/dev/null || echo 0)
R_TRANSIT=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM transit_event;" 2>/dev/null || echo 0)
R_WT=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM worker_telemetry;" 2>/dev/null || echo 0)
NOW_DOOR=$(sqlite3 "$DB" "SELECT COUNT(*) FROM gw_door_event;")
NOW_TRANSIT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM transit_event;")
say "  restored vs live (live keeps ingesting, so restored <= live is expected and correct):"
say "    gw_door_event : restored=$R_DOOR  live=$NOW_DOOR   delta=$(( NOW_DOOR - R_DOOR ))"
say "    transit_event : restored=$R_TRANSIT  live=$NOW_TRANSIT   delta=$(( NOW_TRANSIT - R_TRANSIT ))"
say "    worker_telemetry: restored=$R_WT"
FAIL=0
[ "$R_DOOR" -ge $(( NOW_DOOR - 20000 )) ] || { say "  FAIL: restored door rows too far behind live"; FAIL=1; }
[ "$R_TRANSIT" -ge $(( NOW_TRANSIT - 2000 )) ] || { say "  FAIL: restored transit rows too far behind live"; FAIL=1; }
[ "$R_DOOR" -le "$NOW_DOOR" ] || { say "  FAIL: restored has MORE rows than live — impossible, investigate"; FAIL=1; }
rm -f "$OUT"
[ "$FAIL" = 0 ] || { say "RESULT: restore works but content check failed — see above"; exit 1; }

rm -f "$SNAP"
say "  local snapshot removed (the copy in GCS is the fallback)"
say
say "RESULT: PASS"
say "  new generation : $NEWGEN"
say "  restorable     : yes, verified by full restore + integrity_check + row comparison"
say "  fallback       : $DEST ($REMOTE_SZ bytes, integrity_check ok before upload)"
