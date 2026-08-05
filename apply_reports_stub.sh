#!/usr/bin/env bash
# Gateway: replace the /reports 404 with a stub that says where reports went.
# A 404 where a working feature used to be reads as breakage and sends someone hunting for a fault
# that does not exist. FILES NEEDED IN /tmp: apply_reports_stub.sh reports_moved.py nav_common.py
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
say(){ echo "[stub] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 2; }
for f in reports_moved.py nav_common.py; do [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }; done
id "$OWNER" >/dev/null 2>&1 || OWNER=root

$PY -m py_compile /tmp/reports_moved.py /tmp/nav_common.py || { say "compile failed"; exit 1; }
TMPD=$(mktemp -d); cp /tmp/reports_moved.py /tmp/nav_common.py "$TMPD/"
( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0,'$TMPD')
from fastapi import FastAPI
import reports_moved
assert reports_moved.__file__.startswith('$TMPD')
a=FastAPI(); a.include_router(reports_moved.reports_moved_router)
assert '/reports' in a.openapi()['paths'], 'stub route did not register'
print('  smoke ok')" ) || { say "SMOKE FAILED — nothing installed"; rm -rf "$TMPD"; exit 1; }
rm -rf "$TMPD"

STAMP=$(date +%Y%m%d-%H%M%S)
for f in reports_moved.py nav_common.py; do
  [ -f "$APP/$f" ] && cp "$APP/$f" "$APP/$f.bak.$STAMP"
  install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"
done
cp "$APP/main.py" "$APP/main.py.bak.$STAMP"
$PY - <<'PYEOF'
import pathlib
p = pathlib.Path("/opt/liftlab-b3/cloud/main.py"); s = p.read_text()
if "reports_moved_router" in s:
    print("  main.py already mounts the stub"); raise SystemExit(0)
a = "from dash_api import dash_router"
b = "app.include_router(dash_router)"
assert a in s and b in s, "anchors missing"
s = s.replace(a, a + "\nfrom reports_moved import reports_moved_router", 1)
s = s.replace(b, b + "\napp.include_router(reports_moved_router)", 1)
p.write_text(s); print("  main.py: stub mounted")
PYEOF
systemctl restart "$SVC"
for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 http://127.0.0.1:9090/openapi.json && break; sleep 2; done
C=$(curl -s -o /tmp/.stub -w '%{http_code}' --max-time 30 http://127.0.0.1:9090/reports)
if [ "$C" != 200 ]; then
  say "VERIFY FAILED: /reports -> $C, rolling back"
  cp "$APP/main.py.bak.$STAMP" "$APP/main.py"; systemctl restart "$SVC"; exit 1
fi
grep -q "Reports moved" /tmp/.stub && say "  /reports: 200 and says it moved" || { say "200 but wrong content"; exit 1; }
grep -q "dev.gargi.online" /tmp/.stub && say "  links dev-box" || say "  WARNING: no link in the page"
for p in /dash "/ops/site-A/data"; do
  say "  $p: $(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090$p")"
done
say "RESULT: PASS — stub live. Backups *.bak.$STAMP"
