#!/usr/bin/env bash
# Add validation-frame viewing to the cloud: reinstall survey_api.py (now with
# validation routes) + a per-card "Validation" link. RUN AS ROOT ON THE VM:
#   sudo bash /tmp/apply_validation.sh      (requires /tmp/survey_api.py present)
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[validation] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/survey_api.py ] || { echo "missing /tmp/survey_api.py — curl it first"; exit 2; }

$PY -m py_compile /tmp/survey_api.py || { say "survey_api.py does NOT compile — aborting"; exit 1; }
install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py (+validation routes)"

sudo -u liftlab $PY - <<'PY'
import pathlib, shutil, time
p = pathlib.Path("/opt/liftlab-b3/cloud/dashboard.html"); s = p.read_text()
old = '<a class="svlink" href="/survey/${g.id}">'
new = '<a class="svlink" href="/validation/${g.id}">Validation ▸</a>' + old
if 'href="/validation/' in s:
    print("dashboard: validation link already present")
elif old not in s:
    print("dashboard: survey-link anchor NOT FOUND — SKIP (link can be added by hand)")
else:
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    p.write_text(s.replace(old, new, 1)); print("dashboard: validation link added")
PY

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
say "AFTER: service=$AC /=$(code http://127.0.0.1:9090/) /survey/site-A=$(code http://127.0.0.1:9090/survey/site-A) /validation/site-A=$(code http://127.0.0.1:9090/validation/site-A) basicauth=$(code https://lift.gargi.online/)"
if [ "$AC" = active ] && [ "$(code http://127.0.0.1:9090/validation/site-A)" = 200 ]; then
  say "RESULT: PASS — validation viewer live at /validation/<gw>"
else
  say "RESULT: CHECK the AFTER line (rollback: restore survey_api.py/dashboard.html *.bak.*, restart liftlab-cloud)"
fi
