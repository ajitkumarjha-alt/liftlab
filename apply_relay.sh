#!/usr/bin/env bash
# Install the DIRECT-PUT relay on the PI: relay_soak.sh + liftlab-relay.service (field-proven path).
# FILES NEEDED IN /tmp: apply_relay.sh relay_soak.sh liftlab-relay.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_relay.sh relay_soak.sh liftlab-relay.service; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON THE PI: sudo bash /tmp/apply_relay.sh   (does NOT start it)
set -uo pipefail
PIAG=/home/askjitk/liftlab-b3/pi-agent
UNIT=/etc/systemd/system/liftlab-relay.service
say(){ echo "[apply-relay] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/relay_soak.sh ] || { echo "missing /tmp/relay_soak.sh"; exit 2; }
[ -f /tmp/liftlab-relay.service ] || { echo "missing /tmp/liftlab-relay.service"; exit 2; }
[ -d "$PIAG" ] || { echo "pi-agent dir $PIAG not found"; exit 2; }

bash -n /tmp/relay_soak.sh || { say "relay_soak.sh syntax error — aborting"; exit 1; }
# STALE GUARD OVERRIDES. The Jul 21 trip fired ~40s after start, which is impossible under the
# 3-strikes/90s rule the old source documented — so the RUNNING config differed from the source, almost
# certainly via one of these keys in the agent env. They override the new measured-baseline logic
# silently, so surface them instead of letting the same ambiguity cost another day.
for k in RELAY_DOOR_FLOOR RELAY_DOOR_STRIKES; do
  if grep -q "^${k}=" /etc/liftlab-agent.env 2>/dev/null; then
    say "WARNING: /etc/liftlab-agent.env sets $k=$(grep "^${k}=" /etc/liftlab-agent.env | cut -d= -f2-)"
    say "  RELAY_DOOR_FLOOR pins an absolute floor and DEFEATS the measured baseline; RELAY_DOOR_STRIKES"
    say "  is no longer read (the sag is timed, not counted). Remove both unless you mean to override."
  fi
done
install -o askjitk -g askjitk -m 755 /tmp/relay_soak.sh "$PIAG/relay_soak.sh"
install -m 644 /tmp/liftlab-relay.service "$UNIT"
mkdir -p /home/askjitk/liftlab-watch && chown askjitk:askjitk /home/askjitk/liftlab-watch
systemctl daemon-reload
systemctl enable liftlab-relay >/dev/null 2>&1 || true
say "installed direct-PUT relay_soak.sh + unit (NOT started). Stall signal = newest-segment AGE on the"
say "  VM (server clock, bitrate-independent); unknown age => NO restart, so a telemetry blip can't"
say "  restart all 7. Supervisor has its own watchdog: no loop tick in RELAY_LOOP_STALL_S => SIGKILL."
say "  door guard floor = 8.0 fps (RELAY_DOOR_FLOOR in the unit; above the 6fps quality alarm)."
say "door watch active? -> $(systemctl is-active liftlab-watch 2>/dev/null)"
say ""
say "PLAN:"
say "  1. Pi: sudo systemctl start liftlab-relay"
say "  1b. VERIFY THE WATCHDOG ARMED — without this line the 22h-stall class is unprotected:"
say "      journalctl -u liftlab-relay -n 30 | grep 'supervisor watchdog armed'"
say "  2. Pi: tail -f /home/askjitk/liftlab-watch/relay_soak.csv   (want 7/7 delivering, sum ~3.6)"
say "  3. Then the stitch fix (restart, no lull — persistence reuses the baseline)"
say "  4. Run the 24h"
say "Stop anytime: sudo systemctl stop liftlab-relay   (door watch untouched either way)"
