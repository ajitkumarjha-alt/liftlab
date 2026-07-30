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
V2_PIN=77e3a44fc61786634fdc64bcb456ac3b   # 2026-07-30 build (read_conf fix + era selector); the OLD pin
                                          # ca1cd6a7 was the artifact that 500'd — a stale staged copy
                                          # matching it must refuse to install, hence the re-pin
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

# BEFORE snapshot from the still-running OLD code, if one wasn't captured already. -f: a 500 body
# is not a snapshot — writing it anyway is what fed the diff step non-JSON and crashed it.
[ -s "$BEFORE" ] || curl -fs -o "$BEFORE" --max-time 15 http://127.0.0.1:9090/dash/site-A/data \
  || say "BEFORE snapshot unavailable (old code not answering /dash data — expected when this apply fixes a 500)"

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
if ! systemctl is-active --quiet "$SVC" || [ "$CODE" != "200" ]; then
  say "NEW PID $NEWPID NOT ANSWERING after 60 probes (active=$(systemctl is-active "$SVC"), HTTP $CODE) — RESTORING"
  cp -p "$APP/dash_api.py.bak.$TS" "$APP/dash_api.py"
  systemctl restart "$SVC"
  exit 1
fi
# PHASE 3 — the endpoint that actually fronts the DB and the derivation. openapi.json proved the
# module IMPORTS; the 2026-07-29 regression (alpha_rows missing read_conf) imported perfectly and
# 500'd on every request all night. A handler that 500s is a broken install: probe it LIVE, require
# 200 AND parseable JSON, restore on anything else. This same body becomes the AFTER snapshot.
say "openapi 200 — phase 3: live GET $DATA"
DCODE=000
for j in $(seq 1 30); do
  DCODE=$(curl -s -o "$AFTER" -w '%{http_code}' --max-time 20 "$DATA") || DCODE=000
  [ "$DCODE" = "200" ] && break
  sleep 2
done
if [ "$DCODE" = "200" ] && "$PY" -c "import json,sys; json.load(open(sys.argv[1]))" "$AFTER" 2>/dev/null; then
  say "service UP at $(date -u +%FT%TZ) UTC (MainPID $OLDPID -> $NEWPID, openapi 200 after $i probes, "
  say "dash data 200 + valid JSON after $j probes)"
else
  say "DASH DATA PROBE FAILED (HTTP $DCODE, or body not JSON) — module imports but the handler is "
  say "broken; exactly the failure smoke-import cannot see. RESTORING"
  cp -p "$APP/dash_api.py.bak.$TS" "$APP/dash_api.py"
  systemctl restart "$SVC"
  exit 1
fi

say "5/5 before/after admitted-set diff (ch16)"
# The AFTER side is guaranteed by phase 3 (200 + valid JSON), so a failure HERE is a real acceptance
# failure and now says WHY instead of dying on json.load with "Expecting value: line 1 column 1"
# — which is how this step spent a week failing "non-fatally" while being the only automatic check.
python3 - "$BEFORE" "$AFTER" <<'PYEOF' || { say "DIFF/ACCEPTANCE FAILED — service is UP but the AFTER payload is not what phase 3 just verified; investigate before trusting the derive"; }
import json, sys

def load(path, name):
    """The snapshot as parsed JSON, or None WITH THE REASON PRINTED — an unexplained diff failure
    is an acceptance check nobody believes."""
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        print("%s: unreadable (%s)" % (name, e)); return None
    if not raw.strip():
        print("%s: EMPTY — curl wrote nothing (endpoint was 500ing / timed out)" % name); return None
    try:
        return json.loads(raw)
    except ValueError:
        print("%s: NOT JSON, first 120 bytes: %r" % (name, raw[:120])); return None

def wl(d):
    t = (d.get("tier2") or {}).get("ch16") or {}
    return set(t.get("floor_whitelist") or []), t

after = load(sys.argv[2], "AFTER")
if after is None:
    sys.exit(1)                                    # phase 3 verified this — a failure here is real
a, t = wl(after)
key = lambda s: (len(s), s)
before = load(sys.argv[1], "BEFORE")
if before is None:
    # Degrade honestly: no before-picture (old code was 500ing, or first run) — show the after
    # state so the operator still gets the admitted set, and say what is missing.
    print("no usable BEFORE snapshot — showing AFTER only (n=%d): %s"
          % (len(a), " ".join(sorted(a, key=key)) or "(empty)"))
    sys.exit(0)
b, _ = wl(before)
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
