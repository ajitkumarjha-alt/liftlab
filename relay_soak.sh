#!/usr/bin/env bash
# DIRECT-PUT relay supervisor (liftlab-relay service). The Pi is a DUMB STREAMER: keep 7 ffmpegs up,
# PUT their HLS segments to the VM, log a CSV row every 30s, restart anything that dies or stops
# delivering. That is the whole job. All analysis — counting, floor OCR, and now door cycles — happens
# on the GPU box against the same feed.
#
# THE DOOR GUARD IS GONE (Jul 21). It watched liftlab-watch's signal_fps and killed the relay to
# protect the door loop. It cost 22h + 25min of pipeline data in two days, both times BY DESIGN, and
# the thing it was protecting is now retired: the door-watch deliverable is banked (2.81s close, n=1053)
# and the GPU's DoorFloorEngine measures door cycles on the same stream. Two components measuring the
# same thing, one authorised to kill the other, is a conflict — deleted rather than tuned. Nothing in
# this file may reference watch_local, door_fps, or liftlab-watch again.
#
# NOTE FOR ANALYSIS: Pi-watch door data and GPU-engine door data are SEPARATE ERAS. Never pool them.
# The boundary is recorded in WATCH_ERA.md and stamped into the CSV at the retirement deploy.
#
# No decoupled uploader: that was built for a 7-concurrent-PUT "starvation" that turned out to be a
# threshold artifact (mixed camera bitrates + a fixed floor). It stays in git if correct-threshold
# delivery ever falls short.
#
# Creds come from the systemd EnvironmentFile (/etc/liftlab-agent.env) injected into the env.
set -uo pipefail

STREAM=2
INTERVAL="${RELAY_INTERVAL:-30}"
CSV="${RELAY_CSV:-/home/askjitk/liftlab-watch/relay_soak.csv}"
# Delivery health = is the stream ALIVE and are SEGMENTS STILL ARRIVING at the VM (bytes up this
# interval). HEVC sub bitrate is scene-dependent, so its VALUE — fixed, peak, OR rolling — cannot
# tell a quiet cabin from a fault: both are just fewer bytes (a rolling EMA still false-flags a
# cabin that empties from ~400->80 kbps). The only bitrate-independent fault signal is "are
# segments landing". A stall/death drops delivery to ~zero; a quiet cabin still trickles bytes.
# The raw per-stream kbps is in the CSV for the full picture.
ARRIVING_KBPS="${RELAY_ARRIVING_KBPS:-10}"     # below this over an interval = no segments = stalled/dead
STALL_STRIKES_MAX="${RELAY_STALL_STRIKES:-3}"  # alive-but-not-delivering for this many intervals => restart.
# ---------------------------------------------------------------------------
# GRACE: how long a stream may be alive with NO delivery before that counts as
# a stall rather than as "hasn't started yet".
#
# THE 2026-08-01 RESTART LOOP. The old default was 120s. Measured against 29
# fleet recovery events in relay_status: p50=160s, p90=350s, p95=376s, max=972s.
# The grace sat BELOW THE MEDIAN — more than half of all recoveries were killed
# before the stream could establish, restarted, and killed again. That is the
# ~170s restart cycle, exactly.
#
# So the floor is 300s and the effective grace is derived from MEASURED
# first-segment latency (see FIRSTSEG_EST), not from a guess. 300s alone would
# still cut into the p90; the adaptive term is what covers the tail.
GRACE_FLOOR_S="${RELAY_GRACE_FLOOR_S:-${RELAY_UNKNOWN_GRACE_S:-300}}"
GRACE_MULT="${RELAY_GRACE_MULT:-3}"            # grace = MULT x rolling first-segment latency
GRACE_CAP_S="${RELAY_GRACE_CAP_S:-1200}"       # never wait longer than this to call a stall
# ---------------------------------------------------------------------------
# FLEET DELIVERY WATCHDOG — the thing whose absence made a 33h outage invisible.
# Per-stream logic cannot see a fleet-wide wedge: every stream is individually
# "alive", so the relay reports healthy while delivering nothing. This is an
# independent check on the SUM.
FLEET_DOWN_S="${RELAY_FLEET_DOWN_S:-300}"      # total delivery ~0 this long = fleet down
FLEET_MIN_KBPS="${RELAY_FLEET_MIN_KBPS:-20}"   # fleet-wide sum below this counts as zero
FLEET_IFACE_BOUNCE="${RELAY_FLEET_IFACE_BOUNCE:-1}"   # 0 disables the link-bounce escalation
FLEET_NAG_S="${RELAY_FLEET_NAG_S:-60}"         # once down and un-recovered, shout this often
# How often to re-resolve the channel list from channel_map while running, so a
# registry change is picked up without a restart.
CHAN_REFRESH_S="${RELAY_CHAN_REFRESH_S:-300}"
# THE 18h-OUTAGE FIX. ffmpeg can wedge ALIVE with no output (RTSP read hangs, or the PUT socket jams):
# kill -0 still passes, so the DIED path (below) never fires and the stream stays dark for hours. But
# `delivering` (segments landing at the VM, bytes up this interval) already SEES it — it drops to ~0
# while the process is nominally alive. So: track per-stream stall strikes and kill+relaunch a stream
# that is alive yet not delivering. At INTERVAL=30 the default 2 strikes = restart ~60s into a stall.
# --- STALL-DETECTION HARDENING (NOT the cause of the Jul 20-21 gaps) ---
# CORRECTION: an earlier revision of this file blamed those outages on a frozen supervisor loop. That
# was wrong. GUARD_TRIP rows in relay_soak.csv show both were trips of the door guard that used to
# live here — the relay was deliberately stopped, not stuck. That guard is now deleted entirely.
# The two changes below are hardening for failures we have NOT yet had; they are kept because both
# holes are real, but neither explains a byte of the lost data.
#
# (1) SEGMENT AGE, not bytes-delta, is the stall signal. live_stats already returns per-cam `last`
#     (wall-clock of the last .ts that LANDED) plus the server's own `t`, so age = t - last is
#     computed on ONE clock — no Pi/VM skew. Age is also fail-safe in a way the delta is not: if the
#     stats call fails, the delta reads 0 bytes for EVERY cam and restarts all seven at once (a cloud
#     blip becomes a relay-wide restart storm). An unknown age restarts nothing.
SEG_STALL_S="${RELAY_SEG_STALL_S:-60}"         # newest segment older than this = that stream is dead
# (2) The supervisor loop had unbounded calls in it. The worst offender (a watch_local shell-out) left
#     with the door guard, but the class remains: anything shelling out of this loop can hang.
#     One hang parks the whole loop forever: no stall checks, no CSV rows, no relay_status
#     POSTs, ffmpeg children unwatched. systemd sees "active" the entire time. Every external call in
#     the loop is now bounded, AND the loop is watched by its own watchdog (see supervisor_watchdog).
CALL_TIMEOUT="${RELAY_CALL_TIMEOUT:-10}"       # cap on any helper shelling out of the loop
LOOP_STALL_S="${RELAY_LOOP_STALL_S:-120}"      # loop hasn't ticked this long => kill the relay, let systemd restart
HB_FILE="${RELAY_HB_FILE:-/tmp/relay_soak.hb}" # touched every loop turn; the watchdog reads its mtime
HLS_TIME="${RELAY_HLS_TIME:-2}"                # segment seconds
# Belt-and-suspenders for the RTSP-read stall specifically: abort a socket read that hangs longer than
# this (microseconds) so ffmpeg EXITS and the DIED path restarts it. Independent of the delivery check
# above (which also catches a wedged PUT). Set RELAY_RW_TIMEOUT_US=0 to disable entirely.
#
# WHICH option that is depends on the ffmpeg build, and getting it wrong kills every stream instantly:
# `-rw_timeout` is an AVIO/protocol option that the RTSP DEMUXER does not accept, so ffmpeg exits with
# "Error opening input files: Option not found" before a byte moves. That is exactly what took all 7
# streams down on Jul 21 15:01, when a redeploy overwrote a unit whose hand-added Environment= line had
# been quietly disabling it. Hardcoding any single spelling just moves the landmine, so PROBE the real
# binary and use whatever it actually accepts:
#   -stimeout    socket I/O timeout, µs — unambiguous, present through ffmpeg 5.x
#   -timeout     its successor for the rtsp demuxer (stimeout removed in 6.x), also µs
#   -rw_timeout  protocol-level; last resort, and the one that fails on rtsp
RW_TIMEOUT_US="${RELAY_RW_TIMEOUT_US:-30000000}"
MREQ_ARG=""; [ "${RELAY_MULTIPLE_REQUESTS:-}" = 1 ] && MREQ_ARG="-multiple_requests 1"
say(){ echo "[relay-soak] $(date -u +%FT%TZ) $*"; }

# creds: prefer the injected env; only source the file if we actually can (manual root run)
if [ -z "${NVR_HOST:-}" ] && [ -r /etc/liftlab-agent.env ]; then set -a; . /etc/liftlab-agent.env; set +a; fi
: "${NVR_HOST:?NVR_HOST not in env (systemd EnvironmentFile should inject it)}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN not in env}"
: "${CLOUD_URL:?CLOUD_URL not in env}"
CLOUD="${CLOUD_URL%/}"; GW="${GW:-${GATEWAY_ID:-site-A}}"
command -v ffmpeg >/dev/null || { say "ffmpeg missing"; exit 1; }

# ---------- THE ffmpeg command, in exactly one place ----------
# Both launch() and --selftest build the command from here, so the deploy gate cannot test a command
# that differs from the one that actually runs. That drift is what let a CLI parse error ship: the
# dry-run stub accepted any flags, so it verified control flow and proved nothing about the arguments.
# $RWTO_ARG / $MREQ_ARG are intentionally UNQUOTED — each must word-split into separate argv entries
# ("-timeout" "30000000"), and an empty one must vanish rather than become an empty argument.
ffmpeg_args(){   # $1=input url  $2=output base url  -> sets FFARGS[]
  FFARGS=(-nostdin -hide_banner -loglevel error
          -rtsp_transport tcp $RWTO_ARG -i "$1" -an -c:v copy
          -f hls -hls_time "$HLS_TIME" -hls_list_size 5
          -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts
          -method PUT -http_persistent 1 $MREQ_ARG
          -headers "Authorization: Bearer ${GATEWAY_TOKEN}"$'\r\n'
          -hls_segment_filename "$2/seg%03d.ts" "$2/index.m3u8")
}

# ---------- probe: does THIS ffmpeg accept the read-timeout option we want to pass? ----------
# Runs the real binary against a dead local port with the candidate option. A rejected option prints
# "Option not found" and exits before any connection is attempted; an accepted one gets as far as
# failing to connect. So the probe distinguishes a PARSE error from a NETWORK error, which is precisely
# the distinction that was missed when this broke (all 7 dying instantly on a CLI error, not a link).
ffmpeg_accepts(){   # $1 = candidate option words, e.g. "-stimeout 30000000"
  local out
  out=$(timeout 10 ffmpeg -nostdin -hide_banner -loglevel error \
        -rtsp_transport tcp $1 -i "rtsp://127.0.0.1:9/probe" -t 0 -f null - 2>&1)
  case "$out" in
    *"Option not found"*|*"Unrecognized option"*|*"Invalid argument"*) return 1 ;;
  esac
  return 0
}
RWTO_ARG=""
if [ "${RW_TIMEOUT_US}" != 0 ]; then
  # ORDER MATTERS, and it is not arbitrary. ffmpeg renamed the rtsp demuxer's socket-I/O timeout
  # `stimeout` -> `timeout` in 6.0 (the old `timeout`, which meant "wait for an incoming connection"
  # in listen mode, became `listen_timeout`). So `-stimeout` identifies a <=5.x build unambiguously,
  # `-timeout` is the >=6.x spelling, and `-rw_timeout` — the one that broke everything on
  # 7.1.5-0+deb13u1+rpt1 — is a protocol/AVIO option the rtsp demuxer never had. It is LAST, and only
  # reachable on some hypothetical build that accepts it. Probing beats remembering.
  for _cand in "-stimeout ${RW_TIMEOUT_US}" "-timeout ${RW_TIMEOUT_US}" "-rw_timeout ${RW_TIMEOUT_US}"; do
    if ffmpeg_accepts "$_cand"; then RWTO_ARG="$_cand"; break; fi
  done
  if [ -n "$RWTO_ARG" ]; then
    say "rtsp read timeout: '${RWTO_ARG%% *}' accepted by this ffmpeg (probed, ${RW_TIMEOUT_US}us)"
  else
    # Streaming without a read timeout is FAR better than not streaming at all. The segment-age stall
    # detector already restarts a wedged stream ~60-90s in; the ffmpeg-level timeout is only a faster path.
    say "WARN no rtsp read-timeout option accepted by this ffmpeg — streaming WITHOUT one."
    say "  A wedged RTSP read will now be caught by the segment-age stall detector instead (~60-90s)."
  fi
else
  # NAME THE KNOB. The previous text said "set 0 to disable" without saying 0 in WHAT, and that cost a
  # debugging round. Every message about this option now states the variable that controls it.
  say "rtsp read timeout: DISABLED because RELAY_RW_TIMEOUT_US=0 is set in the environment."
  say "  The anti-wedge is OFF. Since the option is now PROBED (not hardcoded), the reason this was"
  say "  set — '-rw_timeout' killing ffmpeg 7.1.5 — no longer applies: unset RELAY_RW_TIMEOUT_US to"
  say "  restore it and the probe will pick '-timeout'. Wedges remain covered by the segment-age check."
fi

# ---------- --selftest: parse-check the REAL command, launch nothing ----------
# The deploy gate (apply_relay.sh) calls this. It builds the exact FFARGS that launch() will use —
# including the HLS/PUT output options, not just the input side — and runs ffmpeg against dead local
# ports. Anything that fails to PARSE fails here, at deploy time, instead of taking all 7 streams down.
if [ "${1:-}" = "--selftest" ]; then
  ffmpeg_args "rtsp://127.0.0.1:9/selftest" "http://127.0.0.1:9/selftest"
  say "selftest: ffmpeg $(ffmpeg -hide_banner -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
  say "selftest: full command -> ffmpeg ${FFARGS[*]}"
  out=$(timeout 15 ffmpeg "${FFARGS[@]}" 2>&1)
  case "$out" in
    *"Option not found"*|*"Unrecognized option"*|*"Invalid argument"*|*"Error splitting the argument"*)
      say "SELFTEST FAILED — ffmpeg rejects this command line:"
      printf '%s\n' "$out" | head -10 | sed 's/^/    /'
      exit 1 ;;
  esac
  # Reaching a connection/protocol error means the CLI parsed — which is all this gate can prove
  # without a live NVR, and exactly the failure class that shipped.
  say "SELFTEST PASSED — command parses; ffmpeg got as far as the (deliberately dead) endpoints."
  exit 0
fi
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); [ -n "$IFACE" ] || IFACE=eth0

# ---------- channels: from channel_map or an explicit override. NEVER GUESSED ----------
# THERE IS NO BUILTIN LIST, deliberately. There used to be:
#     CHANS=(27 28 29 30 32 33 34); CSRC="builtin"
# It contained ch28 and ch33, which do not exist, and omitted ch16 and ch37, which do. It fired
# whenever the channel_map fetch returned nothing usable — silently, because a guessed list looks
# exactly like a real one downstream. On the 2026-08-01 reboot it fired and the Pi streamed the
# wrong seven cameras for 17 minutes; the fetch is intermittent, so it fired again on a later
# restart. A relay streaming the WRONG cameras is worse than one not streaming: the second failure
# is visible, the first produces plausible data attributed to the wrong lifts.
#
# So: fetch, and if we cannot, KEEP TRYING. Launch nothing until the list is known.
fetch_channels(){   # echoes space-separated channel numbers, empty on failure
  local j
  j=$(curl -s --max-time 8 -H "Authorization: Bearer $GATEWAY_TOKEN" \
        "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null) || return 1
  printf '%s' "$j" | grep -oE '"channels":\[[0-9,]*\]' | grep -oE '[0-9]+' | tr '\n' ' '
}

# Resolve into CHANS/CAMS/NCH/CSRC. Blocks until it succeeds. Backoff 5,10,20,40,60(cap).
resolve_channels(){
  local attempt=0 backoff=5 got=""
  if [ -n "${CHANNELS:-}" ]; then
    CHANS=($CHANNELS); CSRC="env override (CHANNELS=)"
  else
    while :; do
      attempt=$((attempt+1))
      got=$(fetch_channels)
      if [ -n "$got" ]; then CHANS=($got); CSRC="channel_map"; break; fi
      say "WARN channel_map fetch returned nothing usable (attempt ${attempt}) — retrying in ${backoff}s."
      say "  NOT falling back to a guessed list: streaming the wrong cameras is worse than streaming none."
      sleep "$backoff"
      backoff=$(( backoff * 2 )); [ "$backoff" -gt 60 ] && backoff=60
    done
  fi
  CHANS=("${CHANS[@]:0:7}"); NCH=${#CHANS[@]}
  CAMS=(); local ch; for ch in "${CHANS[@]}"; do CAMS+=("ch${ch}"); done
  say "channels resolved from ${CSRC}: ${CHANS[*]}  (n=${NCH})"
}
resolve_channels
LAST_CHAN_REFRESH=$(date +%s)
say "iface=$IFACE  interval=${INTERVAL}s  csv=$CSV"
say "grace: floor ${GRACE_FLOOR_S}s, ${GRACE_MULT}x measured first-segment latency, cap ${GRACE_CAP_S}s"
say "fleet watchdog: total delivery < ${FLEET_MIN_KBPS}kbps for ${FLEET_DOWN_S}s = FLEET DOWN"

say "delivery health = segments still arriving (>= ${ARRIVING_KBPS}kbps/interval); bitrate value can't distinguish a quiet cabin from a fault"

# ---------- helpers ----------
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }
# EVERY helper that shells out is wrapped in `timeout`. An unbounded one of these froze the loop for
# an unbounded shell-out — vcgencmd can block on a busy VideoCore mailbox.
temp_c(){ timeout "$CALL_TIMEOUT" vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/"; }
throttle_live(){ local v; v=$(timeout "$CALL_TIMEOUT" vcgencmd get_throttled 2>/dev/null|sed 's/.*=//'); v=$((v)); local o="";
  (( v & 1 ))&&o+="undervolt "; (( v & 2 ))&&o+="freqcap "; (( v & 4 ))&&o+="throttled "; (( v & 8 ))&&o+="templimit "; echo "${o:-none}"|tr ' ' '+'|sed 's/+$//'; }
mem_avail(){ awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo; }
pid_jiffies(){ awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null||echo 0; }
live_stats_json(){ curl -s --max-time 6 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/live_stats" 2>/dev/null; }
stat_bytes(){ printf '%s' "$1" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(0); sys.exit()
print(d.get('cams',{}).get('$2',{}).get('bytes',0))" 2>/dev/null || echo 0; }
# GROUND-TRUTH STALENESS: seconds since this cam's last .ts landed on the VM, measured entirely on the
# SERVER's clock (t - last), so Pi/VM skew cannot manufacture or mask a stall. Prints -1 for "unknown"
# (stats call failed, cam absent, or never delivered since the cloud restarted) and callers must treat
# -1 as DO NOTHING — restarting on missing telemetry is how a cloud blip becomes a 7-stream restart storm.
stat_age(){ printf '%s' "$1" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(-1); sys.exit()
c=d.get('cams',{}).get('$2') or {}
a=c.get('age_s')
if a is not None: print(round(float(a),1)); sys.exit()
last=c.get('last') or 0
t=d.get('t') or 0
print(round(t-last,1) if (last>0 and t>0) else -1)" 2>/dev/null || echo -1; }

launch(){ # $1=slot -> (re)start the direct-PUT ffmpeg for that cam, echo pid
  local i=$1 ch=${CHANS[$i]} cam=${CAMS[$i]}
  local url="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam"
  local base="$CLOUD/api/gw/$GW/live/$cam"
  ffmpeg_args "$url" "$base"
  ffmpeg "${FFARGS[@]}" >"/tmp/relay_soak_${cam}.log" 2>&1 &
  echo $!
}

# ---------- CSV header (write once; append if resuming) ----------
mkdir -p "$(dirname "$CSV")"
if [ ! -s "$CSV" ]; then
  hdr="ts,uplink_mbps,sum_delivered_mbps"; for cam in "${CAMS[@]}"; do hdr+=",d_${cam}_kbps"; done
  # door_fps column is RETAINED but always NA from the watch-retirement onward: dropping it would
  # break append-compatibility with existing relay_soak.csv files, and its emptiness is itself the
  # era marker. Door timing now comes from the GPU engine's gw_door_event stream, not from here.
  hdr+=",ff_cpu_total,soc_temp,throttle_live,mem_avail_mb,door_fps,streams_alive,streams_delivering"
  echo "$hdr" > "$CSV"
fi

# ---------- launch producers, set up teardown ----------
declare -a PIDS; declare -A PREVJ; declare -A LAUNCHED
start_streams(){
  local i alive_now=0
  for ((i=0;i<NCH;i++)); do PIDS[$i]=$(launch "$i"); PREVJ[${PIDS[$i]}]=$(pid_jiffies "${PIDS[$i]}"); LAUNCHED[$i]=$(date +%s); done
  say "launched $NCH direct-PUT sub relays: ${PIDS[*]}"
  # INSTANT-DEATH CHECK. A bad ffmpeg option kills every stream in milliseconds, and the loop below
  # would then relaunch them every INTERVAL forever, logging one truncated tail line per stream — which
  # is how a CLI parse error masqueraded as a running relay for an hour. Look, once, immediately, and
  # print the ACTUAL error instead of hiding it in a per-stream tail.
  sleep 3
  for ((i=0;i<NCH;i++)); do kill -0 "${PIDS[$i]}" 2>/dev/null && alive_now=$((alive_now+1)); done
  if [ "$alive_now" = 0 ]; then
    say "FATAL: all $NCH ffmpeg died within 3s of launch — this is a COMMAND error, not the network."
    say "  command was: ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp $RWTO_ARG -i <rtsp-url> -an -c:v copy"
    say "               -f hls -hls_time $HLS_TIME -hls_list_size 5 -hls_flags delete_segments+omit_endlist"
    say "               -hls_segment_type mpegts -method PUT -http_persistent 1 $MREQ_ARG -headers <auth>"
    say "               -hls_segment_filename $CLOUD/api/gw/$GW/live/${CAMS[0]}/seg%03d.ts .../index.m3u8"
    say "  ffmpeg said:"
    sed 's/^/    /' "/tmp/relay_soak_${CAMS[0]}.log" 2>/dev/null | head -10
    return 1
  fi
  [ "$alive_now" -lt "$NCH" ] && say "WARN only $alive_now/$NCH streams survived the first 3s — see /tmp/relay_soak_*.log"
  return 0
}
stop_streams(){
  local p
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
  sleep 1
  for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null; done
  PIDS=()
}
if ! start_streams; then
  # Exit non-zero so systemd restarts us (Restart=always) AND the journal carries the real reason.
  # Restarting will not fix a bad option, but a loud repeating FATAL is findable; seven streams
  # silently respawning into the same parse error is not.
  say "relay cannot stream with this command — exiting so the failure is visible, not looping quietly."
  for p in "${PIDS[@]:-}"; do kill -9 "$p" 2>/dev/null; done
  exit 1
fi
cleanup(){ say "stopping — killing $NCH ffmpeg"; for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
           [ -n "${WD_PID:-}" ] && kill "$WD_PID" 2>/dev/null; rm -f "$HB_FILE"; }
trap 'cleanup; exit 0' TERM INT
trap 'cleanup' EXIT

# ---------- supervisor self-watchdog (the bash analogue of gpu_watchdog's os._exit) ----------
# The relay's OWN liveness. Everything above watches the ffmpeg streams; nothing watched the watcher.
# NOTE: this did NOT cause the Jul 20-21 outages — those were door-guard trips (GUARD_TRIP rows in the
# CSV confirm it), not a freeze. It stays because the hole is real: a dumb streamer still needs dumb
# babysitting, and any shell-out from this loop can hang it. Hardening for a failure we have not had
# yet. The loop touches HB_FILE every
# turn; if that mtime stops advancing the supervisor is wedged and CANNOT recover itself — so
# this kills it and lets systemd restart. Same reasoning as os._exit over sys.exit in gpu_watchdog:
# a watchdog that cannot force the exit is theatre.
supervisor_watchdog(){
  local main=$1 hbm now age
  while :; do
    sleep 15
    hbm=$(stat -c %Y "$HB_FILE" 2>/dev/null || echo 0)
    now=$(date +%s); age=$(( now - hbm ))
    kill -0 "$main" 2>/dev/null || exit 0            # main gone; nothing to guard
    if [ "$hbm" -gt 0 ] && [ "$age" -gt "$LOOP_STALL_S" ]; then
      say "SUPERVISOR STALL: loop has not ticked in ${age}s (limit ${LOOP_STALL_S}s) — the supervisor is wedged."
      say "  last CSV row: $(tail -1 "$CSV" 2>/dev/null)"
      say "  ffmpeg children still up: $(pgrep -f "$CLOUD/api/gw/$GW/live/" 2>/dev/null | wc -l)/$NCH"
      say "  blocked in: $(cat /proc/$main/wchan 2>/dev/null || echo unknown); children of the loop:"
      ps --ppid "$main" -o pid,etime,stat,wchan:20,cmd --no-headers 2>/dev/null | sed 's/^/    /' || true
      say "  killing relay (SIGKILL) so systemd Restart=on-failure brings it back with fresh streams."
      pkill -f "$CLOUD/api/gw/$GW/live/" 2>/dev/null      # ffmpeg first: SIGKILL on main skips the EXIT trap
      kill -9 "$main" 2>/dev/null
      exit 1
    fi
  done
}
: > "$HB_FILE"
supervisor_watchdog $$ &
WD_PID=$!
say "supervisor watchdog armed: loop must tick every ${LOOP_STALL_S}s (hb=$HB_FILE, pid $WD_PID)"
sleep 6
prev_tx=$(tx_bytes); prev_sj=$(live_stats_json); prev_t=$(date +%s.%N)
declare -A PREVB; for ((i=0;i<NCH;i++)); do PREVB[$i]=$(stat_bytes "$prev_sj" "${CAMS[$i]}"); PREVJ[${PIDS[$i]}]=$(pid_jiffies "${PIDS[$i]}"); done
hz=$(getconf CLK_TCK); STALL_RESTARTS=0; SEQ=0
declare -A STALL; for ((i=0;i<NCH;i++)); do STALL[$i]=0; done   # per-stream alive-but-not-delivering strikes

# ---------- per-stream state machine ----------
# THREE states, because the old code had two and the missing one is what killed us:
#   STARTING   launched, nothing delivered yet, still inside grace   -> NEVER restartable
#   DELIVERING bytes advancing (ground truth)                        -> NEVER restartable
#   STALLED    alive, bytes NOT advancing, past grace                -> restartable via strikes
# The old code collapsed STARTING into STALLED ("no measurable segment age yet" = failure), so a
# stream was killed while it was still establishing. "Not yet" is not "dead".
declare -A STATE FIRSTSEG_S TOTBYTES
for ((i=0;i<NCH;i++)); do STATE[$i]="STARTING"; FIRSTSEG_S[$i]=""; TOTBYTES[$i]=0; done
# Rolling estimate of first-segment latency, measured per successful start. Seeded from the
# floor so the first stream out of the gate is not judged against an empty estimate.
FIRSTSEG_EST="${RELAY_FIRSTSEG_SEED_S:-60}"
FIRSTSEG_N=0

# Effective grace: generous multiple of what first segments ACTUALLY take here, floored and capped.
grace_now(){
  local g=$(( FIRSTSEG_EST * GRACE_MULT ))
  [ "$g" -lt "$GRACE_FLOOR_S" ] && g=$GRACE_FLOOR_S
  [ "$g" -gt "$GRACE_CAP_S" ] && g=$GRACE_CAP_S
  echo "$g"
}
# Observed a first segment for a stream: fold it into the rolling estimate (max-biased, because
# under-estimating grace is what caused the outage and over-estimating merely delays a restart).
note_first_segment(){   # $1 = seconds from launch to first delivery
  local obs=$1
  [ "$obs" -le 0 ] && return 0
  FIRSTSEG_N=$((FIRSTSEG_N+1))
  if [ "$obs" -gt "$FIRSTSEG_EST" ]; then
    FIRSTSEG_EST=$obs                                   # jump straight up
  else
    FIRSTSEG_EST=$(( (FIRSTSEG_EST * 3 + obs) / 4 ))    # decay down slowly
  fi
  say "first-segment latency ${obs}s (rolling estimate now ${FIRSTSEG_EST}s over ${FIRSTSEG_N} starts; grace $(grace_now)s)"
}
# Every state change gets a line. The next incident has to be diagnosable from the journal alone.
set_state(){   # $1=slot $2=new state $3=why
  local i=$1 new=$2 why=$3
  [ "${STATE[$i]}" = "$new" ] && return 0
  say "STATE ${CAMS[$i]}: ${STATE[$i]} -> ${new} (${why})"
  STATE[$i]=$new
}
restart_stream(){   # $1=slot $2=reason
  local i=$1 reason=$2 old=${PIDS[$i]} np
  say "RESTART ${CAMS[$i]} — ${reason} (pid ${old}). tail: $(tail -1 "/tmp/relay_soak_${CAMS[$i]}.log" 2>/dev/null)"
  kill "$old" 2>/dev/null; sleep 0.5; kill -9 "$old" 2>/dev/null
  np=$(launch "$i"); PIDS[$i]=$np; PREVJ[$np]=$(pid_jiffies "$np")
  STALL[$i]=0; LAUNCHED[$i]=$(date +%s); FIRSTSEG_S[$i]=""
  set_state "$i" "STARTING" "relaunched"
  STALL_RESTARTS=$((STALL_RESTARTS+1))
}

# ---------- fleet delivery watchdog state ----------
FLEET_ZERO_SINCE=0        # epoch when fleet delivery first hit ~zero, 0 = delivering
FLEET_DOWN=0              # 1 once the fleet-down condition has been declared
FLEET_STAGE=0             # escalation stage reached: 1=restart-all 2=iface bounce 3=nagging
FLEET_LAST_NAG=0
iface_bounce(){
  # The 33h outage was a soft bcmgenet TX wedge that a reboot cleared. A link bounce may clear it
  # without one — worth trying before giving up. Needs root; the service runs as askjitk, so this
  # goes through sudo -n and says so plainly when it cannot.
  if [ "$FLEET_IFACE_BOUNCE" != 1 ]; then
    say "FLEET: interface bounce disabled (RELAY_FLEET_IFACE_BOUNCE=0) — skipping."
    return 1
  fi
  say "FLEET: bouncing ${IFACE} (down/up) to clear a possible NIC TX wedge."
  if timeout "$CALL_TIMEOUT" sudo -n ip link set "$IFACE" down 2>/dev/null \
     && sleep 2 && timeout "$CALL_TIMEOUT" sudo -n ip link set "$IFACE" up 2>/dev/null; then
    say "FLEET: ${IFACE} bounced; waiting 10s for the link to come back."
    sleep 10
    return 0
  fi
  say "FLEET: ERROR could not bounce ${IFACE} — 'sudo -n ip link' failed (no passwordless sudo?)."
  say "  To enable this recovery add to sudoers:  askjitk ALL=(root) NOPASSWD: /usr/sbin/ip link set * up, /usr/sbin/ip link set * down"
  say "  Until then a NIC TX wedge needs a manual reboot, which is what cost 33 hours."
  return 1
}

# ---------- soak loop ----------
while :; do
  sleep "$INTERVAL"
  : > "$HB_FILE"                                  # LOOP TICK — the supervisor watchdog's only evidence
  SEQ=$((SEQ+1))
  now=$(date +%s.%N); dt=$(awk -v a="$prev_t" -v b="$now" 'BEGIN{d=b-a; print (d>0?d:1)}')
  cur_tx=$(tx_bytes); cur_sj=$(live_stats_json)
  upl=$(awk -v a="$prev_tx" -v b="$cur_tx" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  # per-stream delivered kbps + cpu + liveness
  percols=""; sumk=0; alive=0; delivering=0; cpu=0; ps_json="{"
  for ((i=0;i<NCH;i++)); do
    cam=${CAMS[$i]}
    b1=$(stat_bytes "$cur_sj" "$cam")
    age=$(stat_age "$cur_sj" "$cam")              # seconds since this cam's last .ts landed (-1 = unknown)
    dk=$(awk -v a="${PREVB[$i]}" -v b="$b1" -v dt="$dt" 'BEGIN{printf "%.0f",(b-a)*8/dt/1000}')
    PREVB[$i]=$b1; percols+=",${dk}"; sumk=$(awk -v s="$sumk" -v k="$dk" 'BEGIN{print s+k}'); ps_json+="\"$cam\":$dk,"
    # delivering = segments still arriving. A dead/stalled ffmpeg drops delivery to ~0; a quiet
    # cabin still trickles > ARRIVING_KBPS. No rate model — bitrate can't tell scene from fault.
    awk "BEGIN{exit !($dk>=$ARRIVING_KBPS)}" && delivering=$((delivering+1))
    local_pid=${PIDS[$i]}
    # LOCAL PROCESS DEATH is a different fact from REMOTE STALENESS and gets a different response.
    # The process is gone: nothing to wait for, no grace applies, restart now.
    if ! kill -0 "$local_pid" 2>/dev/null; then
      set_state "$i" "STARTING" "ffmpeg process exited"
      restart_stream "$i" "process DIED (local: ffmpeg exited — wifi/NVR dropout or a fatal option)"
      continue
    fi
    alive=$((alive+1)); j1=$(pid_jiffies "$local_pid"); pj=${PREVJ[$local_pid]:-$j1}
    cpu=$(awk -v s="$cpu" -v a="$pj" -v b="$j1" -v hz="$hz" -v dt="$dt" 'BEGIN{printf "%.1f",s+(b-a)/hz/dt*100}')
    PREVJ[$local_pid]=$j1

    # GROUND TRUTH: is this stream's cumulative delivered-bytes counter ADVANCING?
    # Bytes moving is a fact about this stream. Segment age is a lagging remote observation read
    # from a VM in Mumbai, and reporting lag alone can exceed any sane stall threshold on a
    # perfectly healthy stream. The old code trusted age over bytes and killed streams whose own
    # delivered-bytes counter read 1,273,954 kbps, logging them as "never delivered".
    advancing=0
    awk "BEGIN{exit !($b1 > ${TOTBYTES[$i]})}" && advancing=1
    TOTBYTES[$i]=$b1
    ALIVE_FOR=$(( $(date +%s) - ${LAUNCHED[$i]:-0} ))
    GRACE_S=$(grace_now)

    if [ "$advancing" = 1 ]; then
      # First delivery for this launch? That IS the first-segment latency — measure it, do not guess it.
      if [ -z "${FIRSTSEG_S[$i]}" ]; then
        FIRSTSEG_S[$i]=$ALIVE_FOR
        note_first_segment "$ALIVE_FOR"
      fi
      STALL[$i]=0
      set_state "$i" "DELIVERING" "bytes advancing (${dk}kbps, segment age ${age}s)"
      # NEVER restart a stream whose bytes are advancing, whatever its segment age says.
      if awk "BEGIN{exit !($age >= 0 && $age > $SEG_STALL_S)}"; then
        [ "$((SEQ % 10))" = 0 ] && say "stream ${cam} segment age ${age}s (> ${SEG_STALL_S}s) but bytes ARE advancing (${dk}kbps) — NOT restarting; age is a lagging remote reading"
      fi
      continue
    fi

    # Bytes are not advancing. Is this "not yet" or "dead"?
    if [ "${FIRSTSEG_S[$i]}" = "" ] && [ "$ALIVE_FOR" -le "$GRACE_S" ]; then
      STALL[$i]=0
      set_state "$i" "STARTING" "no delivery yet, ${ALIVE_FOR}s of ${GRACE_S}s grace used"
      [ "$((SEQ % 10))" = 0 ] && say "stream ${cam} STARTING — ${ALIVE_FOR}s/${GRACE_S}s grace, no segment yet, no action"
      continue
    fi

    # Past grace (or it HAD been delivering and stopped): alive but not delivering = stalled.
    STALL[$i]=$(( ${STALL[$i]:-0} + 1 ))
    if [ -n "${FIRSTSEG_S[$i]}" ]; then
      why="was delivering, bytes stopped advancing (segment age ${age}s)"
    else
      why="never delivered in ${ALIVE_FOR}s, past ${GRACE_S}s grace"
    fi
    set_state "$i" "STALLED" "$why"
    say "stream ${cam} STALLED — ${why} (strike ${STALL[$i]}/${STALL_STRIKES_MAX})"
    if [ "${STALL[$i]}" -ge "$STALL_STRIKES_MAX" ]; then
      restart_stream "$i" "stall sustained: ${why}"
    fi
  done
  # ---------- FLEET DELIVERY WATCHDOG ----------
  # Independent of every per-stream decision above. Per-stream logic cannot see this failure: on
  # 2026-08-01 all seven were individually "alive" and the relay reported healthy for 33 hours
  # while the NIC delivered nothing. The SUM is the only thing that catches it.
  NOWS=$(date +%s)
  if awk "BEGIN{exit !($sumk < $FLEET_MIN_KBPS)}"; then
    [ "$FLEET_ZERO_SINCE" = 0 ] && { FLEET_ZERO_SINCE=$NOWS
      say "WARN fleet delivery is ~zero (${sumk}kbps total, ${alive}/${NCH} alive) — watching for ${FLEET_DOWN_S}s"; }
    ZERO_FOR=$(( NOWS - FLEET_ZERO_SINCE ))
    if [ "$ZERO_FOR" -ge "$FLEET_DOWN_S" ]; then
      if [ "$FLEET_DOWN" = 0 ]; then
        FLEET_DOWN=1; FLEET_STAGE=0
        say "ERROR FLEET DOWN — total delivery below ${FLEET_MIN_KBPS}kbps for ${ZERO_FOR}s across ALL ${NCH} streams."
        say "  ${alive}/${NCH} ffmpeg alive, so this is NOT process death: the pipe is dead, not the processes."
        say "  This is the condition that went unnoticed for 33h on 2026-08-01."
      fi
      # Escalate in order, one stage per pass, so each is given a chance to work before the next.
      case "$FLEET_STAGE" in
        0) say "FLEET recovery stage 1/3: restarting ALL ${NCH} ffmpeg."
           for ((k=0;k<NCH;k++)); do restart_stream "$k" "fleet-down stage 1: restart all"; done
           FLEET_STAGE=1 ;;
        1) say "FLEET recovery stage 2/3: still nothing after a full restart — bouncing the interface."
           if iface_bounce; then
             say "FLEET: link bounced; restarting all streams and re-testing."
             for ((k=0;k<NCH;k++)); do restart_stream "$k" "fleet-down stage 2: post-bounce restart"; done
           fi
           FLEET_STAGE=2 ;;
        *) # Nothing left to try automatically. Be unmissable rather than quiet.
           if [ $(( NOWS - FLEET_LAST_NAG )) -ge "$FLEET_NAG_S" ]; then
             FLEET_LAST_NAG=$NOWS
             say "ERROR FLEET STILL DOWN after restart-all and interface bounce — ${ZERO_FOR}s with no delivery."
             say "  ${alive}/${NCH} ffmpeg alive and delivering nothing. Likely a NIC TX wedge needing a reboot."
             say "  uplink tx delta this interval: ${upl} Mbps; iface=${IFACE}"
           fi
           FLEET_STAGE=3 ;;
      esac
    fi
  else
    if [ "$FLEET_DOWN" = 1 ]; then
      say "FLEET RECOVERED — delivery back to ${sumk}kbps after $(( NOWS - FLEET_ZERO_SINCE ))s down (reached stage ${FLEET_STAGE})."
    fi
    FLEET_ZERO_SINCE=0; FLEET_DOWN=0; FLEET_STAGE=0
  fi

  # ---------- periodic channel re-resolve ----------
  # A registry change should not need a restart. Only acts when the list actually CHANGES, and
  # still never guesses: a failed fetch leaves the current list alone.
  if [ $(( NOWS - LAST_CHAN_REFRESH )) -ge "$CHAN_REFRESH_S" ] && [ -z "${CHANNELS:-}" ]; then
    LAST_CHAN_REFRESH=$NOWS
    NEWCH=$(fetch_channels)
    if [ -n "$NEWCH" ]; then
      NEWSET=$(printf '%s\n' $NEWCH | head -7 | sort -n | tr '\n' ' ')
      CURSET=$(printf '%s\n' "${CHANS[@]}" | sort -n | tr '\n' ' ')
      if [ "$NEWSET" != "$CURSET" ]; then
        say "channel_map CHANGED: was [${CURSET% }] now [${NEWSET% }] — restarting streams onto the new list."
        stop_streams
        resolve_channels
        declare -A STALL STATE FIRSTSEG_S TOTBYTES LAUNCHED
        start_streams || say "WARN could not start streams on the new channel list"
        for ((k=0;k<NCH;k++)); do STALL[$k]=0; STATE[$k]="STARTING"; FIRSTSEG_S[$k]=""; TOTBYTES[$k]=0; PREVB[$k]=0; done
      fi
    else
      say "WARN periodic channel_map refresh failed — keeping the current list (never guessing)."
    fi
  fi

  smbps=$(awk -v k="$sumk" 'BEGIN{printf "%.2f",k/1000}')
  tp=$(temp_c); thr=$(throttle_live); ma=$(mem_avail); ps_json="${ps_json%,}}"
  # door_fps column is literal NA from the watch-retirement onward — the Pi no longer measures doors.
  echo "$(date -u +%FT%TZ),${upl},${smbps}${percols},${cpu},${tp},${thr},${ma},NA,${alive},${delivering}" >> "$CSV"
  # POST relay metrics to the cloud so /ops shows relay health without SSH (separate from the watch)
  # THE HEARTBEAT MUST DISTINGUISH "alive" FROM "delivering". It could not, which is the whole
  # reason 33 hours passed unnoticed: streams_alive=7 read as healthy while sum_delivered_mbps was
  # 0.00. fleet_down is an EXPLICIT flag so the gateway does not have to infer it.
  FLEET_DOWN_FOR=0
  [ "$FLEET_ZERO_SINCE" != 0 ] && FLEET_DOWN_FOR=$(( NOWS - FLEET_ZERO_SINCE ))
  STATES_JSON="{"; for ((k=0;k<NCH;k++)); do STATES_JSON+="\"${CAMS[$k]}\":\"${STATE[$k]}\","; done
  STATES_JSON="${STATES_JSON%,}}"
  payload="{\"sum_delivered_mbps\":${smbps},\"streams_alive\":${alive},\"streams_delivering\":${delivering},\"ff_cpu\":${cpu:-0},\"soc_temp\":${tp:-0},\"throttle_live\":\"${thr}\",\"mem_avail_mb\":${ma:-0},\"stall_restarts\":${STALL_RESTARTS:-0},\"per_stream\":${ps_json},\"fleet_down\":$([ "$FLEET_DOWN" = 1 ] && echo true || echo false),\"fleet_zero_for_s\":${FLEET_DOWN_FOR},\"fleet_stage\":${FLEET_STAGE},\"first_segment_est_s\":${FIRSTSEG_EST},\"grace_s\":$(grace_now),\"channel_source\":\"${CSRC}\",\"stream_states\":${STATES_JSON}}"
  curl -s -o /dev/null --max-time 5 -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" \
    -H "Content-Type: application/json" -d "$payload" "$CLOUD/api/gw/$GW/relay_status" 2>/dev/null || true
  prev_tx=$cur_tx; prev_sj=$cur_sj; prev_t=$now
done
