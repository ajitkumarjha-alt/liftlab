#!/usr/bin/env bash
# LOCALIZE the relay one-winner starvation. The overnight soak: 7 subs, streams_delivering 1-2/7,
# sum ~2.2 Mbps, link mostly IDLE, first stream (ch16) owns the pipe for 9.5h. Plain continuous
# curl PUT saturates at ~9.6. So it's REQUEST-LATENCY under concurrency, not bandwidth. This runs
# graded experiments, each 7-concurrent, to pin WHERE:
#
#   pull      : 7 ffmpeg copy SUB -> LOCAL files (NO PUT). Even growth => NVR pull is fine for 7;
#               the bug is the PUT/relay side, not the source.
#   stream    : 7 curl CONTINUOUS big PUT -> /blackhole. (your test) link + streaming transport.
#   seg-bh    : 7 curl LOOPED small (~225KB) PUT -> /blackhole. per-request overhead + transport,
#               NO disk. If this starves but 'stream' didn't => the many-small-PUTs PATTERN is it.
#   seg-live  : 7 curl LOOPED small PUT -> real /live/.../seg.ts. adds handler write+prune+_STATS.
#               starves vs seg-bh => the handler. fine but ffmpeg starves => ffmpeg's PUT muxer.
#
# INTERPRETATION:
#   pull even + stream even + seg-bh even + seg-live even, yet ffmpeg starves => 100% ffmpeg's
#     HLS-over-HTTP PUT behaviour (connection reuse / playlist re-PUT / segment lockstep). Fix =
#     ship a CONTINUOUS transport per stream, mux HLS on the VM (plain curl already proved that path).
#   seg-* starve while stream is even => per-request serialization at Caddy/uvicorn/handler.
#   pull uneven => the NVR rations concurrent pulls (unlikely; main ramp pulled 7 at 8.5).
#
# RUN ON THE PI AS ROOT, WITH liftlab-relay STOPPED:  sudo systemctl stop liftlab-relay
#   sudo bash relay_diag.sh
#   sudo MODE=seg-live DUR=300 bash relay_diag.sh          # one experiment
set -uo pipefail
N="${N:-7}"; DUR="${DUR:-300}"; PULL_DUR="${PULL_DUR:-60}"; MODE="${MODE:-all}"
ENVF="${ENVF:-/etc/liftlab-agent.env}"; STREAM=2
say(){ echo "[relay-diag] $*"; }
hr(){ printf '%s\n' "----------------------------------------------------------------"; }
[ -r "$ENVF" ] || { echo "run as root (needs $ENVF)"; exit 2; }
set -a; . "$ENVF"; set +a
: "${NVR_HOST:?}"; : "${GATEWAY_TOKEN:?}"; : "${CLOUD_URL:?}"
CLOUD="${CLOUD_URL%/}"; GW="${GW:-${GATEWAY_ID:-site-A}}"
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); [ -n "$IFACE" ] || IFACE=eth0
# channels (7)
J=$(curl -s --max-time 8 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null)
CHANS=($(printf '%s' "$J" | grep -oE '"channels":\[[0-9,]*\]' | grep -oE '[0-9]+'))
[ "${#CHANS[@]}" -gt 0 ] || CHANS=(27 28 29 30 32 33 34)
CHANS=("${CHANS[@]:0:N}")
say "N=$N iface=$IFACE dur=${DUR}s channels=${CHANS[*]}  (stop liftlab-relay first!)"
BIG=/tmp/diag_big; SEG=/tmp/diag_seg
dd if=/dev/zero of="$BIG" bs=1M count=8 status=none
dd if=/dev/zero of="$SEG" bs=1K count=225 status=none    # ~one sub segment
tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }

report(){ # $1=label ; reads /tmp/diag_s_* (per-stream bytes) + tx delta via globals TX0/TX1/DT
  local label=$1 sum=0 mn="" mx=0 per=""
  for ((i=0;i<N;i++)); do
    local b; b=$(cat "/tmp/diag_s_$i" 2>/dev/null||echo 0)
    local m; m=$(awk -v b="$b" -v dt="$DT" 'BEGIN{printf "%.2f",b*8/dt/1e6}')
    sum=$(awk -v s="$sum" -v m="$m" 'BEGIN{printf "%.2f",s+m}'); per+="$m "
    awk "BEGIN{exit !($m>$mx)}" && mx=$m; [ -z "$mn" ] && mn=$m; awk "BEGIN{exit !($m<$mn)}" && mn=$m
  done
  local txm; txm=$(awk -v a="$TX0" -v b="$TX1" -v dt="$DT" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  [ -z "$mn" ] && mn=0
  local verdict
  verdict=$(awk -v sum="$sum" -v mn="$mn" -v mx="$mx" 'BEGIN{
    even=(mx>0 && mn/mx>=0.6)?"EVEN":"ONE-WINNER";
    sat=(sum>=8.0)?"SATURATED(~link)":((sum<4.0)?"LINK IDLE(request-bound)":"partial");
    printf "%s + %s", sat, even}')
  printf "  %-9s aggregate=%.2f Mbps (iface_tx=%.2f)  per-stream: %s\n           min=%s max=%s => %s\n" \
    "$label:" "$sum" "$txm" "$per" "$mn" "$mx" "$verdict"
}

# ---------- pull: 7 ffmpeg copy -> LOCAL, no PUT ----------
exp_pull(){
  hr; say "PULL: 7 ffmpeg copy SUB -> local files, ${PULL_DUR}s (isolates NVR pull from the PUT)"
  rm -rf /tmp/diag_pull; mkdir -p /tmp/diag_pull; local pids=()
  for ((i=0;i<N;i++)); do
    local ch=${CHANS[$i]}
    ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp \
      -i "rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam" \
      -an -c:v copy -f mpegts -t "$PULL_DUR" "/tmp/diag_pull/ch${ch}.ts" >/dev/null 2>&1 &
    pids+=($!)
  done
  DT="$PULL_DUR"; sleep "$((PULL_DUR+3))"; for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
  for ((i=0;i<N;i++)); do stat -c%s "/tmp/diag_pull/ch${CHANS[$i]}.ts" 2>/dev/null > "/tmp/diag_s_$i" || echo 0 > "/tmp/diag_s_$i"; done
  TX0=0; TX1=0; report "pull"
  say "  (each ~${PULL_DUR}s of sub @ ~900kbps should be ~$((PULL_DUR*900/8/1000))MB if the NVR serves it fully)"
  rm -rf /tmp/diag_pull
}

# ---------- curl experiments ----------
run_curl(){ # $1=idx $2=deadline $3=mode(stream|seg-bh|seg-live)
  local i=$1 deadline=$2 mode=$3 tot=0 n=0 blob url b
  case "$mode" in
    stream)   blob=$BIG; url="$CLOUD/api/gw/$GW/blackhole" ;;
    seg-bh)   blob=$SEG; url="$CLOUD/api/gw/$GW/blackhole" ;;
    seg-live) blob=$SEG ;;
  esac
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$mode" = seg-live ] && url="$CLOUD/api/gw/$GW/live/chdiag${i}/seg$((n%20)).ts"
    b=$(curl -s -o /dev/null --max-time "$DUR" -w '%{size_upload}' -T "$blob" \
         -H "Authorization: Bearer $GATEWAY_TOKEN" "$url" 2>/dev/null)
    [ -n "$b" ] && tot=$(( tot + b )); n=$((n+1))
  done
  echo "$tot" > "/tmp/diag_s_$i"
}
exp_curl(){ local mode=$1
  hr; say "$mode: $N concurrent, ${DUR}s"
  rm -f /tmp/diag_s_*; TX0=$(tx_bytes); local t0; t0=$(date +%s); local dl=$((t0+DUR))
  for ((i=0;i<N;i++)); do run_curl "$i" "$dl" "$mode" & done
  wait; TX1=$(tx_bytes); DT=$(( $(date +%s) - t0 )); [ "$DT" -lt 1 ] && DT=1
  report "$mode"
}

case "$MODE" in
  all)     exp_pull; exp_curl stream; exp_curl seg-bh; exp_curl seg-live ;;
  pull)    exp_pull ;;
  stream|seg-bh|seg-live) exp_curl "$MODE" ;;
esac
hr
say "READ THE PATTERN:"
say "  pull EVEN => NVR fine, bug is the PUT side.   stream SATURATED => link/transport streaming fine."
say "  seg-bh IDLE/one-winner (but stream fine) => the many-small-PUTs PATTERN starves (per-request)."
say "  seg-live worse than seg-bh => the handler. seg-live FINE but ffmpeg starves => ffmpeg's PUT muxer."
say "  If seg-* are all fine + even, the fix is NOT segmentation — look at ffmpeg conn reuse (next)."
rm -f "$BIG" "$SEG"; say "done."
