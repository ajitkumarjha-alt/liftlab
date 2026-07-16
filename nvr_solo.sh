#!/usr/bin/env bash
# Measure each cabin's SOLO sub-stream bitrate (one at a time, uncontended) and store it as that
# camera's EXPECTED source rate. The cameras are mixed: ch16 is 720p30 (~1 Mbps), the six 4CIF/25
# subs are ~260-520 kbps. A FIXED kbps delivery floor is meaningless across them and falsely
# flagged 5/6 subs as "starved" for delivering 100% of what exists. relay_soak reads this and
# judges each stream as a FRACTION OF ITS OWN measured rate.
# RUN ON THE PI AS ROOT (relay stopped is cleanest but not required):
#   sudo bash nvr_solo.sh          -> writes /home/askjitk/liftlab-watch/nvr_solo.json
set -uo pipefail
ENVF=/etc/liftlab-agent.env; STREAM=2; PROBE_S="${PROBE_S:-15}"
OUT="${NVR_SOLO_JSON:-/home/askjitk/liftlab-watch/nvr_solo.json}"
say(){ echo "[nvr-solo] $*"; }
[ -r "$ENVF" ] || { echo "run as root"; exit 2; }
set -a; . "$ENVF"; set +a
: "${NVR_HOST:?}"; : "${GATEWAY_TOKEN:?}"; : "${CLOUD_URL:?}"
CLOUD="${CLOUD_URL%/}"; GW="${GW:-${GATEWAY_ID:-site-A}}"
USER_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_USER','admin'),safe=''))")
PASS_ENC=$(python3 -c "import os,urllib.parse as u;print(u.quote(os.environ.get('NVR_PASS',os.environ.get('NVR_PASSWORD','')),safe=''))")
# same channels the relay uses (channel_map source of truth)
J=$(curl -s --max-time 8 -H "Authorization: Bearer $GATEWAY_TOKEN" "$CLOUD/api/gw/$GW/lift_channels" 2>/dev/null)
CHANS=($(printf '%s' "$J" | grep -oE '"channels":\[[0-9,]*\]' | grep -oE '[0-9]+'))
[ "${#CHANS[@]}" -gt 0 ] || CHANS=(27 28 29 30 32 33 34)
say "probing ${#CHANS[@]} channels solo, ${PROBE_S}s each: ${CHANS[*]}"
mkdir -p "$(dirname "$OUT")"
tmp=$(mktemp); entries=""
for ch in "${CHANS[@]}"; do
  cam="ch$ch"
  ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp \
    -i "rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam" \
    -an -c:v copy -t "$PROBE_S" -f mpegts "$tmp" >/dev/null 2>&1 || true
  sz=$(stat -c%s "$tmp" 2>/dev/null||echo 0); rm -f "$tmp"
  kbps=$(awk -v s="$sz" -v t="$PROBE_S" 'BEGIN{printf "%.0f",s*8/t/1000}')
  # resolution/fps for the record
  res=$(ffprobe -v error -rtsp_transport tcp -select_streams v:0 -show_entries stream=width,height,avg_frame_rate \
        -of csv=p=0 "rtsp://${USER_ENC}:${PASS_ENC}@${NVR_HOST}:554/${ch}/${STREAM}?transmode=unicast&profile=vam" 2>/dev/null | tr ',' 'x')
  say "  $cam: ${kbps} kbps  ${res:-?}"
  entries+="\"$cam\":$kbps,"
done
echo "{${entries%,}}" > "$OUT"
say "wrote $OUT:"; cat "$OUT" | sed 's/^/    /'
say "relay_soak now judges each stream vs 70% of its own rate (RELAY_DELIVER_FRAC)."
