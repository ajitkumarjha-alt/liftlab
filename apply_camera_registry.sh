#!/usr/bin/env bash
# Install the GPU camera registry (wizard piece 5, CLOUD side) + the /dash toggle.
# Deploy this BEFORE apply_gpu_fleet.sh on the GPU box — the fleet refuses to switch over until the
# registry exists and names at least one enabled camera.
# FILES NEEDED IN /tmp: apply_camera_registry.sh camera_registry_api.py apply_camera_registry_patch.py dash_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_camera_registry.sh camera_registry_api.py apply_camera_registry_patch.py dash_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_camera_registry.sh
#
# CADDY: POST /api/gw/{gw}/cameras/{cam} is the operator toggle and carries NO Bearer (a browser has
# none) — it must sit behind the same basicauth as the other human POSTs (/floorcheck review). The
# GET is Bearer-authed for the GPU and must stay reachable WITHOUT basicauth, like the other /api/gw
# routes. If your Caddy protects /api/gw/* wholesale, the fleet will get 401 and change nothing.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[cam-registry] $*"; }
say "REV=registry-1  (GET cameras for the fleet; POST toggle from /dash)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in camera_registry_api.py apply_camera_registry_patch.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/camera_registry_api.py || { say "compile failed"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/camera_registry_api.py "$APP/camera_registry_api.py"
if [ -f /tmp/dash_api.py ]; then
  $PY -m py_compile /tmp/dash_api.py && install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py" \
    && say "installed dash_api.py (GPU analysis toggle on the camera panel)"
fi
if ! ( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" $PY -c "from fastapi import FastAPI
import camera_registry_api
a=FastAPI(); a.include_router(camera_registry_api.camera_registry_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched."; exit 1; fi

BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_camera_registry_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED — RESTORING $BAK"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"
  systemctl restart "$SVC"; say "restored. journalctl -u $SVC -n 40"; exit 1
fi
PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
[ -n "$MPID" ] && [ "$MPID" != 0 ] && PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
if [ -z "$PORT" ]; then say "RESULT: PASS (port unknown, not probed). Toggle on https://lift.gargi.online/dash"; exit 0; fi
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
UN=$(code "http://127.0.0.1:$PORT/api/gw/site-A/cameras")            # no token -> must be 401
DASH=$(code "http://127.0.0.1:$PORT/dash/site-A/data")
say "AFTER (port $PORT): /cameras(no token)=$UN (want 401)  /dash/data=$DASH"
if [ "$UN" = 401 ] && [ "$DASH" = 200 ]; then
  say "RESULT: PASS — registry live. Enable a camera at https://lift.gargi.online/dash (GPU analysis)."
  say "  THEN deploy the fleet on liftlab-gpu: sudo ANALYSIS_TOKEN=... bash /tmp/apply_gpu_fleet.sh"
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
