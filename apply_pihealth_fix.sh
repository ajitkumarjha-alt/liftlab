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

# the shared-header guard needs these; this script did not define them
OWNER=liftlab
id "$OWNER" >/dev/null 2>&1 || OWNER=root
# nav_common.py is a HARD dependency of every page module (the shared header). Installing a page
# module without it is an ImportError that takes the ENTIRE operator UI down, not just this page.
if [ -f /tmp/nav_common.py ]; then
  $PY -m py_compile /tmp/nav_common.py || { say "nav_common.py failed to compile — aborting"; exit 1; }
  install -o "$OWNER" -g "$OWNER" -m 644 /tmp/nav_common.py "$APP/nav_common.py"
  say "installed nav_common.py (shared header)"
elif [ ! -f "$APP/nav_common.py" ]; then
  say "ABORT: nav_common.py is neither in /tmp nor installed. Every page imports it; continuing"
  say "  would ImportError the whole UI. Re-curl nav_common.py and run again."; exit 1
fi

$PY -m py_compile /tmp/survey_api.py || { say "survey_api.py does NOT compile — aborting"; exit 1; }
install -o liftlab -g liftlab -m 644 /tmp/survey_api.py "$APP/survey_api.py"
say "installed survey_api.py (+/pihealth graph)"
sudo -u liftlab $PY /tmp/apply_pihealth_fix_patch.py

systemctl restart liftlab-cloud; sleep 3
AC=$(systemctl is-active liftlab-cloud)
say "AFTER: service=$AC /=$(code http://127.0.0.1:9090/) /pihealth/site-A=$(code http://127.0.0.1:9090/pihealth/site-A) /telemetry/site-A=$(code http://127.0.0.1:9090/telemetry/site-A)"
[ "$AC" = active ] && [ "$(code http://127.0.0.1:9090/pihealth/site-A)" = 200 ] && say "RESULT: PASS — flicker fixed, live graph at /pihealth/<gw>" || say "RESULT: CHECK the AFTER line"
