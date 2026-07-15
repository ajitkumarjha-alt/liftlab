#!/usr/bin/env bash
# Install the overnight relay soak on the PI: relay_soak.sh + liftlab-relay.service.
# RUN AS ROOT ON THE PI: sudo bash /tmp/apply_relay.sh   (needs /tmp/relay_soak.sh + /tmp/liftlab-relay.service)
# Does NOT start it — prints the start command so you deploy the VM cap FIRST.
set -uo pipefail
PIAG=/home/askjitk/liftlab-b3/pi-agent
UNIT=/etc/systemd/system/liftlab-relay.service
say(){ echo "[apply-relay] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/relay_soak.sh ] || { echo "missing /tmp/relay_soak.sh"; exit 2; }
[ -f /tmp/liftlab-relay.service ] || { echo "missing /tmp/liftlab-relay.service"; exit 2; }
[ -d "$PIAG" ] || { echo "pi-agent dir $PIAG not found"; exit 2; }

bash -n /tmp/relay_soak.sh || { say "relay_soak.sh has a syntax error — aborting"; exit 1; }
install -o askjitk -g askjitk -m 755 /tmp/relay_soak.sh "$PIAG/relay_soak.sh"
install -m 644 /tmp/liftlab-relay.service "$UNIT"
mkdir -p /home/askjitk/liftlab-watch && chown askjitk:askjitk /home/askjitk/liftlab-watch
systemctl daemon-reload
systemctl enable liftlab-relay >/dev/null 2>&1 || true
say "installed relay_soak.sh + unit (NOT started)."
say "door watch still running? -> $(systemctl is-active liftlab-watch 2>/dev/null)"
say ""
say "NEXT, IN ORDER:"
say "  1. VM: sudo bash /tmp/apply_relay_vm.sh   (mounts the 200M tmpfs cap + guard, PROVES it)"
say "  2. Pi: sudo systemctl start liftlab-relay"
say "  3. Pi: watch the first rows:  tail -f /home/askjitk/liftlab-watch/relay_soak.csv"
say "  4. Pi: confirm door unharmed: $PIAG/.venv/bin/python $PIAG/watch_local.py status 29 | grep -o \"'signal_fps': [0-9.]*\""
say "Stop anytime: sudo systemctl stop liftlab-relay   (door watch is untouched either way)"
