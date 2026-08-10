"""Gateway-side slow-request log — the latency signal that must survive the async POST queue.

WHY THIS SHIPS BEFORE THE QUEUE. Today the workers feel gateway latency directly: it lands in
`post=` in the seg-timing line and as `SLOW POST` in the worker journal, and that is the only reason
anyone noticed 0.3-10s responses at all. The async POST queue removes exactly that coupling — after
it lands, a sick gateway stops hurting the workers and therefore stops being visible from them. The
signal has to move to the gateway BEFORE we stop feeling it, or the queue quietly converts a
diagnosable problem into an invisible one.

WHAT IT MEASURES. Handler wall time per request: the interval this process spent producing the
response. That is deliberately NOT the same number the worker sees — the worker's includes DNS, TCP,
TLS, both network legs and any time queued in front of the app. Comparing the two IS the diagnosis:

    worker SLOW POST 5237ms + gateway slow 4900ms  -> the app (or what it waits on) is slow
    worker SLOW POST 5237ms + gateway slow    40ms  -> the network, TLS, or a queue in front
                                                       of the app (Caddy, uvicorn backlog)

Neither number alone can tell those apart, which is why this is additive rather than a replacement.

DELIBERATELY BORING. One monotonic clock read per request, a comparison, and a print on the slow
path only. No storage, no new endpoint, no dependency, nothing that can itself become a source of
latency on the fast path.
"""
from __future__ import annotations

import os
import time

# Threshold in SECONDS. 1.0 by default: the observed bad cases are 0.3-10s, and a 1s handler on this
# workload is already pathological — the fast path is single-digit milliseconds.
SLOW_S = float(os.environ.get("SLOWLOG_S", "1.0"))
# Sampling for the fast path is NOT implemented on purpose: nothing is logged below the threshold,
# so the common case costs one subtraction and one comparison.


def _log(msg):
    print(f"[slowlog] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


async def slow_request_middleware(request, call_next):
    """Starlette/FastAPI HTTP middleware. Mount with:

        from slowlog import slow_request_middleware
        app.middleware("http")(slow_request_middleware)

    Logs only requests whose HANDLER took >= SLOWLOG_S, with the endpoint, method, status and
    duration. The path is logged WITHOUT its query string: gateway ids and camera names are already
    in the path, and a query string can carry an operator's filter values into a log nobody has
    reviewed for content.
    """
    t0 = time.monotonic()
    status = "?"
    try:
        response = await call_next(request)
        status = getattr(response, "status_code", "?")
        return response
    finally:
        dt = time.monotonic() - t0
        if dt >= SLOW_S:
            # request.url.path, not str(request.url): no query string.
            _log(f"SLOW {request.method} {request.url.path} -> {status} in {dt * 1000:.0f}ms "
                 f"(threshold {SLOW_S * 1000:.0f}ms)")


def banner():
    """Called at startup so the threshold in force is in the journal, not only in the env."""
    _log(f"slow-request logging armed: handler wall time >= {SLOW_S * 1000:.0f}ms is logged "
         f"(endpoint + method + status + duration). Set SLOWLOG_S to change.")
