#!/usr/bin/env bash
# Serve the live segment ring (playlist + segments) directly from Caddy/tmpfs, bypassing the app.
# The uvicorn event-loop contention fix: warm-socket TTFB measured 266ms for a disk file because
# segment GETs share one process with relay PUTs and dash era-scans. Bearer-gated via env-injected
# token (never written into the Caddyfile); a wrong/missing token falls through to the app which
# 401s as before, so the ring is never public and the failure mode is the old slow path.
# Backup -> patch -> validate -> restart caddy (~1s blip, relay retries) -> byte-exact verify or restore.
# CURL: T=$(date +%s); curl -fsSL -o /tmp/apply_caddy_livering.sh \
#       "https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts/apply_caddy_livering.sh?$T"
#   sudo bash /tmp/apply_caddy_livering.sh
set -uo pipefail
CF=/etc/caddy/Caddyfile
RING=/dev/shm/liftlab-live
say(){ echo "[livering] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f "$CF" ] || { say "no $CF"; exit 2; }
if grep -q "path_regexp live" "$CF"; then
  say "@live already present in $CF -- nothing to do (edit by hand if changing)"; exit 0
fi

# Token: extracted from the liftlab-cloud unit env (ANALYSIS_TOKENS=site-A:<tok>) -- no manual
# handling, no cleartext transit. Written only into a root-read-only caddy unit drop-in.
TOK=$(systemctl show liftlab-cloud -p Environment --value | tr ' ' '\n' \
      | sed -n 's/^ANALYSIS_TOKENS=site-A:\(.*\)$/\1/p' | head -1)
[ -n "$TOK" ] || { say "could not extract the site-A analysis token from liftlab-cloud env"; exit 2; }
say "token extracted from liftlab-cloud unit env (${#TOK} chars, not printed)"

# Caddy must be able to READ the ring (files are written by the app user). A permissions miss would
# turn matched requests into 403s WITH NO app fallback -- refuse rather than deploy a trap.
NEWEST=$(ls -t "$RING"/site-A/*/*.ts 2>/dev/null | head -1)
[ -n "$NEWEST" ] || { say "no segments under $RING -- is the relay delivering?"; exit 2; }
if ! sudo -u caddy head -c1 "$NEWEST" >/dev/null 2>&1; then
  say "the caddy user CANNOT read $NEWEST -- fix ring permissions first (refusing to deploy a 403 trap)"
  ls -la "$(dirname "$NEWEST")" | head -5
  exit 1
fi
say "ring readable by caddy: $NEWEST"

TS=$(date -u +%Y%m%d-%H%M%S)
cp -p "$CF" "$CF.bak-livering.$TS"
say "backup: $CF.bak-livering.$TS"

mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironment=LIFTLAB_ANALYSIS_TOKEN=%s\n' "$TOK" \
  > /etc/systemd/system/caddy.service.d/liftlab-token.conf
chmod 600 /etc/systemd/system/caddy.service.d/liftlab-token.conf
say "caddy unit drop-in written (mode 600)"

python3 - "$CF" <<'PY'
import re, sys
p = sys.argv[1]
src = open(p).read()
block = '''    # LIVE SEGMENT RING - served from tmpfs by Caddy, bypassing the app (event-loop contention fix).
    # Bearer-gated; the token is env-injected via the caddy unit drop-in, never written here. A
    # wrong/missing token falls through to the app, which 401s as before - the ring is never public.
    @live {
        method GET
        path_regexp live ^/api/gw/([A-Za-z0-9._-]+)/live/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$
        header Authorization "Bearer {$LIFTLAB_ANALYSIS_TOKEN}"
    }
    handle @live {
        rewrite * /{re.live.1}/{re.live.2}/{re.live.3}
        root * /dev/shm/liftlab-live
        file_server
    }

    handle {
        reverse_proxy 127.0.0.1:9090
    }
'''
m = re.search(r"\n(\s*)reverse_proxy 127\.0\.0\.1:9090\n", src)
if not m:
    sys.exit("Caddyfile shape unexpected: no bare 'reverse_proxy 127.0.0.1:9090' line -- refusing to guess")
out = src[:m.start()] + "\n" + block + src[m.end():]
open(p, "w").write(out)
print("Caddyfile patched: @live block added, reverse_proxy wrapped in fall-through handle")
PY
if [ $? != 0 ]; then say "patch FAILED -- restoring"; cp -p "$CF.bak-livering.$TS" "$CF"; exit 1; fi

if ! caddy validate --config "$CF" >/dev/null 2>&1; then
  say "caddy validate FAILED -- output follows; restoring"
  caddy validate --config "$CF" 2>&1 | tail -5
  cp -p "$CF.bak-livering.$TS" "$CF"; exit 1
fi
say "caddy validate OK"

systemctl daemon-reload
say "restarting caddy at $(date -u +%FT%TZ) (env change needs restart, not reload -- ~1s blip on all routes; relay retries)"
systemctl restart caddy
sleep 2
if ! systemctl is-active --quiet caddy; then
  say "caddy DOWN -- restoring"; cp -p "$CF.bak-livering.$TS" "$CF"; systemctl restart caddy; exit 1
fi
systemctl show caddy -p Environment | grep -q LIFTLAB_ANALYSIS_TOKEN \
  || say "WARNING: caddy env lacks LIFTLAB_ANALYSIS_TOKEN -- matcher inert, all traffic falls through (slow path, not broken)"

# VERIFY through the front door: byte-exact with token; NOT 200 without; basicauth intact on /dash.
# The ring rotates every ~2s -- retry once with a fresh pick before declaring failure.
H1=000; M1=x; M2=y
for attempt in 1 2; do
  NEWEST=$(ls -t "$RING"/site-A/*/*.ts 2>/dev/null | head -1)
  REL=${NEWEST#"$RING"/}
  GWP=${REL%%/*}; REST=${REL#*/}; CAMP=${REST%%/*}; FILEP=${REST#*/}
  URL="https://lift.gargi.online/api/gw/$GWP/live/$CAMP/$FILEP"
  M2=$(md5sum "$NEWEST" | cut -d' ' -f1)
  H1=$(curl -s --resolve lift.gargi.online:443:127.0.0.1 -H "Authorization: Bearer $TOK" \
       -o /tmp/_ring_check.ts -w '%{http_code}' "$URL")
  M1=$(md5sum /tmp/_ring_check.ts 2>/dev/null | cut -d' ' -f1)
  [ "$H1" = 200 ] && [ "$M1" = "$M2" ] && break
  say "verify attempt $attempt: HTTP $H1 (ring rotation race?) -- retrying with a fresh segment"
done
H2=$(curl -s --resolve lift.gargi.online:443:127.0.0.1 -o /dev/null -w '%{http_code}' "$URL")
H3=$(curl -s --resolve lift.gargi.online:443:127.0.0.1 -o /dev/null -w '%{http_code}' "https://lift.gargi.online/dash")
rm -f /tmp/_ring_check.ts
say "with token:    HTTP $H1  served-md5=$M1 disk-md5=$M2 $( [ "$M1" = "$M2" ] && echo BYTE-EXACT || echo MISMATCH )"
say "without token: HTTP $H2 (want 401/403 via app fall-through, NOT 200)"
say "/dash no-auth: HTTP $H3 (want 401 -- basicauth intact)"
if [ "$H1" = 200 ] && [ "$M1" = "$M2" ] && [ "$H2" != 200 ] && [ "$H3" = 401 ]; then
  say "RESULT: PASS -- ring served by Caddy, Bearer-gated, app path shed."
  say "  watch /ops: connect/transfer should collapse; dash + relay PUT latency should improve too."
else
  say "RESULT: CHECK -- restoring Caddyfile"; cp -p "$CF.bak-livering.$TS" "$CF"; systemctl restart caddy; exit 1
fi
