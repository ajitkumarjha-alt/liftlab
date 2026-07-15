#!/usr/bin/env bash
# ONE-CAMERA live relay test: ch29 RTSP -> H264 -> HLS -> VM, measured.
# Proves whether the Pi can relay a live feed to lift.gargi.online for browser viewing,
# and produces the numbers that decide whether 7-8 concurrent relays are real.
#
# RUN ON THE PI AS ROOT (door watch liftlab-watch stays RUNNING — we measure if it survives).
# Root is required because creds live in the root-only /etc/liftlab-agent.env (the same wall
# that broke watch_local when run as askjitk). door_fps still queries the watch as askjitk.
#   sudo bash live_relay.sh                             # sweep: copy, then hw, then sw
#   sudo MODE=copy DURATION=120 bash live_relay.sh      # one mode only
#   sudo MODE=hw   bash live_relay.sh                    # force h264_v4l2m2m
#   sudo MODE=sw   bash live_relay.sh                    # force libx264
#   sudo RTSP_URL='rtsp://...'  bash live_relay.sh       # override the input URL
#
# It does NOT touch the door pipeline. It reads creds from /etc/liftlab-agent.env, opens a
# SECOND RTSP session to the NVR (ch29 main), and PUTs HLS to the VM's live_api.
set -uo pipefail

# ---------- config ----------
CH="${CH:-29}"
CAM="${CAM:-ch$CH}"
DURATION="${DURATION:-90}"            # seconds measured per mode
MODE="${MODE:-sweep}"                 # sweep | copy | hw | sw
ENVF="${ENVF:-/etc/liftlab-agent.env}"
IFACE="${IFACE:-}"                    # uplink iface; auto-detected if empty
SEG_T="${SEG_T:-2}"                   # HLS segment seconds
SW_BR="${SW_BR:-1500k}"              # libx264 target bitrate (cap SW cost)
say(){ echo "[relay] $*"; }
hr(){ printf '%s\n' "----------------------------------------------------------------"; }

[ -f "$ENVF" ] || { echo "missing $ENVF"; exit 2; }
[ -r "$ENVF" ] || { echo "cannot READ $ENVF (root-only). Run as root: sudo bash $0"; exit 2; }
# shellcheck disable=SC1090
set -a; . "$ENVF"; set +a
: "${NVR_HOST:?NVR_HOST empty in env}"
: "${GATEWAY_TOKEN:?GATEWAY_TOKEN empty in env}"
# Bind to the SAME cloud URL + gateway id the agent already POSTs to (proven reachable,
# valid cert). Do NOT invent a fallback — if it's absent, the env is wrong, fail loud.
: "${CLOUD_URL:?CLOUD_URL empty in env — the agent uses this to POST; not guessing a URL}"
CLOUD="${CLOUD_URL%/}"
GW="${GW:-${GATEWAY_ID:-site-A}}"
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")
RTSP_URL="${RTSP_URL:-rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${CH}/1?transmode=unicast&profile=vam}"
PUT_BASE="$CLOUD/api/gw/$GW/live/$CAM"
command -v ffmpeg >/dev/null || { echo "ffmpeg not installed"; exit 2; }

# uplink iface = the one with the default route
[ -n "$IFACE" ] || IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
[ -n "$IFACE" ] || IFACE=eth0

say "input : ${RTSP_URL/$PASS_ENC/********}"
say "output: $PUT_BASE/index.m3u8   iface=$IFACE   dur=${DURATION}s/mode   mode=$MODE"

# ---------- source probe: codec decides whether COPY is even possible ----------
hr; say "SOURCE PROBE (ffprobe)"
PROBE=$(ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
  -show_entries stream=codec_name,width,height,avg_frame_rate,bit_rate \
  -of default=noprint_wrappers=1 "$RTSP_URL" 2>/dev/null)
echo "$PROBE" | sed 's/^/    /'
SRC_CODEC=$(echo "$PROBE" | awk -F= '/codec_name/{print $2}')
[ -n "$SRC_CODEC" ] || { say "could not probe source — check RTSP_URL/creds. Aborting."; exit 1; }
say "source codec = ${SRC_CODEC:-?}  (ch29 main is HEVC; ONVIF metadata lies and claims h264)"
if [ "$SRC_CODEC" = hevc ]; then
  say "  copy-mode = near-zero CPU remux, but Chrome CANNOT play HEVC-in-HLS (black player)."
  say "  copy answers STEP 2 (analyse on the VM: ffmpeg decodes HEVC fine, no browser)."
  say "  hw/sw transcode answer the DEMO (browser viewing). Different questions — both reported."
fi

# ---------- helpers: sample tx bytes / temp / throttle / door fps ----------
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null || echo 0; }
temp_c(){ vcgencmd measure_temp 2>/dev/null | sed -E "s/temp=([0-9.]+).*/\1/"; }
throttle_bits(){ # live bits only (0..3): undervolt freqcap throttled templimit
  local v; v=$(vcgencmd get_throttled 2>/dev/null | sed 's/.*=//'); v=$((v))
  local o=""; (( v & 1 ))&&o+="undervolt "; (( v & 2 ))&&o+="freqcap "
  (( v & 4 ))&&o+="throttled "; (( v & 8 ))&&o+="templimit "; echo "${o:-none}"; }
door_fps(){ # signal_fps from the running watch (same command proven at the console); empty if n/a
  /home/askjitk/liftlab-b3/pi-agent/.venv/bin/python \
    /home/askjitk/liftlab-b3/pi-agent/watch_local.py status "$CH" 2>/dev/null \
    | grep -oE "'signal_fps': [0-9.]+" | head -1 | grep -oE "[0-9.]+$"; }
proc_cpu_pct(){ # avg %CPU of PID over the last window using /proc/PID/stat (can exceed 100)
  local pid=$1 w=$2; local hz; hz=$(getconf CLK_TCK)
  local a; a=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null||echo 0)
  sleep "$w"
  local b; b=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null||echo "$a")
  awk -v a="$a" -v b="$b" -v hz="$hz" -v w="$w" 'BEGIN{printf "%.0f",(b-a)/hz/w*100}'; }

# ---------- pick the ffmpeg video args for a mode ----------
enc_args(){
  case "$1" in
    copy) echo "-c:v copy";;
    hw)   echo "-c:v h264_v4l2m2m -b:v $SW_BR -pix_fmt yuv420p";;
    sw)   echo "-c:v libx264 -preset veryfast -tune zerolatency -b:v $SW_BR -pix_fmt yuv420p";;
  esac
}

# ---------- does hardware h264_v4l2m2m actually work here? ----------
hw_probe(){
  ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc=size=640x360:rate=15 \
    -frames:v 30 -c:v h264_v4l2m2m -f null - >/dev/null 2>&1 && return 0 || return 1
}

# ---------- run ONE mode: relay for DURATION, measure everything ----------
run_mode(){
  local mode="$1" args; args=$(enc_args "$mode")
  hr; say "MODE=$mode   ffmpeg video args: $args"
  local fps0; fps0=$(door_fps); say "door signal_fps BEFORE: ${fps0:-unavailable}"
  local logf="/tmp/live_relay_${mode}.fflog"
  # -re not needed (live source paces itself). HLS via PUT with Bearer, delete_segments = rolling.
  ffmpeg -nostdin -hide_banner -loglevel warning -stats \
    -rtsp_transport tcp -i "$RTSP_URL" -an $args \
    -g $((SEG_T*15)) -sc_threshold 0 \
    -f hls -hls_time "$SEG_T" -hls_list_size 5 \
    -hls_flags delete_segments+omit_endlist+program_date_time \
    -hls_segment_type mpegts -method PUT -http_persistent 1 \
    -headers "Authorization: Bearer ${GATEWAY_TOKEN}"$'\r\n' \
    -hls_segment_filename "$PUT_BASE/seg%03d.ts" "$PUT_BASE/index.m3u8" \
    >"$logf" 2>&1 &
  local pid=$!
  sleep 6                                            # startup grace: encoder open + first PUT
  if ! kill -0 "$pid" 2>/dev/null; then
    say "MODE=$mode FAILED to start (see below). Skipping."
    tail -6 "$logf" | sed 's/^/    ff| /'
    echo "$mode|FAILED|-|-|-|-|-|-" >> /tmp/live_relay_results.txt
    return 1
  fi
  say "relay up (pid $pid). Open in Chrome:  $CLOUD/live/$GW/$CAM   (basicauth)"
  # measurement window
  local tx0 t0 tmax=0 thr="none" fpsmin=99 cpus="" i n
  tx0=$(tx_bytes); t0=$(date +%s.%N)
  n=$(( DURATION / 5 )); [ "$n" -lt 1 ] && n=1
  for ((i=0;i<n;i++)); do
    kill -0 "$pid" 2>/dev/null || { say "ffmpeg died mid-run — see $logf"; break; }
    local c; c=$(proc_cpu_pct "$pid" 5)             # this sleeps 5s = the sample tick
    cpus+="$c "
    local tp; tp=$(temp_c); awk "BEGIN{exit !($tp>$tmax)}" && tmax=$tp
    local tb; tb=$(throttle_bits); [ "$tb" != none ] && thr="$tb"
    local fp; fp=$(door_fps); [ -n "$fp" ] && awk "BEGIN{exit !($fp<$fpsmin)}" && fpsmin=$fp
    printf "    t=%3ds  cpu=%3s%%  temp=%s'C  throttle=%-9s  door_fps=%s\n" \
      $(( (i+1)*5 )) "$c" "$tp" "$tb" "${fp:-?}"
  done
  local tx1 t1; tx1=$(tx_bytes); t1=$(date +%s.%N)
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  # results
  local mbps cpuavg dt
  dt=$(awk "BEGIN{print $t1-$t0}")
  mbps=$(awk -v a="$tx0" -v b="$tx1" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  cpuavg=$(echo "$cpus" | awk '{for(i=1;i<=NF;i++)s+=$i;printf "%.0f",NF?s/NF:0}')
  local ffbr; ffbr=$(grep -oE "bitrate=[0-9. ]+kbits/s" "$logf" | tail -1 | grep -oE "[0-9.]+" | head -1)
  local fps1; fps1=$(door_fps)
  say "MODE=$mode RESULT: uplink=${mbps}Mbps (total egress; relay dominates)  ff_bitrate=${ffbr:-?}kbps"
  say "   pi_cpu(ffmpeg)=${cpuavg}%  peak_temp=${tmax}'C  throttle_seen=$thr  door_fps min=${fpsmin} after=${fps1:-?}"
  echo "$mode|OK|$mbps|$cpuavg|$tmax|$thr|$fpsmin|${ffbr:-?}" >> /tmp/live_relay_results.txt
}

# ---------- drive the sweep ----------
: > /tmp/live_relay_results.txt
MODES=()
case "$MODE" in
  sweep)
    MODES+=(copy)     # always: measures the STEP-2 (VM-analysis) relay bandwidth regardless of codec
    if hw_probe; then MODES+=(hw); say "h264_v4l2m2m PROBE: works -> will test hw"; \
      else say "h264_v4l2m2m PROBE: NOT functional on this Pi -> skipping hw (would fall back to sw)"; fi
    MODES+=(sw);;
  copy) MODES+=(copy);;
  hw)   if hw_probe; then MODES+=(hw); else say "hw not functional; forcing sw"; MODES+=(sw); fi;;
  sw)   MODES+=(sw);;
esac
for m in "${MODES[@]}"; do run_mode "$m" || true; sleep 3; done

# ---------- report + extrapolation ----------
hr; say "SUMMARY  (source=$SRC_CODEC, ${DURATION}s/mode, iface=$IFACE, door watch running)"
printf "    %-6s %-7s %-9s %-9s %-8s %-11s %-9s %s\n" mode ok uplink cpu% peakT throttle doorFPS ff_kbps
while IFS='|' read -r m ok mb cpu tmax thr fmin ffbr; do
  [ -z "$m" ] && continue
  printf "    %-6s %-7s %-9s %-9s %-8s %-11s %-9s %s\n" "$m" "$ok" "${mb}Mbps" "${cpu}%" "${tmax}C" "$thr" "$fmin" "$ffbr"
done < /tmp/live_relay_results.txt
hr; say "EXTRAPOLATION TO 7-8 CAMS — copy and transcode answer DIFFERENT questions:"
COPY=$(awk -F'|' '$1=="copy"&&$2=="OK"{print; exit}' /tmp/live_relay_results.txt)
XCODE=$(awk -F'|' '($1=="hw"||$1=="sw")&&$2=="OK"{print; exit}' /tmp/live_relay_results.txt)  # prefer hw (listed first)

say "  [STEP 2 — analyse on the VM] read the COPY row (HEVC remux; no browser, VM decodes fine):"
if [ -n "$COPY" ]; then
  cmb=$(echo "$COPY"|cut -d'|' -f3); ccpu=$(echo "$COPY"|cut -d'|' -f4)
  awk -v mb="$cmb" -v cpu="$ccpu" 'BEGIN{
    printf "    copy: per-stream %.2f Mbps up, ffmpeg ~%s%% of one core (remux = near-free CPU)\n", mb, cpu;
    printf "    x8 uplink = %.1f Mbps SUSTAINED 24/7 (the REAL question — site uplink must carry this)\n", mb*8;
    printf "    x8 pi cpu = ~%d%% aggregate of 400%% — remux is cheap; the constraint is the network, not the Pi\n", cpu*8;
  }'
else say "    copy row missing/failed — check /tmp/live_relay_copy.fflog"; fi

say "  [DEMO — browser viewing] read the transcode row (HEVC->H264 so Chrome can play):"
if [ -n "$XCODE" ]; then
  xm=$(echo "$XCODE"|cut -d'|' -f1); xmb=$(echo "$XCODE"|cut -d'|' -f3); xcpu=$(echo "$XCODE"|cut -d'|' -f4)
  awk -v m="$xm" -v mb="$xmb" -v cpu="$xcpu" 'BEGIN{
    printf "    %s: per-stream %.2f Mbps up, ffmpeg ~%s%% of one core-equiv\n", m, mb, cpu;
    printf "    x8 pi cpu = ~%d%% aggregate of 400%%\n", cpu*8;
    if(cpu*8>300) print "    => transcode x8 SATURATES the Pi. Browser viewing of all 8 is not a Pi job — transcode on the VM.";
    else print "    => transcode x8 may fit on CPU, but check thermal below before believing it.";
  }'
  [ "$xm" = sw ] && say "    NOTE: this is libx264 (SW) — h264_v4l2m2m didn't engage; SW transcode x8 will not scale."
else say "    no transcode mode succeeded — Chrome viewing unproven; see the fflogs"; fi

say "  [BOTH] THERMAL: any row that set freqcap/templimit already fails at x8 (see occ 81C finding)."
say "  [BOTH] DOOR LOOP: if doorFPS dropped below ~10 or fired the fps alarm at x1, x8 is off the table."
hr
say "done. Viewer (transcode row only; HEVC copy shows black): $CLOUD/live/$GW/$CAM"
