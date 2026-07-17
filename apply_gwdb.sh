#!/usr/bin/env bash
# UNIFY THE DB. Symptom: "[analysis_api] gw_event columns: []" -> backfill DISABLED -> gw_event.boarded/
# alighted stay NULL, so door timing and rider counts live in separate files. Cause: the analysis/
# validation/ops routers default to ./gateway.db (CWD-relative), a DIFFERENT file than the door ingest's
# real gateway.db that holds gw_event. Fix: pin GATEWAY_DB to the file that actually has gw_event so ALL
# routers open the same DB as the ingest. DISCOVERY-FIRST: it scans + REPORTS which tables live in which
# file, moves NO data, and only sets a systemd env var. It never touches main.py or the ingest itself.
# FILES NEEDED IN /tmp: apply_gwdb.sh
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; curl -fsSL -o /tmp/apply_gwdb.sh $B/apply_gwdb.sh
#   sudo bash /tmp/apply_gwdb.sh          # or override discovery: sudo DB=/path/to/gateway.db bash /tmp/apply_gwdb.sh
set -uo pipefail
say(){ echo "[gwdb] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python; [ -x "$PY" ] || PY=$(command -v python3 || echo python3)
SVC=liftlab-cloud
DROPIN=/etc/systemd/system/$SVC.service.d/gatewaydb.conf

CURENV=$(systemctl show "$SVC" -p Environment 2>/dev/null | tr ' ' '\n' | grep -oP '^GATEWAY_DB=\K\S+' | head -1)
WD=$(systemctl show "$SVC" -p WorkingDirectory --value 2>/dev/null)
say "service GATEWAY_DB='${CURENV:-<unset>}'  WorkingDirectory='${WD:-<unset>}'  python=$PY"

# introspect one db file -> 'gw_event:<n|NO> transit_event:<n|NO> validation_item:<n|NO> watch_status:<n|NO>'
inspect(){ "$PY" - "$1" <<'PYEOF'
import sqlite3, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print("MISSING"); sys.exit()
try:
    db = sqlite3.connect(p)
    def has(t):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None
    def n(t):
        try: return db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception: return "ERR"
    print(" ".join(f"{t}:{n(t) if has(t) else 'NO'}"
                   for t in ("gw_event", "transit_event", "validation_item", "watch_status")))
except Exception as e:
    print(f"OPENERR {e}")
PYEOF
}

# candidates, most-authoritative first. DB=... override wins.
CANDS=()
[ -n "${DB:-}" ] && CANDS+=("$DB")
[ -n "$CURENV" ] && CANDS+=("$CURENV")
[ -n "$WD" ] && CANDS+=("$WD/gateway.db")
CANDS+=("/var/lib/liftlab/gateway.db" "$APP/gateway.db" "/var/lib/liftlab-cloud/gateway.db" "/gateway.db")

say "scanning for the gw_event table (the ingest's real DB):"
GWDB=""
declare -A SEEN
for f in "${CANDS[@]}"; do
  [ -n "${SEEN[$f]:-}" ] && continue; SEEN[$f]=1
  info=$(inspect "$f")
  say "  $f -> $info"
  if [ -z "$GWDB" ] && echo "$info" | grep -qE 'gw_event:[0-9]'; then GWDB="$f"; fi
done
[ -n "$GWDB" ] || { say "RESULT: FAIL — no candidate has a populated gw_event table. Rerun with DB=/real/path/gateway.db"; exit 1; }
say "==> gw_event lives in: $GWDB"

if [ -n "$CURENV" ] && [ "$CURENV" = "$GWDB" ]; then
  say "GATEWAY_DB already points at the gw_event DB — columns:[] was a stale in-process cache; restart clears it."
else
  say "MISMATCH: routers were NOT reading $GWDB. Pinning GATEWAY_DB=$GWDB so analysis/validation/ops unify with the ingest."
  say "  NOTE: any transit_event/validation_item rows in the routers' OLD file re-init in $GWDB. That's fine —"
  say "        you're bumping COUNTING_VERSION (11m), so those verdicts reset anyway; door history stays intact in $GWDB."
fi

mkdir -p "$(dirname "$DROPIN")"
printf '[Service]\nEnvironment=GATEWAY_DB=%s\n' "$GWDB" > "$DROPIN"
systemctl daemon-reload
systemctl restart "$SVC"; sleep 4
[ "$(systemctl is-active "$SVC")" = active ] || { say "RESULT: CHECK — $SVC not active after restart; journalctl -u $SVC -n 40"; exit 1; }

VER=$(inspect "$GWDB")
say "AFTER: $SVC=active  GATEWAY_DB=$GWDB -> $VER"
if echo "$VER" | grep -qE 'gw_event:[0-9]'; then
  say "RESULT: PASS — routers now open the gw_event DB."
  say "  Finish the deliverable: POST the one-shot backfill so existing transits fill gw_event.boarded/alighted:"
  say "    curl -fsS -X POST -H 'Authorization: Bearer <analysis-token>' https://lift.gargi.online/api/gw/site-A/backfill_transits"
  say "  Then confirm no longer disabled: journalctl -u $SVC | grep 'backfill active'  (NOT 'backfill DISABLED')."
else
  say "RESULT: CHECK — gw_event still not visible at $GWDB"; exit 1
fi
