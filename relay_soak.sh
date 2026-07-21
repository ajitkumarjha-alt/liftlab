#!/usr/bin/env bash
# DIRECT-PUT relay supervisor (liftlab-relay service) — the FIELD-PROVEN path (9.5h overnight).
# Each cabin SUB stream: ffmpeg HLS with -method PUT straight to the VM. Logs a CSV row every 30s
# to a DURABLE path, restarts dropped streams, and KILLS ITSELF if the door loop (liftlab-watch
# signal_fps) sags — the watch is sacred, the relay is expendable. No decoupled uploader: that was
# built for a 7-concurrent-PUT "starvation" that turned out to be a threshold artifact (mixed
# camera bitrates + a fixed floor). It stays in git if correct-threshold delivery ever falls short.
#
# Creds come from the systemd EnvironmentFile (/etc/liftlab-agent.env) injected into the env.
set -uo pipefail

STREAM=2
INTERVAL="${RELAY_INTERVAL:-30}"
CSV="${RELAY_CSV:-/home/askjitk/liftlab-watch/relay_soak.csv}"
DOOR_FLOOR="${RELAY_DOOR_FLOOR:-8.0}"          # relay stops if door_fps sags below this. 8.0 sits
# between the ~9.4 the relay costs and the 6 fps QUALITY floor (the watch alarm). The old 9.5 was
# proximity-to-idle-baseline (9.9 alone), not a quality line: resolving a ~2.6s close needs ~24
# samples at 9.4 vs ~26 at 9.9 — no meaningful difference. Env-tunable via the systemd unit.
DOOR_STRIKES_MAX="${RELAY_DOOR_STRIKES:-3}"    # for this many consecutive samples (~90s)
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
# --- THE 22h-OUTAGE FIX (Jul 20 16:00 -> Jul 21 14:15, third occurrence) ---
# The bytes-delta check above did NOT fire, because it never got to run: the supervisor loop itself
# froze. Two independent defects, both fixed below.
#
# (1) SEGMENT AGE, not bytes-delta, is the stall signal. live_stats already returns per-cam `last`
#     (wall-clock of the last .ts that LANDED) plus the server's own `t`, so age = t - last is
#     computed on ONE clock — no Pi/VM skew. Age is also fail-safe in a way the delta is not: if the
#     stats call fails, the delta reads 0 bytes for EVERY cam and restarts all seven at once (a cloud
#     blip becomes a relay-wide restart storm). An unknown age restarts nothing.
SEG_STALL_S="${RELAY_SEG_STALL_S:-60}"         # newest segment older than this = that stream is dead
# (2) The supervisor loop had unbounded calls in it. door_fps() shells into watch_local.py with NO
#     timeout, and watch_local reads the /dev/shm segment ring — the same ring that has a known race.
#     One hang there parks the whole loop forever: no stall checks, no CSV rows, no relay_status
#     POSTs, ffmpeg children unwatched. systemd sees "active" the entire time. Every external call in
#     the loop is now bounded, AND the loop is watched by its own watchdog (see supervisor_watchdog).
CALL_TIMEOUT="${RELAY_CALL_TIMEOUT:-10}"       # cap on any helper shelling out of the loop
LOOP_STALL_S="${RELAY_LOOP_STALL_S:-120}"      # loop hasn't ticked this long => kill the relay, let systemd restart
HB_FILE="${RELAY_HB_FILE:-/tmp/relay_soak.hb}" # touched every loop turn; the watchdog reads its mtime
HLS_TIME="${RELAY_HLS_TIME:-2}"                # segment seconds
# Belt-and-suspenders for the RTSP-read stall specifically: abort a socket read that hangs longer than
# this (microseconds) so ffmpeg EXITS and the DIED path restarts it. Independent of the delivery check
# above (which also catches a wedged PUT). Set 0 to disable if an ffmpeg build rejects the option.
RW_TIMEOUT_US="${RELAY_RW_TIMEOUT_US:-30000000}"
RWTO_ARG=""; [ "${RW_TIMEOUT_US}" != 0 ] && RWTO_ARG="-rw_timeout ${RW_TIMEOUT_US}"
MREQ_ARG=""; [ "${RELAY_MULTIPLE_REQUESTS:-}" = 1 ] && MREQ_ARG="-multiple_requests 1"
WATCH_CH="${WATCH_CHANNEL:-29}"
AGENT_PY=/home/askjitk/liftlab-b3/pi-agent/.venv/bin/python
WATCH_LOCAL=/home/askjitk/liftlab-b3/pi-agent/watch_local.py
say(){ echo "[relay-soak] $(date -u +%FT%TZ) $*"; }

# creds: prefer the injected env; only source the file if we actually can (manual root run)
if [ -z "${NVR_HOST:-}" ] && [ -r /etc/liftlab-agent.env ]; then set -a; . /etc/liftlab-agent.env; set +a; fi
: "${NVR_HOST:?NVR_HOST not in env (systemd EnvironmentFile should inject it)}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN not in env}"
: "${CLOUD_URL:?CLOUD_URL not in env}"
CLOUD="${CLOUD_URL%/}"; GW="${GW:-${GATEWAY_ID:-site-A}}"
command -v ffmpeg >/dev/null || { say "ffmpeg missing"; exit 1; }
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
say "channels ($CSRC): ${CHANS[*]}  iface=$IFACE  interval=${INTERVAL}s  csv=$CSV  door_floor=$DOOR_FLOOR"

say "delivery health = segments still arriving (>= ${ARRIVING_KBPS}kbps/interval); bitrate value can't distinguish a quiet cabin from a fault"

# ---------- helpers ----------
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }
# EVERY helper that shells out is wrapped in `timeout`. An unbounded one of these froze the loop for
# 22h — vcgencmd can block on a busy VideoCore mailbox, and door_fps reads the /dev/shm ring.
temp_c(){ timeout "$CALL_TIMEOUT" vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/"; }
throttle_live(){ local v; v=$(timeout "$CALL_TIMEOUT" vcgencmd get_throttled 2>/dev/null|sed 's/.*=//'); v=$((v)); local o="";
  (( v & 1 ))&&o+="undervolt "; (( v & 2 ))&&o+="freqcap "; (( v & 4 ))&&o+="throttled "; (( v & 8 ))&&o+="templimit "; echo "${o:-none}"|tr ' ' '+'|sed 's/+$//'; }
mem_avail(){ awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo; }
# THE FREEZE POINT. This shells into watch_local.py, which reads the /dev/shm segment ring — the ring
# with the known reader race. Unbounded, it parks the supervisor loop indefinitely; the door guard then
# never evaluates either, so the relay neither watches its streams nor protects the watch. Bounded now:
# a timeout yields empty output, which the guard already treats as "no reading" (no strike, no trip).
door_fps(){ timeout "$CALL_TIMEOUT" "$AGENT_PY" "$WATCH_LOCAL" status "$WATCH_CH" 2>/dev/null | grep -oE "'signal_fps': [0-9.]+" | head -1 | grep -oE "[0-9.]+$"; }
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
  hdr+=",ff_cpu_total,soc_temp,throttle_live,mem_avail_mb,door_fps,streams_alive,streams_delivering"
  echo "$hdr" > "$CSV"
fi

# ---------- launch producers, set up teardown ----------
declare -a PIDS; declare -A PREVJ
for ((i=0;i<NCH;i++)); do PIDS[$i]=$(launch "$i"); done
cleanup(){ say "stopping — killing $NCH ffmpeg"; for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
           [ -n "${WD_PID:-}" ] && kill "$WD_PID" 2>/dev/null; rm -f "$HB_FILE"; }
trap 'cleanup; exit 0' TERM INT
trap 'cleanup' EXIT
say "launched $NCH direct-PUT sub relays: ${PIDS[*]}"

# ---------- supervisor self-watchdog (the bash analogue of gpu_watchdog's os._exit) ----------
# The relay's OWN liveness. Everything above watches the ffmpeg streams; nothing watched the watcher,
# and it was the watcher that froze for 22h while systemd reported "active". The loop touches HB_FILE
# every turn; if that mtime stops advancing the supervisor is wedged and CANNOT recover itself — so
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
      say "  ffmpeg children still up: $(pgrep -c -f "$CLOUD/api/gw/$GW/live/" 2>/dev/null || echo 0)/$NCH"
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
strikes=0; hz=$(getconf CLK_TCK); GUARD_TRIPS=0; STALL_RESTARTS=0; SEQ=0
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
  tp=$(temp_c); thr=$(throttle_live); ma=$(mem_avail); df=$(door_fps); ps_json="${ps_json%,}}"
  echo "$(date -u +%FT%TZ),${upl},${smbps}${percols},${cpu},${tp},${thr},${ma},${df:-NA},${alive},${delivering}" >> "$CSV"
  # POST relay metrics to the cloud so /ops shows relay health without SSH (separate from the watch)
  payload="{\"sum_delivered_mbps\":${smbps},\"streams_alive\":${alive},\"streams_delivering\":${delivering},\"ff_cpu\":${cpu:-0},\"soc_temp\":${tp:-0},\"throttle_live\":\"${thr}\",\"mem_avail_mb\":${ma:-0},\"door_fps\":${df:-0},\"guard_trips\":${GUARD_TRIPS:-0},\"stall_restarts\":${STALL_RESTARTS:-0},\"per_stream\":${ps_json}}"
  curl -s -o /dev/null --max-time 5 -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" \
    -H "Content-Type: application/json" -d "$payload" "$CLOUD/api/gw/$GW/relay_status" 2>/dev/null || true
  prev_tx=$cur_tx; prev_sj=$cur_sj; prev_t=$now
  # ---------- DOOR GUARD: the watch is sacred ----------
  if [ -n "$df" ] && awk "BEGIN{exit !($df < $DOOR_FLOOR)}"; then
    strikes=$((strikes+1))
    say "WARN door_fps=$df < $DOOR_FLOOR (strike $strikes/$DOOR_STRIKES_MAX)"
    if [ "$strikes" -ge "$DOOR_STRIKES_MAX" ]; then
      say "DOOR GUARD TRIPPED: door_fps=$df sustained < $DOOR_FLOOR — STOPPING relay to protect the watch."
      echo "$(date -u +%FT%TZ),GUARD_TRIP,door_fps=$df,relay_stopped_to_protect_watch" >> "$CSV"
      GUARD_TRIPS=1
      curl -s -o /dev/null --max-time 5 -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" -H "Content-Type: application/json" \
        -d "{\"sum_delivered_mbps\":0,\"streams_alive\":0,\"streams_delivering\":0,\"door_fps\":${df:-0},\"guard_trips\":1,\"per_stream\":{}}" \
        "$CLOUD/api/gw/$GW/relay_status" 2>/dev/null || true   # surface the trip on /ops
      cleanup; trap - EXIT; exit 0   # exit 0 => systemd Restart=on-failure will NOT restart
    fi
  else
    strikes=0
  fi
done
