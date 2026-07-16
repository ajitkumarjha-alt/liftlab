#!/usr/bin/env bash
# Install the live HLS relay proxy (live_api.py + mount) on the VM. ADDITIVE, transient.
# FILES NEEDED IN /tmp: apply_live.sh live_api.py apply_live_patch.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_live.sh live_api.py apply_live_patch.py; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON THE VM: sudo bash /tmp/apply_live.sh   (verify routes by STATUS, not substrings)
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
LIVE_DIR="${LIVE_DIR:-/dev/shm/liftlab-live}"
say(){ echo "[live] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/live_api.py ] || { echo "missing /tmp/live_api.py — curl it first"; exit 2; }
[ -f /tmp/apply_live_patch.py ] || { echo "missing /tmp/apply_live_patch.py"; exit 2; }
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }

PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=9090
BASE="http://127.0.0.1:$PORT"
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }

$PY -m py_compile /tmp/live_api.py || { say "live_api.py does NOT compile — aborting"; exit 1; }

# tmpfs store (RAM-backed; transient by construction). /dev/shm is already tmpfs.
install -d -o "$OWNER" -g "$OWNER" -m 755 "$LIVE_DIR"
say "live store: $LIVE_DIR (tmpfs — segments never hit disk, pruned to a rolling window)"

install -o "$OWNER" -g "$OWNER" -m 644 /tmp/live_api.py "$APP/live_api.py"
# SMOKE-IMPORT before touching main.py — a missing dep fails HERE, ingest untouched.
if ! ( cd "$APP" && sudo -u "$OWNER" $PY -c "from fastapi import FastAPI
import live_api
a=FastAPI(); a.include_router(live_api.live_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi
BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_live_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }  # ROOT
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK and restarting to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi
AC=$(systemctl is-active "$SVC")
PAGE=$(code "$BASE/live/site-A/ch29")                       # viewer page (app-direct, bypasses Caddy)
PUT_NOAUTH=$(code -X PUT --data-binary 'x' "$BASE/api/gw/site-A/live/ch29/index.m3u8")  # 401 = route exists
SEG_404=$(code "$BASE/live/site-A/ch29/nope.ts")           # 404 = serve route exists, file absent
LC_NOAUTH=$(code "$BASE/api/gw/site-A/lift_channels")       # 401 = lift_channels route exists
LS_NOAUTH=$(code "$BASE/api/gw/site-A/live_stats")          # 401 = live_stats route exists
BH_NOAUTH=$(code -X PUT --data-binary 'x' "$BASE/api/gw/site-A/blackhole")  # 401 = blackhole route exists
say "AFTER: service=$AC  /live page=$PAGE  PUT(no-token)=$PUT_NOAUTH  GET(missing seg)=$SEG_404  lift_channels=$LC_NOAUTH  live_stats=$LS_NOAUTH  blackhole=$BH_NOAUTH"
if [ "$AC" = active ] && [ "$PAGE" = 200 ] && [ "$PUT_NOAUTH" = 401 ] && [ "$SEG_404" = 404 ] && [ "$LC_NOAUTH" = 401 ] && [ "$LS_NOAUTH" = 401 ] && [ "$BH_NOAUTH" = 401 ]; then
  say "RESULT: PASS — ingest live (401 w/o token = route exists), viewer + serve up."
  say "Now run live_relay.sh on the Pi; then open  https://lift.gargi.online/live/site-A/ch29  in Chrome."
else
  say "RESULT: CHECK — restore newest $APP/main.py.bak.* + $APP/live_api.py, then: systemctl restart $SVC"
  say "  (PUT=404 => route didn't register; PAGE!=200 => import error, check: journalctl -u $SVC -n 40)"
  exit 1
fi
