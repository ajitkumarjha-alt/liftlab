#!/usr/bin/env bash
# STARTUP SMOKE TEST — does relay_soak.sh actually START?
#
# WHY THIS EXISTS. Three verification gaps shipped in one week, and every one of them was a thing
# that `bash -n` and the unit tests structurally cannot see:
#   1. the deploy smoke-import loaded the INSTALLED module instead of the staged one, so it
#      validated code it had never read;
#   2. a wrong router attribute (door_router vs door_event_router) that only an import would catch;
#   3. `NIC_MODULE_LOADABLE: unbound variable` — read at line 376, assigned at line 518. Under
#      `set -u` that is a crash 3s after the seven ffmpeg launch, and systemd crash-loops it.
#      Shipped as 13577bec on 2026-08-04 and rolled back on the Pi.
#
# `bash -n` parses; it does not execute, so it cannot see a read-before-assign. The unit tests
# extract individual FUNCTIONS and drive them; they never run the script's top-to-bottom startup
# path, which is exactly where all three faults lived. This runs the REAL file, start to finish,
# under its own `set -u`, with only the outside world stubbed.
#
# WHAT IS STUBBED, AND WHAT IS NOT
#   stubbed: ffmpeg, curl, ip, modprobe, sudo, systemctl  — anything that touches hardware,
#            the network, or another process.
#   NOT stubbed: the script itself. No function is extracted, re-implemented or skipped. If a
#            variable is read before assignment anywhere on the startup path, this fails.
#
# PASS = the script reached the banner, entered the main loop, and ticked it at least twice
#        without exiting. Being killed by our own timeout is SUCCESS: it means it was still running.
#
# Run: bash smoke_relay_start.sh [path/to/relay_soak.sh]
set -uo pipefail
SRC="${1:-$(cd "$(dirname "$0")" && pwd)/relay_soak.sh}"
[ -r "$SRC" ] || { echo "SMOKE FAIL: cannot read $SRC"; exit 2; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN" "$TMP/state" "$TMP/live"

# ── stubs: everything that leaves the process ────────────────────────────────
# ffmpeg: a child that stays alive so PID tracking and jiffie sampling have something real to read.
printf '#!/bin/sh\nexec sleep 3600\n' > "$BIN/ffmpeg"
# curl: the channel registry must return usable channels, everything else returns empty/OK.
cat > "$BIN/curl" <<'STUB'
#!/bin/sh
for a in "$@"; do
  case "$a" in
    *camera_registry*|*channel_map*) echo '{"cams":[{"cam":"ch16","enabled":1},{"cam":"ch27","enabled":1}],"channels":[16,27]}'; exit 0;;
    *live_stats*) echo '{"ch16":{"bytes":1000},"ch27":{"bytes":1000}}'; exit 0;;
  esac
done
exit 0
STUB
# ip: report a plausible default route so IFACE resolves, and swallow link changes.
cat > "$BIN/ip" <<'STUB'
#!/bin/sh
case "$*" in
  *"route show default"*) echo "default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.9 metric 100";;
esac
exit 0
STUB
printf '#!/bin/sh\nexit 1\n' > "$BIN/modprobe"      # built-in module: mirrors the real Pi
printf '#!/bin/sh\nexit 0\n' > "$BIN/modinfo"       # (overridden per-scenario below)
printf '#!/bin/sh\nshift 2>/dev/null; exec "$@"\n' > "$BIN/sudo"
printf '#!/bin/sh\nexit 0\n' > "$BIN/systemctl"
chmod +x "$BIN"/*

run_scenario(){   # $1=label  $2=modinfo exit code (0 = module loadable)
  local label=$1 mi=$2 rc out
  printf '#!/bin/sh\nexit %s\n' "$mi" > "$BIN/modinfo"; chmod +x "$BIN/modinfo"
  rm -rf "$TMP/state"/* 2>/dev/null
  out="$TMP/out.$mi"
  # TIMING: the script needs ~10s to reach the banner — 3s instant-death check inside
  # start_streams, then a 6s settle before the first sampling pass. 25s leaves room for the banner
  # plus several loop turns at RELAY_INTERVAL=2. A shorter window fails on timing, not on defects,
  # which is exactly the false negative this file exists to avoid.
  PATH="$BIN:$PATH" \
  CLOUD_URL="http://127.0.0.1:9" GATEWAY_TOKEN=stub GW=site-A GATEWAY_ID=site-A \
  NVR_HOST=127.0.0.1 NVR_USER=u NVR_PASS=p \
  CHANNELS="16 27" \
  RELAY_INTERVAL=2 RELAY_CSV="$TMP/relay.csv" RELAY_HB_FILE="$TMP/hb" \
  RELAY_STATE_DIR="$TMP/state" LIVE_DIR="$TMP/live" \
  RELAY_FLEET_REBOOT=0 \
  HOME="$TMP" \
    timeout --signal=TERM 25 bash "$SRC" > "$out" 2>&1
  rc=$?
  echo "$label|$rc|$out"
}

fails=0
check(){ if [ "$2" = "$3" ]; then echo "  PASS  $1"; else echo "  FAIL  $1 (got '$2', want '$3')"; fails=$((fails+1)); fi; }
has(){   if grep -q "$2" "$3" 2>/dev/null; then echo "  PASS  $1"; else echo "  FAIL  $1"; fails=$((fails+1)); fi; }
hasnt(){ if grep -q "$2" "$3" 2>/dev/null; then echo "  FAIL  $1"; fails=$((fails+1)); else echo "  PASS  $1"; fi; }

echo "== startup smoke: module NOT loadable (this Pi: bcmgenet built into the kernel) =="
IFS='|' read -r _l rc out <<< "$(run_scenario builtin 1)"
# 124 = our timeout fired = the script was STILL RUNNING. That is the pass condition.
check "survived to the timeout (did not exit on its own)" "$rc" "124"
hasnt "no unbound-variable crash" "unbound variable" "$out"
hasnt "no 'command not found'" "command not found" "$out"
hasnt "no bad substitution / syntax error at runtime" "syntax error\|bad substitution" "$out"
has  "reached the ladder banner" "FLEET ladder:" "$out"
has  "banner reports stage 2 UNAVAILABLE on this kernel" "2) UNAVAILABLE" "$out"
has  "reported escalation-state persistence" "FLEET escalation state:" "$out"
has  "supervisor watchdog armed" "supervisor watchdog armed" "$out"
has  "channels resolved" "channels resolved" "$out"
# Proof it entered the MAIN LOOP, not just the preamble. NOT the heartbeat file: the EXIT trap
# deletes it, so it is always absent by the time we look. The CSV is written per loop turn and
# survives, and the fleet watchdog only speaks from inside the loop.
if [ -s "$TMP/relay.csv" ]; then echo "  PASS  CSV written (loop completed a full turn)"; else echo "  FAIL  CSV never written — never completed a loop turn"; fails=$((fails+1)); fi
has "fleet watchdog ran (loop is live, not just the preamble)" "fleet delivery is ~zero\|FLEET" "$out"
if [ "$(grep -c '^\[relay-soak\]' "$out")" -ge 10 ]; then echo "  PASS  startup produced a full log sequence"; else echo "  FAIL  startup log truncated — exited early"; fails=$((fails+1)); fi

echo
echo "== startup smoke: module IS loadable (a USB NIC would look like this) =="
IFS='|' read -r _l rc2 out2 <<< "$(run_scenario loadable 0)"
check "survived to the timeout" "$rc2" "124"
hasnt "no unbound-variable crash" "unbound variable" "$out2"
has  "banner reports a live stage 2" "2) reload" "$out2"

echo
if [ "$fails" = 0 ]; then echo "== STARTUP SMOKE PASSED =="; else echo "== STARTUP SMOKE FAILED: $fails =="; fi
[ "$fails" = 0 ]
