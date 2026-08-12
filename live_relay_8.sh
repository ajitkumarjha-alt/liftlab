#!/usr/bin/env bash
# CONCURRENT N-CAMERA relay load test (copy mode) with a RAMP. Measures where it breaks,
# not just whether x8 holds. The 1-cam copy baseline was 1.46 Mbps / 3% CPU / 66-68C /
# door_fps 9.88 — naive x8 = 12 Mbps / 24%. Concurrency isn't linear; this measures it.
#
# RUN ON THE PI AS ROOT (door watch liftlab-watch stays RUNNING — it is the hard gate):
#   sudo STREAM=1 bash live_relay_8.sh                # MAIN stream (1920x1080 HEVC)
#   sudo STREAM=2 LINK_CEIL=10 bash live_relay_8.sh   # SUB (1280x720 HEVC); LINK_CEIL from link_probe
#   sudo STEP_S=60 STEPS_REQ="1 2 4 6 7" bash live_relay_8.sh
#   sudo CHANNELS="27 28 29 30 32 33 34" bash live_relay_8.sh   # override the channel source
#   sudo ALLOW_DUP=1 bash live_relay_8.sh                        # pad past #cabins with dup channels
#
# copy mode = remux (near-zero CPU); this stresses NIC + RTSP sessions + TLS PUTs + NVR limits +
# UPLINK, NOT the CPU. Per-stream health is judged on DELIVERED bytes/sec at the VM (live_stats),
# NOT process liveness — ffmpeg stays alive at speed=1x while the PUT blocks and drops under a
# saturated uplink (the main ramp's 8x byte spread across identical cams was exactly that).
set -uo pipefail

GW_DEFAULT=site-A
STREAM="${STREAM:-1}"                  # 1=main, 2=sub
STEP_S="${STEP_S:-60}"
STEPS_REQ="${STEPS_REQ:-1 2 4 6 7}"
ENVF="${ENVF:-/etc/liftlab-agent.env}"
SEG_T="${SEG_T:-2}"
ALLOW_DUP="${ALLOW_DUP:-0}"
say(){ echo "[relay8] $*"; }
hr(){ printf '%s\n' "======================================================================"; }

[ -f "$ENVF" ] || { echo "missing $ENVF"; exit 2; }
[ -r "$ENVF" ] || { echo "cannot READ $ENVF (root-only). Run as root: sudo bash $0"; exit 2; }
set -a; . "$ENVF"; set +a
: "${NVR_HOST:?NVR_HOST empty in env}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN empty in env}"
: "${CLOUD_URL:?CLOUD_URL empty in env — not guessing}"
CLOUD="${CLOUD_URL%/}"
GW="${GW:-${GATEWAY_ID:-$GW_DEFAULT}}"
command -v ffmpeg >/dev/null || { echo "ffmpeg not installed"; exit 2; }
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")

IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); [ -n "$IFACE" ] || IFACE=eth0

# ---------- resolve channels: env > cloud channel_map > builtin (report source) ----------
CHAN_SRC=""
if [ -n "${CHANNELS:-}" ]; then
  CHANS=($CHANNELS); CHAN_SRC="env override"
else
  J=$(curl -s --max-time 8 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null)
  CHANS=($(echo "$J" | grep -oE '"channels":\[[0-9,]*\]' | grep -oE '[0-9]+'))
  if [ "${#CHANS[@]}" -gt 0 ]; then CHAN_SRC="cloud channel_map (is_lift=1)"; \
    else CHANS=(27 28 29 30 32 33 34); CHAN_SRC="builtin default (cloud lift_channels unavailable)"; fi
fi
NCH=${#CHANS[@]}
[ "$NCH" -gt 0 ] || { say "no channels resolved — aborting"; exit 1; }
say "channels ($CHAN_SRC): ${CHANS[*]}   count=$NCH   iface=$IFACE   step=${STEP_S}s   STREAM=$STREAM ($([ "$STREAM" = 2 ] && echo sub || echo main))"

# ---------- probe the ACTUAL bytes (ONVIF lied: main claimed h264, is HEVC) ----------
PROBE_CH=${CHANS[0]}
PURL="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${PROBE_CH}/${STREAM}?transmode=unicast&profile=vam"
say "ffprobe ch${PROBE_CH} stream=$STREAM (real codec/res/bitrate):"
PROBE=$(ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
  -show_entries stream=codec_name,width,height,avg_frame_rate,bit_rate \
  -of default=noprint_wrappers=1 "$PURL" 2>/dev/null)
echo "$PROBE" | sed 's/^/    /'
SRC_CODEC=$(echo "$PROBE" | awk -F= '/codec_name/{print $2}')
SRC_BR=$(echo "$PROBE" | awk -F= '/bit_rate/{print $2}')
[ -n "$SRC_CODEC" ] || say "  (probe empty — stream=$STREAM may not exist on this NVR; check RTSP path)"
[ "$IFACE" = wlan0 ] && say "NOTE: uplink is WIFI (wlan0). Sustained Mbps on wifi != wired; flagging per the brief."

# ---------- build the ordered stream plan (distinct first; dup only if ALLOW_DUP) ----------
# actual ramp steps = requested steps clamped to what we can run, plus the max as a final step.
MAXN=$NCH; [ "$ALLOW_DUP" = 1 ] && MAXN=$(echo "$STEPS_REQ" | tr ' ' '\n' | sort -n | tail -1)
STEPS=(); for s in $STEPS_REQ; do [ "$s" -le "$MAXN" ] && STEPS+=("$s"); done
case " ${STEPS[*]} " in *" $MAXN "*) ;; *) STEPS+=("$MAXN");; esac
# stream i -> channel (cycle) + unique cam label so VM dirs never collide
declare -a S_CH S_CAM
for ((i=0;i<MAXN;i++)); do
  ch=${CHANS[$(( i % NCH ))]}; dup=$(( i / NCH ))
  S_CH[$i]=$ch
  if [ "$dup" -eq 0 ]; then S_CAM[$i]="ch${ch}"; else S_CAM[$i]="ch${ch}d${dup}"; fi
done
[ "$MAXN" -gt "$NCH" ] && say "WARNING: ramp to $MAXN exceeds $NCH cabins — streams past #$NCH REUSE a channel (labeled chNdK); NVR sees 2 sessions on it."
[ "$(printf '%s\n' "${STEPS_REQ}" | grep -qw 8 && echo y)" = y ] && [ "$MAXN" -lt 8 ] && say "NOTE: 8 requested but only $NCH cabins (ALLOW_DUP=0) — top real step is $MAXN. Set ALLOW_DUP=1 to force 8 with a duplicate."

# ---------- helpers ----------
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }
temp_c(){ vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/"; }
mem_avail(){ awk '/MemAvailable/{printf "%d",$2/1024}' /proc/meminfo; }
thr_decode(){ local v=$1 which=$2 o=""; local base=0; [ "$which" = sticky ] && base=16
  (( v & (1<<(base+0)) ))&&o+="undervolt "; (( v & (1<<(base+1)) ))&&o+="freqcap "
  (( v & (1<<(base+2)) ))&&o+="throttled "; (( v & (1<<(base+3)) ))&&o+="templimit "; echo "${o:-none}"; }
throttle_live(){ local v; v=$(vcgencmd get_throttled 2>/dev/null|sed 's/.*=//'); thr_decode $((v)) live; }
throttle_sticky(){ local v; v=$(vcgencmd get_throttled 2>/dev/null|sed 's/.*=//'); thr_decode $((v)) sticky; }
door_fps(){ /home/askjitk/liftlab-b3/pi-agent/.venv/bin/python \
    /home/askjitk/liftlab-b3/pi-agent/watch_local.py status "${WATCH_CH:-29}" 2>/dev/null \
    | grep -oE "'signal_fps': [0-9.]+" | head -1 | grep -oE "[0-9.]+$"; }
pid_jiffies(){ awk '{print $14+$15}' "/proc/$1/stat" 2>/dev/null||echo 0; }
ff_stat(){ tr '\r' '\n' < "$1" 2>/dev/null | grep -oE "$2=[0-9.]+" | tail -1 | grep -oE "[0-9.]+$"; }

uplink_audit(){ # can eth0 (NVR VLAN) reach the internet, or is wlan0 the ONLY path? (24/7 risk)
  hr; say "UPLINK AUDIT — which interface can actually carry a 24/7 relay to the internet?"
  ip -brief -4 addr 2>/dev/null | sed 's/^/    /'
  local defif; defif=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
  say "default-route iface (current uplink): ${defif:-none}"
  local reach_wlan="" reach_eth=""
  for f in wlan0 eth0; do
    ip link show "$f" >/dev/null 2>&1 || { say "  $f: absent"; continue; }
    local ip4; ip4=$(ip -brief -4 addr show "$f" 2>/dev/null | awk '{print $3}')
    # SO_BINDTODEVICE (root): force egress out THIS iface, try to reach the cloud host.
    local code; code=$(curl --interface "$f" -s -o /dev/null -w '%{http_code}' --max-time 6 \
      -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null)
    if [ -n "$code" ] && [ "$code" != 000 ]; then
      say "  $f (${ip4:-no-ip}): REACHES internet/cloud (HTTP $code) => viable uplink"
      [ "$f" = wlan0 ] && reach_wlan=1; [ "$f" = eth0 ] && reach_eth=1
    else
      say "  $f (${ip4:-no-ip}): cannot reach cloud (no internet route via this iface)"
    fi
  done
  if [ -n "$reach_eth" ]; then
    say "VERDICT: eth0 CAN reach the internet — prefer WIRED for a 24/7 relay (set IFACE=eth0 + route)."
  elif [ "$defif" = wlan0 ] || [ -n "$reach_wlan" ]; then
    say "VERDICT: wlan0 is the ONLY internet path (eth0 = NVR VLAN 172.50.x, no internet route)."
    say "  A 24/7 production relay would ride entirely on WIFI stability. Real risk — flag to Aj."
  fi; hr
}

live_stats_json(){ curl -s --max-time 6 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/live_stats" 2>/dev/null; }
stat_bytes(){ # $1=json $2=cam -> delivered bytes for that cam at the VM (0 if absent)
  printf '%s' "$1" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print(0); sys.exit()
print(d.get('cams',{}).get('$2',{}).get('bytes',0))" 2>/dev/null || echo 0; }

launch_stream(){ # $1=idx  -> starts ffmpeg copy relay, echoes pid
  local i=$1 ch=${S_CH[$1]} cam=${S_CAM[$1]}
  local url="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam"
  local base="$CLOUD/api/gw/$GW/live/$cam" log="/tmp/relay8_${i}_${cam}.log"
  ffmpeg -nostdin -hide_banner -loglevel warning -stats \
    -rtsp_transport tcp -i "$url" -an -c:v copy \
    -start_number "$(date +%s)" \
    -f hls -hls_time "$SEG_T" -hls_list_size 5 \
    -hls_flags delete_segments+omit_endlist -hls_segment_type mpegts \
    -method PUT -http_persistent 1 -headers "Authorization: Bearer ${GATEWAY_TOKEN}"$'\r\n' \
    -hls_segment_filename "$base/seg%03d.ts" "$base/index.m3u8" >"$log" 2>&1 &
  echo $!
}

# ---------- ramp ----------
: > /tmp/relay8_results.txt
declare -a PIDS IDX_ORDER
running=0
trap 'for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done' EXIT
uplink_audit
fps0=$(door_fps); say "door signal_fps BEFORE any relay: ${fps0:-unavailable}"
DOOR_FLOOR="${DOOR_FLOOR:-$(awk -v b="${fps0:-9.9}" 'BEGIN{f=b-0.1; if(f>9.8)f=9.8; printf "%.2f",f}')}"
say "door gate = signal_fps must stay >= $DOOR_FLOOR (baseline ${fps0:-?})"
REF_KBPS=""                              # uncontended single-stream delivery, set at step 1
hr

for target in "${STEPS[@]}"; do
  while [ "$running" -lt "$target" ]; do
    p=$(launch_stream "$running"); PIDS[$running]=$p; IDX_ORDER+=("$running")
    running=$((running+1))
  done
  sleep 4                                  # settle: RTSP open + first segments
  say "STEP: $target concurrent  (cams: $(for i in $(seq 0 $((target-1))); do printf '%s ' "${S_CAM[$i]}"; done))"
  # measurement window: snapshot tx, cpu jiffies, AND VM delivered-bytes per cam
  local_tx0=$(tx_bytes); t0=$(date +%s.%N); SJ0=$(live_stats_json)
  declare -A CJ0 B0; for i in $(seq 0 $((target-1))); do CJ0[$i]=$(pid_jiffies "${PIDS[$i]}"); B0[$i]=$(stat_bytes "$SJ0" "${S_CAM[$i]}"); done
  tmax=0; thrL="none"; thrS="none"; fpsmin=99; n=$(( STEP_S/5 )); [ "$n" -lt 1 ]&&n=1
  for ((k=0;k<n;k++)); do
    sleep 5
    tp=$(temp_c); awk "BEGIN{exit !($tp>$tmax)}" 2>/dev/null && tmax=$tp
    tl=$(throttle_live); [ "$tl" != none ] && thrL=$tl
    ts=$(throttle_sticky); [ "$ts" != none ] && thrS=$ts
    fp=$(door_fps); [ -n "$fp" ] && awk "BEGIN{exit !($fp<$fpsmin)}" 2>/dev/null && fpsmin=$fp
  done
  t1=$(date +%s.%N); local_tx1=$(tx_bytes); dt=$(awk "BEGIN{print $t1-$t0}"); SJ1=$(live_stats_json)
  # per-stream: DELIVERED kbps at the VM (the honest signal) + cpu; delivering != alive
  hz=$(getconf CLK_TCK); cpusum=0; alive=0; deliver=0; sumk=0; mink=""; maxk=0
  FLOOR_KBPS=$(awk -v r="${REF_KBPS:-0}" 'BEGIN{f=r*0.5; if(f<50)f=50; printf "%.0f",f}')
  perstream=""
  for i in $(seq 0 $((target-1))); do
    b1=$(stat_bytes "$SJ1" "${S_CAM[$i]}")
    dk=$(awk -v a="${B0[$i]}" -v b="$b1" -v dt="$dt" 'BEGIN{printf "%.0f",(b-a)*8/dt/1000}')   # delivered kbps
    sumk=$(awk -v s="$sumk" -v k="$dk" 'BEGIN{print s+k}')
    awk "BEGIN{exit !($dk>$maxk)}" && maxk=$dk
    [ -z "$mink" ] && mink=$dk; awk "BEGIN{exit !($dk<$mink)}" && mink=$dk
    if kill -0 "${PIDS[$i]}" 2>/dev/null; then
      j1=$(pid_jiffies "${PIDS[$i]}"); c=$(awk -v a="${CJ0[$i]}" -v b="$j1" -v hz="$hz" -v dt="$dt" 'BEGIN{printf "%.1f",(b-a)/hz/dt*100}')
      cpusum=$(awk -v s="$cpusum" -v c="$c" 'BEGIN{printf "%.1f",s+c}'); alive=$((alive+1))
      sp=$(ff_stat "/tmp/relay8_${i}_${S_CAM[$i]}.log" speed)
      if awk "BEGIN{exit !($dk>=$FLOOR_KBPS)}"; then
        deliver=$((deliver+1)); tag="delivering"
      else
        tag="STARVED (< ${FLOOR_KBPS}kbps floor = rationed by the uplink)"
      fi
      perstream+="      ${S_CAM[$i]}: ${dk}kbps delivered  src_speed=${sp:-?}x  cpu=${c}%  -> $tag\n"
    else
      err=$(tr '\r' '\n' < "/tmp/relay8_${i}_${S_CAM[$i]}.log" 2>/dev/null | grep -iE "error|failed|refused|453|503|unauthor|timed out" | tail -1)
      perstream+="      ${S_CAM[$i]}: DEAD (0kbps)  ${err:-<no stderr; check log>}\n"
    fi
  done
  [ -z "$REF_KBPS" ] && [ "$target" -ge 1 ] && REF_KBPS=$maxk    # uncontended reference from step 1
  [ -z "$mink" ] && mink=0
  mbps=$(awk -v a="$local_tx0" -v b="$local_tx1" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  ma=$(mem_avail); fpsA=$(door_fps)
  printf "%d|%d|%d|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n" \
    "$target" "$alive" "$deliver" "$mbps" "$cpusum" "$tmax" "$thrL" "$thrS" "$fpsmin" "$ma" "$mink" "$maxk" "$sumk" >> /tmp/relay8_results.txt
  say "  -> alive=$alive/$target  DELIVERING=$deliver/$target  uplink=${mbps}Mbps (sum_delivered=$(awk -v k="$sumk" 'BEGIN{printf "%.2f",k/1000}')Mbps)"
  say "     ff_cpu=${cpusum}%  temp=${tmax}C  thr_live=$thrL  door_fps min=$fpsmin after=${fpsA:-?}  mem=${ma}MB  per-stream kbps: min=$mink max=$maxk"
  printf "%b" "$perstream"
  hr
done

# ---------- report + honest ceiling ----------
# fields: 1 step 2 alive 3 deliver 4 mbps 5 cpu 6 tmax 7 thrL 8 thrS 9 fmin 10 mem 11 mink 12 maxk 13 sumk
say "SUMMARY  (copy remux, stream=$STREAM codec=${SRC_CODEC:-?}, ${STEP_S}s/step, iface=$IFACE, src=$CHAN_SRC)"
printf "    %-5s %-9s %-10s %-11s %-7s %-6s %-9s %-9s %s\n" step deliver tx_Mbps deliv_Mbps ffcpu peakT thr_live doorFPS "kbps min/max"
while IFS='|' read -r st al dl mb cpu tmax tl ts fmin ma mink maxk sumk; do
  [ -z "$st" ] && continue
  printf "    %-5s %-9s %-10s %-11s %-7s %-6s %-9s %-9s %s\n" \
    "$st" "$dl/$st" "${mb}" "$(awk -v k="$sumk" 'BEGIN{printf "%.2f",k/1000}')" "${cpu}%" "${tmax}C" "$tl" "$fmin" "$mink/$maxk"
done < /tmp/relay8_results.txt
hr
say "DELIVERED THROUGHPUT vs stream count (set LINK_CEIL=<Mbps> from link_probe to auto-classify):"
awk -F'|' '{printf "    %s streams: %.2f Mbps delivered  (delivering %s/%s, per-stream kbps min/max %s/%s)\n",
    $1,$13/1000,$3,$1,$11,$12}' /tmp/relay8_results.txt
# flat total = SATURATED (at the link, IF near LINK_CEIL; at the RELAY if well below it);
# growing = SCALING (headroom); dropping = COLLAPSE. Never "% of N x single-stream".
SCAL=$(awk -F'|' -v ceil="${LINK_CEIL:-0}" '
NR==1{first=$13/1000; firstN=$1}
{last=$13/1000; lastN=$1; if($13/1000>cap)cap=$13/1000}
END{
  rt=(first>0)?last/first:1; grew=(lastN>firstN)?(lastN/firstN):1;
  if(rt<0.85) printf "COLLAPSE: delivered FELL %.2f->%.2f as N %s->%s => starvation/serialization, not the link.",first,last,firstN,lastN;
  else if(rt < grew*0.6){
    printf "PLATEAU ~%.2f Mbps delivered (flat while N rose).",cap;
    if(ceil>0 && cap<ceil*0.85) printf " BELOW the ~%.1f Mbps link => capped by the RELAY/VM (handler?), NOT the link.",ceil;
    else if(ceil>0) printf " ~= the ~%.1f Mbps link => LINK-limited (expected for main).",ceil;
    else printf " (pass LINK_CEIL to say whether that is the link or the relay.)";
  } else printf "SCALING ~linearly to %s streams (%.2f Mbps delivered) => link has headroom, delivery healthy.",lastN,last;
}' /tmp/relay8_results.txt)
say "  => $SCAL"
hr
say "NOTE: thr_sticky is PRE-SET (freqcap/throttled/templimit) from the occ 81C event (clears on"
say "  reboot). Judge THERMAL by thr_live + peakT. And judge each stream by DELIVERED kbps, not"
say "  liveness — the main ramp's 'alive=7/7' hid an 8x rationing spread; deliver-count is the truth."
# ceiling = highest step where ALL streams DELIVERING evenly AND door held AND no LIVE throttle
CEIL=$(awk -F'|' -v fl="$DOOR_FLOOR" '$3==$1 && $9>=fl && $7=="none" {c=$1; u=$13/1000} END{if(c)printf "%d|%.2f",c,u}' /tmp/relay8_results.txt)
if [ -n "$CEIL" ]; then
  cn=${CEIL%|*}; cu=${CEIL#*|}
  say "HONEST CEILING: $cn cameras — all $cn DELIVERING (not just alive), door_fps>=$DOOR_FLOOR, no throttle."
  say "  delivered throughput at $cn = ${cu} Mbps sustained on ${IFACE}. Above $cn a gate broke (see the failing row)."
  [ "$STREAM" = 2 ] && say "  PASS CONDITION for subs = all 7 delivering + linear + door held. Check deliver=7/7 at the top row."
else
  say "HONEST CEILING: even 1 stream failed to deliver or harmed the door loop — see the table."
fi
say "  Failing row: deliver<step => streams starved (rationed, not dead); door<floor => watch starved;"
say "  thr_live sets => thermal. Delivered FLAT while N rises = SATURATION: at the LINK if ~LINK_CEIL,"
say "  at the RELAY if well below it. Delivered DROPPING as N rises = serialization (a real bug)."
say "done. (VM tmpfs holds only the last few segments per cam; nothing durable written.)"
