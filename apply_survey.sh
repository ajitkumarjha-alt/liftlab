#!/usr/bin/env bash
# Phase A cloud apply — survey + channel-map + fleet state badge/button.
# RUN AS ROOT ON THE VM:  sudo bash /tmp/apply_survey.sh
# Requires /tmp/survey_api.py and /tmp/apply_survey_patch.py present (curl them first).
# Additive: installs survey_api.py, patches main.py/gateway_api.py/dashboard.html
# (backups first), restarts, verifies. Survey snapshots live in /var/lib/liftlab
# (runtime data, NOT the source tree) — never in the repo/build context.
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
say(){ echo "[survey] $*"; }
code(){ curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$@"; }

[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in /tmp/survey_api.py /tmp/apply_survey_patch.py; do
  [ -f "$f" ] || { echo "missing $f — curl it first"; exit 2; }
done

say "BEFORE: service=$(systemctl is-active liftlab-cloud) /=$(code http://127.0.0.1:9090/) basicauth=$(code https://lift.gargi.online/)"

install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py"

# byte-compile the new module before wiring it in (fail fast on a typo)
$PY -m py_compile "$APP/survey_api.py" || { say "survey_api.py does NOT compile — aborting"; exit 1; }

# patch the three existing files as the liftlab owner (preserves ownership)
sudo -u liftlab $PY /tmp/apply_survey_patch.py
$PY -m py_compile "$APP/main.py" "$APP/gateway_api.py" || { say "post-patch compile FAILED — check backups"; exit 1; }

systemctl restart liftlab-cloud
sleep 3
AC=$(systemctl is-active liftlab-cloud)
C_ROOT=$(code http://127.0.0.1:9090/)
C_SURVEY=$(code http://127.0.0.1:9090/survey/site-A)
C_EVENTS=$(code http://127.0.0.1:9090/events)
C_AUTH=$(code https://lift.gargi.online/)
BTN=$(curl -s http://127.0.0.1:9090/ | grep -c 'Run channel survey')
say "AFTER: service=$AC /=$C_ROOT /survey/site-A=$C_SURVEY /events=$C_EVENTS basicauth=$C_AUTH survey-button=$BTN"

if [ "$AC" = active ] && [ "$C_ROOT" = 200 ] && [ "$C_SURVEY" = 200 ] && [ "$C_EVENTS" = 200 ] && [ "$C_AUTH" = 401 ] && [ "$BTN" -ge 1 ]; then
  say "RESULT: PASS — survey backend live; fleet badge/button in; events + basicauth intact"
else
  say "RESULT: CHECK the AFTER line. Rollback: restore the newest *.bak.* for main.py/gateway_api.py/dashboard.html, rm survey_api.py, systemctl restart liftlab-cloud"
fi
