#!/usr/bin/env bash
# Install the VALIDATION UI on the VM: validation_api.py (per-camera review gate) + updated ops_api.py
# (provenance card), mount validation_router, create the image dir. Backup-first, compile, verify.
# FILES NEEDED IN /tmp: apply_valui.sh validation_api.py apply_validation_patch.py ops_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_valui.sh validation_api.py apply_validation_patch.py ops_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_valui.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
IMGDIR="${VALIDATION_IMG_DIR:-/var/lib/liftlab/validation_img}"
say(){ echo "[valui] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in validation_api.py apply_validation_patch.py ops_api.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/validation_api.py /tmp/ops_api.py || { say "compile failed — aborting"; exit 1; }
# image dir (residents' frames; short-lived, deleted at each verdict; NOT in the litestream backup)
install -d -o "$OWNER" -g "$OWNER" -m 750 "$IMGDIR"
say "validation images: $IMGDIR (deleted at each verdict; files not DB, so never in the backup)"
cp "$APP/main.py" "$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/validation_api.py "$APP/validation_api.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/ops_api.py "$APP/ops_api.py"
sudo -u "$OWNER" $PY /tmp/apply_validation_patch.py || { say "patch failed — restore main.py.bak.*"; exit 1; }
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restore backup"; exit 1; }

# VALIDATION_IMG_DIR into the service env (so the app writes to $IMGDIR, matching this install)
DROPIN=/etc/systemd/system/$SVC.service.d/validation.conf
mkdir -p "$(dirname "$DROPIN")"
printf '[Service]\nEnvironment=VALIDATION_IMG_DIR=%s\n' "$IMGDIR" > "$DROPIN"

systemctl daemon-reload
systemctl restart "$SVC"; sleep 3
PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1); [ -n "$PORT" ] || PORT=9090
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
AC=$(systemctl is-active "$SVC")
MODE=$(code "$BASE/api/gw/site-A/validation_mode/ch29")           # 401 (no token) = route exists
PAGE=$(code "$BASE/validate")                                    # 200 = review page
VITEM=$(code -X POST --data '{}' "$BASE/api/gw/site-A/validation_item/ch29")  # 401 = ingest exists
say "AFTER: cloud=$AC  validation_mode=$MODE  /validate=$PAGE  validation_item(no-token)=$VITEM"
if [ "$AC" = active ] && [ "$MODE" = 401 ] && [ "$PAGE" = 200 ] && [ "$VITEM" = 401 ]; then
  say "RESULT: PASS — validation UI up. Review at https://lift.gargi.online/validate"
  say "ch29 starts 'validating' (default). Redeploy gpu_analyze.py so it polls the mode + posts"
  say "episodes with images; click GO-LIVE on /validate when precision satisfies you."
else
  say "RESULT: CHECK — restore $APP/main.py.bak.* + ops_api.py; journalctl -u $SVC -n 40"; exit 1
fi
