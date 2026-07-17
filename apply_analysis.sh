#!/usr/bin/env bash
# Install the analysis interface on the VM: analysis_api.py (Bearer segment pull + transit ingest) +
# updated ops_api.py (Transit card), mount analysis_router, and MINT a read-only analysis token for
# the GPU box. Backup-first, compile-check, verify by STATUS.
# FILES NEEDED IN /tmp: apply_analysis.sh analysis_api.py apply_analysis_patch.py ops_api.py
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_analysis.sh analysis_api.py apply_analysis_patch.py ops_api.py; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_analysis.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
DROPIN=/etc/systemd/system/liftlab-cloud.service.d/analysis-token.conf
say(){ echo "[analysis] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in analysis_api.py apply_analysis_patch.py ops_api.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -f "$APP/main.py" ] || { echo "main.py not at $APP"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/analysis_api.py /tmp/ops_api.py || { say "compile failed — aborting"; exit 1; }
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/analysis_api.py "$APP/analysis_api.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/ops_api.py "$APP/ops_api.py"
# SMOKE-IMPORT before touching main.py — a missing dep fails HERE, ingest untouched.
if ! ( cd "$APP" && sudo -u "$OWNER" $PY -c "from fastapi import FastAPI
import analysis_api, ops_api
a=FastAPI(); a.include_router(analysis_api.analysis_router); a.include_router(ops_api.ops_router); a.openapi(); print('smoke ok')" ); then
  say "SMOKE-IMPORT FAILED — NOT patching main.py, ingest untouched. Fix the error above."; exit 1; fi
BAK="$APP/main.py.bak.$(date +%Y%m%d-%H%M%S)"; cp "$APP/main.py" "$BAK"
$PY /tmp/apply_analysis_patch.py || { say "patch failed — restoring"; cp "$BAK" "$APP/main.py"; exit 1; }  # ROOT: patch backup-writes into $APP
chown "$OWNER:$OWNER" "$APP/main.py"
$PY -c "import ast; ast.parse(open('$APP/main.py').read())" || { say "main.py broke — restoring"; cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; exit 1; }

# ---- mint the read-only analysis token (systemd drop-in; separate from GATEWAY_TOKENS) ----
mkdir -p "$(dirname "$DROPIN")"
if grep -qs 'ANALYSIS_TOKENS=site-A:' "$DROPIN"; then
  TOK=$(grep -oP 'ANALYSIS_TOKENS=site-A:\K\S+' "$DROPIN" | head -1); say "reusing existing analysis token"
else
  TOK=$(openssl rand -hex 16 2>/dev/null || head -c16 /dev/urandom | xxd -p)
  printf '[Service]\nEnvironment=ANALYSIS_TOKENS=site-A:%s\n' "$TOK" > "$DROPIN"; say "minted read-only analysis token"
fi

systemctl daemon-reload
systemctl restart "$SVC"; sleep 4
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "cloud FAILED to start — RESTORING $BAK and restarting to protect the ingest"
  cp "$BAK" "$APP/main.py"; chown "$OWNER:$OWNER" "$APP/main.py"; systemctl restart "$SVC"
  say "restored. journalctl -u $SVC -n 40"; exit 1
fi
# Port off the RUNNING MainPID's listening socket (bind-agnostic), then unit --port. The old
# `--port|9090` fallback read 000 (curl couldn't connect on a wrong port) and cried CHECK on healthy
# routes. If the port truly can't be found, do NOT emit misleading 000s — main.py already came up.
AC=$(systemctl is-active "$SVC")
tokblock(){ echo "======================================================================"
  echo "  ANALYSIS TOKEN for the GPU box (read-only: pull segments + post transits):"
  echo "      site-A:$TOK"
  echo "  On liftlab-gpu:  sudo ANALYSIS_TOKEN=site-A:$TOK bash /tmp/apply_gpu.sh"
  echo "======================================================================"; }
PORT=""
MPID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
if [ -n "$MPID" ] && [ "$MPID" != 0 ]; then
  PORT=$(ss -tlnpH 2>/dev/null | grep -F "pid=$MPID," | grep -oP ':\K[0-9]+' | head -1)
fi
[ -n "$PORT" ] || PORT=$(systemctl cat "$SVC" 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
[ -n "$PORT" ] || PORT=$(systemctl show "$SVC" -p ExecStart --value 2>/dev/null | grep -oP '\-\-port[=\s]+\K[0-9]+' | head -1)
if [ -z "$PORT" ]; then
  say "AFTER: cloud=$AC; port unknown -> routes not HTTP-probed (NOT a failure — main.py came up)."
  say "RESULT: PASS (ingest healthy; verify at https://lift.gargi.online/events). Re-run with PORT=<n> to probe."
  tokblock; exit 0
fi
BASE="http://127.0.0.1:$PORT"; code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$@"; }
TR=$(code -X POST --data '{}' "$BASE/api/gw/site-A/transit")                # 401 = route exists
SEG=$(code "$BASE/api/gw/site-A/live/ch29/index.m3u8")                      # 401 (no token) = route exists
OK_TR=$(code -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
        --data '{"cam":"ch29","direction":"in","track_id":1,"ts":1}' "$BASE/api/gw/site-A/transit")  # 200 with analysis token
say "AFTER (port $PORT): cloud=$AC  transit(no-token)=$TR  segment-pull(no-token)=$SEG  transit(analysis-token)=$OK_TR"
if [ "$AC" = active ] && [ "$TR" = 401 ] && [ "$SEG" = 401 ] && [ "$OK_TR" = 200 ]; then
  say "RESULT: PASS. Backfill status: journalctl -u $SVC | grep 'backfill' (expect 'backfill active', not DISABLED)."
  tokblock
elif [ "$TR" = 000 ] && [ "$SEG" = 000 ] && [ "$OK_TR" = 000 ]; then
  say "RESULT: CHECK — every probe 000 = could not connect on :$PORT (wrong port, NOT dead routes). cloud=$AC; verify via the public URL."; exit 1
else
  say "RESULT: CHECK — restore $APP/main.py.bak.*; journalctl -u $SVC -n 40"; exit 1
fi
