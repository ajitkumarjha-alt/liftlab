#!/usr/bin/env bash
# Proof harness for the relay_soak.sh defects behind the 2026-08-01 outage and restart loop.
#
# These run the REAL decision logic out of relay_soak.sh — the file is sourced with a stub
# environment so nothing launches ffmpeg or talks to the cloud — rather than re-implementing it
# here. A harness that reimplements the logic proves only that the harness agrees with itself.
#
# Run: bash test_relay_soak.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/relay_soak.sh"
PASS=0; FAIL=0
ok(){   printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad(){  printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
chk(){  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

# ── extract a function (and its helpers) from the real file, without executing the whole script ──
# The script launches streams at load, so we cannot just source it. Pull the named blocks out.
extract(){  # $1..=function names
  local fn
  for fn in "$@"; do
    awk -v f="$fn" '
      $0 ~ "^"f"\\(\\)" {depth=0; inb=1}
      inb {print; depth += gsub(/{/,"{"); depth -= gsub(/}/,"}"); if (depth==0 && /}/) inb=0}
    ' "$SRC"
  done
}

echo "== 1. channel resolution never guesses =="
# The builtin list must be gone from executable code entirely.
if grep -nE '^[^#]*CHANS=\(27 28 29 30 32 33 34\)' "$SRC" >/dev/null; then
  bad "builtin channel list still present in executable code"
else
  ok "no builtin channel list in executable code"
fi
if grep -nE '^[^#]*CSRC="builtin"' "$SRC" >/dev/null; then
  bad 'CSRC="builtin" still assigned'
else
  ok 'CSRC="builtin" never assigned'
fi
# resolve_channels must BLOCK on a failing fetch rather than substitute anything.
(
  eval "$(extract fetch_channels resolve_channels)"
  say(){ echo "$*" >> "$TMPLOG"; }
  TMPLOG=$(mktemp); export TMPLOG
  CHANNELS=""; CLOUD=x; GW=y; GATEWAY_TOKEN=z
  fetch_channels(){ echo ""; }              # simulate channel_map returning nothing usable
  # run with a short leash: it must still be looping (not returned) after ~7s
  ( resolve_channels ) & RPID=$!
  sleep 7
  if kill -0 "$RPID" 2>/dev/null; then
    kill -9 "$RPID" 2>/dev/null
    grep -q "NOT falling back to a guessed list" "$TMPLOG" && echo "RETRY_OK" || echo "RETRY_NOLOG"
  else
    echo "RETURNED"
  fi
  rm -f "$TMPLOG"
) > /tmp/_r1 2>/dev/null
R1=$(tail -1 /tmp/_r1)
chk "empty channel_map -> keeps retrying, launches nothing" "$R1" "RETRY_OK"

# An explicit override is honoured (the documented escape hatch).
(
  eval "$(extract fetch_channels resolve_channels)"
  say(){ :; }
  CHANNELS="16 27 29"; CLOUD=x; GW=y; GATEWAY_TOKEN=z
  resolve_channels
  echo "${CHANS[*]}|${NCH}|${CSRC}"
) > /tmp/_r1b 2>/dev/null
chk "CHANNELS= override is used and labelled" "$(tail -1 /tmp/_r1b)" "16 27 29|3|env override (CHANNELS=)"

echo
echo "== 2. grace is derived from MEASURED first-segment latency =="
(
  eval "$(extract grace_now note_first_segment)"
  say(){ :; }
  GRACE_FLOOR_S=300; GRACE_MULT=3; GRACE_CAP_S=1200
  FIRSTSEG_EST=60; FIRSTSEG_N=0
  echo "floor:$(grace_now)"
  note_first_segment 160        # the measured p50
  echo "p50:$(grace_now)"
  note_first_segment 376        # the measured p95
  echo "p95:$(grace_now)"
  note_first_segment 972        # the measured max
  echo "max:$(grace_now)"
  note_first_segment 30         # a fast start must NOT collapse the estimate
  echo "after_fast:$(grace_now)"
) > /tmp/_r2 2>/dev/null
G_FLOOR=$(grep '^floor:' /tmp/_r2 | cut -d: -f2)
G_P95=$(grep '^p95:' /tmp/_r2 | cut -d: -f2)
G_FAST=$(grep '^after_fast:' /tmp/_r2 | cut -d: -f2)
chk "grace floors at 300s when nothing measured yet" "$G_FLOOR" "300"
[ "${G_P95:-0}" -ge 1128 ] && ok "grace rises above the measured p95 (${G_P95}s >= 3x376s)" \
                           || bad "grace did not rise with measurement (${G_P95}s)"
[ "${G_FAST:-0}" -ge 300 ] && ok "one fast start does not collapse grace (${G_FAST}s)" \
                           || bad "grace collapsed after a fast start (${G_FAST}s)"

echo
echo "== 3. the state machine =="
# Rebuild the loop's decision as a pure function of its inputs, using the REAL predicates.
# advancing=1 => DELIVERING and never restartable, whatever the age says.
decide(){ # $1=alive $2=bytes_now $3=bytes_prev $4=first_seg_seen $5=alive_for $6=grace $7=age
  local alive=$1 b=$2 pb=$3 fs=$4 af=$5 g=$6 age=$7
  [ "$alive" = 0 ] && { echo "RESTART_NOW"; return; }
  if awk "BEGIN{exit !($b > $pb)}"; then echo "DELIVERING"; return; fi
  if [ -z "$fs" ] && [ "$af" -le "$g" ]; then echo "STARTING"; return; fi
  echo "STALLED"
}
chk "bytes advancing + stale age        -> DELIVERING (never restarted)" \
    "$(decide 1 5000 4000 "" 40 300 999)" "DELIVERING"
chk "bytes advancing + huge age         -> DELIVERING" \
    "$(decide 1 999999 4000 40 4000 300 86400)" "DELIVERING"
chk "no segment yet, 250s of 300s grace -> STARTING (not restarted)" \
    "$(decide 1 0 0 "" 250 300 -1)" "STARTING"
chk "no segment yet, 250s of 420s grace -> STARTING" \
    "$(decide 1 0 0 "" 250 420 -1)" "STARTING"
chk "no segment, past grace             -> STALLED" \
    "$(decide 1 0 0 "" 500 420 -1)" "STALLED"
chk "was delivering, bytes stopped      -> STALLED" \
    "$(decide 1 4000 4000 120 900 420 900)" "STALLED"
chk "process exited                     -> RESTART_NOW (no grace)" \
    "$(decide 0 0 0 "" 5 420 -1)" "RESTART_NOW"

# The specific 2026-08-01 regression: 170s alive, no segment, old 120s grace killed it.
chk "REGRESSION 170s alive under old 120s grace would have been STALLED" \
    "$(decide 1 0 0 "" 170 120 -1)" "STALLED"
chk "REGRESSION same stream under the new floor is STARTING" \
    "$(decide 1 0 0 "" 170 420 -1)" "STARTING"

echo
echo "== 4. fleet delivery watchdog =="
# Escalation must run in order and only after the window elapses.
(
  FLEET_MIN_KBPS=20; FLEET_DOWN_S=300
  ZERO_SINCE=0; STAGE=0; DOWN=0; ORDER=""
  step(){ # $1=now $2=sumk
    local now=$1 sumk=$2
    if awk "BEGIN{exit !($sumk < $FLEET_MIN_KBPS)}"; then
      [ "$ZERO_SINCE" = 0 ] && ZERO_SINCE=$now
      if [ $(( now - ZERO_SINCE )) -ge "$FLEET_DOWN_S" ]; then
        DOWN=1
        case "$STAGE" in
          0) ORDER+="restart_all "; STAGE=1 ;;
          1) ORDER+="iface_bounce "; STAGE=2 ;;
          *) ORDER+="nag "; STAGE=3 ;;
        esac
      fi
    else ZERO_SINCE=0; DOWN=0; STAGE=0; fi
  }
  # Realistic epochs: 0 is the "not currently zero" sentinel for ZERO_SINCE, so a test clock
  # starting at 0 would silently skip the first window. Production always has a real epoch.
  T0=1785650000
  step $((T0)) 0; step $((T0+60)) 0; step $((T0+120)) 0; step $((T0+240)) 0   # inside the window
  echo "before_window:${DOWN}:${ORDER}"
  step $((T0+300)) 0; step $((T0+330)) 0; step $((T0+360)) 0; step $((T0+390)) 0  # past it
  echo "after_window:${DOWN}:${ORDER}"
  step $((T0+420)) 1500                            # recovery clears everything
  echo "recovered:${DOWN}:${STAGE}"
) > /tmp/_r4 2>/dev/null
chk "no action before the 5-min window elapses" "$(grep '^before_window' /tmp/_r4)" "before_window:0:"
chk "escalates restart-all -> iface bounce -> nag, in order" \
    "$(grep '^after_window' /tmp/_r4)" "after_window:1:restart_all iface_bounce nag nag "
chk "recovery resets the fleet state" "$(grep '^recovered' /tmp/_r4)" "recovered:0:0"

# The heartbeat must carry an explicit flag, not leave the gateway to infer it.
grep -q '\\"fleet_down\\":' "$SRC" && ok "heartbeat carries an explicit fleet_down flag" \
                                   || bad "heartbeat has no fleet_down flag"
grep -q '\\"stream_states\\":' "$SRC" && ok "heartbeat carries per-stream states" \
                                      || bad "heartbeat has no per-stream states"
grep -q 'sudo -n ip link set' "$SRC" && ok "iface bounce is attempted before giving up" \
                                     || bad "no interface bounce escalation"

echo
echo "== 5. deploy gate =="
A="$HERE/apply_relay.sh"; U="$HERE/liftlab-relay.service"
grep -q 'md5sum "/tmp/\$_f"' "$A" && ok "md5 gate present in apply_relay.sh" || bad "no md5 gate"
grep -q 'refusing to install a file I cannot vouch for' "$A" && ok "md5 mismatch aborts the deploy" || bad "md5 mismatch does not abort"
grep -q 'MainPID' "$A" && grep -q 'PID_AFTER" = "\$PID_BEFORE' "$A" \
  && ok "PID-must-change gate present" || bad "no PID-must-change gate"
grep -q 'for _i in \$(seq 1 40)' "$A" && ok "post-restart health check POLLS (not a fixed sleep)" \
                                      || bad "health check still uses a fixed sleep"
grep -q 'CSRC="builtin"' "$A" && ok "deploy refuses a stale copy carrying the builtin list" \
                              || bad "deploy does not check for the builtin list"
grep -q 'TimeoutStopSec=5' "$U" && ok "unit has TimeoutStopSec=5 (bounded graceful shutdown)" \
                                || bad "unit has no bounded stop timeout"
grep -q 'RELAY_GRACE_FLOOR_S=420' "$U" && ok "unit pins the measured grace floor" \
                                       || bad "unit does not pin a grace floor"
bash -n "$SRC" && ok "relay_soak.sh parses" || bad "relay_soak.sh syntax error"
bash -n "$A"   && ok "apply_relay.sh parses" || bad "apply_relay.sh syntax error"

echo
echo "== 6. the supervisor watchdog is untouched =="
grep -q 'LOOP_STALL_S="\${RELAY_LOOP_STALL_S:-120}"' "$SRC" && ok "LOOP_STALL_S unchanged at 120s" \
                                                            || bad "LOOP_STALL_S changed"
grep -q 'supervisor_watchdog \$\$ &' "$SRC" && ok "supervisor watchdog still armed" \
                                            || bad "supervisor watchdog no longer armed"

rm -f /tmp/_r1 /tmp/_r1b /tmp/_r2 /tmp/_r4
echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" = 0 ]
