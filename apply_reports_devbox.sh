#!/usr/bin/env bash
# Deploy /reports to dev-box, reading an hourly Litestream restore instead of the live database.
#
# WHY HERE. Measured on liftlab-cloud 2026-08-04: a 30-day export took /ops from a ~6s baseline to
# 26.3s and /dash past its 2s bar. Memory was never the constraint (254-394MB peak against 898MB
# free) — CPU was, on 2 vCPUs already carrying seven live streams. dev-box has 8GB and no streams.
#
# CREDENTIAL DECISION, stated because it is a security choice and not an obvious one:
# this box authenticates to GCS with the EXISTING USER ADC already present here. The gateway's
# /etc/liftlab/backup-key.json is deliberately NOT copied — that key can WRITE to the backup bucket,
# and a dev box must never be able to damage backups. The tradeoff is that the restore is tied to a
# personal account; that is why a failing refresh is a red banner on the page rather than a log line.
# Replacing it with a dedicated read-only service account is the right follow-up.
#
# AUTH DECISION: /reports* on dev.gargi.online goes behind Caddy basic_auth reusing the SAME operator
# credential as lift.gargi.online. code-server is not running here, so "existing code-server auth"
# does not exist; the host currently serves :8080 with NO auth at all, and resident movement data is
# not going behind nothing. The existing :8080 route is left exactly as it is.
#
# FILES NEEDED IN /tmp: apply_reports_devbox.sh reports_api.py report_runner.py reports_app.py
#   dash_api.py nav_common.py smoke_reports.sh refresh_reports_db.sh liftlab_report/ lift_banks.json
#   plus /tmp/liftlab_caddy_auth.line (the basicauth line, copied machine-to-machine)
#   sudo bash /tmp/apply_reports_devbox.sh
set -uo pipefail
APP=/opt/liftlab-reports
VAR=/var/lib/liftlab
RUN_USER="${RUN_USER:-ajit_kumarjha_lodhagroup_com}"
PORT="${REPORTS_PORT:-9091}"
BUCKET=liftlab-backup-lodha
HOSTN=dev.gargi.online
FILES="reports_api.py report_runner.py reports_app.py dash_api.py nav_common.py"
say(){ echo "[devbox] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
for f in $FILES smoke_reports.sh refresh_reports_db.sh; do
  [ -f "/tmp/$f" ] || { echo "missing /tmp/$f"; exit 2; }
done
[ -d /tmp/liftlab_report ] || { echo "missing /tmp/liftlab_report"; exit 2; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "no such user: $RUN_USER"; exit 2; }
HOME_DIR=$(getent passwd "$RUN_USER" | cut -d: -f6)

# ── prerequisites ────────────────────────────────────────────────────────────
if ! command -v litestream >/dev/null; then
  say "installing litestream v0.3.13 (same version as the gateway — a restore must be read by the"
  say "  same implementation that wrote the chain) ..."
  curl -sL -o /tmp/ls.tgz https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz \
    || { say "ABORT: could not download litestream"; exit 1; }
  tar xzf /tmp/ls.tgz -C /usr/local/bin litestream && rm -f /tmp/ls.tgz
fi
litestream version | sed 's/^/  litestream /'
command -v sqlite3 >/dev/null || { say "installing sqlite3 cli"; apt-get install -y -qq sqlite3 >/dev/null 2>&1; }

install -d -m 755 "$APP"
if [ ! -x "$APP/.venv/bin/python" ]; then
  say "creating venv ..."
  python3 -m venv "$APP/.venv" || { say "ABORT: venv creation failed"; exit 1; }
fi
say "installing python deps (fastapi, uvicorn, openpyxl — openpyxl is the report package's ONLY"
say "  third-party dependency; no numpy/pandas) ..."
"$APP/.venv/bin/pip" install --quiet --no-input fastapi uvicorn openpyxl \
  || { say "ABORT: pip install failed"; exit 1; }
"$APP/.venv/bin/python" -c "import fastapi,uvicorn,openpyxl;print('  deps ok:',openpyxl.__version__)"

# ── files ────────────────────────────────────────────────────────────────────
for f in $FILES; do install -m 644 "/tmp/$f" "$APP/$f"; done
rm -rf "$APP/liftlab_report"; cp -r /tmp/liftlab_report "$APP/liftlab_report"
rm -rf "$APP/liftlab_report/__pycache__"
install -m 644 /tmp/lift_banks.json "$APP/lift_banks.json"
install -m 644 /tmp/lift_banks.json "$APP/liftlab_report/lift_banks.json"
install -m 755 /tmp/refresh_reports_db.sh "$APP/refresh_reports_db.sh"
chown -R "$RUN_USER:$RUN_USER" "$APP"

# resident movement data lives under here; 0750 and owned by the service user, never world-readable
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$VAR"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$VAR/reports"
touch "$VAR/db.lock"; chown "$RUN_USER:$RUN_USER" "$VAR/db.lock"

install -d -m 755 /etc/liftlab
cat > /etc/liftlab/litestream-restore.yml <<YML
# RESTORE ONLY. This box never replicates TO the bucket — it has no write credential and must not
# have one. The path mirrors the gateway's replica so `litestream restore` finds the same chain.
dbs:
  - path: $VAR/gateway.db
    replicas:
      - type: gcs
        bucket: $BUCKET
        path: liftlab/gateway.db
YML
chmod 644 /etc/liftlab/litestream-restore.yml

# ── systemd: the app ─────────────────────────────────────────────────────────
cat > /etc/systemd/system/liftlab-reports.service <<UNIT
[Unit]
Description=liftlab /reports (dev-box, restored database)
After=network-online.target

[Service]
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP
Environment=HOME=$HOME_DIR
Environment=GATEWAY_DB=$VAR/gateway.db
Environment=REPORTS_DB=$VAR/reports.db
Environment=REPORTS_DIR=$VAR/reports
Environment=LIFTLAB_DB_LOCK=$VAR/db.lock
Environment=LIFTLAB_RESTORE_STATE=$VAR/restore_state.json
Environment=DASH_GW=site-A
Environment=REPORT_TIMEOUT_S=1800
Environment=REPORT_QUEUE_CAP=3
Environment=REPORT_RETENTION_DAYS=7
ExecStart=$APP/.venv/bin/python -m uvicorn reports_app:app --host 127.0.0.1 --port $PORT --log-level info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

# ── systemd: the hourly refresh ──────────────────────────────────────────────
cat > /etc/systemd/system/liftlab-refresh.service <<UNIT
[Unit]
Description=liftlab reports DB refresh (Litestream restore + continuous restore test)

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP
Environment=HOME=$HOME_DIR
Environment=LIFTLAB_DB=$VAR/gateway.db
Environment=LIFTLAB_DB_LOCK=$VAR/db.lock
Environment=LIFTLAB_RESTORE_STATE=$VAR/restore_state.json
Environment=LITESTREAM_CONFIG=/etc/liftlab/litestream-restore.yml
ExecStart=$APP/refresh_reports_db.sh
UNIT

cat > /etc/systemd/system/liftlab-refresh.timer <<UNIT
[Unit]
Description=hourly liftlab reports DB refresh

[Timer]
OnCalendar=hourly
# Not on the hour: the gateway is busiest then and the restore reads GCS, not the gateway, but a
# predictable stampede is still worth avoiding.
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload

# ── first restore, BEFORE starting the app ───────────────────────────────────
# If this fails the deploy stops: an app serving a page with no database is not a useful outcome,
# and the restore is the whole premise of putting it here.
say "first restore (this also proves the backup chain is readable from this box) ..."
if ! sudo -u "$RUN_USER" HOME="$HOME_DIR" \
     LIFTLAB_DB="$VAR/gateway.db" LIFTLAB_DB_LOCK="$VAR/db.lock" \
     LIFTLAB_RESTORE_STATE="$VAR/restore_state.json" \
     LITESTREAM_CONFIG=/etc/liftlab/litestream-restore.yml \
     "$APP/refresh_reports_db.sh"; then
  say "ABORT: the first restore failed. Nothing is serving. State file:"
  cat "$VAR/restore_state.json" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

# ── the gate, against the restored database ──────────────────────────────────
say "reports gate: full export round trip against the STAGED code and the restored db ..."
STAGE=$(mktemp -d); cp "$APP"/*.py "$STAGE/"; cp /tmp/reports_api.py /tmp/report_runner.py "$STAGE/"
if ! GATEWAY_DB="$VAR/gateway.db" bash /tmp/smoke_reports.sh "$STAGE" "$APP"; then
  say "ABORT: the export does not work here. Service NOT started."
  rm -rf "$STAGE"; exit 1
fi
rm -rf "$STAGE"
say "reports gate: PASSED"

systemctl enable --now liftlab-reports.service >/dev/null 2>&1
systemctl enable --now liftlab-refresh.timer >/dev/null 2>&1
for i in $(seq 1 40); do curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/healthz" && break; sleep 2; done

# ── Caddy: auth, and DO NOT disturb the existing :8080 route ─────────────────
CF=/etc/caddy/Caddyfile
cp "$CF" "$CF.bak.$(date +%Y%m%d-%H%M%S)"
if grep -q "reports_upstream_marker" "$CF"; then
  say "Caddyfile already has the reports block — leaving it"
else
  [ -s /tmp/liftlab_caddy_auth.line ] || { say "ABORT: /tmp/liftlab_caddy_auth.line missing (the operator credential)"; exit 1; }
  AUTHLINE=$(cat /tmp/liftlab_caddy_auth.line)
  python3 - "$CF" "$PORT" "$AUTHLINE" <<'PY'
import sys
cf, port, authline = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(cf).read()
block = """	# reports_upstream_marker — /reports carries RESIDENT MOVEMENT DATA and is the only
	# authenticated route on this host. The bare reverse_proxy below serves the dev app and is
	# deliberately untouched; do not fold these together.
	@reports path /reports /reports/*
	handle @reports {
		basic_auth {
			%s
		}
		reverse_proxy 127.0.0.1:%s
	}

""" % (authline, port)
i = s.index("{") + 1
open(cf, "w").write(s[:i] + "\n" + block + s[i:].lstrip("\n"))
print("  Caddyfile: reports block inserted ahead of the existing route")
PY
fi
caddy fmt --overwrite "$CF" >/dev/null 2>&1
if ! caddy validate --config "$CF" >/tmp/.caddyval 2>&1; then
  say "ABORT: Caddyfile invalid, restoring backup:"; tail -3 /tmp/.caddyval | sed 's/^/    /'
  cp "$CF.bak."* "$CF" 2>/dev/null; exit 1
fi
systemctl reload caddy || systemctl restart caddy
sleep 3

# ── verify ───────────────────────────────────────────────────────────────────
say "verification:"
echo "  app healthz      : $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 http://127.0.0.1:$PORT/healthz)"
echo "  app /reports     : $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 http://127.0.0.1:$PORT/reports)"
echo "  public NO auth   : $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 https://$HOSTN/reports)  (expect 401)"
echo "  existing :8080   : $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 https://$HOSTN/)  (unchanged by this deploy)"
echo "  refresh timer    : $(systemctl is-active liftlab-refresh.timer)  next: $(systemctl show liftlab-refresh.timer -p NextElapseUSecRealtime --value)"
echo "  reports service  : $(systemctl is-active liftlab-reports.service)"
sed -n '1,12p' "$VAR/restore_state.json" 2>/dev/null | sed 's/^/    /'
say "RESULT: /reports on https://$HOSTN/reports (basic_auth, same operator credential as the gateway)"
