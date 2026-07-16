#!/usr/bin/env bash
# DIAGNOSTIC ONLY. Measure each cabin's SOLO sub-stream bitrate (one at a time) to SEE the mixed
# camera configs: ch16 720p30 (~1 Mbps) vs six 4CIF/25 subs (~260-520 kbps). NOTE these rates
# DRIFT run-to-run — HEVC bitrate is scene-dependent (empty cabin ~nothing, busy cabin more), e.g.
# ch27 0.51->0.23, ch34 0.52->0.19 between runs. So a STATIC per-camera threshold is fragile;
# relay_soak does NOT use this file — it self-calibrates each stream against a rolling EMA of its
# OWN recent rate. This probe is just for eyeballing the configs.
# RUN ON THE PI AS ROOT:  sudo bash nvr_solo.sh   (writes /home/askjitk/liftlab-watch/nvr_solo.json)
# FILES: nvr_solo.sh (no other deps)
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
say "wrote $OUT (informational):"; cat "$OUT" | sed 's/^/    /'
say "NOTE: these drift with cabin activity; relay_soak self-calibrates and does NOT read this file."
