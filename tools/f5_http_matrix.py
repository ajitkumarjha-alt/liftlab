#!/usr/bin/env python3
"""HTTP matrix for /dash/{gw}/trends — run this BEFORE any deploy of dash_api.py.

WHY THIS EXISTS. Commit 23e56aa shipped an UnboundLocalError on `t0` that returned HTTP 500 on
EVERY call to this endpoint, and the equivalence harness that "proved" the change had passed. It
passed because it called `_tier2` and `_aggregate_read` as functions and never executed
`dash_trends` at all — so a use-before-assignment forty lines into that handler was invisible to it.
Python does not catch unbound locals statically; only running the code does.

So this harness does the one thing that harness could not: it starts the REAL app and makes REAL
HTTP requests across the whole parameter surface. A single non-200 fails the run.

  python3 tools/f5_http_matrix.py            # uses DASH_DIR / GATEWAY_DB from env
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_DIR = os.environ.get("DASH_DIR", "/tmp/dashsrc")
GW = os.environ.get("DASH_GW", "site-A")
CAM = os.environ.get("MATRIX_CAM", "ch27")
PY = os.environ.get("MATRIX_PY", sys.executable)

# PRODUCTION-LIKE ENV. dev-box had DASH_DOOR_GUARD_TS unset while production has it set, and that
# difference already showed up once as a phantom field diff. A proof that runs in a materially
# different configuration from production is proving something else.
GUARD_TS = os.environ.get("DASH_DOOR_GUARD_TS", "2026-07-25T02:13:00Z")


def free_port(start=18400):
    import socket
    for p in range(start, start + 60):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("no free port")


def main():
    port = free_port()
    app_py = os.path.join(APP_DIR, "_matrix_app.py")
    with open(app_py, "w") as fh:
        fh.write(
            "import sys, os\n"
            f"sys.path.insert(0, {APP_DIR!r})\n"
            "from fastapi import FastAPI\n"
            "import dash_api\n"
            "app = FastAPI()\n"
            "app.include_router(dash_api.dash_router)\n"
            "sys.stderr.write('MATRIX-MODULE %s\\n' % dash_api.__file__)\n")
    env = dict(os.environ)
    env.update({"DASH_DOOR_GUARD_TS": GUARD_TS, "PYTHONPATH": APP_DIR})
    log = open("/tmp/.matrix_uvicorn.log", "wb")
    proc = subprocess.Popen([PY, "-m", "uvicorn", "_matrix_app:app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"],
                            cwd=APP_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2).read()
                break
            except Exception:
                if proc.poll() is not None:
                    print("app died on startup:"); print(open("/tmp/.matrix_uvicorn.log").read()[-2000:])
                    return 1
                time.sleep(0.5)
        else:
            print("app never bound"); return 1

        print(f"DASH_DOOR_GUARD_TS={GUARD_TS}   app={APP_DIR}   db={os.environ.get('GATEWAY_DB')}")
        print(f"{'cam':>6} {'period':>7} {'from_d':>11} {'to_d':>11} {'era':>10} "
              f"{'HTTP':>5} {'secs':>7} {'tier2_source':>13} {'expect':>7}  verdict")
        print("-" * 108)

        cases = []
        for cam in ("", CAM):
            for period in ("", "all", "week", "day"):
                cases.append((cam, period, "", "", ""))
            cases.append((cam, "", "2026-07-30", "2026-08-04", ""))     # explicit range
            cases.append((cam, "all", "", "", "425f92e1"))              # era override
            cases.append((cam, "week", "2026-08-01", "", ""))           # half-open range
        fails = 0
        for cam, period, fd, td, era in cases:
            q = [f"cam={cam}", f"period={period}", f"from_d={fd}", f"to_d={td}", f"era={era}"]
            url = f"http://127.0.0.1:{port}/dash/{GW}/trends?" + "&".join(q)
            # DESIGNED behaviour: tier2 exists only for a named camera. It comes from the cache only
            # when nothing narrows or re-scopes the window: no era override, no explicit dates, and
            # a period the default window covers. Everything else must derive live.
            if not cam:
                expect = "none"
            elif era or fd or td or period not in ("", "all", "week"):
                expect = "live"
            else:
                expect = "cache"
            a = time.time()
            try:
                with urllib.request.urlopen(url, timeout=300) as r:
                    body = r.read(); code = r.status
            except urllib.error.HTTPError as e:
                body = e.read(); code = e.code
            except Exception as e:
                body = str(e).encode(); code = 0
            el = time.time() - a
            src = "-"
            ok_json = False
            try:
                d = json.loads(body); ok_json = True
                src = str(d.get("tier2_source"))
                if d.get("tier2_range") is None and not cam:
                    src = "none" if d.get("tier2_source") is None else src
            except Exception:
                pass
            bad = []
            if code != 200: bad.append(f"HTTP {code}")
            if not ok_json: bad.append("unparseable JSON")
            if code == 200 and ok_json:
                if expect == "none" and src not in ("none", "None"): bad.append(f"src={src}")
                elif expect in ("cache", "live") and src not in (expect, "live"):
                    bad.append(f"src={src} not {expect}")
                elif expect == "cache" and src == "live":
                    bad.append("MISS: expected cache, got live")
            verdict = "ok" if not bad else "FAIL " + "; ".join(bad)
            if bad:
                fails += 1
                snippet = body[:200].decode("utf-8", "replace").replace("\n", " ")
                verdict += f"  <<{snippet}>>"
            print(f"{cam or '(fleet)':>6} {period or '(none)':>7} {fd or '-':>11} {td or '-':>11} "
                  f"{era or '-':>10} {code:>5} {el:>7.2f} {src:>13} {expect:>7}  {verdict}")
        print("-" * 108)
        print(f"{len(cases)} combinations, {fails} failed")
        if fails:
            print("\nuvicorn log tail:"); print(open("/tmp/.matrix_uvicorn.log").read()[-3000:])
        return 1 if fails else 0
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except Exception: proc.kill()


if __name__ == "__main__":
    sys.exit(main())
