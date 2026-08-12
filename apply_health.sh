#!/usr/bin/env bash
# Install the DAILY HEALTH LINE on the VM: health_check.py + a 15-minute timer.
#
# WHY A 15-MINUTE TIMER FOR A DAILY LINE. The daily 08:30 line is the heartbeat of the monitor
# itself; "immediately on breach" is the part that needs a short cadence. The check is a handful of
# indexed MAX() queries — measured below before the timer is installed — so the cadence costs
# effectively nothing on a 2-vCPU box, unlike the precompute job that shares it.
#
# WHAT IT DOES NOT DO: it does not restart anything, touch the GPU box, or write to any table other
# than its own health_status. It is read-only over every table it judges.
#
# FILES NEEDED IN /tmp:  apply_health.sh health_check.py dash_api.py
#   sudo bash /tmp/apply_health.sh
#   sudo EVERY=5min bash /tmp/apply_health.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
EVERY="${EVERY:-15min}"
say(){ echo "[health] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in health_check.py dash_api.py; do
  [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }
done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/health_check.py /tmp/dash_api.py || { say "compile failed"; exit 1; }
grep -q "_health" /tmp/dash_api.py || { say "ABORT: /tmp/dash_api.py has no _health — wrong file?"; exit 2; }
grep -q "healthbar" /tmp/dash_api.py || { say "ABORT: /tmp/dash_api.py has no banner — wrong file?"; exit 2; }

DB=$(systemctl show "$SVC" -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
[ -f "$DB" ] || { say "ABORT: no database at $DB"; exit 2; }

install -o "$OWNER" -g "$OWNER" -m 755 /tmp/health_check.py "$APP/health_check.py"
say "installed health_check.py"

# ── RUN IT ONCE, IN THE FOREGROUND, BEFORE INSTALLING THE TIMER ──────────────────────────────
# A monitor that is installed but broken is worse than no monitor: its silence reads as health. So
# the first run happens here, its output is shown, and a crash aborts the install.
say "first run (this also creates health_status) ..."
T0=$(date +%s%N)
# CAPTURE THE STATUS DIRECTLY. This was `if ! cmd; then RC=$?` — and inside `if !`, `$?` is the
# status of the NEGATION, which is 0 whenever the command failed. So RC was ALWAYS 0, the
# `[ "$RC" = 1 ]` test could never pass, and every breach aborted the install with the
# self-contradicting message "failed with exit 0". Live first run, 2026-08-12 20:07. A genuine
# crash reported the same 0, so the abort has never once said what actually happened.
sudo -u "$OWNER" env GATEWAY_DB="$DB" GATEWAY_ID="$GW" \
     $(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^HEALTH_[A-Z_]+=' | tr '\n' ' ') \
     $PY "$APP/health_check.py" "$GW"
RC=$?
# EXIT 1 IS A BREACH, NOT A CRASH. The script reports fleet state through its exit code, so a
# non-zero here is only fatal if it is not 1 — otherwise installing would be blocked by exactly the
# condition the monitor exists to report.
if [ "$RC" -ne 0 ] && [ "$RC" -ne 1 ]; then
  say "ABORT: health_check.py crashed with exit $RC — no units installed, dash_api.py untouched."
  say "       ($APP/health_check.py WAS copied; it is inert without the timer.)"
  exit 1
fi
[ "$RC" = 1 ] && say "(exit 1 = at least one camera is currently silent — a fleet fault, not an install fault)"
MS=$(( ($(date +%s%N) - T0) / 1000000 ))
say "first run took ${MS}ms"
if [ "$MS" -gt 5000 ]; then
  say "WARNING: ${MS}ms is slow for a check that runs every $EVERY — consider a longer EVERY"
fi

N=$(sqlite3 "$DB" "SELECT COUNT(*) FROM health_status WHERE gateway_id='$GW';" 2>/dev/null || echo 0)
[ "${N:-0}" -ge 1 ] || { say "ABORT: health_status empty after the run — the banner would read 'never run'"; exit 1; }

# ── dashboard: banner + /dash/{gw}/health ────────────────────────────────────────────────────
STAMP=$(date +%Y%m%d-%H%M%S); BAK="$APP/dash_api.py.bak.$STAMP"
cp "$APP/dash_api.py" "$BAK"; say "backup: $BAK"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/dash_api.py "$APP/dash_api.py"

cat > /etc/systemd/system/liftlab-health.service <<UNIT
[Unit]
Description=liftlab fleet health line (are all cameras still posting)
After=network.target
[Service]
Type=oneshot
User=$OWNER
WorkingDirectory=$APP
Nice=10
IOSchedulingClass=idle
Environment=GATEWAY_DB=$DB
Environment=GATEWAY_ID=$GW
$(systemctl show "$SVC" -p Environment --value | tr ' ' '\n' | grep -E '^HEALTH_[A-Z_]+=' | sed 's/^/Environment=/')
ExecStart=$PY $APP/health_check.py $GW
# SuccessExitStatus: exit 1 means "a camera is silent" — a finding, not a unit failure. Without
# this every breach would also show as a failed systemd unit and the real signal gets buried.
SuccessExitStatus=0 1
UNIT
cat > /etc/systemd/system/liftlab-health.timer <<UNIT
[Unit]
Description=run the liftlab health check every $EVERY (daily line at 08:30 IST, plus breach edges)
[Timer]
OnBootSec=2min
OnUnitActiveSec=$EVERY
AccuracySec=30s
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now liftlab-health.timer >/dev/null 2>&1
say "timer: $(systemctl is-active liftlab-health.timer), every $EVERY"

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
if [ "$(systemctl is-active "$SVC")" != active ]; then
  say "SERVICE DOWN — restoring $BAK"
  cp "$BAK" "$APP/dash_api.py"; chown "$OWNER:$OWNER" "$APP/dash_api.py"
  systemctl disable --now liftlab-health.timer >/dev/null 2>&1
  rm -f /etc/systemd/system/liftlab-health.{timer,service}; systemctl daemon-reload
  systemctl restart "$SVC"; journalctl -u "$SVC" -n 30 --no-pager; exit 1
fi

say "---- the line, as the box now reports it ----"
curl -fsS "http://127.0.0.1:9090/dash/$GW/health" || say "(could not reach /dash/$GW/health locally)"
say "---------------------------------------------"
say "done. journal: journalctl -u liftlab-health -n 20 --no-pager"
if ! systemctl show "$SVC" -p Environment --value | grep -q 'HEALTH_WEBHOOK_URL\|HEALTH_SMTP_HOST'; then
  say "NOTE: no push channel is configured, so this line exists ONLY on the dashboard, at"
  say "      /dash/$GW/health, and in the journal. Nothing will reach a phone or an inbox."
  say "      To enable one: add HEALTH_WEBHOOK_URL=... to the $SVC environment and re-run."
fi
