#!/usr/bin/env bash
# Install the continuous single-cabin scheduler (v1=A) into the live Pi agent.
# RUN ON THE PI AS ROOT:  sudo bash /tmp/apply_watch.sh
# Requires (curl first): /tmp/continuous_scheduler.py /tmp/watch_manager.py
#                        /tmp/watch_channel_graft.py
#
# The agent venv is import-light (no numpy/av/liftlab). So:
#  * watch_manager.py (STDLIB ONLY) runs IN the agent venv (imports there).
#  * continuous_scheduler.py runs as a SUBPROCESS under the FULL-DEPS python,
#    which this script DISCOVERS (numpy+av+cv2+onnxruntime-free path: numpy,av,cv2,
#    onvif_resolve,liftlab). The discovered runtime is written to watch_runtime.conf.
# Additive + reversible: backups first, smoke-tests each runtime with the RIGHT
# interpreter, aborts before grafting on any failure.
set -uo pipefail
AGENT_DIR="${AGENT_DIR:-/home/askjitk/liftlab-b3/pi-agent}"
AGENT="$AGENT_DIR/agent.py"
say(){ echo "[watch] $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/continuous_scheduler.py /tmp/watch_manager.py /tmp/watch_channel_graft.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done
[ -f "$AGENT" ] || { echo "agent not at $AGENT — set AGENT_DIR"; exit 2; }

# ---- discover the agent service + its (import-light) python ----
SERVICE="${SERVICE:-}"
if [ -z "$SERVICE" ]; then
  SERVICE=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
            | awk '{print $1}' | grep -iE 'liftlab.*agent|agent.*liftlab|liftlab-b' | head -1)
fi
[ -n "$SERVICE" ] || { echo "could not find agent service — set SERVICE=<name>.service"; exit 2; }
AGENT_PY="${AGENT_PY:-$(systemctl show -p ExecStart "$SERVICE" 2>/dev/null | grep -oE '/[^ ]*/python[0-9.]*' | head -1)}"
[ -n "$AGENT_PY" ] && [ -x "$AGENT_PY" ] || AGENT_PY="$AGENT_DIR/.venv/bin/python"
OWNER=$(stat -c '%U' "$AGENT")
say "service=$SERVICE  agent_py=$AGENT_PY  owner=$OWNER  dir=$AGENT_DIR"
say "BEFORE: agent active=$(systemctl is-active "$SERVICE")  version=$(grep -oE 'VERSION = \"[0-9.]+\"' "$AGENT" | head -1)"

# ---- discover the FULL-DEPS python (numpy,av,cv2,onvif_resolve,liftlab) ----
CANDIDATES=("${FULL_PY:-}" /home/askjitk/liftlab-b4/.venv/bin/python \
            /home/askjitk/liftlab-b3/.venv/bin/python "$AGENT_PY" python3)
PROBE="import sys; sys.path.insert(0,'$AGENT_DIR'); import numpy,av,cv2,onvif_resolve,liftlab.doors,liftlab.stitch; print('DEPS OK')"
FULL_PY=""
for c in "${CANDIDATES[@]}"; do
  [ -n "$c" ] || continue
  CBIN=$(command -v "$c" 2>/dev/null || echo "$c")
  [ -x "$CBIN" ] || continue
  OUT=$("$CBIN" -c "$PROBE" 2>&1 | tail -1 || true)
  say "  candidate $CBIN -> $OUT"
  if echo "$OUT" | grep -q "DEPS OK"; then FULL_PY="$CBIN"; break; fi
done
[ -n "$FULL_PY" ] || { say "NO python with numpy+av+cv2+onvif_resolve+liftlab found — cannot run the scheduler. Aborting, no change. (Install the deps or set FULL_PY=)"; exit 1; }
say "FULL_PY=$FULL_PY"

# ---- install the two modules beside the agent ----
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/continuous_scheduler.py "$AGENT_DIR/continuous_scheduler.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/watch_manager.py "$AGENT_DIR/watch_manager.py"
cat > "$AGENT_DIR/watch_runtime.conf" <<EOF
FULL_PY=$FULL_PY
EXTRA_PATH=$AGENT_DIR
SCRIPT=$AGENT_DIR/continuous_scheduler.py
EOF
chown "$OWNER":"$OWNER" "$AGENT_DIR/watch_runtime.conf"
say "installed continuous_scheduler.py + watch_manager.py + watch_runtime.conf"

# ---- smoke test each runtime with its OWN interpreter ----
"$FULL_PY" -m py_compile "$AGENT_DIR/continuous_scheduler.py" || { say "scheduler does NOT compile under FULL_PY — aborting"; exit 1; }
( cd "$AGENT_DIR" && "$FULL_PY" -c "import continuous_scheduler; print('scheduler import OK')" ) || { say "scheduler import FAILED under FULL_PY — aborting"; rm -f "$AGENT_DIR/continuous_scheduler.py" "$AGENT_DIR/watch_manager.py" "$AGENT_DIR/watch_runtime.conf"; exit 1; }
"$AGENT_PY" -m py_compile "$AGENT_DIR/watch_manager.py" || { say "watch_manager does NOT compile under agent_py — aborting"; exit 1; }
( cd "$AGENT_DIR" && sudo -u "$OWNER" "$AGENT_PY" -c "import watch_manager; print('manager import OK (stdlib-only, agent venv)')" ) || { say "watch_manager import FAILED in agent venv (should be stdlib-only!) — aborting"; rm -f "$AGENT_DIR/watch_manager.py" "$AGENT_DIR/continuous_scheduler.py" "$AGENT_DIR/watch_runtime.conf"; exit 1; }
say "smoke: scheduler imports under FULL_PY; watch_manager imports in the agent venv"

# ---- graft the non-blocking dispatch (backup-first, idempotent) ----
sudo -u "$OWNER" "$AGENT_PY" /tmp/watch_channel_graft.py || { say "graft step failed"; exit 1; }
"$AGENT_PY" -m py_compile "$AGENT" || { say "agent.py does NOT compile after graft — RESTORE the newest $AGENT.bak.*"; exit 1; }

# ---- restart + verify ----
systemctl restart "$SERVICE"; sleep 4
AC=$(systemctl is-active "$SERVICE")
VER=$(grep -oE 'VERSION = "[0-9.]+"' "$AGENT" | head -1)
say "AFTER: agent active=$AC  version=$VER"
if [ "$AC" = active ] && echo "$VER" | grep -q '0.7.0'; then
  say "RESULT: PASS — watch_channel dispatch live (subprocess model), VERSION 0.7.0, agent healthy."
  say "Next: watch_channel {channel:29, action:start} during a DOORS-SHUT quiet moment, then eyeball /validation/$( [ -f "$AGENT_DIR/watch_runtime.conf" ] && echo '<gw>' )."
else
  say "RESULT: CHECK above. Rollback: restore newest $AGENT.bak.*, rm $AGENT_DIR/{continuous_scheduler,watch_manager}.py watch_runtime.conf, systemctl restart $SERVICE"
  journalctl -u "$SERVICE" -n 20 --no-pager | sed 's/^/[watch]   /'
fi
