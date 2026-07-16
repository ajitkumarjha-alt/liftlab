#!/usr/bin/env bash
# Install the DECOUPLED relay on the PI: relay_soak.sh (supervisor) + relay_upload.py (uploader)
# + liftlab-relay.service, and CAP the local Pi tmpfs so a stalled uploader can never eat Pi RAM
# and threaten the door watch. RUN AS ROOT ON THE PI:
#   sudo bash /tmp/apply_relay.sh   (needs /tmp/relay_soak.sh /tmp/relay_upload.py /tmp/liftlab-relay.service)
# Does NOT start it — deploy the VM cap first.
set -uo pipefail
PIAG=/home/askjitk/liftlab-b3/pi-agent
UNIT=/etc/systemd/system/liftlab-relay.service
OUT=/dev/shm/liftlab-relay-out
CAP="${PI_RELAY_CAP:-300M}"
say(){ echo "[apply-relay] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in relay_soak.sh relay_upload.py liftlab-relay.service; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
[ -d "$PIAG" ] || { echo "pi-agent dir $PIAG not found"; exit 2; }
PY="$PIAG/.venv/bin/python"
[ -x "$PY" ] || { echo "B3 venv python missing at $PY"; exit 2; }

bash -n /tmp/relay_soak.sh || { say "relay_soak.sh syntax error — aborting"; exit 1; }
"$PY" -m py_compile /tmp/relay_upload.py || { say "relay_upload.py does not compile — aborting"; exit 1; }
"$PY" -c "import httpx" 2>/dev/null || { say "httpx NOT in the B3 venv — uploader needs it. Aborting."; exit 1; }
install -o askjitk -g askjitk -m 755 /tmp/relay_soak.sh  "$PIAG/relay_soak.sh"
install -o askjitk -g askjitk -m 755 /tmp/relay_upload.py "$PIAG/relay_upload.py"
install -m 644 /tmp/liftlab-relay.service "$UNIT"
mkdir -p /home/askjitk/liftlab-watch && chown askjitk:askjitk /home/askjitk/liftlab-watch

# ---------- CAP the LOCAL Pi tmpfs (protect Pi RAM / the door watch) ----------
mkdir -p "$OUT"
if mountpoint -q "$OUT"; then
  mount -o remount,size=$CAP "$OUT"
else
  mount -t tmpfs -o size=$CAP,mode=0755,uid=askjitk,gid=askjitk tmpfs "$OUT"
fi
chown askjitk:askjitk "$OUT"
grep -qF "$OUT" /etc/fstab || echo "tmpfs $OUT tmpfs size=$CAP,mode=0755,uid=askjitk,gid=askjitk 0 0" >> /etc/fstab
# PROVE the local cap holds (a stalled uploader must hit ENOSPC, not eat Pi RAM)
if dd if=/dev/zero of="$OUT/_captest" bs=1M count=400 status=none 2>/dev/null; then
  rm -f "$OUT/_captest"; say "WARNING: local tmpfs cap NOT enforced (400M write succeeded) — check the mount"
else
  U=$(du -m "$OUT/_captest" 2>/dev/null|awk '{print $1}'); rm -f "$OUT/_captest"
  say "local Pi tmpfs cap PROVEN: ENOSPC at ~${U}MB (<= $CAP). A stalled uploader cannot eat Pi RAM."
fi

systemctl daemon-reload
systemctl enable liftlab-relay >/dev/null 2>&1 || true
say "installed relay_soak.sh + relay_upload.py + unit + capped local store $OUT ($CAP). NOT started."
say "door watch active? -> $(systemctl is-active liftlab-watch 2>/dev/null)"
say ""
say "NEXT, IN ORDER:"
say "  1. VM: sudo bash /tmp/apply_relay_vm.sh   (200M VM tmpfs cap + watchdog, PROVES it)"
say "  2. Pi: sudo systemctl start liftlab-relay"
say "  3. Pi: tail -f /home/askjitk/liftlab-watch/relay_soak.csv   (want streams_delivering=7/7, sum ~6.5)"
say "     Pi: journalctl -u liftlab-relay -f   (uploader/producer restarts, door strikes)"
say "  4. Confirm door unharmed: $PY $PIAG/watch_local.py status 29 | grep -o \"'signal_fps': [0-9.]*\""
say "Stop anytime: sudo systemctl stop liftlab-relay   (door watch untouched either way)"
