#!/usr/bin/env bash
# PURE-LINK concurrency test — NO ffmpeg, NO HLS, NO disk. N parallel HTTPS PUTs of a fixed
# blob to the VM /blackhole sink, ramped. Isolates the LINK + Caddy + uvicorn from the relay.
#
# WHY: the sub ramp collapsed to 1.83 Mbps with ONE winner (ch16 ~1Mbps, rest ~200kbps) even
# though the link demonstrably carried 8.5 Mbps of main. One-winner = SERIALIZATION, not a
# bandwidth ceiling. This test answers: is the wall the LINK, or the VM/relay?
#   - aggregate scales to ~6-8 Mbps, streams roughly EVEN  -> link is fine; bug is VM/relay-side
#     (the async live_put blocking, or ffmpeg's PUT handling). Re-ramp with the handler fix.
#   - aggregate ALSO collapses / one-winner                 -> wall is below the app: Caddy,
#     uvicorn accept loop, or wifi. Then run vm_diag.sh + check Caddy.
#
# RUN ON THE PI AS ROOT:  sudo bash link_probe.sh
#   sudo SZ_MB=8 DUR=25 STEPS_REQ="1 2 4 7" bash link_probe.sh
set -uo pipefail
ENVF="${ENVF:-/etc/liftlab-agent.env}"
SZ_MB="${SZ_MB:-8}"; DUR="${DUR:-25}"; STEPS_REQ="${STEPS_REQ:-1 2 4 7}"
say(){ echo "[linkprobe] $*"; }
hr(){ printf '%s\n' "----------------------------------------------------------------"; }
[ -r "$ENVF" ] || { echo "cannot read $ENVF — run as root"; exit 2; }
set -a; . "$ENVF"; set +a
: "${GATEWAY_TOKEN:?}"; : "${CLOUD_URL:?}"
CLOUD="${CLOUD_URL%/}"; GW="${GW:-${GATEWAY_ID:-site-A}}"
URL="$CLOUD/api/gw/$GW/blackhole"
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); [ -n "$IFACE" ] || IFACE=eth0
BLOB=/tmp/lp_blob
dd if=/dev/zero of="$BLOB" bs=1M count="$SZ_MB" status=none
say "target=$URL  iface=$IFACE  blob=${SZ_MB}MB  window=${DUR}s/step  steps=$STEPS_REQ"
# sanity: does the sink exist + accept our token?
probe=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -T "$BLOB" -H "Authorization: Bearer $GATEWAY_TOKEN" "$URL")
say "single-PUT sanity: HTTP $probe (200 => sink live; 404 => redeploy live_api.py; 401 => token)"
[ "$probe" = 200 ] || { say "sink not ready — aborting"; exit 1; }
hr

tx_bytes(){ cat "/sys/class/net/$IFACE/statistics/tx_bytes" 2>/dev/null||echo 0; }
run_stream(){ # $1=idx $2=deadline_epoch  -> loops PUTs, writes total uploaded bytes to /tmp/lp_tot_$1
  local i=$1 deadline=$2 tot=0 b
  while [ "$(date +%s)" -lt "$deadline" ]; do
    b=$(curl -s -o /dev/null --max-time "$DUR" -w '%{size_upload}' -T "$BLOB" \
         -H "Authorization: Bearer $GATEWAY_TOKEN" "$URL" 2>/dev/null)
    [ -n "$b" ] && tot=$(( tot + b ))
  done
  echo "$tot" > "/tmp/lp_tot_$i"
}

: > /tmp/lp_results.txt
for N in $STEPS_REQ; do
  rm -f /tmp/lp_tot_*
  local_tx0=$(tx_bytes); t0=$(date +%s); deadline=$(( t0 + DUR ))
  for ((i=0;i<N;i++)); do run_stream "$i" "$deadline" & done
  wait
  t1=$(date +%s); local_tx1=$(tx_bytes); dt=$(( t1 - t0 )); [ "$dt" -lt 1 ] && dt=1
  # aggregate two ways: iface tx (authoritative) and sum of per-stream uploaded bytes
  txmbps=$(awk -v a="$local_tx0" -v b="$local_tx1" -v dt="$dt" 'BEGIN{printf "%.2f",(b-a)*8/dt/1e6}')
  per=""; summb=0; mn=""; mx=0
  for ((i=0;i<N;i++)); do
    tb=$(cat "/tmp/lp_tot_$i" 2>/dev/null || echo 0)
    m=$(awk -v tb="$tb" -v dt="$dt" 'BEGIN{printf "%.2f",tb*8/dt/1e6}')
    summb=$(awk -v s="$summb" -v m="$m" 'BEGIN{printf "%.2f",s+m}')
    awk "BEGIN{exit !($m>$mx)}" && mx=$m; [ -z "$mn" ] && mn=$m; awk "BEGIN{exit !($m<$mn)}" && mn=$m
    per+="$m "
  done
  [ -z "$mn" ] && mn=0
  printf "%d|%s|%s|%s|%s\n" "$N" "$txmbps" "$summb" "$mn" "$mx" >> /tmp/lp_results.txt
  say "N=$N  iface_tx=${txmbps}Mbps  sum_streams=${summb}Mbps  per-stream Mbps: $per (min=$mn max=$mx)"
  hr
done

say "SUMMARY (pure HTTPS PUT to /blackhole — no ffmpeg, no disk):"
printf "    %-4s %-12s %-12s %-9s %s\n" N iface_tx sum_streams min_Mbps max_Mbps
while IFS='|' read -r n tx sm mn mx; do [ -z "$n" ] && continue
  printf "    %-4s %-12s %-12s %-9s %s\n" "$n" "${tx}Mbps" "${sm}Mbps" "$mn" "$mx"; done < /tmp/lp_results.txt
hr
VERDICT=$(awk -F'|' 'NR==1{base=$2} END{ratio=(base>0?$2/(base*$1)*100:0);
  even=($5>0 && $4/$5>=0.5)?"even":"ONE-WINNER";
  printf "at %s streams: %.2f Mbps (%d%% of linear), distribution=%s",$1,$2,ratio,even}' /tmp/lp_results.txt)
say "=> $VERDICT"
say "READ IT:"
say "  scales ~linearly + even  => LINK IS FINE. The sub-ramp collapse is VM/relay-side."
say "                              Redeploy live_api.py (handler now off-loads writes) + re-ramp."
say "  collapses / one-winner   => wall is BELOW the app (Caddy / uvicorn accept loop / wifi)."
say "                              Run vm_diag.sh on the VM; check Caddy for connection caps."
rm -f "$BLOB"