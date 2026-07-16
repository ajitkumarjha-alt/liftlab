#!/usr/bin/env bash
# PROVE THE RESTORE — an untested backup isn't a backup. Restores gateway.db from the GCS replica to
# a scratch path, opens it, counts rows per table, and compares to the LIVE db. RUN AS ROOT on
# liftlab-cloud: sudo bash /tmp/restore_proof.sh
# FILES NEEDED IN /tmp: restore_proof.sh  (litestream + /etc/litestream.yml already installed)
set -uo pipefail
say(){ echo "[restore-proof] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
command -v litestream >/dev/null || { echo "litestream not installed — run apply_litestream.sh first"; exit 2; }
DB="${DB:-$(systemctl show liftlab-cloud -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)}"
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
OUT="/tmp/gateway_restore_$(date +%s).db"

say "restoring from the GCS replica -> $OUT"
if ! litestream restore -config /etc/litestream.yml -o "$OUT" "$DB"; then
  say "RESTORE FAILED — the backup is NOT usable. journalctl -u litestream -n 40"; exit 1
fi
[ -s "$OUT" ] || { say "restored file is empty — FAIL"; exit 1; }
say "restored OK. Row counts (RESTORED vs LIVE) per table:"
printf "    %-16s %10s %10s   %s\n" table restored live match
TABLES="gw_event watch_status gw_source channel_map relay_status transit_event"
allmatch=1
for t in $TABLES; do
  r=$(sqlite3 "$OUT" "SELECT COUNT(*) FROM $t" 2>/dev/null || echo NA)
  l=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $t" 2>/dev/null || echo NA)
  if [ "$r" = "$l" ]; then m="exact";
  elif [ "$r" != NA ] && [ "$l" != NA ] && [ "$((l - r))" -ge 0 ] && [ "$((l - r))" -le 5 ]; then m="+$((l-r)) live (repl lag)";
  else m="MISMATCH"; allmatch=0; fi
  printf "    %-16s %10s %10s   %s\n" "$t" "$r" "$l" "$m"
done
# integrity of the restored copy
INTEG=$(sqlite3 "$OUT" "PRAGMA integrity_check;" 2>/dev/null | head -1)
say "restored DB integrity_check: $INTEG"
say "point-in-time generations available:"; litestream snapshots "$DB" 2>/dev/null | sed 's/^/    /' | head -6
rm -f "$OUT"
if [ "$allmatch" = 1 ] && [ "$INTEG" = "ok" ]; then
  say "RESULT: PASS — the replica restores to a valid db whose rows match live (within ~1s repl lag)."
else
  say "RESULT: CHECK — a table mismatched by more than the lag, or integrity != ok. Investigate before trusting."
  exit 1
fi
