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
STALL_STRIKES_MAX="${RELAY_STALL_STRIKES:-2}"  # alive-but-not-delivering for this many intervals => restart.
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
  say "rtsp read timeout: DISABLED by RELAY_RW_TIMEOUT_US=0"
fi
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); [ -n "$IFACE" ] || IFACE=eth0

# ---------- channels from channel_map, cap at 7 ----------
if [ -n "${CHANNELS:-}" ]; then CHANS=($CHANNELS); CSRC="env";
else
  J=$(curl -s --max-time 8 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null)
  CHANS=($(printf '%s' "$J" | grep -oE '"channels":\[[0-9,]*\]' | grep -oE '[0-9]+'))
  if [ "${#CHANS[@]}" -gt 0 ]; then CSRC="channel_map"; else CHANS=(27 28 29 30 32 33 34); CSRC="builtin"; fi
fi
CHANS=("${CHANS[@]:0:7}"); NCH=${#CHANS[@]}
CAMS=(); for ch in "${CHANS[@]}"; do CAMS+=("ch${ch}"); done
say "channels ($CSRC): ${CHANS[*]}  iface=$IFACE  interval=${INTERVAL}s  csv=$CSV"

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
last=c.get('last') or 0
t=d.get('t') or 0
print(round(t-last,1) if (last>0 and t>0) else -1)" 2>/dev/null || echo -1; }

launch(){ # $1=slot -> (re)start the direct-PUT ffmpeg for that cam, echo pid
  local i=$1 ch=${CHANS[$i]} cam=${CAMS[$i]}
  local url="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam"
  local base="$CLOUD/api/gw/$GW/live/$cam"
  ffmpeg -nostdin -hide_banner -loglevel error \
    -rtsp_transport tcp $RWTO_ARG -i "$url" -an -c:v copy \
    -f hls -hls_time "$HLS_TIME" -hls_list_size 5 -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
    -method PUT -http_persistent 1 $MREQ_ARG \
    -headers "Authorization: Bearer ${GATEWAY_TOKEN}"$'\r\n' \
    -hls_segment_filename "$base/seg%03d.ts" "$base/index.m3u8" \
    >"/tmp/relay_soak_${cam}.log" 2>&1 &
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
declare -a PIDS; declare -A PREVJ
start_streams(){
  local i alive_now=0
  for ((i=0;i<NCH;i++)); do PIDS[$i]=$(launch "$i"); PREVJ[${PIDS[$i]}]=$(pid_jiffies "${PIDS[$i]}"); done
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
    if kill -0 "$local_pid" 2>/dev/null; then
      alive=$((alive+1)); j1=$(pid_jiffies "$local_pid"); pj=${PREVJ[$local_pid]:-$j1}
      cpu=$(awk -v s="$cpu" -v a="$pj" -v b="$j1" -v hz="$hz" -v dt="$dt" 'BEGIN{printf "%.1f",s+(b-a)/hz/dt*100}')
      PREVJ[$local_pid]=$j1
      # STALL DETECT: alive but no segments landing at the VM => ffmpeg wedged (RTSP read hang or jammed
      # PUT). kill -0 passed, so the DIED path won't fire — this is the 18h/22h-outage class.
      # PRIMARY signal = newest-segment AGE from the VM (server clock, bitrate-independent, and unknown
      # when telemetry is missing). The kbps delta is kept only as a SECONDARY confirmation for the log:
      # it cannot be the trigger, because a failed stats call reads 0 for every cam at once.
      if awk "BEGIN{exit !($age >= 0 && $age > $SEG_STALL_S)}"; then
        STALL[$i]=$(( ${STALL[$i]:-0} + 1 ))
        say "stream ${cam} STALLED — newest segment ${age}s old (> ${SEG_STALL_S}s), delivered ${dk}kbps (strike ${STALL[$i]}/${STALL_STRIKES_MAX})"
        if [ "${STALL[$i]}" -ge "$STALL_STRIKES_MAX" ]; then
          say "stream ${cam} stall sustained — killing+restarting ffmpeg (pid $local_pid). tail: $(tail -1 /tmp/relay_soak_${cam}.log 2>/dev/null)"
          kill "$local_pid" 2>/dev/null; sleep 0.5; kill -9 "$local_pid" 2>/dev/null
          np=$(launch "$i"); PIDS[$i]=$np; PREVJ[$np]=$(pid_jiffies "$np"); STALL[$i]=0
          STALL_RESTARTS=$((STALL_RESTARTS+1))
        fi
      elif awk "BEGIN{exit !($age < 0)}"; then
        # Telemetry unknown (stats call failed / cam not seen since a cloud restart). Do NOT restart —
        # say so, so an outage of the TELEMETRY never masquerades as seven healthy streams either.
        STALL[$i]=0
        [ "$((SEQ % 10))" = 0 ] && say "stream ${cam} age UNKNOWN (live_stats unavailable) — no action taken"
      else
        STALL[$i]=0
      fi
    else
      say "stream ${CAMS[$i]} DIED — restarting (wifi/NVR dropout). tail: $(tail -1 /tmp/relay_soak_${CAMS[$i]}.log 2>/dev/null)"
      np=$(launch "$i"); PIDS[$i]=$np; PREVJ[$np]=$(pid_jiffies "$np"); STALL[$i]=0
    fi
  done
  smbps=$(awk -v k="$sumk" 'BEGIN{printf "%.2f",k/1000}')
  tp=$(temp_c); thr=$(throttle_live); ma=$(mem_avail); ps_json="${ps_json%,}}"
  # door_fps column is literal NA from the watch-retirement onward — the Pi no longer measures doors.
  echo "$(date -u +%FT%TZ),${upl},${smbps}${percols},${cpu},${tp},${thr},${ma},NA,${alive},${delivering}" >> "$CSV"
  # POST relay metrics to the cloud so /ops shows relay health without SSH (separate from the watch)
  payload="{\"sum_delivered_mbps\":${smbps},\"streams_alive\":${alive},\"streams_delivering\":${delivering},\"ff_cpu\":${cpu:-0},\"soc_temp\":${tp:-0},\"throttle_live\":\"${thr}\",\"mem_avail_mb\":${ma:-0},\"stall_restarts\":${STALL_RESTARTS:-0},\"per_stream\":${ps_json}}"
  curl -s -o /dev/null --max-time 5 -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" \
    -H "Content-Type: application/json" -d "$payload" "$CLOUD/api/gw/$GW/relay_status" 2>/dev/null || true
  prev_tx=$cur_tx; prev_sj=$cur_sj; prev_t=$now
done
