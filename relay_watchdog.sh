#!/usr/bin/env bash
# VM-side relay watchdog. Belt-and-suspenders on top of the HARD tmpfs cap: if the live store
# nears the cap OR liftlab-cloud RSS climbs, drop a .reject flag so live_put sheds segments
# (503) BEFORE anything can threaten the door-event ingest. Clears the flag when back to normal.
# Runs as the liftlab-relay-guard systemd service. Logs to journald.
set -uo pipefail
LIVE_DIR="${LIVE_DIR:-/dev/shm/liftlab-live}"
CAP_MB="${CAP_MB:-200}"                 # must match the tmpfs mount size
STORE_TRIP_MB="${STORE_TRIP_MB:-$(( CAP_MB * 90 / 100 ))}"   # trip at 90% of cap
RSS_TRIP_MB="${RSS_TRIP_MB:-900}"       # liftlab-cloud RSS ceiling
INTERVAL="${INTERVAL:-15}"
FLAG="$LIVE_DIR/.reject"
say(){ echo "[relay-guard] $(date -u +%FT%TZ) $*"; }

cloud_rss_mb(){ # RSS of the uvicorn/main:app process, MB
  local pid; pid=$(pgrep -f 'uvicorn.*main:app' | head -1)
  [ -n "$pid" ] || { echo 0; return; }
  awk '/VmRSS/{printf "%d",$2/1024}' "/proc/$pid/status" 2>/dev/null || echo 0
}
store_mb(){ du -sm "$LIVE_DIR" 2>/dev/null | awk '{print $1+0}'; }

mkdir -p "$LIVE_DIR"
say "watchdog up: cap=${CAP_MB}MB store_trip=${STORE_TRIP_MB}MB rss_trip=${RSS_TRIP_MB}MB every ${INTERVAL}s"
tripped=0
while :; do
  s=$(store_mb); r=$(cloud_rss_mb)
  if [ "$s" -ge "$STORE_TRIP_MB" ] || [ "$r" -ge "$RSS_TRIP_MB" ]; then
    if [ "$tripped" = 0 ]; then
      : > "$FLAG"; tripped=1
      say "GUARD ON (store=${s}MB rss=${r}MB) -> .reject set; live_put now sheds segments (503)."
    fi
  else
    if [ "$tripped" = 1 ]; then
      rm -f "$FLAG"; tripped=0
      say "GUARD OFF (store=${s}MB rss=${r}MB) -> .reject cleared; accepting segments again."
    fi
  fi
  sleep "$INTERVAL"
done
