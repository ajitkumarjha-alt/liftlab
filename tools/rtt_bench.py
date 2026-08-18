#!/usr/bin/env python3
"""What does an RTT walk actually cost ON THIS DATABASE — run it on the live file, not a snapshot.

WHY THIS EXISTS. The in-process cache measured 0 ms on a 285k-row fixture and 9 s on the live box.
Not because the live database is bigger: because the fixture called _rtt_by_cam with an explicit
(None, None) range, which is a STABLE cache key, and the endpoints call it with
`t0 = time.time() - days*86400` — a different float on every request. The fixture exercised the
cache in a way production never does, so it proved the cache worked and could not see that it never
once fired. This tool calls through the endpoint path, so its cache numbers are the real ones.

It is READ-ONLY. Point it at a copy anyway if you want to be sure:
    sudo cp /var/lib/liftlab/gateway.db /tmp/gw_copy.db && sudo chown "$USER" /tmp/gw_copy.db
    python3 tools/rtt_bench.py --db /tmp/gw_copy.db --cam ch29

It answers three questions and nothing else:
  1. how many door rows are in the camera's current era over the default range — and, separately,
     how many rows the query has to SCAN to find them, which is the number that sets the cost;
  2. what the walk costs per 10k rows scanned, across window widths, so the live cost is a
     measurement rather than an extrapolation from mine;
  3. whether the cache fires when called the way the endpoints call it.
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load(db_path):
    """Import dash_api with fastapi stubbed if it is not installed (it is, on the VM)."""
    os.environ["GATEWAY_DB"] = db_path
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api
    return dash_api


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db"))
    ap.add_argument("--gw", default="site-A")
    ap.add_argument("--cam", default="ch29")
    ap.add_argument("--windows", default="1,2,4,7,14,30",
                    help="window widths in days; 'all' for unbounded")
    a = ap.parse_args()

    D = _load(a.db)
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    era, era_how = D._era_for(db, a.gw, a.cam)
    print(f"db     {a.db}")
    print(f"cam    {a.cam}   era prefix {era!r}  ({era_how})")
    print(f"default window: {D.WINDOW_DAYS:g}d      RTT_MAX_ROWS={D.RTT_MAX_ROWS}")

    # ── 1. rows in the era vs rows SCANNED to find them ───────────────────────────────────────
    # The index is (gateway_id, cam, ts). `door_version LIKE 'prefix%'` is NOT part of it, so it is
    # evaluated as a filter on every row the ts range returns. A camera whose current era is one day
    # old still pays for every row in the window.
    print("\n-- rows: used vs scanned --")
    plan = [r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM gw_door_event WHERE gateway_id=? AND cam=? "
        "AND door_version LIKE ? AND ts>=?", (a.gw, a.cam, era + "%", time.time() - 86400))]
    for p in plan:
        print("   plan:", p)

    widths = [None if w.strip() == "all" else float(w) for w in a.windows.split(",")]
    now = time.time()
    print(f"\n{'window':>8} {'scanned':>10} {'in era':>9} {'walk ms':>9} {'ms/10k scanned':>15} {'trips':>7}")
    for d in widths:
        D._RTT_CACHE.clear()
        t0 = None if d is None else now - d * 86400.0
        w, args = D._ts_clause(t0, None)
        args = list(args)
        scanned = db.execute(
            f"SELECT COUNT(*) c FROM gw_door_event WHERE gateway_id=? AND cam=? {w}",
            [a.gw, a.cam] + args).fetchone()["c"]
        used = db.execute(
            f"SELECT COUNT(*) c FROM gw_door_event WHERE gateway_id=? AND cam=? "
            f"AND door_version LIKE ? {w}", [a.gw, a.cam, era + "%"] + args).fetchone()["c"]
        s = time.time()
        rt, err = D._rtt_by_cam(db, a.gw, [a.cam], t0, None, "")
        ms = (time.time() - s) * 1000.0
        row = (rt or {}).get(a.cam) or {}
        per10k = (ms / scanned * 10000.0) if scanned else 0.0
        lbl = "all" if d is None else f"{d:g}d"
        print(f"{lbl:>8} {scanned:10d} {used:9d} {ms:9.1f} {per10k:15.1f} "
              f"{row.get('n_trips', '-' if not err else 'ERR'):>7}")
        if err:
            print(f"         refused: {err}")

    # ── 2. does the cache fire, called the way the endpoints call it ──────────────────────────
    # THIS is the test the fixture could not perform. dash_rtt and dash_trends both build t0 from
    # _window(days), which is time.time()-relative, so three identical user requests produce three
    # different cache keys and three full walks.
    print("\n-- three IDENTICAL requests, built the way the endpoints build them --")
    D._RTT_CACHE.clear()
    for i in range(3):
        t0, t1, _ = D._window(None)           # exactly what dash_rtt/dash_trends do
        s = time.time()
        D._rtt_by_cam(db, a.gw, [a.cam], t0, t1, "")
        ms = (time.time() - s) * 1000.0
        print(f"   request {i + 1}: {ms:9.1f} ms    distinct cache keys now = {len(D._RTT_CACHE)}")
    n = len(D._RTT_CACHE)
    print(f"   -> {n} keys from 3 identical requests. 1 = the cache works; "
          f"{'>1 = it never fires and every request is a full walk' if n > 1 else 'good'}")

    # ── 3. is RTT on any shared request path ──────────────────────────────────────────────────
    src = open(os.path.join(ROOT, "dash_api.py"), encoding="utf-8").read()
    print("\n-- request paths that compute RTT --")
    for fn in ("dash_data", "dash_trends", "dash_rtt"):
        i = src.find(f"def {fn}(")
        if i < 0:
            print(f"   {fn:12s} (absent)")
            continue
        j = src.find("\n@", i)
        body = src[i:j if j > 0 else len(src)]
        print(f"   {fn:12s} {'COMPUTES RTT' if '_rtt_by_cam(' in body else 'clean'}")
    print("\n   Only a single-camera, explicitly-requested endpoint should say COMPUTES RTT — and "
          "\n   even that one should be reading a precomputed row, not walking.")


if __name__ == "__main__":
    main()
