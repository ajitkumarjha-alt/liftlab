#!/usr/bin/env bash
# Install the continuous single-cabin scheduler (v1=A) into the live Pi agent.
# RUN ON THE PI AS ROOT:  sudo bash /tmp/apply_watch.sh
# Requires /tmp/continuous_scheduler.py + /tmp/watch_channel_graft.py (curl first).
# Additive + reversible: installs the module beside the agent, smoke-imports it in
# the AGENT's own python (fails fast if liftlab/av aren't importable there), grafts
# a NON-BLOCKING watch_channel dispatch (backup first), compiles, restarts, verifies.
set -uo pipefail
AGENT_DIR="${AGENT_DIR:-/home/askjitk/liftlab-b3/pi-agent}"
AGENT="$AGENT_DIR/agent.py"
say(){ echo "[watch] $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/continuous_scheduler.py /tmp/watch_channel_graft.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done
[ -f "$AGENT" ] || { echo "agent not at $AGENT — set AGENT_DIR"; exit 2; }

# ---- discover the agent service + its python ----
SERVICE="${SERVICE:-}"
if [ -z "$SERVICE" ]; then
  SERVICE=$(systemctl list-units --type=service --all --no-legend 2>/dev/null \
            | awk '{print $1}' | grep -iE 'liftlab.*agent|agent.*liftlab|liftlab-b' | head -1)
fi
[ -n "$SERVICE" ] || { echo "could not find agent service — set SERVICE=<name>.service"; exit 2; }
PYBIN="${PYBIN:-$(systemctl show -p ExecStart "$SERVICE" 2>/dev/null | grep -oE '/[^ ]*/python[0-9.]*' | head -1)}"
[ -n "$PYBIN" ] && [ -x "$PYBIN" ] || PYBIN=$(command -v python3)
OWNER=$(stat -c '%U' "$AGENT")
say "service=$SERVICE  python=$PYBIN  agent_owner=$OWNER  dir=$AGENT_DIR"
say "BEFORE: agent active=$(systemctl is-active "$SERVICE")  version=$(grep -oE 'VERSION = \"[0-9.]+\"' "$AGENT" | head -1)"

# ---- install module beside the agent (agent's dir is on its sys.path) ----
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/continuous_scheduler.py "$AGENT_DIR/continuous_scheduler.py"
say "installed continuous_scheduler.py"

# ---- byte-compile + SMOKE IMPORT in the agent's python, from the agent dir ----
"$PYBIN" -m py_compile "$AGENT_DIR/continuous_scheduler.py" || { say "module does NOT compile — aborting (nothing grafted)"; exit 1; }
if ! ( cd "$AGENT_DIR" && sudo -u "$OWNER" "$PYBIN" -c "import continuous_scheduler; print('import OK')" ); then
  say "SMOKE IMPORT FAILED — continuous_scheduler cannot import (liftlab/av/onvif_resolve missing in the agent venv?)."
  say "ABORTING before any graft. Removing the module. No agent change made."
  rm -f "$AGENT_DIR/continuous_scheduler.py" "$AGENT_DIR/__pycache__/continuous_scheduler."*
  exit 1
fi
say "smoke import OK (liftlab + av + onvif_resolve importable in the agent runtime)"

# ---- graft the non-blocking dispatch (backup-first, idempotent) ----
sudo -u "$OWNER" "$PYBIN" /tmp/watch_channel_graft.py || { say "graft step failed"; exit 1; }
"$PYBIN" -m py_compile "$AGENT" || { say "agent.py does NOT compile after graft — RESTORE the newest $AGENT.bak.*"; exit 1; }

# ---- restart + verify ----
systemctl restart "$SERVICE"; sleep 4
AC=$(systemctl is-active "$SERVICE")
VER=$(grep -oE 'VERSION = "[0-9.]+"' "$AGENT" | head -1)
say "AFTER: agent active=$AC  version=$VER"
if [ "$AC" = active ] && echo "$VER" | grep -q '0.7.0'; then
  say "RESULT: PASS — watch_channel dispatch live, VERSION 0.7.0, agent healthy."
  say "Next: start with a watch_channel job {channel:29, action:start} during a DOORS-SHUT quiet moment (baseline seed)."
else
  say "RESULT: CHECK above. Rollback: restore newest $AGENT.bak.*, rm $AGENT_DIR/continuous_scheduler.py, systemctl restart $SERVICE"
  journalctl -u "$SERVICE" -n 20 --no-pager | sed 's/^/[watch]   /'
fi
