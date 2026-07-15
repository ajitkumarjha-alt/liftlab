#!/usr/bin/env bash
# Install the live HLS relay proxy (live_api.py + mount) on the VM. ADDITIVE, transient.
# RUN AS ROOT ON THE VM: sudo bash /tmp/apply_live.sh   (needs /tmp/live_api.py + /tmp/apply_live_patch.py)
# Backup-first, compile-check, restart, verify routes by STATUS (not substrings).
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

cp "$APP/main.py" "$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/live_api.py "$APP/live_api.py"
say "installed live_api.py (main.py backed up)"
sudo -u "$OWNER" $PY /tmp/apply_live_patch.py || { say "patch failed — restore main.py.bak.*"; exit 1; }

$PY -c "import ast,sys; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restore backup"; exit 1; }
systemctl restart "$SVC"; sleep 3
AC=$(systemctl is-active "$SVC")
PAGE=$(code "$BASE/live/site-A/ch29")                       # viewer page (app-direct, bypasses Caddy)
PUT_NOAUTH=$(code -X PUT --data-binary 'x' "$BASE/api/gw/site-A/live/ch29/index.m3u8")  # 401 = route exists
SEG_404=$(code "$BASE/live/site-A/ch29/nope.ts")           # 404 = serve route exists, file absent
LC_NOAUTH=$(code "$BASE/api/gw/site-A/lift_channels")       # 401 = lift_channels route exists
say "AFTER: service=$AC  /live page=$PAGE  PUT(no-token)=$PUT_NOAUTH  GET(missing seg)=$SEG_404  lift_channels(no-token)=$LC_NOAUTH"
if [ "$AC" = active ] && [ "$PAGE" = 200 ] && [ "$PUT_NOAUTH" = 401 ] && [ "$SEG_404" = 404 ] && [ "$LC_NOAUTH" = 401 ]; then
  say "RESULT: PASS — ingest live (401 w/o token = route exists), viewer + serve up."
  say "Now run live_relay.sh on the Pi; then open  https://lift.gargi.online/live/site-A/ch29  in Chrome."
else
  say "RESULT: CHECK — restore newest $APP/main.py.bak.* + $APP/live_api.py, then: systemctl restart $SVC"
  say "  (PUT=404 => route didn't register; PAGE!=200 => import error, check: journalctl -u $SVC -n 40)"
  exit 1
fi
