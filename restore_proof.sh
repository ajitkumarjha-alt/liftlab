#!/usr/bin/env bash
# PROVE THE RESTORE — an untested backup isn't a backup. Restores gateway.db from the GCS replica to
# a scratch path, integrity-checks it, and compares row counts RESTORED vs LIVE. RUN AS ROOT on
# liftlab-cloud: sudo bash /tmp/restore_proof.sh
# FILES NEEDED IN /tmp: restore_proof.sh  (litestream + /etc/litestream.yml already installed)
#
# Reads via the cloud venv PYTHON (sqlite3 module is always present) — NOT the sqlite3 CLI, which
# isn't installed on this VM. And an unreadable count is a LOUD FAILURE, never silently "exact":
# the earlier version read NA everywhere (no CLI) and called NA==NA a match.
set -uo pipefail
say(){ echo "[restore-proof] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
command -v litestream >/dev/null || { echo "litestream not installed — run apply_litestream.sh first"; exit 2; }
DB="${DB:-$(systemctl show liftlab-cloud -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)}"
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
PY="${PY:-/opt/liftlab-b3/cloud/.venv/bin/python}"; [ -x "$PY" ] || PY=$(command -v python3 || echo python3)
OUT="/tmp/gateway_restore_$(date +%s).db"
cleanup(){ rm -f "$OUT" "$OUT"-wal "$OUT"-shm; }
trap cleanup EXIT

count(){ "$PY" -c "import sqlite3,sys
try: print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM '+sys.argv[2]).fetchone()[0])
except Exception: print('NA')" "$1" "$2" 2>/dev/null || echo NA; }
integ(){ "$PY" -c "import sqlite3,sys
try: print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])
except Exception as e: print('ERR:'+type(e).__name__)" "$1" 2>/dev/null || echo ERR; }

# reader sanity: the LIVE db MUST read (gw_event exists, ~770 rows). If not, the reader is broken —
# fail NOW rather than mislabel everything NA as 'exact'.
[ "$(count "$DB" gw_event)" != NA ] || { say "cannot read LIVE $DB via $PY — reader broken; fix before trusting any result"; exit 1; }

say "restoring from the GCS replica -> $OUT  (reader: $PY)"
if ! litestream restore -config /etc/litestream.yml -o "$OUT" "$DB"; then
  say "RESTORE FAILED — the backup is NOT usable. journalctl -u litestream -n 40"; exit 1
fi
[ -s "$OUT" ] || { say "restored file is empty/missing — FAIL"; exit 1; }
[ "$(count "$OUT" gw_event)" != NA ] || { say "restored db has no readable gw_event — FAIL (restore didn't produce a valid db)"; exit 1; }

say "restored OK. Row counts (RESTORED vs LIVE) per table  [read while the file still exists]:"
printf "    %-16s %10s %10s   %s\n" table restored live match
allok=1
for t in gw_event watch_status gw_source channel_map relay_status transit_event; do
  r=$(count "$OUT" "$t"); l=$(count "$DB" "$t")
  if [ "$l" = NA ]; then m="(not in live schema — skip)"
  elif [ "$r" = NA ]; then m="MISSING IN RESTORE"; allok=0
  elif [ "$r" = "$l" ]; then m="exact"
  elif [ "$((l - r))" -ge 0 ] && [ "$((l - r))" -le 5 ]; then m="+$((l - r)) live (repl lag)"
  else m="MISMATCH"; allok=0; fi
  printf "    %-16s %10s %10s   %s\n" "$t" "$r" "$l" "$m"
done
INTEG=$(integ "$OUT")
say "restored DB integrity_check: $INTEG"
say "point-in-time generations available:"; litestream snapshots "$DB" 2>/dev/null | sed 's/^/    /' | head -6

if [ "$allok" = 1 ] && [ "$INTEG" = "ok" ]; then
  say "RESULT: PASS — replica restores to a valid db whose rows match live (within ~1s repl lag)."
else
  say "RESULT: FAIL — a table was MISSING/MISMATCHED in the restore, or integrity != ok. Do NOT trust the backup yet."
  exit 1
fi
