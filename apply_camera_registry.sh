#!/usr/bin/env bash
# Install the GPU camera registry (wizard piece 5, CLOUD side) + the /dash toggle.
# Deploy this BEFORE apply_gpu_fleet.sh on the GPU box — the fleet refuses to switch over until the
# registry exists and names at least one enabled camera.
# FILES NEEDED IN /tmp: apply_camera_registry.sh camera_registry_api.py apply_camera_registry_patch.py dash_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_camera_registry.sh camera_registry_api.py apply_camera_registry_patch.py dash_api.py nav_common.py; do curl -fsSL -o /tmp/$f $B/$f; done
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
# Two-phase verdict (drain/cold-start lesson 2026-07-28): (1) a NEW MainPID must exist — the old
# process drains under the relay's PUT stream; (2) the new process must answer. A sleep-then-
# single-sample probe reads 000 mid-drain/mid-bind and misdiagnoses a healthy install.
OLDPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
systemctl restart "$SVC"
NEWPID=$OLDPID
for i in $(seq 1 120); do
  NEWPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null || echo 0)
  [ -n "$NEWPID" ] && [ "$NEWPID" != "$OLDPID" ] && [ "$NEWPID" != 0 ] && break
  sleep 1
done
if [ -z "$NEWPID" ] || [ "$NEWPID" = "$OLDPID" ] || [ "$NEWPID" = 0 ]; then
  say "no NEW MainPID after 120s — RESTORING $BAK"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"
  systemctl restart "$SVC"; say "restored. journalctl -u $SVC -n 40"; exit 1
fi
PORT=""
for i in $(seq 1 30); do   # the new PID binds a few seconds in — poll ss, don't sample once
  PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$NEWPID," | grep -oP ':\K[0-9]+' | head -1)
  [ -n "$PORT" ] && break
  sleep 1
done
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
if [ -z "$PORT" ]; then say "RESULT: PASS (port unknown, not probed). Toggle on https://lift.gargi.online/dash"; exit 0; fi
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
LIVE=000
for i in $(seq 1 60); do
  LIVE=$(code "http://127.0.0.1:$PORT/openapi.json"); [ "$LIVE" = 200 ] && break; sleep 1
done
UN=$(code "http://127.0.0.1:$PORT/api/gw/site-A/cameras")            # no token -> must be 401
DASH=$(code "http://127.0.0.1:$PORT/dash/site-A/data")
say "AFTER (port $PORT, openapi $LIVE after $i probes, MainPID $OLDPID -> $NEWPID): /cameras(no token)=$UN (want 401)  /dash/data=$DASH"
if [ "$UN" = 401 ] && [ "$DASH" = 200 ]; then
  say "RESULT: PASS — registry live. Enable a camera at https://lift.gargi.online/dash (GPU analysis)."
  say "  THEN deploy the fleet on liftlab-gpu: sudo ANALYSIS_TOKEN=... bash /tmp/apply_gpu_fleet.sh"
else
  say "RESULT: CHECK — restore $BAK; journalctl -u $SVC -n 40"; exit 1
fi
