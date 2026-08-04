#!/usr/bin/env bash
# INGEST GATE — do the ingest handlers actually WORK, against the STAGED code, before install?
#
# WHY THIS EXISTS. On 2026-08-04 apply_gwingest.sh smoke-IMPORTED ops_api, built the router, printed
# "smoke ok", and installed. The handler body contained `_f(...)` — a helper that lives in
# door_event_api.py, not ops_api.py. Python resolves names at CALL time, not import time, so the
# import gate could not see it. Every POST /api/gw/{gw}/relay_status then returned 500 with
# `NameError: name '_f' is not defined` for six minutes.
#
# That was the fourth failure of one shape in a week:
#   `bash -n` passes            != the script starts
#   the module imports          != the handler runs
# A gate must exercise the thing it claims to verify. This one CALLS every producer-facing ingest
# handler with a realistic payload and checks the row landed.
#
# HOW IT RUNS PRE-INSTALL. A scratch uvicorn is started from the STAGED directory (that dir first on
# PYTHONPATH), pointed at a scratch SQLite file and scratch token env. Nothing touches the live app,
# the live DB, or the installed files. FastAPI's TestClient would be tidier but needs httpx, which
# is not installed on the gateway — and a deploy gate must not install packages on a production box
# to run itself.
#
# TWO ASSERTIONS PER ENDPOINT, both required:
#   1. HTTP 2xx                       — the handler ran to completion
#   2. the row is in the scratch DB   — it actually wrote. A handler that returns 200 and silently
#                                       swallows the write is the same defect class as _q() returning
#                                       [] on OperationalError: a success code over missing data.
#
# Run: bash smoke_ingest.sh <staged-dir> [app-dir]
set -uo pipefail
STAGED="${1:?usage: smoke_ingest.sh <staged-dir> [app-dir]}"
APP="${2:-/opt/liftlab-b3/cloud}"
PY="$APP/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)
GW=site-A
TOK=smoketoken

TMP=$(mktemp -d); trap 'rm -rf "$TMP"; [ -n "${UPID:-}" ] && kill -9 "$UPID" 2>/dev/null' EXIT
DB="$TMP/gw.db"
PASS=0; FAIL=0
ok(){  echo "  PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

echo "ingest gate: staged=$STAGED  app=$APP  scratch_db=$DB"

# ── the app, built from the STAGED modules ───────────────────────────────────
cat > "$TMP/smoke_app.py" <<'PYAPP'
import os, sys
sys.path.insert(0, os.environ["SMOKE_STAGED"])   # staged code WINS over the installed copy
sys.path.insert(1, os.environ["SMOKE_APP"])
from fastapi import FastAPI
import ops_api, door_event_api, analysis_api
app = FastAPI()
app.include_router(ops_api.ops_router)
app.include_router(door_event_api.door_event_router)
app.include_router(analysis_api.analysis_router)
for m in (ops_api, door_event_api, analysis_api):
    sys.stderr.write("SMOKE-MODULE %s -> %s\n" % (m.__name__, m.__file__))
PYAPP

# a free high port
PORT=0
for p in $(seq 18081 18120); do
  (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && exec 3<&- || { PORT=$p; break; }
done
[ "$PORT" != 0 ] || { echo "  FAIL  no free port in 18081-18120"; exit 2; }

SMOKE_STAGED="$STAGED" SMOKE_APP="$APP" \
GATEWAY_DB="$DB" GATEWAY_TOKENS="$GW:$TOK" ANALYSIS_TOKENS="$GW:$TOK" \
LIVE_DIR="$TMP/live" SNAP_DIR="$TMP/snap" CALIB_DIR="$TMP/calib" \
VALIDATION_IMG_DIR="$TMP/vimg" TEMPLATES_DIR="$TMP/tpl" LIFTLAB_DATA="$TMP" \
PYTHONPATH="$TMP:$STAGED:$APP" \
  "$PY" -m uvicorn smoke_app:app --host 127.0.0.1 --port "$PORT" --log-level warning \
  > "$TMP/uvicorn.log" 2>&1 &
UPID=$!

READY=""
for i in $(seq 1 60); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/openapi.json" && { READY=1; break; }
  kill -0 "$UPID" 2>/dev/null || break
  sleep 0.5
done
if [ -z "$READY" ]; then
  echo "  FAIL  scratch app never started — the staged code does not even import."
  echo "  ---- uvicorn output ----"; tail -25 "$TMP/uvicorn.log" | sed 's/^/    /'
  exit 1
fi
grep '^SMOKE-MODULE' "$TMP/uvicorn.log" | sed 's/^/  /'
# Prove we are testing the STAGED file, not the installed one — the exact mistake that made the
# original import gate vacuous.
if grep -q "SMOKE-MODULE ops_api -> $STAGED" "$TMP/uvicorn.log"; then
  ok "modules loaded from the STAGED dir (not the installed copy)"
else
  bad "loaded the INSTALLED modules — this gate would be testing the wrong code"
fi

TS=$(date +%s)
post(){   # $1=label  $2=path  $3=json  $4=table  $5=where
  local code n
  code=$(curl -s -o "$TMP/resp" -w '%{http_code}' --max-time 20 \
         -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
         -d "$3" "http://127.0.0.1:$PORT$2")
  if [ "${code:0:1}" = 2 ]; then ok "$1: HTTP $code"; else
    bad "$1: HTTP $code"; sed 's/^/      /' "$TMP/resp" 2>/dev/null | head -4
    grep -iE 'Traceback|Error' -A 6 "$TMP/uvicorn.log" | tail -12 | sed 's/^/      /'
    return
  fi
  # 2xx is not enough: prove the row is really there.
  n=$(sqlite3 "$DB" "SELECT COUNT(*) FROM $4 WHERE $5;" 2>/dev/null || echo 0)
  if [ "${n:-0}" -ge 1 ]; then ok "$1: row landed in $4 ($n)"; else
    bad "$1: HTTP 2xx but NO ROW in $4 — handler returned success and swallowed the write"
  fi
}

echo
echo "== producer-facing ingest endpoints =="

# relay_status — shaped from relay_soak.sh's real payload, including the two fields added 2026-08-04
post "relay_status (Pi heartbeat)" "/api/gw/$GW/relay_status" \
  "{\"sum_delivered_mbps\":3.4,\"streams_alive\":7,\"streams_delivering\":7,\"ff_cpu\":41.2,\"soc_temp\":54.1,\"throttle_live\":\"0x0\",\"mem_avail_mb\":1801,\"door_fps\":null,\"guard_trips\":0,\"stall_restarts\":2,\"per_stream\":{\"ch16\":441,\"ch27\":237,\"ch29\":172},\"guard_state\":\"ok\",\"guard_floor\":null,\"fleet_down\":false,\"fleet_zero_for_s\":0,\"fleet_stage\":0,\"tx_packets\":1756828,\"last_reboot_epoch\":0,\"first_segment_est_s\":8,\"grace_s\":420,\"channel_source\":\"channel_map\",\"stream_states\":{}}" \
  relay_status "gateway_id='$GW'"

# door_event — shaped from gpu_analyze.post_door_event
post "door_event (GPU door engine)" "/api/gw/$GW/door_event" \
  "{\"cam\":\"ch29\",\"ts\":$TS,\"floor\":\"12\",\"direction\":\"up\",\"door_state\":\"open\",\"openness\":0.94,\"read_conf\":0.81,\"panels_agreed\":false,\"reason\":\"single_panel\",\"candidates\":null,\"close_travel_s\":2.15,\"door_version\":\"260d4a0fh2Laa52+495e8f48\",\"templates_hash\":\"260d4a0f\",\"n_arrow_labels\":2}" \
  gw_door_event "gateway_id='$GW' AND cam='ch29'"

# floorcheck — shaped from gpu_analyze.post_floorcheck (crop omitted; the b64 path is optional)
post "floorcheck (sampled read)" "/api/gw/$GW/floorcheck" \
  "{\"cam\":\"ch29\",\"ts\":$TS,\"floor\":\"12\",\"direction\":\"up\",\"read_conf\":0.81,\"panels_agreed\":false,\"reason\":\"single_panel\",\"door_version\":\"260d4a0fh2Laa52+495e8f48\",\"crop_jpeg_b64\":null}" \
  floor_sample "gateway_id='$GW' AND cam='ch29'"

# transit — shaped from gpu_analyze's transit POST
post "transit (counting engine)" "/api/gw/$GW/transit" \
  "{\"cam\":\"ch29\",\"ts\":$TS,\"direction\":\"in\",\"track_id\":4242}" \
  transit_event "gateway_id='$GW' AND cam='ch29'"

# analyzer_status — shaped from gpu_analyze's heartbeat
post "analyzer_status (GPU worker)" "/api/gw/$GW/analyzer_status" \
  "{\"cam\":\"ch29\",\"counting_version\":\"2026-07-28-registry-zones\",\"zones\":\"registry\",\"uptime_s\":3600,\"segments\":1200,\"dropped\":8,\"posted\":140,\"last_transit_ts\":$TS,\"mode\":\"live\",\"door_opens_since_transit\":0,\"s_since_transit_post\":12,\"hist_rate_hr\":40.0,\"proc_ms\":1800,\"seg_budget_ms\":2000,\"drop_frac\":0.0}" \
  analyzer_status "gateway_id='$GW' AND cam='ch29'"

echo
echo "== handler-body faults must FAIL this gate =="
# A NameError inside a handler is invisible to an import check. Confirm the gate is actually
# sensitive to one by checking the scratch log for the signature we would expect.
if grep -qiE "NameError|Traceback" "$TMP/uvicorn.log"; then
  bad "a handler raised during this run — see above"
else
  ok "no handler raised during any request"
fi

kill "$UPID" 2>/dev/null; UPID=""
echo
echo "== ingest gate: $PASS passed, $FAIL failed =="
[ "$FAIL" = 0 ]
