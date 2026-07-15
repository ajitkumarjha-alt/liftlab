#!/usr/bin/env bash
# Wrap the continuous watch as a restart-on-boot service with DURABLE state under
# /home/askjitk/liftlab-watch (out of /tmp — /tmp survived 11.5h only by luck of no
# reboot). RUN ON THE PI AS ROOT. ENABLES boot-start but does NOT start — start it
# during a doors-shut lull (task 3): sudo systemctl start liftlab-watch
# Requires: /tmp/liftlab-watch.service, /tmp/watch_local.py (redeploys it),
#           /tmp/continuous_scheduler.py (redeploys the freeze-fix to B4_DIR).
set -uo pipefail
OWNER=askjitk
AGENT_DIR=/home/askjitk/liftlab-b3/pi-agent
B4_DIR=/home/askjitk/liftlab-b4
STATE_DIR=/home/askjitk/liftlab-watch
UNIT=/etc/systemd/system/liftlab-watch.service
say(){ echo "[watch-svc] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/liftlab-watch.service /tmp/watch_local.py /tmp/continuous_scheduler.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done

# 1) redeploy the freeze-fixed scheduler (B4) + the run-capable watch_local (agent dir)
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/continuous_scheduler.py "$B4_DIR/continuous_scheduler.py"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/watch_local.py "$AGENT_DIR/watch_local.py"
grep -q '_seg_remeasure' "$B4_DIR/continuous_scheduler.py" && say "scheduler: freeze-fix present"
( cd "$B4_DIR" && sudo -u "$OWNER" env PYTHONPATH="$B4_DIR" "$B4_DIR/.venv/bin/python" -c "import continuous_scheduler;print('scheduler import OK')" ) \
  || { say "scheduler import FAILED under B4 — aborting"; exit 1; }

# 2) durable state dir + migrate any /tmp state
install -d -o "$OWNER" -g "$OWNER" -m 755 "$STATE_DIR"
[ -d /tmp/liftlab-watch ] && cp -a /tmp/liftlab-watch/. "$STATE_DIR/" 2>/dev/null && say "migrated /tmp/liftlab-watch/* -> $STATE_DIR" || true
chown -R "$OWNER":"$OWNER" "$STATE_DIR"
say "state dir: $STATE_DIR (owner $OWNER)"

# 3) install + enable the unit (boot-start), do NOT start
install -m 644 /tmp/liftlab-watch.service "$UNIT"
systemctl daemon-reload
systemctl enable liftlab-watch.service >/dev/null 2>&1
say "installed + ENABLED liftlab-watch.service (starts on boot); NOT started now."
say "WATCH_RUN_DIR=$(systemctl cat liftlab-watch | grep -oP 'WATCH_RUN_DIR=\K[^ ]+')"
say ""
say "TASK 3 — during a DOORS-SHUT lull:  sudo systemctl start liftlab-watch"
say "  watch:   journalctl -u liftlab-watch -f   ;  tail -f $STATE_DIR/watch_ch29.log"
say "  status:  python3 -m json.tool $STATE_DIR/watch_ch29.status.json   (expect seed motion-quiet, baseline_confirmed=false)"
say "  eyeball /validation/site-A, then CONFIRM doors-shut:"
say "    sudo -u $OWNER $AGENT_DIR/.venv/bin/python $AGENT_DIR/watch_local.py confirm 29"
