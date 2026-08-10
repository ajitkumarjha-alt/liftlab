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
# sudo: passes through to the real command EXCEPT reboot. The old stub was `shift; exec "$@"`, and
# `sudo -n /sbin/reboot` is an ABSOLUTE path, so $BIN-first PATH could not shadow it — this harness
# would have executed a real /sbin/reboot the moment a scenario reached stage 3. It never did before
# because no scenario got that far; the outage scenarios below do.
cat > "$BIN/sudo" <<'STUB'
#!/bin/sh
case "$*" in
  *reboot*) echo "STUB-SUDO-REBOOT: $*"; exit 0;;
esac
shift 2>/dev/null; exec "$@"
STUB
printf '#!/bin/sh\nexit 0\n' > "$BIN/systemctl"
chmod +x "$BIN"/*
# Prove the stubs execute before drawing any conclusion from the run. pick_execdir already probed
# the filesystem, but the stubs are what actually matter, so check one of them directly.
"$BIN/modinfo" >/dev/null 2>&1
STUBRC=$?
# `if [ "$?" = 126 ] || [ "$?" = 127 ]` was wrong twice over: the second $? is the exit status of the
# FIRST test, and the $? inside the message is the status of the `[` before it. It could report any
# number. Capture once, then test the captured value.
if [ "$STUBRC" = 126 ] || [ "$STUBRC" = 127 ]; then
  echo "SMOKE ABORT: stubs in $BIN will not execute (rc=$STUBRC — 126=not executable, 127=not found)."
  echo "  fs=$(stat -f -c %T "$TMP" 2>/dev/null)  mount opts: $(findmnt -no OPTIONS --target "$TMP" 2>/dev/null)"
  exit 2
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
  RELAY_STATE_DIR="$TMP/state" LIVE_DIR="$TMP/live" RELAY_LOG_DIR="$TMP/fflog" \
  RELAY_FLEET_REBOOT=0 \
  HOME="$TMP" \
    timeout --signal=TERM 25 bash "$SRC" > "$out" 2>&1
  rc=$?
  echo "$label|$rc|$out"
}

# ── ESCALATION-GATE SCENARIOS ────────────────────────────────────────────────
# The 2026-08-07..10 incident: the ladder ran stage 1/3 every ~6 minutes for THREE DAYS through a
# total NVR-leg outage and never reached the gated reboot, because the outage clock restarted with
# every supervisor restart. These scenarios drive the REAL script against a pre-seeded escalation
# state file, which is how a 900-second gate is tested without waiting 900 seconds: the clock is a
# persisted timestamp, so "15 minutes ago" is a value, not a delay.
#
# No clock is faked and no function is extracted — same discipline as the startup scenarios above.
seed_state(){   # $1=stage $2=zero_since_ago $3=last_reboot $4=last_good_ago
  local now; now=$(date +%s)
  mkdir -p "$TMP/state"
  printf '%s %s %s %s %s\n' "$1" "$(( now - $2 ))" "$now" "$3" "$(( now - $4 ))" \
    > "$TMP/state/fleet_escalation"
}
run_gate(){   # $1=label — state must already be seeded; reboot ENABLED so the gate is observable
  local label=$1 out="$TMP/out.gate.$1"
  printf '#!/bin/sh\nexit 1\n' > "$BIN/modinfo"; chmod +x "$BIN/modinfo"   # built-in NIC, as on the Pi
  PATH="$BIN:$PATH" \
  CLOUD_URL="http://127.0.0.1:9" GATEWAY_TOKEN=stub GW=site-A GATEWAY_ID=site-A \
  NVR_HOST=127.0.0.1 NVR_USER=u NVR_PASS=p \
  CHANNELS="16 27" \
  RELAY_INTERVAL=2 RELAY_CSV="$TMP/relay.gate.csv" RELAY_HB_FILE="$TMP/hb.gate" \
  RELAY_STATE_DIR="$TMP/state" LIVE_DIR="$TMP/live" RELAY_LOG_DIR="$TMP/fflog" \
  RELAY_FLEET_REBOOT=1 RELAY_FLEET_DOWN_S=1 \
  HOME="$TMP" \
    timeout --signal=TERM 45 bash "$SRC" > "$out" 2>&1
  # 45s, not the 25s the startup scenarios use. A scenario seeded at stage 0 has to walk the whole
  # ladder — zero detected, fleet-down declared, stage 1's restarts, stage 2, then the gate — and
  # that is four loop turns after a ~10s startup and a 6s settle. At 25s it was cut off one turn
  # short and reported a ladder failure that was really a stopwatch failure.
  echo "$out"
}
last_good_field(){ awk '{print $5}' "$TMP/state/fleet_escalation" 2>/dev/null || echo ""; }

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
          # EVIDENCE, NOT A GUESS. The previous version answered "the workdir is noexec" for ANY
          # 'permission denied' anywhere in the output, and on the Pi it was wrong twice: the stubs
          # had executed fine and the real fault was a REDIRECT — /tmp/relay_soak_chNN.log already
          # existed owned by the production user, /tmp is sticky, and fs.protected_regular refused
          # the open. A canned diagnosis that names the wrong file burns the deploy and sends the
          # operator to the wrong place, which is worse than saying "I don't know".
          #
          # So: decide from what the run actually recorded. If the script logged the launched pids,
          # exec worked, full stop — whatever went wrong came after.
          if grep -q 'launched .* direct-PUT sub relays: [0-9]' "$out"; then
            echo "  EXEC IS FINE: the script logged launched pids —"
            grep -m1 'launched .* direct-PUT sub relays:' "$out" | sed 's/^/         /'
            echo "         so this is NOT a noexec workdir. The stubs ran; something after exec failed."
            local redir
            redir=$(grep -m3 -iE 'cannot create|no such file|permission denied|operation not permitted|Read-only file system' "$out")
            if [ -n "$redir" ]; then
              echo "  FAILED OPEN/REDIRECT (verbatim from the run):"
              printf '%s\n' "$redir" | sed 's/^/         /'
              echo "         Check ownership and sticky/protected_regular on that path. relay_soak.sh"
              echo "         writes per-channel logs to \$RELAY_LOG_DIR (default \${STATE_DIR:-/tmp});"
              echo "         this harness sets it to its own workdir so it never contends with prod."
            else
              echo "  No open/redirect error was recorded either — see the tail below; this one is likely real."
            fi
          elif grep -qi 'permission denied' "$out"; then
            echo "  CAUSE: permission denied AND no launched-pids line, so exec itself is suspect."
            echo "         fs=$(stat -f -c %T "$TMP" 2>/dev/null)  opts: $(findmnt -no OPTIONS --target "$TMP" 2>/dev/null)"
            echo "         If those show noexec, set SMOKE_WORKDIR to a path that permits execution."
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

# Timestamp reference for the "nothing landed in /tmp" assertion below. Created before any scenario
# runs so `find -newer` sees exactly this run's writes.
TMP_MARK="$TMP/.run_mark"; : > "$TMP_MARK"

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

# ── RELAY_LOG_DIR IS HONOURED: nothing of ours lands in /tmp ─────────────────
# The Pi failure was a hardcoded /tmp path colliding with production's files. Asserting the new
# variable "works" by reading the script is not evidence; asserting that /tmp gained no
# relay_soak_ch*.log during the run is. TMP_MARK was created immediately before the first scenario,
# so -newer finds anything this run wrote there regardless of what pre-existed.
STRAY=$(find /tmp -maxdepth 1 -name 'relay_soak_ch*.log' -newer "$TMP_MARK" 2>/dev/null | head -5)
if [ -z "$STRAY" ]; then
  echo "  PASS  no relay_soak_ch*.log written under /tmp (RELAY_LOG_DIR honoured)"
else
  echo "  FAIL  wrote per-channel logs into /tmp despite RELAY_LOG_DIR:"
  printf '%s\n' "$STRAY" | sed 's/^/          /'
  fails=$((fails+1))
fi
# ...and they went where they were told to go, so this is not passing by writing nothing at all.
if ls "$TMP/fflog"/relay_soak_ch*.log >/dev/null 2>&1; then
  echo "  PASS  per-channel logs written under RELAY_LOG_DIR ($TMP/fflog)"
else
  echo "  FAIL  no per-channel logs under RELAY_LOG_DIR — the redirect never happened at all"
  fails=$((fails+1))
fi
hasnt "script never printed a /tmp per-channel log path" "/tmp/relay_soak_ch" "$out"

echo
echo "== startup smoke: module IS loadable (a USB NIC would look like this) =="
IFS='|' read -r _l rc2 out2 <<< "$(run_scenario loadable 0)"
explain_exit "$rc2" "$out2"
check "survived to the timeout" "$rc2" "124"
hasnt "no unbound-variable crash" "unbound variable" "$out2"
has  "banner reports a live stage 2" "2) reload" "$out2"

echo
echo "== escalation gate: TOTAL OUTAGE, 1000s since last good delivery =="
# The Friday-to-Monday case. Nothing has delivered for >15 min and all streams are down, so the
# gate must OPEN. Seeded at stage 2 (driver reload already attempted/unavailable) so this pass
# exercises the gate itself.
rm -rf "$TMP/state"/* 2>/dev/null; seed_state 2 1000 0 1000
GOUT=$(run_gate outage)
has  "reached the reboot gate"                    "reboot gate\|stage 3/3" "$GOUT"
has  "stage 3 FIRED (gate opened)"                "stage 3/3: REBOOTING" "$GOUT"
has  "gate reports time since last GOOD DELIVERY" "since the last good delivery\|down [0-9]*s" "$GOUT"
hasnt "did not refuse for a still-delivering stream" "still delivering" "$GOUT"

echo
echo "== escalation gate: FLAP — brief delivery 60s ago (Fri 09:44-09:52 pattern) =="
# A link that recovers briefly between stalls must NEVER accumulate toward a reboot. The clock is
# last-GOOD-delivery, so a 60s-old recovery leaves 60s on a 900s gate.
rm -rf "$TMP/state"/* 2>/dev/null; seed_state 2 1000 0 60
FOUT=$(run_gate flap)
hasnt "stage 3 did NOT fire on a flapping link"   "stage 3/3: REBOOTING" "$FOUT"
has  "gate CLOSED and said why"                   "reboot gate CLOSED" "$FOUT"

echo
echo "== escalation gate: stage-1 restarts do NOT reset the outage clock =="
# The defect itself. Seeded at stage 0 with a 1000s-old outage: the ladder runs stage 1 (seven
# restarts) and must still reach the gate, because restarting ffmpeg is not delivery. Before the
# fix the clock restarted here and 900s was never reachable.
rm -rf "$TMP/state"/* 2>/dev/null; seed_state 0 1000 0 1000
SEEDED_LG=$(last_good_field)
SOUT=$(run_gate stage1)
has  "ran stage 1 (restart all)"                  "stage 1/3" "$SOUT"
has  "still escalated to the gate despite stage 1" "stage 3/3: REBOOTING\|reboot gate CLOSED" "$SOUT"
NOW_LG=$(last_good_field)
if [ -n "$SEEDED_LG" ] && [ "$NOW_LG" = "$SEEDED_LG" ]; then
  echo "  PASS  last-good-delivery unchanged by the restarts ($NOW_LG)"
else
  echo "  FAIL  last-good-delivery moved: seeded '$SEEDED_LG' -> '$NOW_LG' (a restart is not a delivery)"
  fails=$((fails+1))
fi

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
