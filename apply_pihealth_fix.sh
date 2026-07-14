#!/usr/bin/env bash
# Pi-health flicker fix + live graph page. RUN AS ROOT ON THE VM:
#   sudo bash /tmp/apply_pihealth_fix.sh   (needs /tmp/survey_api.py + /tmp/apply_pihealth_fix_patch.py)
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[pihealth-fix] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
for f in /tmp/survey_api.py /tmp/apply_pihealth_fix_patch.py; do [ -f "$f" ] || { echo "missing $f"; exit 2; }; done

$PY -m py_compile /tmp/survey_api.py || { say "survey_api.py does NOT compile — aborting"; exit 1; }
install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py (+/pihealth graph)"
sudo -u liftlab $PY /tmp/apply_pihealth_fix_patch.py

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
say "AFTER: service=$AC /=$(code http://127.0.0.1:9090/) /pihealth/site-A=$(code http://127.0.0.1:9090/pihealth/site-A) /telemetry/site-A=$(code http://127.0.0.1:9090/telemetry/site-A)"
[ "$AC" = active ] && [ "$(code http://127.0.0.1:9090/pihealth/site-A)" = 200 ] && say "RESULT: PASS — flicker fixed, live graph at /pihealth/<gw>" || say "RESULT: CHECK the AFTER line"
