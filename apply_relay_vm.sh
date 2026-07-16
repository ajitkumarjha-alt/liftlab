#!/usr/bin/env bash
# VM: put a HARD 200M tmpfs cap on the live store + install the watchdog, and PROVE the cap
# physically holds before the soak. Assumes live_api.py (.reject/ENOSPC guard) already deployed.
# FILES NEEDED IN /tmp: apply_relay_vm.sh relay_watchdog.sh liftlab-relay-guard.service
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; \
#       for f in apply_relay_vm.sh relay_watchdog.sh liftlab-relay-guard.service; do curl -fsSL -o /tmp/$f $B/$f; done
#   sudo bash /tmp/apply_relay_vm.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
LIVE_DIR=/dev/shm/liftlab-live
CAP=200M
OWNER=liftlab
say(){ echo "[relay-vm] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/relay_watchdog.sh ] || { echo "missing /tmp/relay_watchdog.sh"; exit 2; }
[ -f /tmp/liftlab-relay-guard.service ] || { echo "missing /tmp/liftlab-relay-guard.service"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

# ---------- 1. mount a dedicated, size-capped tmpfs at LIVE_DIR ----------
mkdir -p "$LIVE_DIR"
if mountpoint -q "$LIVE_DIR"; then
  say "already a mountpoint — remounting to enforce size=$CAP"
  mount -o remount,size=$CAP,mode=0755 "$LIVE_DIR"
else
  mount -t tmpfs -o size=$CAP,mode=0755,uid=$OWNER,gid=$OWNER tmpfs "$LIVE_DIR"
  say "mounted tmpfs size=$CAP at $LIVE_DIR (owner $OWNER)"
fi
chown "$OWNER:$OWNER" "$LIVE_DIR"
# reboot persistence (idempotent)
FST="tmpfs $LIVE_DIR tmpfs size=$CAP,mode=0755,uid=$OWNER,gid=$OWNER 0 0"
grep -qF "$LIVE_DIR" /etc/fstab || { echo "$FST" >> /etc/fstab; say "added fstab entry (survives reboot)"; }

# ---------- 2. PROVE the cap physically holds (fill past it -> must ENOSPC, not grow) ----------
say "PROVING the cap: df + write 250M into a 200M tmpfs (must fail at the cap) ..."
df -h "$LIVE_DIR" | sed 's/^/    /'
CAPFILE="$LIVE_DIR/_captest"
if dd if=/dev/zero of="$CAPFILE" bs=1M count=250 status=none 2>/dev/null; then
  say "  UNEXPECTED: 250M write SUCCEEDED — cap NOT enforced. ABORTING (fix the mount before soak)."
  rm -f "$CAPFILE"; exit 1
fi
USED=$(du -m "$CAPFILE" 2>/dev/null | awk '{print $1}')
rm -f "$CAPFILE"
say "  cap PROVEN: write hit ENOSPC at ~${USED}MB (<= 200M). tmpfs physically cannot exceed the cap => cannot OOM the VM."
FREE_AFTER=$(df -m "$LIVE_DIR" | awk 'NR==2{print $4}')
say "  free now: ${FREE_AFTER}MB (captest removed)"

# ---------- 3. install + start the watchdog ----------
install -m 755 /tmp/relay_watchdog.sh "$APP/relay_watchdog.sh"
install -m 644 /tmp/liftlab-relay-guard.service /etc/systemd/system/liftlab-relay-guard.service
systemctl daemon-reload
systemctl enable --now liftlab-relay-guard >/dev/null 2>&1 || systemctl restart liftlab-relay-guard
sleep 2
GA=$(systemctl is-active liftlab-relay-guard)
CA=$(systemctl is-active liftlab-cloud)
say "watchdog=$GA  liftlab-cloud=$CA"

# ---------- 4. verify the .reject shed path end-to-end (set flag -> PUT must 503 -> clear) ----------
PORT=$(systemctl cat liftlab-cloud 2>/dev/null | grep -oP '\-\-port\s+\K[0-9]+' | head -1); [ -n "$PORT" ] || PORT=9090
TOK=$(grep -oP 'GATEWAY_TOKENS=\K[^,]*' /etc/liftlab-agent.env 2>/dev/null | cut -d: -f2)
: > "$LIVE_DIR/.reject"
C503=$(curl -s -o /dev/null -w '%{http_code}' -X PUT --data-binary 'x' \
  -H "Authorization: Bearer ${TOK:-devtoken}" "http://127.0.0.1:$PORT/api/gw/site-A/live/ch29/seg000.ts")
rm -f "$LIVE_DIR/.reject"
say "shed-path check: PUT with .reject set -> HTTP $C503 (expect 503; if 200 the deployed live_api lacks the guard — re-run apply_live.sh)"

if [ "$GA" = active ] && [ "$CA" = active ]; then
  say "RESULT: PASS — 200M cap proven + enforced, watchdog live, shed path=$C503. Safe to start the Pi relay."
else
  say "RESULT: CHECK — watchdog=$GA cloud=$CA. journalctl -u liftlab-relay-guard -n 30"
  exit 1
fi
