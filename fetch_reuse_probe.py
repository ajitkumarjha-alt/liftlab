#!/usr/bin/env python3
"""Why does the worker's pooled session handshake on EVERY fetch? Reproduce and count.

Runs gpu_analyze's EXACT session recipe (same headers, same HTTPAdapter pool sizing) against the
real endpoint and reads urllib3's own counter of connections CREATED (pool.num_connections):
after N sequential fetches, 1 = reuse works, N = every fetch opened a socket. Three body-handling
patterns are compared, because the difference between them IS the shortlist of suspects:

  exact      stream=True -> r.content -> r.close()     (what gpu_analyze does today)
  noclose    stream=True -> r.content                  (consumption auto-releases; is close() the bug?)
  plain      get() with no stream                      (requests' default happy path)

Then the exact pattern again under a 3-thread pool (the prefetch shape), to catch thread-affinity
or pool-eviction effects that sequential runs hide. Read-only; a handful of playlist GETs.

    ANALYSIS_TOKEN=<token> python3 fetch_reuse_probe.py
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
URL = f"{CLOUD}/api/gw/{GW}/live/{CAM}/index.m3u8"
PREFETCH_N = int(os.environ.get("PREFETCH_N", "2"))

tok = os.environ.get("ANALYSIS_TOKEN", "")
if ":" in tok and tok.split(":", 1)[0] == GW:
    tok = tok.split(":", 1)[1]
if not tok:
    sys.exit("need ANALYSIS_TOKEN")
HDRS = {"Authorization": "Bearer " + tok}


def make_session():
    """gpu_analyze's recipe, verbatim."""
    s = requests.Session()
    s.headers.update({**HDRS, "Connection": "keep-alive"})
    ad = requests.adapters.HTTPAdapter(pool_connections=4,
                                       pool_maxsize=max(4, PREFETCH_N + 2), max_retries=0)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def conns(sess):
    """urllib3's count of sockets CREATED for this host's pool — the ground truth of reuse."""
    return sess.get_adapter(URL).poolmanager.connection_from_url(URL).num_connections


def fetch_exact(sess):
    t0 = time.time()
    r = sess.get(URL, timeout=15, stream=True)
    t1 = time.time()
    body = r.content
    r.close()
    return len(body), (t1 - t0) * 1000, (time.time() - t1) * 1000


def fetch_noclose(sess):
    t0 = time.time()
    r = sess.get(URL, timeout=15, stream=True)
    t1 = time.time()
    body = r.content
    return len(body), (t1 - t0) * 1000, (time.time() - t1) * 1000


def fetch_plain(sess):
    t0 = time.time()
    r = sess.get(URL, timeout=15)
    return len(r.content), (time.time() - t0) * 1000, 0.0


def run(name, fn, n=4):
    s = make_session()
    times = []
    for i in range(n):
        blen, hdr_ms, body_ms = fn(s)
        times.append(hdr_ms)
        print(f"  {name} #{i+1}: headers {hdr_ms:6.1f}ms body {body_ms:5.1f}ms ({blen}B)")
    c = conns(s)
    verdict = "REUSE OK" if c == 1 else f"NO REUSE ({c} sockets for {n} fetches)"
    print(f"  {name}: connections created = {c} -> {verdict}")
    s.close()
    return c


print(f"requests {requests.__version__}  urllib3 {urllib3.__version__}  target {URL}\n")
print("— sequential, per pattern (fresh session each) —")
c_exact = run("exact  ", fetch_exact)
c_noclose = run("noclose", fetch_noclose)
c_plain = run("plain  ", fetch_plain)

print("\n— exact pattern under the prefetch shape (3 threads, 6 fetches, one session) —")
s = make_session()
with ThreadPoolExecutor(max_workers=3) as ex:
    for f in [ex.submit(fetch_exact, s) for _ in range(6)]:
        blen, hdr_ms, body_ms = f.result()
        print(f"  threaded: headers {hdr_ms:6.1f}ms body {body_ms:5.1f}ms")
c_thr = conns(s)
print(f"  threaded: connections created = {c_thr} (<= 3 expected with reuse; 6 = none)")
s.close()

print("\nVERDICT:")
if c_exact > 1 and c_plain == 1:
    print("  the EXACT pattern breaks reuse and plain get() does not -> the stream/close handling is")
    print("  the bug on this requests/urllib3 stack; fix = change http_get_timed's body handling.")
elif c_exact > 1 and c_noclose == 1:
    print("  r.close() is the bug (consumption already released the connection; close() kills it).")
elif c_exact == 1 and c_thr > 3:
    print("  sequential reuse OK but the THREADED shape churns sockets -> pool/thread interaction.")
elif c_exact == 1:
    print("  reuse works HERE — the worker's env differs (proxy vars? another session recipe drift?);")
    print("  compare `systemctl show liftlab-gpu-fleet -p Environment` for *_proxy, and the installed")
    print("  gpu_analyze.py's session block against the branch.")
else:
    print("  no pattern reuses -> stack-level (adapter/urllib3) — send this full output back.")
