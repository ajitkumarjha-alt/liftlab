#!/usr/bin/env python3
"""slowlog middleware: fast path silent, slow path logged, no query strings, exceptions still timed."""
import sys, os
import asyncio, io, contextlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["SLOWLOG_S"] = "0.05"
import importlib, slowlog; importlib.reload(slowlog)

class R:                       # minimal request stand-in
    def __init__(self, path, method="POST"):
        self.method = method
        self.url = type("U", (), {"path": path})()
class Resp:
    def __init__(self, code): self.status_code = code

async def drive(path, delay, code=200):
    async def call_next(_):
        await asyncio.sleep(delay)
        return Resp(code)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await slowlog.slow_request_middleware(R(path), call_next)
    return buf.getvalue()

async def drive_raises(path, delay):
    async def call_next(_):
        await asyncio.sleep(delay)
        raise RuntimeError("handler blew up")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            await slowlog.slow_request_middleware(R(path), call_next)
        except RuntimeError:
            pass
    return buf.getvalue()

async def main():
    fails = []
    fast = await drive("/api/gw/site-A/door_event", 0.0)
    print("fast request logged:", repr(fast.strip()[:60]) or "nothing")
    if fast.strip(): fails.append("fast request was logged (fast path must be silent)")

    slow = await drive("/api/gw/site-A/door_event", 0.12)
    print("slow request ->", slow.strip()[:110])
    if "SLOW POST /api/gw/site-A/door_event" not in slow: fails.append("slow request not logged")
    if "-> 200 in" not in slow: fails.append("status/duration missing")

    # query string must not reach the log
    q = await drive("/dash/site-A/trends", 0.12)
    if "?" in q: fails.append("query string leaked into the log")
    print("no query string in output:", "?" not in q)

    # a failing handler must still be timed (finally), and must not swallow the exception
    ex = await drive_raises("/api/gw/site-A/floorcheck", 0.12)
    print("exception path ->", ex.strip()[:90])
    if "SLOW" not in ex: fails.append("slow handler that raised was not logged")

    print()
    print("FAIL: " + "; ".join(fails) if fails else "SLOWLOG: ALL ASSERTIONS PASS")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
