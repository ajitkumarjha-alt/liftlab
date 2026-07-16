#!/usr/bin/env bash
# Litestream: CONTINUOUS replication of gateway.db -> GCS. Managed backups + point-in-time restore,
# ZERO app change, does NOT touch the gw_event ingest. Checks GCS auth FIRST and refuses to half-
# configure. RUN AS ROOT ON liftlab-cloud:
#   sudo BUCKET=your-bucket bash /tmp/apply_litestream.sh
#   sudo BUCKET=your-bucket SA_KEY=/etc/liftlab-litestream-sa.json bash /tmp/apply_litestream.sh  # if scopes lack GCS
# FILES NEEDED IN /tmp: apply_litestream.sh
set -uo pipefail
LSVER="${LSVER:-v0.3.13}"
say(){ echo "[litestream] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo BUCKET=... bash $0"; exit 2; }
: "${BUCKET:?set BUCKET=your-gcs-bucket}"
BKT="${BUCKET#gs://}"; BKT="${BKT%%/*}"
# find the REAL gateway.db from the cloud service env (fallback to the stated path)
DB="${DB:-$(systemctl show liftlab-cloud -p Environment 2>/dev/null | grep -oP 'GATEWAY_DB=\K\S+' | head -1)}"
[ -n "$DB" ] || DB=/var/lib/liftlab/gateway.db
[ -f "$DB" ] || { say "gateway.db not found at '$DB' — pass DB=/path/to/gateway.db"; exit 2; }
say "database: $DB   bucket: gs://$BKT   litestream $LSVER"

# ---- 1. SCOPE + AUTH CHECK (no gcloud needed; metadata server) ----
MD='http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default'
SA=$(curl -s -H "Metadata-Flavor: Google" "$MD/email" 2>/dev/null)
SCOPES=$(curl -s -H "Metadata-Flavor: Google" "$MD/scopes" 2>/dev/null)
say "VM service account: ${SA:-<none>}"
say "VM scopes:"; echo "${SCOPES:-<none>}" | sed 's/^/    /'
if [ -n "${SA_KEY:-}" ] && [ -f "$SA_KEY" ]; then
  say "using SA key $SA_KEY (no VM restart needed)."
elif echo "$SCOPES" | grep -qE 'auth/cloud-platform|devstorage\.(read_write|full_control)'; then
  say "scopes CAN write GCS. (Also confirm the SA has 'Storage Object Admin' IAM on gs://$BKT.)"
else
  say "STOP: this VM's scopes do NOT allow GCS writes, and no SA_KEY was given. Pick ONE, then re-run:"
  say "  (a) NO DOWNTIME  — create a service account with 'Storage Object Admin' on gs://$BKT, download"
  say "                     its JSON key onto this VM, re-run with SA_KEY=/path/key.json"
  say "  (b) CHANGE SCOPES — requires STOP/START (downtime for liftlab-cloud, the Pi's POSTs will fail"
  say "                     while it's down): gcloud compute instances stop <vm> --zone <zone> ;"
  say "                     gcloud compute instances set-service-account <vm> --zone <zone>"
  say "                     --scopes=cloud-platform ; gcloud compute instances start <vm> --zone <zone>"
  say "Nothing changed. Decide auth first."; exit 3
fi

# ---- 2. install litestream (official .deb) ----
if ! command -v litestream >/dev/null; then
  ARCH=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/litestream.deb \
    "https://github.com/benbjohnson/litestream/releases/download/$LSVER/litestream-$LSVER-linux-$ARCH.deb" \
    || { say "download failed"; exit 1; }
  dpkg -i /tmp/litestream.deb || apt-get -f install -y
fi
say "litestream: $(litestream version 2>/dev/null || echo installed)"

# ---- 3. WAL mode (litestream replicates the WAL; persistent setting, safe with the app running) ----
MODE=$(sqlite3 "$DB" "PRAGMA journal_mode=WAL;" 2>/dev/null)
say "journal_mode -> $MODE (WAL required for continuous replication)"

# ---- 4. config + optional SA-key env ----
cat > /etc/litestream.yml <<YML
dbs:
  - path: $DB
    replicas:
      - type: gcs
        bucket: $BKT
        path: liftlab/gateway.db
YML
if [ -n "${SA_KEY:-}" ]; then
  mkdir -p /etc/systemd/system/litestream.service.d
  printf '[Service]\nEnvironment=GOOGLE_APPLICATION_CREDENTIALS=%s\n' "$SA_KEY" \
    > /etc/systemd/system/litestream.service.d/creds.conf
fi

# ---- 5. start + verify it actually replicated ----
systemctl daemon-reload
systemctl enable --now litestream >/dev/null 2>&1 || systemctl restart litestream
sleep 6
AC=$(systemctl is-active litestream)
SNAP=$(litestream snapshots "$DB" 2>&1 | tail -n +2 | head -1)
say "litestream service = $AC"
if [ "$AC" = active ] && [ -n "$SNAP" ]; then
  say "RESULT: PASS — replicating $DB -> gs://$BKT/liftlab/gateway.db. First snapshot: $SNAP"
  say "PROVE THE RESTORE NEXT: sudo bash /tmp/restore_proof.sh"
else
  say "RESULT: CHECK — journalctl -u litestream -n 40  (auth/bucket/IAM?). Service=$AC snapshot='${SNAP:-none}'"
  exit 1
fi
