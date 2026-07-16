#!/usr/bin/env bash
# OVERNIGHT SUB-STREAM RELAY SOAK supervisor. Run by the liftlab-relay systemd service (NOT
# by hand for the soak). Relays 7 cabin SUB streams (copy mode) continuously, logs a CSV row
# every 30s to a DURABLE path, restarts dropped streams, and KILLS ITSELF if the door loop
# (liftlab-watch signal_fps) sags — the watch is sacred, the relay is expendable.
#
# Creds come from the systemd EnvironmentFile (/etc/liftlab-agent.env) injected into the env;
# this script reads os.environ-style vars and does NOT read the root-only file itself.
set -uo pipefail

STREAM=2
INTERVAL="${RELAY_INTERVAL:-30}"
CSV="${RELAY_CSV:-/home/askjitk/liftlab-watch/relay_soak.csv}"
DOOR_FLOOR="${RELAY_DOOR_FLOOR:-9.5}"          # relay stops if door_fps sags below this
DOOR_STRIKES_MAX="${RELAY_DOOR_STRIKES:-3}"    # for this many consecutive samples (~90s)
DELIVER_FLOOR_KBPS="${RELAY_DELIVER_FLOOR:-400}"  # a sub delivering below this = starved
HLS_TIME="${RELAY_HLS_TIME:-2}"                # segment seconds; bigger = fewer/less-synced PUTs
# opt-in ffmpeg http tweak (empty by default = keep the known-to-run flags; set to 1 to try
# forcing single-connection reuse). Some ffmpeg builds reject it on the hls muxer, so off unless asked.
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

# ---------- helpers ----------
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }
temp_c(){ vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/"; }
throttle_live(){ local v; v=$(vcgencmd get_throttled 2>/dev/null|sed 's/.*=//'); v=$((v)); local o="";
  (( v & 1 ))&&o+="undervolt "; (( v & 2 ))&&o+="freqcap "; (( v & 4 ))&&o+="throttled "; (( v & 8 ))&&o+="templimit "; echo "${o:-none}"|tr ' ' '+'|sed 's/+$//'; }
mem_avail(){ awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo; }
door_fps(){ "$AGENT_PY" "$WATCH_LOCAL" status "$WATCH_CH" 2>/dev/null | grep -oE "'signal_fps': [0-9.]+" | head -1 | grep -oE "[0-9.]+$"; }
pid_jiffies(){ awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null||echo 0; }
live_stats_json(){ curl -s --max-time 6 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/live_stats" 2>/dev/null; }
stat_bytes(){ printf '%s' "$1" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(0); sys.exit()
print(d.get('cams',{}).get('$2',{}).get('bytes',0))" 2>/dev/null || echo 0; }

launch(){ # $1=slot -> (re)start ffmpeg for that cam, echo pid
  local i=$1 ch=${CHANS[$i]} cam=${CAMS[$i]}
  local url="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam"
  local base="$CLOUD/api/gw/$GW/live/$cam"
  ffmpeg -nostdin -hide_banner -loglevel error \
    -rtsp_transport tcp -i "$url" -an -c:v copy \
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

# ---------- launch all, set up teardown ----------
declare -a PIDS; declare -A PREVJ
for ((i=0;i<NCH;i++)); do PIDS[$i]=$(launch "$i"); done
cleanup(){ say "stopping — killing ${#PIDS[@]} ffmpeg"; for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done; }
trap 'cleanup; exit 0' TERM INT
trap 'cleanup' EXIT
say "launched $NCH sub relays: ${PIDS[*]}"
sleep 6
prev_tx=$(tx_bytes); prev_sj=$(live_stats_json); prev_t=$(date +%s.%N)
declare -A PREVB; for ((i=0;i<NCH;i++)); do PREVB[$i]=$(stat_bytes "$prev_sj" "${CAMS[$i]}"); PREVJ[${PIDS[$i]}]=$(pid_jiffies "${PIDS[$i]}"); done
strikes=0; hz=$(getconf CLK_TCK)

# ---------- soak loop ----------
while :; do
  sleep "$INTERVAL"
  now=$(date +%s.%N); dt=$(awk -v a="$prev_t" -v b="$now" 'BEGIN{d=b-a; print (d>0?d:1)}')
  cur_tx=$(tx_bytes); cur_sj=$(live_stats_json)
  upl=$(awk -v a="$prev_tx" -v b="$cur_tx" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  # per-stream delivered kbps + cpu + liveness
  percols=""; sumk=0; alive=0; delivering=0; cpu=0
  for ((i=0;i<NCH;i++)); do
    b1=$(stat_bytes "$cur_sj" "${CAMS[$i]}")
    dk=$(awk -v a="${PREVB[$i]}" -v b="$b1" -v dt="$dt" 'BEGIN{printf "%.0f",(b-a)*8/dt/1000}')
    PREVB[$i]=$b1; percols+=",${dk}"; sumk=$(awk -v s="$sumk" -v k="$dk" 'BEGIN{print s+k}')
    awk "BEGIN{exit !($dk>=$DELIVER_FLOOR_KBPS)}" && delivering=$((delivering+1))
    local_pid=${PIDS[$i]}
    if kill -0 "$local_pid" 2>/dev/null; then
      alive=$((alive+1)); j1=$(pid_jiffies "$local_pid"); pj=${PREVJ[$local_pid]:-$j1}
      cpu=$(awk -v s="$cpu" -v a="$pj" -v b="$j1" -v hz="$hz" -v dt="$dt" 'BEGIN{printf "%.1f",s+(b-a)/hz/dt*100}')
      PREVJ[$local_pid]=$j1
    else
      say "stream ${CAMS[$i]} DIED — restarting (wifi/NVR dropout). tail: $(tail -1 /tmp/relay_soak_${CAMS[$i]}.log 2>/dev/null)"
      np=$(launch "$i"); PIDS[$i]=$np; PREVJ[$np]=$(pid_jiffies "$np")
    fi
  done
  smbps=$(awk -v k="$sumk" 'BEGIN{printf "%.2f",k/1000}')
  tp=$(temp_c); thr=$(throttle_live); ma=$(mem_avail); df=$(door_fps)
  echo "$(date -u +%FT%TZ),${upl},${smbps}${percols},${cpu},${tp},${thr},${ma},${df:-NA},${alive},${delivering}" >> "$CSV"
  prev_tx=$cur_tx; prev_sj=$cur_sj; prev_t=$now
  # ---------- DOOR GUARD: the watch is sacred ----------
  if [ -n "$df" ] && awk "BEGIN{exit !($df < $DOOR_FLOOR)}"; then
    strikes=$((strikes+1))
    say "WARN door_fps=$df < $DOOR_FLOOR (strike $strikes/$DOOR_STRIKES_MAX)"
    if [ "$strikes" -ge "$DOOR_STRIKES_MAX" ]; then
      say "DOOR GUARD TRIPPED: door_fps=$df sustained < $DOOR_FLOOR — STOPPING relay to protect the watch."
      echo "$(date -u +%FT%TZ),GUARD_TRIP,door_fps=$df,relay_stopped_to_protect_watch" >> "$CSV"
      cleanup; trap - EXIT; exit 0   # exit 0 => systemd Restart=on-failure will NOT restart
    fi
  else
    strikes=0
  fi
done
