#!/usr/bin/env bash
# STEP 2c: move the floor-alphabet derivation OFF the request path.
#
# /dash re-derived the alphabet from every floor-bearing row a camera had ever written, on every
# page load — 247,019 rows per request across cameras (ch29 alone 141,430). It is deliberately
# all-era (floors are physical; a rebuild moves door_version but not the building), so it cannot be
# windowed. It moves to a scheduled job writing `floor_alphabet`; /dash does one indexed read.
#
# ORDER MATTERS AND THIS SCRIPT ENFORCES IT. An EMPTY floor_alphabet table means alphabet=None,
# which the derivation treats as "accept all floors" — off-alphabet reads stop being rejected. That
# is a real behaviour change, not just a slow page. So the job runs and the table is verified
# populated BEFORE the new dash_api is allowed to serve. If the job fails, nothing is installed.
#
# FILES NEEDED IN /tmp: apply_gwalphabet.sh dash_api.py alphabet_job.py
#   sudo bash /tmp/apply_gwalphabet.sh
#   sudo EVERY=15min bash /tmp/apply_gwalphabet.sh     # timer cadence (default 30min)
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
EVERY="${EVERY:-30min}"
say(){ echo "[alphabet] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in dash_api.py alphabet_job.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'def alphabet_refresh' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py has no alphabet_refresh — wrong file?"; exit 2; }
grep -q '_alphabet_read' /tmp/dash_api.py \
  || { say "ABORT: /tmp/dash_api.py does not read the alphabet table"; exit 2; }
# The read path must NOT be able to derive. If _tier2 still calls _derive_floor_alphabet directly,
# the 140k-row walk is still on the request path and this change is a no-op with extra machinery.
if awk '/^def _tier2/,/^def [^_]|^# ══/' /tmp/dash_api.py | grep -q '_derive_floor_alphabet'; then
  say "ABORT: _tier2 still calls _derive_floor_alphabet — derivation is still on the request path"; exit 2
fi

$PY -m py_compile /tmp/dash_api.py /tmp/alphabet_job.py || { say "compile failed"; exit 1; }

TMPD=$(mktemp -d); cp /tmp/dash_api.py "$TMPD/dash_api.py"
if ! ( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0, '$TMPD')
from fastapi import FastAPI
import dash_api
assert dash_api.__file__.startswith('$TMPD'), 'imported the WRONG dash_api: '+dash_api.__file__
a=FastAPI(); a.include_router(dash_api.dash_router); a.openapi()
assert hasattr(dash_api,'alphabet_refresh') and hasattr(dash_api,'_alphabet_read')
print('smoke ok:', dash_api.__file__)" ); then
  say "SMOKE-IMPORT FAILED — nothing installed."; rm -rf "$TMPD"; exit 1
fi
rm -rf "$TMPD"

install -o "$OWNER" -g "$OWNER" -m 755 /tmp/alphabet_job.py "$APP/alphabet_job.py"
say "installed alphabet_job.py"

# ── POPULATE FIRST, using the NEW module but WITHOUT serving it ────────────────
# The job needs alphabet_refresh, which only exists in the candidate. Run it from a staging dir so
# the live app is still the old code while the table fills.
STAGE=$(mktemp -d); cp /tmp/dash_api.py "$STAGE/dash_api.py"
say "deriving alphabet for $GW (this is the 140k-row walk, once, off the request path) ..."
if ! ( cd "$APP" && PYTHONPATH="$STAGE:$APP" LIFTLAB_APP="$STAGE" \
       $(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^[A-Z_]+=' | sed 's/^/env /' | tr '\n' ' ') \
       $PY "$APP/alphabet_job.py" "$GW" ); then
  say "ALPHABET JOB FAILED — not installing dash_api.py. Live app untouched."; rm -rf "$STAGE"; exit 1
fi
rm -rf "$STAGE"

DB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
N=$(sqlite3 "$DB" "SELECT COUNT(*) FROM floor_alphabet WHERE gateway_id='$GW';" 2>/dev/null || echo 0)
if [ "${N:-0}" -lt 1 ]; then
  say "ABORT: floor_alphabet is empty for $GW after the job. Serving now would mean alphabet=None"
  say "       ('accept all floors') — a behaviour change, not just a slow page. Nothing installed."
  exit 1
fi
say "floor_alphabet populated: $N camera rows"
sqlite3 "$DB" "SELECT '    '||cam||': '||COALESCE(alphabet,'(none)')||'  evidence='||evidence_rows||' era='||COALESCE(era,'-') FROM floor_alphabet WHERE gateway_id='$GW';"

# ── now install and serve ─────────────────────────────────────────────────────
STAMP=$(date +%Y%m%d-%H%M%S); BAK="$APP/dash_api.py.bak.$STAMP"
cp "$APP/dash_api.py" "$BAK"; say "backup: $BAK"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"

restore(){ say "RESTORING $BAK"; cp "$BAK" "$APP/dash_api.py"; chown "$OWNER:$OWNER" "$APP/dash_api.py";
           systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"; }

# ── the schedule ──────────────────────────────────────────────────────────────
cat > /etc/systemd/system/liftlab-alphabet.service <<UNIT
[Unit]
Description=liftlab floor-alphabet derivation (off the /dash request path)
After=network.target
[Service]
Type=oneshot
User=$OWNER
WorkingDirectory=$APP
EnvironmentFile=-/etc/default/liftlab-alphabet
$(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^[A-Z_]+=' | sed 's/^/Environment=/')
ExecStart=$PY $APP/alphabet_job.py
UNIT
cat > /etc/systemd/system/liftlab-alphabet.timer <<UNIT
[Unit]
Description=run the liftlab floor-alphabet derivation every $EVERY
[Timer]
OnBootSec=5min
OnUnitActiveSec=$EVERY
AccuracySec=1min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now liftlab-alphabet.timer >/dev/null 2>&1
say "timer: $(systemctl is-active liftlab-alphabet.timer), every $EVERY"

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }
say "waiting for :9090 ..."
READY=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null && { READY=1; break; }; sleep 2; done
[ -n "$READY" ] || { say "port never opened"; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

say "measuring /dash/$GW/data x5 ..."
PASSN=0
for i in 1 2 3 4 5; do
  O=$(curl -s -o /tmp/alpha_probe.json -w '%{http_code} %{time_total}' --max-time 90 "http://127.0.0.1:9090/dash/$GW/data")
  C=${O%% *}; S=${O##* }
  say "  sample$i: HTTP $C in ${S}s"
  [ "$C" = 200 ] && PASSN=$((PASSN+1))
done
if [ "$PASSN" -eq 0 ]; then
  say "VERIFY FAILED: /dash/$GW/data never returned 200"; head -c 300 /tmp/alpha_probe.json; echo; restore; exit 1
fi
SHELL_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 "http://127.0.0.1:9090/dash")
[ "$SHELL_CODE" = 200 ] || { say "VERIFY FAILED: /dash shell $SHELL_CODE"; restore; exit 1; }
OPS_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090/ops/$GW/data")
[ "$OPS_CODE" = 200 ] || { say "VERIFY FAILED: /ops $OPS_CODE"; restore; exit 1; }
say "RESULT: PASS — $PASSN/5 returned 200, shell 200, /ops 200. Backup $BAK"
