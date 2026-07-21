#!/usr/bin/env bash
# Make the Pi a DUMB STREAMER: retire liftlab-watch, install the guard-free relay, start it.
# FILES NEEDED IN /tmp: apply_relay.sh relay_soak.sh liftlab-relay.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_relay.sh relay_soak.sh liftlab-relay.service; do curl -fsSL -o /tmp/$f $B/$f; done
# RUN AS ROOT ON THE PI: sudo bash /tmp/apply_relay.sh
#
# THIS SCRIPT RETIRES liftlab-watch (stop + disable). That is a one-way architectural change: the
# door-watch deliverable is banked (2.81s close, n=1053) and door cycles now come from the GPU
# DoorFloorEngine on the same feed. Set KEEP_WATCH=1 to install the relay WITHOUT retiring the watch.
set -uo pipefail
PIAG=/home/askjitk/liftlab-b3/pi-agent
UNIT=/etc/systemd/system/liftlab-relay.service
ERA=/home/askjitk/liftlab-watch/WATCH_ERA.md
CSV=/home/askjitk/liftlab-watch/relay_soak.csv
say(){ echo "[apply-relay] $*"; }
say "REV=dumb-streamer-1  (retires liftlab-watch; relay has NO door guard; Restart=always)"
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/relay_soak.sh ] || { echo "missing /tmp/relay_soak.sh"; exit 2; }
[ -f /tmp/liftlab-relay.service ] || { echo "missing /tmp/liftlab-relay.service"; exit 2; }
[ -d "$PIAG" ] || { echo "pi-agent dir $PIAG not found"; exit 2; }

bash -n /tmp/relay_soak.sh || { say "relay_soak.sh syntax error — aborting"; exit 1; }
# The new relay must not contain a single executable reference to the watch. Assert it, so a stale
# /tmp copy can never quietly reinstate the thing that cost 22h.
if grep -nE '^[^#]*(watch_local|door_fps\(|GUARD_TRIP)' /tmp/relay_soak.sh; then
  say "ABORT: /tmp/relay_soak.sh still references the watch/door guard (lines above) — stale copy?"; exit 1
fi

# ---------- 1. retire the door watch ----------
if [ "${KEEP_WATCH:-0}" = 1 ]; then
  say "KEEP_WATCH=1 — leaving liftlab-watch alone (relay is guard-free either way)"
else
  WAS=$(systemctl is-active liftlab-watch 2>/dev/null || echo unknown)
  ERA_TS=$(date -u +%FT%TZ)
  if [ "$WAS" = active ]; then
    systemctl stop liftlab-watch
    say "liftlab-watch STOPPED (was active)"
  else
    say "liftlab-watch was already $WAS"
  fi
  systemctl disable liftlab-watch >/dev/null 2>&1 && say "liftlab-watch DISABLED (will not start on boot)" \
    || say "liftlab-watch disable: nothing to do"
  # ---------- 2. the comparability boundary ----------
  # Pi-watch door data and GPU-engine door data are separate eras and must never be pooled. Record
  # the boundary where the data lives, not just in a commit message nobody reads at analysis time.
  mkdir -p "$(dirname "$ERA")"
  if [ ! -s "$ERA" ]; then
    cat > "$ERA" <<EOF
# Door-measurement era boundary

**Pi-watch era ENDED: ${ERA_TS}**

Everything before this timestamp: door cycles measured by \`liftlab-watch\` on the Pi
(watch_local.py, signal_fps ~9.4). Banked deliverable: 2.81s close, n=1053.

Everything after: door cycles measured by the GPU \`DoorFloorEngine\` (gpu_door.py) on the
same camera feed, emitted as \`gw_door_event\` with door_state + close_travel_s, stamped with
door_version + templates_hash.

## DO NOT POOL THE TWO ERAS
Different sensor, different sampling rate, different edge definition, different clock. A close
duration from before this timestamp and one from after are not comparable measurements and must
not be averaged, binned, or regressed together. Split every door-timing query on this boundary.

Retired because the watch's guard could stop the relay, and did — 22h (Jul 20 16:00 UTC) and
25min (Jul 21 14:12 UTC) of lost counting + floor OCR. Two components measuring the same thing,
one able to kill the other.
EOF
    chown askjitk:askjitk "$ERA"
    say "era boundary recorded: $ERA (watch era ended ${ERA_TS})"
  else
    say "era boundary already recorded at $ERA — not overwriting"
  fi
  # Stamp the CSV too, so the column that goes NA is explained in-band.
  [ -f "$CSV" ] && echo "${ERA_TS},WATCH_ERA_END,door_fps_now_NA,door_cycles_move_to_gpu_engine" >> "$CSV"
fi

# ---------- 3. install the guard-free relay ----------
install -o askjitk -g askjitk -m 755 /tmp/relay_soak.sh "$PIAG/relay_soak.sh"
install -m 644 /tmp/liftlab-relay.service "$UNIT"
mkdir -p /home/askjitk/liftlab-watch && chown askjitk:askjitk /home/askjitk/liftlab-watch
# Dead guard knobs in the agent env. Harmless now (nothing reads them) but they document a policy
# that no longer exists, and RELAY_DOOR_FLOOR in particular misleads anyone debugging later.
for k in RELAY_DOOR_FLOOR RELAY_DOOR_STRIKES RELAY_DOOR_MARGIN RELAY_DOOR_ABS_FLOOR; do
  grep -q "^${k}=" /etc/liftlab-agent.env 2>/dev/null && \
    say "NOTE: /etc/liftlab-agent.env still sets $k — now UNREAD (door guard deleted); safe to remove."
done
systemctl daemon-reload
systemctl enable liftlab-relay >/dev/null 2>&1 || true
systemctl restart liftlab-relay
sleep 8
AC=$(systemctl is-active liftlab-relay)
say "liftlab-relay = $AC | liftlab-watch = $(systemctl is-active liftlab-watch 2>/dev/null || echo inactive)"
if [ "$AC" != active ]; then
  say "RESULT: CHECK — relay not active. journalctl -u liftlab-relay -n 40"; exit 1
fi
# The supervisor watchdog is the only babysitter a dumb streamer has left. Assert it armed.
if journalctl -u liftlab-relay --since -60s --no-pager 2>/dev/null | grep -q "supervisor watchdog armed"; then
  say "supervisor watchdog: ARMED"
else
  say "RESULT: CHECK — relay is up but the supervisor watchdog did NOT arm."
  say "  journalctl -u liftlab-relay -n 40"; exit 1
fi
# Restart=always is the point of this rev: nothing the relay does should leave it dead.
say "restart policy: $(systemctl show -p Restart --value liftlab-relay) | start limit interval: $(systemctl show -p StartLimitIntervalUSec --value liftlab-relay)"
say ""
say "RESULT: PASS — Pi is a dumb streamer."
say "  Watch:  journalctl -u liftlab-relay -f"
say "  CSV:    tail -f $CSV   (want 7/7 delivering, sum ~3.6; door_fps column is NA forever now)"
say "  Truth:  https://lift.gargi.online/ops/site-A  — per-channel segment age is the pipe's health"
say "  Door timing now lives in gw_door_event from the GPU engine, NOT on this box."
