#!/usr/bin/env bash
# Add permanent Pi-health trends to the fleet dashboard: reinstall survey_api.py
# (pi_telemetry table + /telemetry endpoint) + patch gateway_api.py & dashboard.html.
# RUN AS ROOT ON THE VM:  sudo bash /tmp/apply_pihealth.sh
# Requires /tmp/survey_api.py and /tmp/apply_pihealth_patch.py present.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[pihealth] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/survey_api.py /tmp/apply_pihealth_patch.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done

$PY -m py_compile /tmp/survey_api.py || { say "survey_api.py does NOT compile — aborting"; exit 1; }
install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py (+pi_telemetry, /telemetry)"

sudo -u liftlab $PY /tmp/apply_pihealth_patch.py
$PY -m py_compile "$APP/gateway_api.py" "$APP/survey_api.py" || { say "post-patch compile FAILED — check backups"; exit 1; }

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
say "AFTER: service=$AC /=$(code http://127.0.0.1:9090/) /telemetry/site-A=$(code http://127.0.0.1:9090/telemetry/site-A) basicauth=$(code https://lift.gargi.online/)"
say "pihealth panel in fleet: $(curl -s http://127.0.0.1:9090/ | grep -c 'pihealth')"
if [ "$AC" = active ] && [ "$(code http://127.0.0.1:9090/telemetry/site-A)" = 200 ]; then
  say "RESULT: PASS — telemetry endpoint live; Pi-health panel in dashboard. (Series fills as heartbeats land.)"
else
  say "RESULT: CHECK the AFTER line (rollback: restore *.bak.* for survey_api.py/gateway_api.py/dashboard.html, restart)"
fi
