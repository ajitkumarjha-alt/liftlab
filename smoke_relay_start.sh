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

# ── the workdir MUST be on a filesystem that allows execution ────────────────
# First run of this gate on the Pi failed with 7 errors and exit 1, and the cause was not a defect
# in relay_soak.sh at all: /tmp is mounted NOEXEC there, so the stub `ffmpeg` could not run, all
# streams "died" within 3s, start_streams returned 1 and the script exited before the banner. A
# smoke test that fails for its own environment is worse than no smoke test — it burns the deploy
# and points at the wrong file. So probe for a directory we can actually execute from, and if there
# is none, say so plainly instead of blaming the script.
pick_execdir(){
  local d c
  for d in "${SMOKE_WORKDIR:-}" "$HOME" "${TMPDIR:-/tmp}" /var/tmp /dev/shm; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    c=$(mktemp -d "$d/.relaysmoke.XXXXXX" 2>/dev/null) || continue
    printf '#!/bin/sh\nexit 7\n' > "$c/probe" 2>/dev/null && chmod +x "$c/probe" 2>/dev/null || { rm -rf "$c"; continue; }
    "$c/probe" >/dev/null 2>&1
    [ "$?" = 7 ] && { echo "$c"; return 0; }
    rm -rf "$c"
  done
  return 1
}
TMP=$(pick_execdir) || { echo "SMOKE ABORT: no writable+executable directory found (tried \$HOME, \$TMPDIR, /var/tmp, /dev/shm)."; echo "  Every candidate is noexec or unwritable, so the stubs cannot run and this test cannot"; echo "  say anything about relay_soak.sh. Set SMOKE_WORKDIR to a path that permits execution."; exit 2; }
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/bin"; mkdir -p "$BIN" "$TMP/state" "$TMP/live"
echo "smoke workdir: $TMP  (fs=$(stat -f -c %T "$TMP" 2>/dev/null))  user=$(id -un)  cwd=$(pwd)"

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
# Prove the stubs execute before drawing any conclusion from the run. pick_execdir already probed
# the filesystem, but the stubs are what actually matter, so check one of them directly.
"$BIN/modinfo" >/dev/null 2>&1
if [ "$?" = 126 ] || [ "$?" = 127 ]; then
  echo "SMOKE ABORT: stubs in $BIN will not execute (rc=$?). This directory is noexec."; exit 2
fi

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

# ── WHY DID IT EXIT? ─────────────────────────────────────────────────────────
# The first Pi run reported only "exit 1, not 124" and left the operator to guess. The script
# prints an excellent FATAL block explaining itself; the harness was throwing it away. Never again:
# on any non-124 exit, name the exit path and show the script's own last words.
explain_exit(){   # $1=rc  $2=output file
  local rc=$1 out=$2
  [ "$rc" = 124 ] && return 0
  echo
  echo "  ---- WHY IT EXITED (rc=$rc) ----"
  case "$rc" in
    1)  if grep -q 'all .* ffmpeg died within 3s' "$out"; then
          echo "  EXIT PATH: start_streams() -> 'all ffmpeg died within 3s' -> exit 1 (relay_soak.sh ~line 331)."
          echo "  This is BEFORE the supervisor watchdog and the banner, which is why those checks failed."
          if grep -qi 'permission denied' "$out"; then
            echo "  CAUSE: the stub ffmpeg could not EXECUTE (Permission denied) — the workdir is noexec."
            echo "         This is a HARNESS fault, NOT a defect in relay_soak.sh. Set SMOKE_WORKDIR"
            echo "         to a path that permits execution and re-run."
          else
            echo "  CAUSE: see the ffmpeg output quoted below — this one is likely real."
          fi
        else
          echo "  EXIT PATH: exited 1 without the ffmpeg FATAL block — see the tail below."
        fi ;;
    2)  echo "  EXIT PATH: harness abort (workdir/stub problem), not a script defect." ;;
    126|127) echo "  EXIT PATH: command not executable / not found — harness environment, not the script." ;;
    *)  echo "  EXIT PATH: unexpected rc=$rc." ;;
  esac
  grep -q 'unbound variable' "$out" && echo "  >>> UNBOUND VARIABLE — this IS a real defect in the script. <<<"
  echo "  ---- last 18 lines of the script's own output ----"
  tail -18 "$out" | sed 's/^/    /'
  echo "  ---- end ----"
}

echo "== startup smoke: module NOT loadable (this Pi: bcmgenet built into the kernel) =="
IFS='|' read -r _l rc out <<< "$(run_scenario builtin 1)"
explain_exit "$rc" "$out"
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
# CSV must have a DATA ROW, not just the header. The header is written at line 285, BEFORE
# start_streams — so `-s` passed even when the script died at line 331 and never reached the loop.
CSVROWS=$(( $(wc -l < "$TMP/relay.csv" 2>/dev/null || echo 0) ))
if [ "$CSVROWS" -ge 2 ]; then echo "  PASS  CSV has a data row (a loop turn really completed)"; else echo "  FAIL  CSV has only the header ($CSVROWS lines) — the loop never ran"; fails=$((fails+1)); fi
# Must match a line only the LOOP can emit. "FLEET" alone also matches the startup line
# "fleet watchdog: total delivery < 20kbps for 300s = FLEET DOWN", which prints at line 244 —
# so the old pattern passed on a script that died 90 lines later.
has "fleet watchdog ran INSIDE the loop" "fleet delivery is ~zero" "$out"

echo
echo "== startup smoke: module IS loadable (a USB NIC would look like this) =="
IFS='|' read -r _l rc2 out2 <<< "$(run_scenario loadable 0)"
explain_exit "$rc2" "$out2"
check "survived to the timeout" "$rc2" "124"
hasnt "no unbound-variable crash" "unbound variable" "$out2"
has  "banner reports a live stage 2" "2) reload" "$out2"

echo
if [ "$fails" = 0 ]; then
  echo "== STARTUP SMOKE PASSED =="
else
  KEEP="${SMOKE_KEEP:-/tmp/relay_smoke_fail.$$}"
  mkdir -p "$KEEP" 2>/dev/null && cp "$TMP"/out.* "$TMP/relay.csv" "$KEEP"/ 2>/dev/null \
    && echo "== STARTUP SMOKE FAILED: $fails — full logs kept in $KEEP ==" \
    || echo "== STARTUP SMOKE FAILED: $fails =="
fi
[ "$fails" = 0 ]
