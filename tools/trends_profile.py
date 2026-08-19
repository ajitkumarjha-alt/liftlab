#!/usr/bin/env python3
"""Where do the seconds in /dash/{gw}/trends actually go — measured on a real database.

WHY THIS AND NOT A GUESS. /trends took 29 s on the PRE-RTT build at 1.81M door rows, so the cost is
in the trends queries themselves and not in anything RTT added. Reasoning about which query is slow
from reading SQL is how the last three wrong diagnoses happened; this runs the REAL dash_trends and
times every statement it issues, with EXPLAIN QUERY PLAN beside each one, so the answer is a
measurement.

It instruments the function rather than reimplementing its queries: a profiler that re-types the SQL
measures the profiler's idea of the endpoint, which is exactly the mistake the RTT cache fixture
made when it passed a cache key production never uses.

READ-ONLY. Point it at a copy if you want certainty:
    sudo cp /var/lib/liftlab/gateway.db /tmp/gw_copy.db && sudo chown "$USER" /tmp/gw_copy.db
    python3 tools/trends_profile.py --db /tmp/gw_copy.db --cam ch29

The plan lines are the diagnosis. Read them for:
    SCAN <table>                     -- no index used at all
    USE TEMP B-TREE FOR ORDER BY     -- the ORDER BY is sorting in memory, not walking the index
    SEARCH ... USING INDEX ix (a=? AND b=?)   -- note WHICH columns; anything not listed there is a
                                                post-filter evaluated on every row the index returns
"""
import argparse
import os
import cProfile
import pstats
import sqlite3
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CALLS = []          # (sql, args, ms, nrows)


def _norm(sql):
    return " ".join(sql.split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db"))
    ap.add_argument("--gw", default="site-A")
    ap.add_argument("--cam", default="ch29", help="'' for the fleet view")
    ap.add_argument("--period", default="all")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    os.environ["GATEWAY_DB"] = a.db
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api as D

    # INSTRUMENT AT ONE LAYER ONLY. Timing _q as well double-counted every statement it issues
    # (_q calls db.execute), which reported 14.9s of query time inside a 9.6s request and a "4 calls"
    # for a statement that ran twice. A profiler that inflates the thing it measures is worse than
    # no profiler: it would have sent the fix at the wrong query.
    # ── and _db, so anything issued through db.execute directly is caught too ────────────────
    # sqlite3.Connection.execute is read-only, so this proxies rather than patches. Statements that
    # bypass _q (the aggregate reads, the era resolution) would otherwise be invisible, and an
    # invisible statement is exactly where a 29-second endpoint hides its cost.
    _orig_db = D._db

    class _Traced:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, args=()):
            t = time.time()
            cur = self._con.execute(sql, args)
            rows = None
            if sql.lstrip()[:6].upper() == "SELECT":
                rows = cur.fetchall()
                cur = _Rows(rows)
            ms = (time.time() - t) * 1000.0
            if "EXPLAIN" not in sql.upper():
                CALLS.append((_norm(sql), list(args), ms, len(rows) if rows is not None else -1))
            return cur

        def __getattr__(self, k):
            return getattr(self._con, k)

    class _Rows(list):
        def fetchall(self):
            return list(self)

        def fetchone(self):
            return self[0] if self else None

    D._db = lambda: _Traced(_orig_db())

    print(f"db     {a.db}  ({os.path.getsize(a.db) / 1e6:.0f} MB)")
    probe = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    for t in ("gw_door_event", "transit_event", "gw_event", "validation_item"):
        try:
            n = probe.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:18s} {n:>10,} rows")
        except sqlite3.OperationalError:
            print(f"  {t:18s} (absent)")
    print("\nindexes:")
    for r in probe.execute("SELECT tbl_name, name, sql FROM sqlite_master WHERE type='index' "
                           "AND tbl_name IN ('gw_door_event','transit_event','gw_event','gw_source',"
                           "'validation_item') ORDER BY tbl_name"):
        print(f"  {r[0]:18s} {r[1]:22s} {(r[2] or '(auto)').split('(', 1)[-1].rstrip(')')}")

    # PROFILE THE DERIVATION, NOT THE CACHE READ. dash_trends now serves a precomputed payload, so
    # timing it would report 2 ms and say nothing about why the payload takes as long as it does.
    fn = getattr(D, "_trends_compute", None) or D.dash_trends
    print(f"\n=== running {fn.__name__}(cam={a.cam!r}, period={a.period!r}) ===")
    prof = cProfile.Profile()
    t0 = time.time()
    prof.enable()
    try:
        fn(a.gw, cam=a.cam, period=a.period)
    except Exception as e:                       # a slow endpoint may also be a broken one
        print(f"  RAISED {type(e).__name__}: {e}")
    prof.disable()
    total = time.time() - t0
    sql_ms = sum(c[2] for c in CALLS)
    # THE SPLIT THAT WAS MISSING. This reported only SQL time, so an endpoint spending 30 s walking
    # rows in Python would have shown a fast query list and no explanation. Row VOLUME is the cost
    # the era filter cannot remove, and volume is spent in Python.
    print(f"  wall {total:.2f}s   SQL {sql_ms/1000:.2f}s across {len(CALLS)} statements   "
          f"Python {max(total - sql_ms/1000, 0):.2f}s")
    if total > 0.2 and sql_ms / 1000 < total * 0.6:
        print("  >>> MOST OF THE TIME IS NOT IN SQL. Top Python frames by cumulative time:")
        st = pstats.Stats(prof)
        st.sort_stats("cumulative")
        rows = [(f, st.stats[f]) for f in st.stats]
        rows.sort(key=lambda kv: -kv[1][3])
        shown = 0
        for (fname, lineno, func), (_cc, _nc, _tt, ct, _cal) in rows:
            if "dash_api" not in fname and "rtt_core" not in fname:
                continue
            print(f"      {ct:7.2f}s cumulative  {func}  ({os.path.basename(fname)}:{lineno})")
            shown += 1
            if shown >= 8:
                break
    print()

    # ── ranked, with the plan for each ──────────────────────────────────────────────────────
    agg = {}
    for sql, args, ms, n in CALLS:
        e = agg.setdefault(sql, {"ms": 0.0, "n": 0, "rows": 0, "args": args})
        e["ms"] += ms
        e["n"] += 1
        e["rows"] += max(n, 0)
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["ms"])

    print(f"{'ms':>9} {'calls':>6} {'rows':>9}  statement")
    print("-" * 100)
    for sql, e in ranked[:a.top]:
        share = e["ms"] / (total * 1000.0) * 100 if total else 0
        print(f"{e['ms']:9.1f} {e['n']:6d} {e['rows']:9,}  ({share:4.1f}%) {sql[:150]}")
        try:
            for p in probe.execute("EXPLAIN QUERY PLAN " + sql, e["args"]):
                flag = ""
                d = p[3]
                if d.startswith("SCAN"):
                    flag = "   <-- NO INDEX"
                elif "TEMP B-TREE" in d:
                    flag = "   <-- SORTING IN MEMORY"
                print(f"{'':9} {'':6} {'':9}      plan: {d}{flag}")
        except sqlite3.OperationalError as ex:
            print(f"{'':9} {'':6} {'':9}      plan: (unavailable: {ex})")
        print()

    probe.close()
    print("Anything marked NO INDEX or SORTING IN MEMORY is a fix with a known shape. A SEARCH line "
          "that\nnames fewer columns than the WHERE clause means the rest are post-filters — they "
          "cost one\nevaluation per row the index returns, which is why a selective-looking query "
          "can still be slow.")


if __name__ == "__main__":
    main()
