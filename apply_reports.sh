#!/usr/bin/env bash
# Deploy /reports — the export UI.
#
# WHAT SHIPS: reports_api.py (router + page), report_runner.py (the export subprocess),
# nav_common.py (adds the Reports link), and a main.py mount patch.
#
# THE RISK THIS DEPLOY CARRIES is not a broken page — it is a feature that lets a user start heavy
# work on a 2-vCPU box carrying seven live streams. That is why the export runs as a niced child
# process and never in a uvicorn worker, and why the gate below runs a REAL export end to end
# (submit -> poll -> download -> open the workbook) against the staged code before anything installs.
# A 200 from /reports/submit means "a row was inserted"; it says nothing about whether a workbook can
# be produced, which is exactly the class of gap that cost four incidents this week.
#
# FILES NEEDED IN /tmp:
#   apply_reports.sh reports_api.py report_runner.py nav_common.py apply_reports_patch.py
#   smoke_reports.sh liftlab_report/ (the package directory) lift_banks.json
#   sudo bash /tmp/apply_reports.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
GW="${GW:-site-A}"
FILES="reports_api.py report_runner.py nav_common.py"
say(){ echo "[reports] $*"; }
# DISCOVERED 2026-08-04: the gateway has never carried the report tooling at all — no
# liftlab_report package and no openpyxl in its venv. /reports is useless without both, so this
# deploy installs them. openpyxl is the package's ONLY third-party dependency (checked by walking
# every import in liftlab_report/), which is why this stays a small addition to a 53MB venv rather
# than dragging numpy/pandas onto a 2GB box.
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in $FILES apply_reports_patch.py smoke_reports.sh; do
  [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }
done
[ -d /tmp/liftlab_report ] || { echo "missing /tmp/liftlab_report (the report package)"; exit 2; }
[ -f /tmp/lift_banks.json ] || { echo "missing /tmp/lift_banks.json (bank sidecar)"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root

grep -q 'reports_router' /tmp/reports_api.py   || { say "ABORT: reports_api.py has no reports_router"; exit 2; }
grep -q 'start_new_session=True' /tmp/reports_api.py || { say "ABORT: worker is not spawned in its own session"; exit 2; }
grep -q 'os.nice' /tmp/reports_api.py          || { say "ABORT: worker is not niced — it would compete with the streams"; exit 2; }
grep -q '"reports", "Reports"' /tmp/nav_common.py || { say "ABORT: nav_common.py has no Reports link"; exit 2; }
# Job state must NOT go into gateway.db. Catch it here rather than after it is replicated.
grep -q 'gateway.db' /tmp/reports_api.py && \
  { grep -q 'REPORTS_DB' /tmp/reports_api.py || { say "ABORT: no separate REPORTS_DB"; exit 2; }; }
if grep -qE 'CREATE TABLE.*report_job' /tmp/reports_api.py && grep -qE 'GATEWAY_DB.*report_job' /tmp/reports_api.py; then
  say "ABORT: report_job would be created in gateway.db — it is Litestream-replicated"; exit 2
fi

$PY -m py_compile /tmp/reports_api.py /tmp/report_runner.py /tmp/nav_common.py \
  || { say "compile failed — nothing changed"; exit 1; }

# ── the report tooling itself (absent from this box until now) ───────────────
if ! $PY -c "import openpyxl" 2>/dev/null; then
  say "installing openpyxl into the venv (the report package's only third-party dependency) ..."
  $PY -m pip install --quiet --no-input openpyxl \
    || { say "ABORT: could not install openpyxl — nothing else changed"; exit 1; }
fi
$PY -c "import openpyxl; print('  openpyxl', openpyxl.__version__)" \
  || { say "ABORT: openpyxl still not importable"; exit 1; }

rm -rf "$APP/liftlab_report"
cp -r /tmp/liftlab_report "$APP/liftlab_report"
rm -rf "$APP/liftlab_report/__pycache__"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/lift_banks.json "$APP/lift_banks.json"
install -o "$OWNER" -g "$OWNER" -m 644 /tmp/lift_banks.json "$APP/liftlab_report/lift_banks.json"
chown -R "$OWNER:$OWNER" "$APP/liftlab_report"
say "installed liftlab_report package + lift_banks.json"
( cd "$APP" && $PY -c "
from liftlab_report import cli, eras, workbook
print('  report package imports OK; coefficients:', len(eras.COEFFICIENTS))" ) \
  || { say "ABORT: the report package does not import on this box"; exit 1; }

# smoke-import the CANDIDATES. sys.path.insert INSIDE the interpreter is the only ordering that
# binds: `cd $APP` puts CWD ahead of PYTHONPATH and would silently import the installed module.
TMPD=$(mktemp -d); cp /tmp/reports_api.py /tmp/report_runner.py /tmp/nav_common.py "$TMPD/"
if ! ( cd "$APP" && PYTHONPATH="$APP" $PY -c "
import sys; sys.path.insert(0, '$TMPD')
from fastapi import FastAPI
import reports_api, nav_common
assert reports_api.__file__.startswith('$TMPD'), 'imported the WRONG module: '+reports_api.__file__
assert nav_common.__file__.startswith('$TMPD'), 'imported the WRONG nav: '+nav_common.__file__
a=FastAPI(); a.include_router(reports_api.reports_router)
# Assert against the OpenAPI schema, not app.routes: on this FastAPI version include_router leaves
# _IncludedRouter objects in .routes which have no .path, so the obvious check raises AttributeError
# instead of answering the question. The schema is the authoritative list of what is reachable.
paths = a.openapi()['paths']
for want in ('/reports', '/reports/submit', '/reports/download/{job_id}', '/reports/job/{job_id}'):
    assert want in paths, f'route {want} did not register; got {sorted(paths)}'
print('smoke ok:', reports_api.__file__, '| routes:', len(paths))" ); then
  say "SMOKE-IMPORT FAILED — nothing installed."; rm -rf "$TMPD"; exit 1
fi

# ── REPORTS GATE (pre-install, against the staged code) ──────────────────────
say "reports gate: building a REAL export end to end against the staged code ..."
STAGE=$(mktemp -d)
cp "$APP"/*.py "$STAGE/" 2>/dev/null
cp /tmp/reports_api.py /tmp/report_runner.py /tmp/nav_common.py "$STAGE/"
# liftlab_report is a package; the staged app must import the INSTALLED one it will actually use.
if ! GATEWAY_DB="${GATEWAY_DB:-/var/lib/liftlab/gateway.db}" bash /tmp/smoke_reports.sh "$STAGE" "$APP"; then
  say "ABORT: the export did not complete end to end. NOTHING INSTALLED — /reports does not exist"
  say "  yet, so nothing regressed. Fix the gate's complaint and re-run."
  rm -rf "$STAGE" "$TMPD"; exit 1
fi
rm -rf "$STAGE" "$TMPD"
say "reports gate: PASSED"

# ── install ──────────────────────────────────────────────────────────────────
STAMP=$(date +%Y%m%d-%H%M%S)
for f in $FILES; do
  [ -f "$APP/$f" ] && { cp "$APP/$f" "$APP/$f.bak.$STAMP"; say "backup: $APP/$f.bak.$STAMP"; }
  install -o "$OWNER" -g "$OWNER" -m 644 "/tmp/$f" "$APP/$f"
done

install -d -o "$OWNER" -g "$OWNER" -m 0750 /var/lib/liftlab/reports
say "output dir: /var/lib/liftlab/reports (0750, $OWNER) — resident movement data, never world-readable"

$PY /tmp/apply_reports_patch.py || { say "ABORT: could not mount the router"; }

restore(){
  say "RESTORING backups .bak.$STAMP"
  for f in $FILES; do [ -f "$APP/$f.bak.$STAMP" ] && cp "$APP/$f.bak.$STAMP" "$APP/$f"; done
  [ -f "$APP/main.py.bak.$STAMP" ] && cp "$APP/main.py.bak.$STAMP" "$APP/main.py"
  chown -R "$OWNER:$OWNER" "$APP" 2>/dev/null
  systemctl restart "$SVC"; sleep 8; say "restored; service=$(systemctl is-active $SVC)"
}

systemctl restart "$SVC"
for i in $(seq 1 30); do [ "$(systemctl is-active "$SVC")" = active ] && break; sleep 1; done
[ "$(systemctl is-active "$SVC")" = active ] || { say "service down"; journalctl -u "$SVC" -n 30 --no-pager; restore; exit 1; }

say "waiting for :9090 ..."
READY=""; for i in $(seq 1 60); do curl -s -o /dev/null --max-time 5 "http://127.0.0.1:9090/openapi.json" 2>/dev/null && { READY=1; break; }; sleep 2; done
[ -n "$READY" ] || { say "port never opened"; restore; exit 1; }
say "  listening after ~$(( i * 2 ))s"

# the pre-existing pages must still work — this deploy touches nav_common, which every page renders
for p in "/dash" "/ops/$GW/data" "/reports"; do
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 120 "http://127.0.0.1:9090$p")
  [ "$C" = 200 ] || { say "VERIFY FAILED: $p -> $C"; restore; exit 1; }
  say "  $p: 200"
done
curl -s --max-time 20 "http://127.0.0.1:9090/reports/jobs?limit=1" | head -c 120 | sed 's/^/    jobs: /'; echo

say "RESULT: PASS — /reports live, $SVC active. Backups *.bak.$STAMP"
say "  export runs niced, one at a time, in its own process; workbooks in /var/lib/liftlab/reports (0750)"
