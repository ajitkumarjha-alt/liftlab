#!/usr/bin/env python3
"""/trends serves a precomputed payload, never derives one, and its "not computed" shape is complete.

WHY. /trends was 29s before RTT and 37.7s after the era range rewrite and ix_door_era — the index is
used, the plan is right, and the page still cannot load. The era filter only helps in proportion to
what it EXCLUDES, and a week-old era is most of what a camera holds; row volume is the residual
cost and no index removes rows the answer needs. So the derivation moved to the precompute timer.

The three things that must hold, and the third is the one that already bit:

  1. a cacheable request NEVER calls _trends_compute — that is the 37s coming back;
  2. a deliberate question (custom range, era override, hour filter) still derives, because those
     are not what the page loads and not what was timing out;
  3. the not-computed payload is STRUCTURALLY COMPLETE. The first version returned
     {'profile': [], 'windows': {}} and the client died on TR.boundaries.close_travel_max.iso and
     windows.all_day. A payload that omits keys is not a smaller answer, it is a crash — and it
     would have shipped as "the trends view is broken after deploy", indistinguishable from the
     timeout it was meant to fix.
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))

DV = "260d4a0fh3-stateT5471cb+495e8f48"
SCHEMA = """
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, floor TEXT, direction TEXT, door_state TEXT, read_conf REAL, panels_agreed INTEGER,
  reason TEXT, close_travel_s REAL, door_version TEXT);
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
  marked_at REAL);
CREATE TABLE analyzer_status (gateway_id TEXT, cam TEXT, counting_version TEXT, ts REAL);
CREATE TABLE transit_event (gateway_id TEXT, cam TEXT, ts REAL, direction TEXT,
  counting_version TEXT, track_id TEXT, ts_bucket INTEGER);
"""


def build(path, now):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO channel_map VALUES ('site-A',29,1,'lift 3',0)")
    for k in range(400):
        t = now - 3600 * 24 + k * 60
        for st in ("open", "closing", "closed"):
            con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,door_version)"
                        " VALUES ('site-A','ch29',?,?,?,?)", (t, "G", st, DV))
            t += 3
        con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id,ts_bucket) "
                    "VALUES ('site-A','ch29',?,'in',?,?)", (t, str(k), int(t // 60)))
    con.commit()
    con.close()


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    now = datetime(2026, 8, 19, 12, 0, tzinfo=IST).timestamp()
    build(path, now)

    os.environ["GATEWAY_DB"] = path
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api as D

    def body(r):
        b = getattr(r, "body", r)
        if isinstance(b, dict):
            return b
        return json.loads(b if isinstance(b, str) else b.decode())

    calls = {"n": 0}
    _real = D._trends_compute

    def counted(*a, **k):
        calls["n"] += 1
        return _real(*a, **k)
    D._trends_compute = counted

    # ── 1. the default view must not derive, before OR after the cache is filled ────────────
    print("=== a cacheable request never derives ===")
    before = body(D.dash_trends("site-A", cam="ch29"))
    if calls["n"]:
        fails.append(f"a cache MISS still called _trends_compute {calls['n']}x — that is the 37s "
                     f"request coming back on the first view after every era change")
    print(f"  miss: state={before.get('state')!r}, _trends_compute called {calls['n']}x")

    db = D._db()
    D.trends_refresh(db, "site-A", "ch29")
    db.close()
    n_after_refresh = calls["n"]
    hit = body(D.dash_trends("site-A", cam="ch29"))
    if calls["n"] != n_after_refresh:
        fails.append("a cache HIT called _trends_compute — the cache is decorative")
    print(f"  hit : state={hit.get('cache', {}).get('state')!r}, "
          f"age={hit.get('cache', {}).get('age_s')}s, n_days={hit.get('n_days')}")
    if hit.get("state") == "not_computed":
        fails.append("the refresh stored nothing the reader could find — era key mismatch between "
                     "trends_refresh and _trends_read")

    # ── 2. deliberate questions still derive ────────────────────────────────────────────────
    print("\n=== a custom range / era override still derives ===")
    n0 = calls["n"]
    body(D.dash_trends("site-A", cam="ch29", from_d="2026-08-01", to_d="2026-08-05"))
    body(D.dash_trends("site-A", cam="ch29", era="deadbeef"))
    body(D.dash_trends("site-A", cam="ch29", from_h=8, to_h=10))
    if calls["n"] != n0 + 3:
        fails.append(f"custom range / era override / hour filter did not derive "
                     f"({calls['n'] - n0} of 3) — those are deliberate questions and must not be "
                     f"answered from a cache built for a different one")
    print(f"  3 deliberate requests -> {calls['n'] - n0} derivations")

    # ── 3. THE SKELETON MUST CARRY EVERY KEY THE REAL PAYLOAD CARRIES ──────────────────────
    print("\n=== the not-computed payload is structurally complete ===")
    real = _real("site-A", cam="ch29")
    skel = D._trends_skeleton("site-A", "ch29", {"state": "x"})
    missing = sorted(set(real) - set(skel))
    if missing:
        fails.append(f"the not-computed payload omits {missing} — the client reads these "
                     f"unconditionally and will throw, which renders as 'trends is broken' rather "
                     f"than 'trends is not computed yet'")
    for sub in ("range", "occupancy_note", "boundaries", "counting_eras"):
        sm = sorted(set(real.get(sub) or {}) - set(skel.get(sub) or {}))
        if sm:
            fails.append(f"skeleton {sub} omits {sm}")
    win = sorted(set(real.get("windows") or {}) - set(skel.get("windows") or {}))
    if win:
        fails.append(f"skeleton windows omits {win}")
    print(f"  top-level keys: real {len(real)}, skeleton {len(skel)}, missing {missing or 'none'}")

    # Every scalar in the skeleton must be null, never 0 — a zero renders as a lift that stood still.
    zeros = [k for k, v in skel.items() if v == 0]
    zeros += [f"range.{k}" for k, v in skel["range"].items() if v == 0]
    if zeros:
        fails.append(f"skeleton reports 0 for {zeros} — a zero here reads as 'the lift never moved'; "
                     f"pending must be null")
    print(f"  zero-valued fields: {zeros or 'none'}")

    # ── 4. A DOOR-LESS CAMERA MUST ROUND-TRIP ──────────────────────────────────────────────
    # _current_keys returns door_version=None for a camera with no door rows, and `WHERE
    # door_version=?` bound to None matches NOTHING — NULL=NULL is NULL, not true. So ch32/34/37
    # wrote cache rows they could never read back and sat on "not computed" for ever. A door-less
    # camera still has occupancy and transits worth serving.
    print("\n=== a door-less camera round-trips (NULL door_version) ===")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id,ts_bucket) "
                "VALUES ('site-A','ch32',?,'in','t1',1)", (now - 3600,))
    con.execute("INSERT INTO channel_map VALUES ('site-A',32,1,'lift 6',0)")
    con.commit()
    con.close()
    db = D._db()
    cv, dv = D._trends_key(db, "site-A", "ch32")
    D.trends_refresh(db, "site-A", "ch32")
    db.close()
    got = body(D.dash_trends("site-A", cam="ch32"))
    print(f"  key=({cv!r}, {dv!r})  ->  state={got.get('cache', {}).get('state')!r}")
    if dv is None or cv is None:
        fails.append("_trends_key returned None — it can never match its own stored row")
    if got.get("state") == "not_computed":
        fails.append("a door-less camera cannot read back the row it just wrote — NULL door_version "
                     "in the primary key; ch32/ch34/ch37 would sit on 'not computed' for ever")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — the default view is served from the cache and never derived, deliberate questions "
          "still derive,\n     and the pending payload has the full shape with nulls rather than "
          "zeros")
    return 0


if __name__ == "__main__":
    sys.exit(main())
