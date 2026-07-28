#!/usr/bin/env python3
"""Fetch-path probe v2 — RTT hypothesis test + fixed reuse gauge (urllib3 2.x).

v1's timings showed reuse WORKS (cold ~600ms then flat ~250ms in every pattern) but its
num_connections gauge read 0: on urllib3 2.x / requests 2.3x, connection_from_url() keys a FRESH
pool (different TLS-context key), so v1 was reading an empty pool. v2 counts actual socket
creation by wrapping HTTPSConnectionPool._new_conn — version-proof ground truth.

The unifying hypothesis v2 tests: the warm ~250ms floor is ONE cross-region RTT
(asia-southeast1 GPU -> asia-south1 VM; last night's bare curl connect was 0.228s = 1 RTT), not a
client or server defect. Measures:
  1. raw TCP connect x3 (no TLS, no HTTP) — the path RTT, full stop
  2. who serves playlist vs segment (Server header: Caddy = file_server, uvicorn = app fall-through)
  3. warm fetch floor on BOTH playlist and a real segment, with the fixed socket counter

    ANALYSIS_TOKEN=<token> python3 fetch_reuse_probe.py
"""
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3
import urllib3.connectionpool as _cp

CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
HOST = CLOUD.split("://", 1)[1].split("/")[0]
GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
BASE = f"{CLOUD}/api/gw/{GW}/live/{CAM}"
PREFETCH_N = int(os.environ.get("PREFETCH_N", "2"))

tok = os.environ.get("ANALYSIS_TOKEN", "")
if ":" in tok and tok.split(":", 1)[0] == GW:
    tok = tok.split(":", 1)[1]
if not tok:
    sys.exit("need ANALYSIS_TOKEN")
HDRS = {"Authorization": "Bearer " + tok}

# ---- fixed gauge: count actual socket creations, immune to pool-key internals ----
_SOCKETS = [0]
_orig_new_conn = _cp.HTTPSConnectionPool._new_conn


def _counted_new_conn(self):
    _SOCKETS[0] += 1
    return _orig_new_conn(self)


_cp.HTTPSConnectionPool._new_conn = _counted_new_conn


def make_session():
    s = requests.Session()
    s.headers.update({**HDRS, "Connection": "keep-alive"})
    ad = requests.adapters.HTTPAdapter(pool_connections=4,
                                       pool_maxsize=max(4, PREFETCH_N + 2), max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def fetch(sess, url):
    t0 = time.time()
    r = sess.get(url, timeout=15, stream=True)
    t1 = time.time()
    body = r.content
    r.close()
    return len(body), (t1 - t0) * 1000, (time.time() - t1) * 1000, r.headers.get("Server", "?")


print(f"requests {requests.__version__}  urllib3 {urllib3.__version__}  target {BASE}\n")

print("— 1. raw TCP connect (no TLS, no HTTP): the path RTT —")
for i in range(3):
    t0 = time.time()
    s = socket.create_connection((HOST, 443), timeout=10)
    ms = (time.time() - t0) * 1000
    s.close()
    print(f"  tcp connect #{i+1}: {ms:6.1f}ms")
print("  (~1 RTT each. If this reads ~200ms+, the warm fetch floor is PHYSICS — cross-region")
print("   placement — and no client/server software change can lower it.)")

print("\n— 2+3. who serves what, warm floors, socket count —")
sess = make_session()
before = _SOCKETS[0]
blen, hdr, body, server = fetch(sess, f"{BASE}/index.m3u8")
print(f"  playlist #1 (cold): headers {hdr:6.1f}ms body {body:5.1f}ms ({blen}B)  Server: {server}")
seg = None
try:
    txt = sess.get(f"{BASE}/index.m3u8", timeout=15).text
    seg = next((ln.strip() for ln in txt.splitlines() if ln.strip().endswith(".ts")), None)
except Exception as e:
    print(f"  playlist parse failed: {e}")
for i in range(3):
    blen, hdr, body, server = fetch(sess, f"{BASE}/index.m3u8")
    print(f"  playlist warm #{i+1}: headers {hdr:6.1f}ms body {body:5.1f}ms  Server: {server}")
if seg:
    for i in range(3):
        blen, hdr, body, server = fetch(sess, f"{BASE}/{seg}")
        print(f"  segment  warm #{i+1}: headers {hdr:6.1f}ms body {body:5.1f}ms ({blen//1024}KB)  Server: {server}")
print(f"  sockets created this session: {_SOCKETS[0] - before} "
      f"(1 = full reuse; one per fetch = none)")

print("\n— threaded shape (3 workers, 6 segment fetches, one session) —")
before = _SOCKETS[0]
target = f"{BASE}/{seg}" if seg else f"{BASE}/index.m3u8"
with ThreadPoolExecutor(max_workers=3) as ex:
    for f in [ex.submit(fetch, sess, target) for _ in range(6)]:
        blen, hdr, body, server = f.result()
        print(f"  threaded: headers {hdr:6.1f}ms body {body:5.1f}ms  Server: {server}")
print(f"  sockets created: {_SOCKETS[0] - before} (<=3 with reuse across 3 threads)")
sess.close()

print("\nREADING:")
print("  Server: Caddy on both  -> livering covers playlist AND segments; app fully shed.")
print("  Server: uvicorn on playlist only -> playlist falls through (matcher/regex gap) — fixable.")
print("  warm headers ≈ tcp RTT -> transport floor is the path; fetch cost is placement physics;")
print("    levers = deeper prefetch (hide latency) or co-region the GPU (remove it). Session is fine.")
print("  warm headers >> tcp RTT with Server: Caddy -> genuine server-side cost, investigate Caddy.")
