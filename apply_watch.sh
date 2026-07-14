#!/usr/bin/env bash
# Install the continuous single-cabin scheduler (v1=A). RUN ON THE PI AS ROOT.
# Requires (curl first): /tmp/continuous_scheduler.py /tmp/watch_manager.py
#                        /tmp/watch_channel_graft.py
#
# Matches analyze_local's out-of-process shape EXACTLY:
#   * continuous_scheduler.py lives in B4_DIR (beside analyze_runner.py), run by
#     B4_PY with PYTHONPATH=B4_DIR + cwd=B4_DIR. onvif_resolve.py is copied into
#     B4_DIR so that single path resolves it (no parallel path-injection).
#   * watch_manager.py (STDLIB ONLY) lives in the agent dir; the agent imports it.
#   * The gateway token is stripped from the child; the agent (parent) posts.
#
# SMOKE_ONLY=1  -> discover + install + smoke-test, STOP before grafting.
# Additive + reversible: backups first, aborts on any smoke failure.
set -uo pipefail
AGENT_DIR="${AGENT_DIR:-/home/askjitk/liftlab-b3/pi-agent}"
AGENT="$AGENT_DIR/agent.py"
B4_DIR="${B4_DIR:-/home/askjitk/liftlab-b4}"
B4_PY="${B4_PY:-$B4_DIR/.venv/bin/python}"
say(){ echo "[watch] $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/continuous_scheduler.py /tmp/watch_manager.py /tmp/watch_channel_graft.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done
[ -f "$AGENT" ] || { echo "agent not at $AGENT — set AGENT_DIR"; exit 2; }
[ -x "$B4_PY" ] || { echo "B4 python not at $B4_PY — set B4_PY/B4_DIR"; exit 2; }
[ -d "$B4_DIR/liftlab" ] || { echo "liftlab package not at $B4_DIR/liftlab — set B4_DIR"; exit 2; }
[ -f "$AGENT_DIR/onvif_resolve.py" ] || { echo "onvif_resolve.py not in $AGENT_DIR"; exit 2; }

# ---- discover agent service + its (import-light) python ----
SERVICE="${SERVICE:-}"
if [ -z "$SERVICE" ]; then
  SERVICE=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
            | awk '{print $1}' | grep -iE 'liftlab.*agent|agent.*liftlab|liftlab-b' | head -1)
fi
[ -n "$SERVICE" ] || { echo "could not find agent service — set SERVICE=<name>.service"; exit 2; }
AGENT_PY="${AGENT_PY:-$(systemctl show -p ExecStart "$SERVICE" 2>/dev/null | grep -oE '/[^ ]*/python[0-9.]*' | head -1)}"
[ -n "$AGENT_PY" ] && [ -x "$AGENT_PY" ] || AGENT_PY="$AGENT_DIR/.venv/bin/python"
OWNER=$(stat -c '%U' "$AGENT")
say "service=$SERVICE  agent_py=$AGENT_PY  B4_PY=$B4_PY  B4_DIR=$B4_DIR  owner=$OWNER"
say "BEFORE: agent active=$(systemctl is-active "$SERVICE")  version=$(grep -oE 'VERSION = \"[0-9.]+\"' "$AGENT" | head -1)"

# ---- install: scheduler + onvif_resolve into B4_DIR; manager + conf into agent dir ----
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/continuous_scheduler.py "$B4_DIR/continuous_scheduler.py"
install -o "$OWNER" -g "$OWNER" -m 644 "$AGENT_DIR/onvif_resolve.py" "$B4_DIR/onvif_resolve.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/watch_manager.py "$AGENT_DIR/watch_manager.py"
cat > "$AGENT_DIR/watch_runtime.conf" <<EOF
FULL_PY=$B4_PY
B4_DIR=$B4_DIR
SCRIPT=$B4_DIR/continuous_scheduler.py
EOF
chown "$OWNER":"$OWNER" "$AGENT_DIR/watch_runtime.conf"
say "installed: $B4_DIR/{continuous_scheduler,onvif_resolve}.py + $AGENT_DIR/{watch_manager.py,watch_runtime.conf}"

# ---- smoke each runtime with its OWN interpreter ----
"$B4_PY" -m py_compile "$B4_DIR/continuous_scheduler.py" || { say "scheduler does NOT compile under B4_PY — aborting"; exit 1; }
if ! ( cd "$B4_DIR" && PYTHONPATH="$B4_DIR" sudo -u "$OWNER" env PYTHONPATH="$B4_DIR" "$B4_PY" -c \
        "import continuous_scheduler as m; import numpy,av,cv2,onvif_resolve,liftlab.doors,liftlab.stitch,liftlab.timestamps; print('scheduler + reuse map import OK under B4_PY (PYTHONPATH=B4_DIR, cwd=B4_DIR)')" ); then
  say "SCHEDULER IMPORT FAILED under B4_PY — aborting, cleaning up"
  rm -f "$B4_DIR/continuous_scheduler.py" "$B4_DIR/onvif_resolve.py" "$AGENT_DIR/watch_manager.py" "$AGENT_DIR/watch_runtime.conf"
  exit 1
fi
"$AGENT_PY" -m py_compile "$AGENT_DIR/watch_manager.py" || { say "watch_manager does NOT compile under agent_py — aborting"; exit 1; }
( cd "$AGENT_DIR" && sudo -u "$OWNER" "$AGENT_PY" -c "import watch_manager; print('watch_manager import OK (stdlib-only, agent venv)')" ) \
  || { say "watch_manager import FAILED in agent venv (should be stdlib-only!) — aborting"; rm -f "$AGENT_DIR/watch_manager.py" "$AGENT_DIR/watch_runtime.conf"; exit 1; }
say "smoke: scheduler+reuse-map import under B4_PY; watch_manager import in agent venv — BOTH GREEN"

if [ "${SMOKE_ONLY:-0}" = 1 ]; then
  say "SMOKE_ONLY: stopping BEFORE graft. Agent untouched at $(grep -oE 'VERSION = \"[0-9.]+\"' "$AGENT" | head -1)."
  say "Re-run without SMOKE_ONLY to graft once the auth boundary is signed off."
  exit 0
fi

# ---- graft the non-blocking dispatch (backup-first, idempotent) ----
sudo -u "$OWNER" "$AGENT_PY" /tmp/watch_channel_graft.py || { say "graft step failed"; exit 1; }
"$AGENT_PY" -m py_compile "$AGENT" || { say "agent.py does NOT compile after graft — RESTORE newest $AGENT.bak.*"; exit 1; }

systemctl restart "$SERVICE"; sleep 4
AC=$(systemctl is-active "$SERVICE"); VER=$(grep -oE 'VERSION = "[0-9.]+"' "$AGENT" | head -1)
say "AFTER: agent active=$AC  version=$VER"
if [ "$AC" = active ] && echo "$VER" | grep -q '0.7.0'; then
  say "RESULT: PASS — watch_channel live (subprocess, token-stripped child), VERSION 0.7.0."
else
  say "RESULT: CHECK above. Rollback: restore newest $AGENT.bak.*; rm $AGENT_DIR/{watch_manager.py,watch_runtime.conf} $B4_DIR/{continuous_scheduler,onvif_resolve}.py; systemctl restart $SERVICE"
  journalctl -u "$SERVICE" -n 20 --no-pager | sed 's/^/[watch]   /'
fi
