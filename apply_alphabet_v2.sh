#!/usr/bin/env bash
# Alphabet-v2 admission rules for the derived floor alphabet (dash_api.py _derive_floor_alphabet):
# anchored components (shadow bands are islands — every edge into the real graph is a teleport) +
# impossible-speed flip quarantine (glyph confusion caught in the act). Quarantine, not verdicts:
# detail carries twin/anchored; stronger evidence re-admits on the next derive.
# .bak -> smoke-import -> install -> restart (times printed) -> verify-or-restore -> before/after diff.
# FILES NEEDED: /tmp/dash_api.py (fetched) OR the pre-staged /home/ajit_kumarjha/dash_api.py.v2 (md5-pinned)
# CURL: B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts; T=$(date +%s); \
#       for f in apply_alphabet_v2.sh dash_api.py; do curl -fsSL -o /tmp/$f "$B/$f?$T"; done
#   sudo bash /tmp/apply_alphabet_v2.sh
set -uo pipefail
APP=/opt/liftlab-b3/cloud
PY=$APP/.venv/bin/python
SVC=liftlab-cloud
OWNER=liftlab
V2_PIN=465a8609cb565696799a52401459f249   # the reviewed artifact; guards a stale/partial staged copy
BEFORE=/home/ajit_kumarjha/alpha_before.json
AFTER=/home/ajit_kumarjha/alpha_after.json
say(){ echo "[alpha-v2] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
id "$OWNER" >/dev/null 2>&1 || OWNER=root
[ -f "$APP/dash_api.py" ] || { say "dash_api.py not at $APP"; exit 2; }

# Source of the patched module: prefer the fetched /tmp copy; fall back to the md5-pinned staged .v2.
if [ -f /tmp/dash_api.py ]; then
  V2=/tmp/dash_api.py
elif [ -f /home/ajit_kumarjha/dash_api.py.v2 ]; then
  V2=/home/ajit_kumarjha/dash_api.py.v2
  SUM=$(md5sum "$V2" | cut -d' ' -f1)
  [ "$SUM" = "$V2_PIN" ] || { say "staged .v2 md5 $SUM != pinned $V2_PIN — refusing"; exit 2; }
else
  say "no /tmp/dash_api.py and no staged .v2 — fetch first (see CURL header)"; exit 2
fi
say "installing from: $V2"

say "1/5 smoke-import in the cloud venv (pre-install)"
# importlib refuses a non-.py extension (spec comes back None), so check a .py-named copy from /tmp.
# cd + PYTHONPATH give the candidate its real directory context (nav_common et al) — the proven
# apply_dash.sh pattern — so the smoke exercises the actual deps, router and openapi, pre-install.
TMP=$(mktemp /tmp/dash_api_v2_check.XXXXXX.py)
cp "$V2" "$TMP"; chmod 644 "$TMP"
( cd "$APP" && sudo -u "$OWNER" env PYTHONPATH="$APP" "$PY" - "$TMP" ) <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dash_api_v2", sys.argv[1])
if spec is None or spec.loader is None:
    sys.exit("cannot load: " + sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert hasattr(m, "_derive_floor_alphabet") and hasattr(m, "_glyph_image")
assert m._glyph_image("79", "19") and m._glyph_image("129", "29") and not m._glyph_image("162", "16")
from fastapi import FastAPI
a = FastAPI(); a.include_router(m.dash_router); a.openapi()
print("smoke-import OK (module + router + openapi)")
PYEOF
RC=$?; rm -f "$TMP"
[ $RC = 0 ] || { say "smoke-import FAILED — nothing installed"; exit 1; }

# BEFORE snapshot from the still-running OLD code, if one wasn't captured already
[ -f "$BEFORE" ] || curl -s -o "$BEFORE" http://127.0.0.1:9090/dash/site-A/data || true

say "2/5 backup"
TS=$(date -u +%Y%m%d-%H%M%S)
cp -p "$APP/dash_api.py" "$APP/dash_api.py.bak.$TS"
say "backup: dash_api.py.bak.$TS"

say "3/5 install"
install -o "$OWNER" -g "$OWNER" -m 644 "$V2" "$APP/dash_api.py"

say "4/5 unit fast-stop + restart ($SVC — dash blips NOW)"
# SHUTDOWN DRAIN (journal-proven 2026-07-28): the relay's segment PUTs never pause, so uvicorn's
# graceful shutdown never finishes draining — a restart hangs in stop for minutes and every
# fixed-length probe window expires INSIDE the drain, restoring a module that never even started.
# Fix at the unit: cap uvicorn's drain at 5s (in-flight PUTs finish; the stream never will) with a
# 15s systemd backstop. These persist even if this apply restores — they fix every future apply.
UNIT=/etc/systemd/system/$SVC.service
if grep -q '^ExecStart=.*uvicorn' "$UNIT" 2>/dev/null \
   && ! grep -q -- '--timeout-graceful-shutdown' "$UNIT"; then
  if "$PY" -m uvicorn --help 2>/dev/null | grep -q -- '--timeout-graceful-shutdown'; then
    cp -p "$UNIT" "$UNIT.bak-alpha.$TS"
    sed -i 's|^\(ExecStart=.*uvicorn[^#]*\)$|\1 --timeout-graceful-shutdown 5|' "$UNIT"
    say "unit: ExecStart += --timeout-graceful-shutdown 5 (backup $UNIT.bak-alpha.$TS)"
  else
    say "unit: uvicorn too old for --timeout-graceful-shutdown — TimeoutStopSec backstop only"
  fi
fi
mkdir -p "/etc/systemd/system/$SVC.service.d"
printf '[Service]\nTimeoutStopSec=15\n' > "/etc/systemd/system/$SVC.service.d/fast-stop.conf"
systemctl daemon-reload
# Port from the RESOLVED ExecStart (house discipline), not a hard 9090.
PORT=$(systemctl show -p ExecStart --value "$SVC" | sed -n 's/.*--port \([0-9]\{2,5\}\).*/\1/p')
PORT=${PORT:-9090}
LIVE="http://127.0.0.1:$PORT/openapi.json"       # cheap liveness probe — no DB behind it
DATA="http://127.0.0.1:$PORT/dash/site-A/data"
OLDPID=$(systemctl show -p MainPID --value "$SVC")
say "restart at: $(date -u +%FT%TZ) UTC / $(TZ=Asia/Kolkata date +%FT%T) IST (old MainPID $OLDPID)"
systemctl restart "$SVC"
# Two-phase verdict so "old still draining" is never mistaken for "new failed to bind":
# phase 1 — a NEW MainPID must exist; phase 2 — the new process must answer HTTP 200.
NEWPID=$OLDPID
for i in $(seq 1 120); do
  NEWPID=$(systemctl show -p MainPID --value "$SVC")
  [ -n "$NEWPID" ] && [ "$NEWPID" != "$OLDPID" ] && [ "$NEWPID" != 0 ] && break
  sleep 1
done
if [ -z "$NEWPID" ] || [ "$NEWPID" = "$OLDPID" ] || [ "$NEWPID" = 0 ]; then
  say "NO NEW MainPID after 120s (still '$NEWPID' — old drain or failed start) — RESTORING"
  cp -p "$APP/dash_api.py.bak.$TS" "$APP/dash_api.py"
  systemctl restart "$SVC"
  exit 1
fi
say "new MainPID $NEWPID after ${i}s — probing $LIVE"
CODE=000
for i in $(seq 1 60); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$LIVE") || CODE=000
  [ "$CODE" = "200" ] && break
done
if systemctl is-active --quiet "$SVC" && [ "$CODE" = "200" ]; then
  curl -s -o "$AFTER" --max-time 15 "$DATA" || true
  say "service UP at $(date -u +%FT%TZ) UTC (MainPID $OLDPID -> $NEWPID, openapi 200 after $i probes)"
else
  say "NEW PID $NEWPID NOT ANSWERING after 60 probes (active=$(systemctl is-active "$SVC"), HTTP $CODE) — RESTORING"
  cp -p "$APP/dash_api.py.bak.$TS" "$APP/dash_api.py"
  systemctl restart "$SVC"
  exit 1
fi

say "5/5 before/after admitted-set diff (ch16)"
[ -s "$AFTER" ] || curl -s -o "$AFTER" --max-time 15 "$DATA" || true
python3 - "$BEFORE" "$AFTER" <<'PYEOF' || say "diff step failed (snapshot missing?) — service is UP regardless"
import json, sys
def wl(p):
    d = json.load(open(p))
    t = (d.get("tier2") or {}).get("ch16") or {}
    return set(t.get("floor_whitelist") or []), t
b, _ = wl(sys.argv[1])
a, t = wl(sys.argv[2])
key = lambda s: (len(s), s)
print("BEFORE n=%d  AFTER n=%d" % (len(b), len(a)))
print("REMOVED:", " ".join(sorted(b - a, key=key)) or "(none)")
print("ADDED:  ", " ".join(sorted(a - b, key=key)) or "(none)")
det = {d["floor"]: d for d in t.get("floor_alphabet_detail", []) if isinstance(d, dict) and "floor" in d}
if det:
    print("\nquarantine/reject reasons for removed floors:")
    for f in sorted(b - a, key=key):
        print("  %-4s %s" % (f, det.get(f, {}).get("via", "(not in detail)")))
PYEOF
say "APPLY COMPLETE — verify /dash in the browser; the era re-derive happens per request (no extra step)."
