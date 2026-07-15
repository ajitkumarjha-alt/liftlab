#!/usr/bin/env bash
# CONCURRENT N-CAMERA relay load test (copy mode) with a RAMP. Measures where it breaks,
# not just whether x8 holds. The 1-cam copy baseline was 1.46 Mbps / 3% CPU / 66-68C /
# door_fps 9.88 — naive x8 = 12 Mbps / 24%. Concurrency isn't linear; this measures it.
#
# RUN ON THE PI AS ROOT (door watch liftlab-watch stays RUNNING — it is the hard gate):
#   sudo bash live_relay_8.sh
#   sudo STEP_S=60 STEPS_REQ="1 2 4 6 8" bash live_relay_8.sh
#   sudo CHANNELS="27 28 29 30 32 33 34" bash live_relay_8.sh   # override the channel source
#   sudo ALLOW_DUP=1 bash live_relay_8.sh                        # pad past #cabins with dup channels
#
# copy mode = HEVC remux (near-zero CPU); this stresses NIC + 8 RTSP sessions + 8 TLS PUTs +
# NVR session limits + uplink, NOT the CPU. Chrome can't play the HEVC; viewing isn't the point.
set -uo pipefail

GW_DEFAULT=site-A
STEP_S="${STEP_S:-60}"
STEPS_REQ="${STEPS_REQ:-1 2 4 6 8}"
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
say "channels ($CHAN_SRC): ${CHANS[*]}   count=$NCH   iface=$IFACE   step=${STEP_S}s"
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

launch_stream(){ # $1=idx  -> starts ffmpeg copy relay, echoes pid
  local i=$1 ch=${S_CH[$1]} cam=${S_CAM[$1]}
  local url="rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/1?transmode=unicast&profile=vam"
  local base="$CLOUD/api/gw/$GW/live/$cam" log="/tmp/relay8_${i}_${cam}.log"
  ffmpeg -nostdin -hide_banner -loglevel warning -stats \
    -rtsp_transport tcp -i "$url" -an -c:v copy \
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
fps0=$(door_fps); say "door signal_fps BEFORE any relay: ${fps0:-unavailable}"; hr

for target in "${STEPS[@]}"; do
  while [ "$running" -lt "$target" ]; do
    p=$(launch_stream "$running"); PIDS[$running]=$p; IDX_ORDER+=("$running")
    running=$((running+1))
  done
  sleep 4                                  # settle: RTSP open + first segments
  say "STEP: $target concurrent  (cams: $(for i in $(seq 0 $((target-1))); do printf '%s ' "${S_CAM[$i]}"; done))"
  # measurement window
  local_tx0=$(tx_bytes); t0=$(date +%s.%N)
  declare -A CJ0; for i in $(seq 0 $((target-1))); do CJ0[$i]=$(pid_jiffies "${PIDS[$i]}"); done
  tmax=0; thrL="none"; thrS="none"; fpsmin=99; n=$(( STEP_S/5 )); [ "$n" -lt 1 ]&&n=1
  for ((k=0;k<n;k++)); do
    sleep 5
    tp=$(temp_c); awk "BEGIN{exit !($tp>$tmax)}" 2>/dev/null && tmax=$tp
    tl=$(throttle_live); [ "$tl" != none ] && thrL=$tl
    ts=$(throttle_sticky); [ "$ts" != none ] && thrS=$ts
    fp=$(door_fps); [ -n "$fp" ] && awk "BEGIN{exit !($fp<$fpsmin)}" 2>/dev/null && fpsmin=$fp
  done
  t1=$(date +%s.%N); local_tx1=$(tx_bytes); dt=$(awk "BEGIN{print $t1-$t0}")
  # aggregate cpu across live streams
  hz=$(getconf CLK_TCK); cpusum=0; alive=0
  perstream=""
  for i in $(seq 0 $((target-1))); do
    if kill -0 "${PIDS[$i]}" 2>/dev/null; then
      j1=$(pid_jiffies "${PIDS[$i]}"); c=$(awk -v a="${CJ0[$i]}" -v b="$j1" -v hz="$hz" -v dt="$dt" 'BEGIN{printf "%.1f",(b-a)/hz/dt*100}')
      cpusum=$(awk -v s="$cpusum" -v c="$c" 'BEGIN{printf "%.1f",s+c}')
      alive=$((alive+1))
      sp=$(ff_stat "/tmp/relay8_${i}_${S_CAM[$i]}.log" speed); fpss=$(ff_stat "/tmp/relay8_${i}_${S_CAM[$i]}.log" fps)
      perstream+="      ${S_CAM[$i]}: alive fps=${fpss:-?} speed=${sp:-?}x cpu=${c}%\n"
    else
      err=$(tr '\r' '\n' < "/tmp/relay8_${i}_${S_CAM[$i]}.log" 2>/dev/null | grep -iE "error|failed|refused|453|503|unauthor|timed out" | tail -1)
      perstream+="      ${S_CAM[$i]}: DEAD  ${err:-<no stderr; check log>}\n"
    fi
  done
  mbps=$(awk -v a="$local_tx0" -v b="$local_tx1" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  ma=$(mem_avail); fpsA=$(door_fps)
  printf "%d|%d|%s|%s|%s|%s|%s|%s|%s\n" "$target" "$alive" "$mbps" "$cpusum" "$tmax" "$thrL" "$thrS" "$fpsmin" "$ma" >> /tmp/relay8_results.txt
  say "  -> alive=$alive/$target  uplink=${mbps}Mbps  ff_cpu=${cpusum}%  temp=${tmax}C  thr_live=$thrL  thr_sticky=$thrS  door_fps min=$fpsmin after=${fpsA:-?}  mem_avail=${ma}MB"
  printf "%b" "$perstream"
  hr
done

# ---------- report + honest ceiling ----------
say "SUMMARY  (copy/HEVC remux, ${STEP_S}s/step, iface=$IFACE, door watch running, src=$CHAN_SRC)"
printf "    %-5s %-7s %-9s %-8s %-7s %-11s %-13s %-9s %s\n" step alive uplink ffcpu% peakT thr_live thr_sticky doorFPS memMB
while IFS='|' read -r st al mb cpu tmax tl ts fmin ma; do
  [ -z "$st" ] && continue
  printf "    %-5s %-7s %-9s %-8s %-7s %-11s %-13s %-9s %s\n" "$st" "$al/$st" "${mb}Mbps" "${cpu}%" "${tmax}C" "$tl" "$ts" "$fmin" "${ma}"
done < /tmp/relay8_results.txt
hr
say "UPLINK SCALING — does Mbps track stream count, or PLATEAU (the link ceiling)?"
awk -F'|' 'NR==1{base=($2?$3/$2:0)} {ps=($2?$3/$2:0); lin=base*$2;
  printf "    %s streams: %.2f Mbps total  (%.2f/stream, linear=%.2f, %d%% of linear)\n",
    $2,$3,ps,lin,(lin?$3/lin*100:0)}' /tmp/relay8_results.txt
PLAT=$(awk -F'|' 'NR==1{base=($2?$3/$2:0)} END{lin=base*$2;
  if(lin>0 && $3/lin<0.85) printf "PLATEAU: at %s streams measured %.2f Mbps vs %.2f linear (%d%%) — LINK CEILING FOUND",$2,$3,lin,$3/lin*100;
  else printf "no plateau: uplink still scaled ~linearly to %s streams (%.2f Mbps) — link not yet the limit",$2,$3}' /tmp/relay8_results.txt)
say "  => $PLAT"
hr
say "NOTE: thr_sticky is almost certainly PRE-SET (freqcap/throttled/templimit) from the occ 81C"
say "  event — sticky bits only clear on reboot. Judge THERMAL by thr_live (current) + peakT, not sticky."
# ceiling = highest step where ALL streams alive AND door_fps held >=9.5 AND no LIVE throttle
CEIL=$(awk -F'|' '$2==$1 && $8>=9.5 && $6=="none" {c=$1; u=$3} END{if(c)printf "%d|%s",c,u}' /tmp/relay8_results.txt)
if [ -n "$CEIL" ]; then
  cn=${CEIL%|*}; cu=${CEIL#*|}
  say "HONEST CEILING: $cn concurrent cameras — all streams alive, door_fps held >=9.5, no throttle."
  say "  uplink at $cn cams = ${cu} Mbps sustained (${IFACE}). Above $cn, a gate broke (see the row that fails)."
else
  say "HONEST CEILING: even 1 stream harmed the door loop or dropped — see the table. Relay-on-this-Pi not viable alongside the watch."
fi
say "  Read the failing row: alive<step => NVR session cap or stream drop; door_fps<9.5 => watch starved;"
say "  thr_sticky sets => thermal even if live clear; uplink plateaus => link ceiling (not linear)."
say "done. (VM tmpfs holds only the last few segments per cam; nothing durable written.)"
