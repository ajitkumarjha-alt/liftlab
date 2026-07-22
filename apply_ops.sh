#!/usr/bin/env bash
# Install the OPERATOR VIEW on the VM: ops_api.py (relay_status ingest + snapshot serve + /ops page)
# + snapshot.py + liftlab-snap.service. Snapshots decode in a SEPARATE niced process, JPEGs live in
# /run/liftlab-snap (RAM, NOT the capped relay store). Backup-first, compile-check, verify by STATUS.
# FILES NEEDED IN /tmp: apply_ops.sh ops_api.py apply_ops_patch.py snapshot.py liftlab-snap.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_ops.sh ops_api.py apply_ops_patch.py snapshot.py liftlab-snap.service nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_ops.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[ops] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in ops_api.py apply_ops_patch.py snapshot.py liftlab-snap.service; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# nav_common.py is a HARD dependency of every page module (the shared header). Installing a page
# without it is an ImportError that takes the ENTIRE operator UI down, not just this page — so it is
# installed here unconditionally and the deploy refuses to continue without it.
if [ -f /tmp/nav_common.py ]; then
  $PY -m py_compile /tmp/nav_common.py || { say "nav_common.py failed to compile — aborting"; exit 1; }
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/nav_common.py "$APP/nav_common.py"
  say "installed nav_common.py (shared header)"
elif [ ! -f "$APP/nav_common.py" ]; then
  say "ABORT: nav_common.py is neither in /tmp nor installed. Every page imports it; continuing"
  say "  would ImportError the whole UI. Re-curl nav_common.py and run again."; exit 1
fi

$PY -m py_compile /tmp/ops_api.py || { say "ops_api.py does not compile — aborting"; exit 1; }
$PY -m py_compile /tmp/snapshot.py || { say "snapshot.py does not compile — aborting"; exit 1; }
command -v ffmpeg >/dev/null || { say "ffmpeg not installed on the VM — snapshots need it. Aborting."; exit 1; }

install -o "$OWNER" -g "$OWNER" -m 644 /tmp/ops_api.py  "$APP/ops_api.py"
install -o "$OWNER" -g "$OWNER" -m 755 /tmp/snapshot.py "$APP/snapshot.py"
install -m 644 /tmp/liftlab-snap.service /etc/systemd/system/liftlab-snap.service
# SMOKE-IMPORT before touching main.py — a missing dep fails HERE, ingest untouched.
if ! ( cd "$APP" && sudo -u "$OWNER" $PY -c "from fastapi import FastAPI
import ops_api
a=FastAPI(); a.include_router(ops_api.ops_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi
BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_ops_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }  # ROOT
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }

# pass the cloud's GATEWAY_DB to the snapshotter so it only decodes channel_map cams (best-effort;
# the JUNK filter skips chdiag*/zonecheck/_ regardless)
GDB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
if [ -n "$GDB" ]; then
  install -d /etc/systemd/system/liftlab-snap.service.d
  printf '[Service]\nEnvironment=GATEWAY_DB=%s\n' "$GDB" > /etc/systemd/system/liftlab-snap.service.d/db.conf
  say "snapshotter GATEWAY_DB=$GDB (channel_map gating on)"
else
  say "GATEWAY_DB not found in $SVC env — snapshotter uses the JUNK filter only (still skips chdiag/zonecheck)"
fi
systemctl daemon-reload
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK and restarting to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi
systemctl enable --now liftlab-snap >/dev/null 2>&1 || systemctl restart liftlab-snap
sleep 2
PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1); [ -n "$PORT" ] || PORT=9090
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
CA=$(systemctl is-active "$SVC"); SA=$(systemctl is-active liftlab-snap)
PAGE=$(code "$BASE/ops/site-A")                                   # 200 = page route
RS=$(code -X POST --data '{}' "$BASE/api/gw/site-A/relay_status") # 401 = ingest exists
DATA=$(code "$BASE/ops/site-A/data")                             # 200 = data route
SNAP=$(code "$BASE/snap/site-A/ch29.jpg")                        # 404 until first frame (route exists)
say "AFTER: cloud=$CA snap=$SA  /ops=$PAGE  relay_status(no-token)=$RS  /ops/data=$DATA  /snap(missing)=$SNAP"
if [ "$CA" = active ] && [ "$SA" = active ] && [ "$PAGE" = 200 ] && [ "$RS" = 401 ] && [ "$DATA" = 200 ] && { [ "$SNAP" = 404 ] || [ "$SNAP" = 200 ]; }; then
  say "RESULT: PASS — operator view up. Open: https://lift.gargi.online/ops/site-A"
  say "Snapshots appear within ~2s of segments arriving; relay health after the Pi's relay_soak posts (redeploy relay_soak.sh so it POSTs relay_status)."
else
  say "RESULT: CHECK — restore $APP/main.py.bak.* + remove ops_api.py; journalctl -u $SVC -u liftlab-snap -n 40"
  exit 1
fi
