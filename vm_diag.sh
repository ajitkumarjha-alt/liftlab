#!/usr/bin/env bash
# VM-side serialization diagnosis for the relay one-winner bug. RUN AS ROOT ON THE VM.
# Read-only; changes nothing.
echo "=== 1) uvicorn service — worker count? (no --workers = ONE worker) ==="
systemctl cat liftlab-cloud 2>/dev/null | grep -iE 'ExecStart|workers|environment' || echo "  (service not found?)"
echo
echo "=== 2) is the app one process or many? ==="
pgrep -af 'uvicorn|main:app' || echo "  (no uvicorn process matched)"
echo
echo "=== 3) uvicorn / anyio / starlette / fastapi versions ==="
/opt/liftlab-b3/cloud/.venv/bin/python - <<'PY' 2>/dev/null || echo "  (venv python not found)"
import uvicorn, anyio, starlette, fastapi
print(f"  uvicorn {uvicorn.__version__}  anyio {anyio.__version__}  starlette {starlette.__version__}  fastapi {fastapi.__version__}")
try:
    import anyio.to_thread as t
    # default anyio threadpool capacity (Starlette run_in_threadpool uses this)
    print("  note: Starlette runs `def` endpoints + run_in_threadpool in the anyio threadpool (default 40).")
    print("        `async def` endpoints doing BLOCKING I/O run ON the loop and serialize.")
except Exception as e:
    print("  (anyio introspection skipped:", e, ")")
PY
echo
echo "=== 4) Caddy — rate limit / connection cap / how /api/gw is handled ==="
CF=$(ls /etc/caddy/Caddyfile 2>/dev/null || ls /etc/caddy/conf.d/* 2>/dev/null | head -1)
if [ -n "$CF" ]; then
  echo "  file: $CF"
  grep -niE 'rate_limit|ratelimit|max_|concurren|reverse_proxy|api/gw|basicauth|lb_|transport|keepalive|flush' "$CF" 2>/dev/null | sed 's/^/    /' || echo "    (no matches)"
else
  echo "  no Caddyfile at /etc/caddy — check: systemctl cat caddy | grep -i config"
fi
echo
echo "=== 5) current connections into the app port (9090) ==="
ss -tnp 2>/dev/null | grep -E ':9090' | awk '{print $1,$5,$6}' | sort | uniq -c | sort -rn | head
echo
echo "READ:"
echo "  - ExecStart has no --workers  => ONE uvicorn worker. Fine for async, FATAL if a handler"
echo "    blocks the loop (our old live_put did sync writes on the loop — now off-loaded)."
echo "  - Adding --workers is NOT the fix here: the in-memory _STATS delivery counter would split"
echo "    across workers and undercount. Keep 1 worker + non-blocking handlers."
echo "  - If Caddy shows rate_limit / a small max_conns / request body buffering on /api/gw, that"
echo "    can serialize PUTs regardless of the app. Report those lines."