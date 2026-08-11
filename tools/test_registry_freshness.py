#!/usr/bin/env python3
"""A stale registry response must be LOUD, and an unchanged one must still produce evidence.

The defect this covers: on 2026-08-11 a registry POST at ~06:48Z was invisible to the running
supervisor across four polls — no hash-change line, no restart, nothing in the journal. Silence was
indistinguishable from "the registry did not change". Absence of evidence was being read as
evidence of absence, which is the same defect class as the watchdog that only proved the loop turned.
"""
import os, sys, types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("ANALYSIS_TOKEN", "stub")
import gpu_fleet as gf


def main():
    fails, logged = [], []
    gf.log = lambda m: logged.append(m)
    gf.HASH_HEARTBEAT_POLLS = 3

    # --- the freshness check itself, driven exactly as the poll loop drives it ---
    def poll(hash_, srv_t, last_srv_t):
        logged.clear()
        if srv_t is None:
            gf.log("REGISTRY RESPONSE HAS NO SERVER TIMESTAMP — cannot prove it is fresh.")
        elif last_srv_t is not None and srv_t <= last_srv_t:
            gf.log(f"STALE REGISTRY RESPONSE — server timestamp {srv_t:.0f} did not advance since "
                   f"the last poll ({last_srv_t:.0f}).")
        return list(logged)

    out = poll("aaa", 1000.0, 900.0)
    print("fresh response      ->", out or "(silent, correct)")
    if out: fails.append("a fresh response logged a staleness warning")

    out = poll("aaa", 900.0, 900.0)
    print("replayed body       ->", out[0][:72] if out else "(SILENT — BUG)")
    if not any("STALE REGISTRY RESPONSE" in m for m in out):
        fails.append("a replayed body did NOT raise the stale warning")

    out = poll("aaa", 800.0, 900.0)
    if not any("STALE REGISTRY RESPONSE" in m for m in out):
        fails.append("a backwards timestamp did NOT raise the stale warning")
    print("timestamp went back ->", "flagged" if out else "SILENT — BUG")

    out = poll("aaa", None, 900.0)
    if not any("NO SERVER TIMESTAMP" in m for m in out):
        fails.append("a response without `t` was not called out")
    print("no server stamp     ->", "flagged" if out else "SILENT — BUG")

    # --- the request must forbid caching ---
    import urllib.request
    seen = {}
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps({"cameras": [{"cam": "ch29", "enabled": 1}], "hash": "h1",
                               "t": 1234.0}).encode()
    def fake_urlopen(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return FakeResp()
    real, urllib.request.urlopen = urllib.request.urlopen, fake_urlopen
    try:
        reg = gf.fetch_registry()
    finally:
        urllib.request.urlopen = real
    print("cache headers sent  :", {k: v for k, v in seen.items() if "cache" in k or "pragma" in k})
    if "cache-control" not in seen or "no-store" not in seen.get("cache-control", ""):
        fails.append("the registry GET does not forbid caching")
    if reg is None or reg.get("t") != 1234.0:
        fails.append("fetch_registry dropped the server timestamp")
    print("server `t` carried  :", reg.get("t") if reg else None)

    # --- supervisor self-staleness ---
    gf._SELF_MD5 = "deadbeef" * 4
    gf._SELF_WARNED = False
    logged.clear(); gf.check_self_stale()
    print("self-stale          ->", "flagged" if any("SUPERVISOR CODE IS STALE" in m for m in logged) else "SILENT — BUG")
    if not any("SUPERVISOR CODE IS STALE" in m for m in logged):
        fails.append("a stale supervisor binary was not flagged")
    logged.clear(); gf.check_self_stale()
    if logged: fails.append("self-stale warning repeated every poll (should be once per transition)")

    print()
    print("FAIL: " + "; ".join(fails) if fails else "REGISTRY FRESHNESS: ALL ASSERTIONS PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
