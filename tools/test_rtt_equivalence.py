#!/usr/bin/env python3
"""CLI and dash, ONE fixture database, identical era / trips / median — or the suite fails.

WHY THIS EXISTS. rtt_core was created so RTT could not be computed two ways, and it still was: the
CLI scoped on the FULL door_version while the dash resolved its own era through _era_for (which
returns everything before the '+' — a PREFIX) and matched it with `=`, selecting nothing. ch29's
panel read "RTT UNAVAILABLE" while the CLI found 1569 round trips on the same database.

Sharing a function is not the same as agreeing. The only test that would have caught it drives BOTH
callers end to end and compares their answers, so this runs the real CLI as a subprocess and the
real dash function in-process against one file, and asserts three things match: the era STRING, the
trip COUNT, and the MEDIAN.

THE ERA IS DELIBERATELY AWKWARD. The fixture gives ch29 four door_versions sharing one templates
hash — untagged, h2, h2 with a levels tag, and h3 with the vestigial levels tag the dead close_th
flag left behind (260d4a0fh3-stateL9f2T5471cb+...). That prefix matched three eras on the live
database, and it is exactly the shape that made `LIKE prefix%` pool instruments and `= prefix` match
none.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))

H3 = "260d4a0fh3-stateL9f2T5471cb+495e8f48"
OLD = ["260d4a0f+495e8f48", "260d4a0fh2+495e8f48", "260d4a0fh2Laa52+495e8f48"]

SCHEMA = """
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, floor TEXT, direction TEXT, door_state TEXT, read_conf REAL, panels_agreed INTEGER,
  reason TEXT, close_travel_s REAL, door_version TEXT);
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
  marked_at REAL);
-- The precompute keys on (counting_version, door_version), so the fixture has to carry the
-- counting side too or _current_keys cannot form the key the aggregate is stored under.
CREATE TABLE analyzer_status (gateway_id TEXT, cam TEXT, counting_version TEXT, ts REAL);
CREATE TABLE transit_event (gateway_id TEXT, cam TEXT, ts REAL, direction TEXT,
  counting_version TEXT);
"""


def add_trip(con, cam, t0, stops, dv, gap=60.0, home="G"):
    """closed at home -> `stops` intermediate stops -> open at home."""
    def row(ts, st, fl=None):
        con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,door_version) "
                    "VALUES ('site-A',?,?,?,?,?)", (cam, ts, fl, st, dv))
    row(t0 - 6, "open", home); row(t0 - 3, "closing", home); row(t0, "closed", home)
    t = t0
    for k in range(stops):
        t += gap
        row(t, "open", str(10 + k)); row(t + 8, "closing"); row(t + 10, "closed")
    t += gap
    row(t, "open", home)
    return t - t0


def build(path, now):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO channel_map VALUES ('site-A',29,1,'lift 3',0)")
    # OLDER ERAS, same templates hash — the trap. Their trips must NOT be counted.
    for i, dv in enumerate(OLD):
        for k in range(3):
            add_trip(con, "ch29", now - (40 + i * 5) * 3600 + k * 900, 4, dv, gap=90.0)
    # THE CURRENT ERA: trips of known, varied length so a median is meaningful.
    for k, gap in enumerate((40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0)):
        add_trip(con, "ch29", now - 20 * 3600 + k * 1800, 3, H3, gap=gap)
    con.commit()
    con.close()


def dash_answer(path, now):
    """The dash's own function, in process, with its own stubs."""
    from test_dash_occupancy import _stub
    _stub()
    os.environ["GATEWAY_DB"] = path
    for m in ("dash_api",):
        sys.modules.pop(m, None)
    import dash_api as D
    db = D._db()
    try:
        out, err = D._rtt_by_cam(db, "site-A", ["ch29"], now - 168 * 3600, now, "")
    finally:
        db.close()
    return out.get("ch29"), err


def cli_answer(path, hours=168.0):
    """The real CLI, as a subprocess, parsed from what it prints."""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "rtt.py"),
                        "--db", path, "--cam", "ch29", "--hours", str(hours)],
                       capture_output=True, text=True, timeout=120)
    out = r.stdout
    era = re.search(r"era (\S+), last", out)
    found = re.search(r"round trips found: (\d+)\s+plausible: (\d+)", out)
    allday = re.search(r"all-day\s+(\d+)\s+([\d.]+)\s+([\d.]+)", out)
    return {"rc": r.returncode, "era": era.group(1) if era else None,
            "n_trips": int(found.group(1)) if found else None,
            "n_plausible": int(found.group(2)) if found else None,
            "median": float(allday.group(2)) if allday else None,
            "raw": out}


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    now = datetime(2026, 8, 18, 12, 0, tzinfo=IST).timestamp()
    build(path, now)

    print("=== the fixture ===")
    con = sqlite3.connect(path)
    for dv, n in con.execute("SELECT door_version, COUNT(*) FROM gw_door_event GROUP BY door_version "
                             "ORDER BY MAX(ts) DESC"):
        print(f"  {dv:42s} {n:>5} rows")
    con.close()
    print(f"  all four share the prefix '260d4a0f' — the shape that pooled three eras on the live DB")

    print("\n=== the two callers, same database ===")
    cli = cli_answer(path)
    dash, err = dash_answer(path, now)
    print(f"  CLI : era={cli['era']}  trips={cli['n_trips']}  plausible={cli['n_plausible']}  "
          f"median={cli['median']}")
    if dash is None:
        fails.append(f"the dash returned nothing for ch29 (err={err!r})")
        print(f"  DASH: <nothing>  err={err!r}")
    else:
        print(f"  DASH: era={dash.get('era')}  trips={dash.get('n_trips')}  "
              f"plausible={dash.get('n_plausible')}  median={(dash.get('all_day') or {}).get('median')}"
              f"  state={dash.get('state')}")

    if dash and dash.get("state") != "ok":
        fails.append(f"the dash reports state={dash.get('state')!r} on a database where the CLI "
                     f"found {cli['n_plausible']} plausible trips — this is the ch29 defect")

    print("\n=== the three things that must match ===")
    if dash:
        checks = [("era string", cli["era"], dash.get("era")),
                  ("trip count", cli["n_trips"], dash.get("n_trips")),
                  ("median", cli["median"], (dash.get("all_day") or {}).get("median"))]
        for name, a, b in checks:
            ok = (a == b) or (isinstance(a, float) and isinstance(b, float) and abs(a - b) < 0.05)
            print(f"  {'OK ' if ok else 'DIFFER'}  {name:12s} CLI={a!r}  DASH={b!r}")
            if not ok:
                fails.append(f"{name} differs between CLI and dash: {a!r} vs {b!r} — one derivation "
                             "was the point")

    print("\n=== the era must be the CURRENT one, in full ===")
    if dash and dash.get("era") != H3:
        fails.append(f"era is {dash.get('era')!r}, not the full current {H3!r}")
    if cli["era"] != H3:
        fails.append(f"the CLI resolved {cli['era']!r}, not {H3!r}")
    print(f"  expected {H3}")
    if dash and dash.get("era") == H3:
        print("  neither caller fell back to a prefix, and neither pooled the three older eras")

    print("\n=== older-era trips are EXCLUDED, not merely outnumbered ===")
    # 9 current-era trips were built; the three older eras contribute 9 more that must not appear.
    if dash and dash.get("n_trips") not in (9, None):
        fails.append(f"trip count {dash.get('n_trips')} != 9 — an older era leaked in, or the "
                     "current era's trips were lost")
    print(f"  built 9 trips in the current era and 9 across the older three; counted "
          f"{dash.get('n_trips') if dash else '?'}")

    print("\n=== the request path must not walk seven cameras' history ===")
    # THE DEPLOYED FAILURE: RTT rode /data, which serves all seven cameras, and blew the 25s budget
    # the moment the era fix made the walk match rows. The budget guard named the phase, which is
    # the only reason it was a five-minute diagnosis — but a guard firing is not a design.
    src = open(os.path.join(ROOT, "dash_api.py")).read()
    i = src.index("def _dash_data_inner")
    j = src.index('@dash_router.get("/dash/{gw}/trends")', i)
    if "_rtt_by_cam(" in src[i:j]:
        fails.append("/data still computes RTT for every camera on every request — that is the "
                     "timeout")
    if '@dash_router.get("/dash/{gw}/rtt")' not in src:
        fails.append("there is no per-camera RTT endpoint to fetch it from instead")
    print("  /data: clear   per-camera endpoint: present")

    print("\n=== bounded: the version scan, the row cap, and the cache ===")
    import time as _t
    from test_dash_occupancy import _stub as _s2
    _s2()
    os.environ["GATEWAY_DB"] = path
    sys.modules.pop("dash_api", None)
    import dash_api as D2
    db2 = D2._db()
    db2.close()
    # THE IN-PROCESS CACHE IS GONE, AND ITS ABSENCE IS THE ASSERTION. It was keyed on
    # (gw, cam, era, t0, t1) where t0 = time.time() - days*86400 — a different float every request —
    # so three identical user requests minted three keys and did three full walks. The old version
    # of this block measured it with an explicit (None, None), a STABLE key production never uses,
    # and so reported "cold 2439ms / cached 0ms" while the live box served 9.0s warm.
    #
    # A cache test that constructs its own key cannot see the key production uses. The lesson is not
    # "fix the key" — a process cache cannot help a cold start or a first view either — so the walk
    # moved to the precompute timer and the cache was deleted rather than repaired.
    if "_RTT_CACHE" in src or "RTT_CACHE_TTL_S" in src:
        fails.append("the in-process RTT cache is still present — it never fired (t0 is "
                     "time.time()-relative) and now has no request-path caller to serve")
    print("  in-process cache removed; the walk runs on the precompute timer only")
    if "_vw, _vargs = _ts_clause(t0, t1)" not in src:
        fails.append("the door_version scan is unbounded — a full history scan per camera per "
                     "request, before a single trip is walked")
    if "RTT_MAX_ROWS" not in src or "too_many_rows" not in src:
        fails.append("no row cap: a wide range on a chattering camera walks the whole window on "
                     "the request path")
    if "truncat" not in src.lower():
        fails.append("the cap must REFUSE rather than truncate — a partial walk drops round trips "
                     "and reports a median from part of the window")
    print("  version scan bounded, row cap refuses rather than truncates")

    # ── THE WALK MUST NOT BE REACHABLE FROM ANY REQUEST HANDLER ──────────────────────────────
    # An in-process cache was the wrong instrument: it cannot help a cold process, a restart, or a
    # first view, and here it could not fire at all. The walk belongs on the precompute timer, and
    # the endpoints read the stored row — which is the rule door_aggregate already stated for tier2.
    print("\n=== the walk is off every request path ===")
    for fn in ("dash_data", "dash_trends", "dash_rtt"):
        i = src.find(f"def {fn}(")
        if i < 0:
            fails.append(f"{fn} is missing entirely")
            continue
        j = src.find("\n@", i)
        body = src[i:j if j > 0 else len(src)]
        if "_rtt_by_cam(" in body:
            fails.append(f"{fn} still WALKS door rows — this is what took /trends to 152.8s live; "
                         f"it must read the precomputed row via _rtt_read")
        print(f"  {fn:12s} {'WALKS' if '_rtt_by_cam(' in body else 'reads'}")
    # SOMETHING OFF THE REQUEST PATH MUST STILL FILL IT. The walk used to live in
    # aggregate_refresh, which covered the DEFAULT WINDOW ONLY — so Today and 30 days had nothing to
    # serve and said so for ever. It now lives in rtt_refresh, one entry per period the picker
    # offers. The assertion is unchanged in substance: a scheduler-only function must walk, or every
    # panel sits on "not yet computed" permanently.
    _rr = src.find("def rtt_refresh(")
    if _rr < 0 or "_rtt_by_cam(" not in src[_rr:src.find("\ndef ", _rr + 10)]:
        fails.append("rtt_refresh does not compute RTT — nothing would ever fill rtt_window, "
                     "and every panel would sit on 'not yet computed' forever")
    # AND IT MUST WALK UNCAPPED. The 120,000-row cap is a request-path guard; applied to the timer
    # it refused ch29 and ch27 outright and left RTT unviewable on every range a user can select.
    if _rr >= 0 and "max_rows=None" not in src[_rr:src.find("\ndef ", _rr + 10)]:
        fails.append("rtt_refresh walks WITH a row cap — the cap exists for the request path, and "
                     "capping the timer is what made RTT unviewable on the busiest cameras")

    # ── AND THE PRECOMPUTE MUST PRESERVE THE EQUIVALENCE ─────────────────────────────────────
    # Moving the walk off the request path is only safe if the stored answer is the SAME answer.
    # Storing a summary the CLI disagrees with would replace a slow truth with a fast falsehood.
    print("\n=== the stored answer equals the walked answer ===")
    _s2()
    os.environ["GATEWAY_DB"] = path
    sys.modules.pop("dash_api", None)
    import dash_api as D3
    db3 = D3._db()
    try:
        pend, pmeta = D3._rtt_read(db3, "site-A", "ch29", D3.WINDOW_DAYS)
        print(f"  before any precompute: {pmeta.get('state')}")
        if pend is not None:
            fails.append("_rtt_read invented a summary with nothing precomputed")
        if "not yet computed" not in (pmeta.get("state") or ""):
            fails.append(f"a miss must report PENDING, not absence — got {pmeta.get('state')!r}; "
                         f"'no round trips' asserts the lift never moved")
        D3.rtt_refresh(db3, "site-A", "ch29", D3.WINDOW_DAYS)
        got, gmeta = D3._rtt_read(db3, "site-A", "ch29", D3.WINDOW_DAYS)
        print(f"  after precompute:      {gmeta.get('state')}  trips={(got or {}).get('n_trips')}")
        if not got:
            fails.append("nothing stored after rtt_refresh")
        elif got.get("n_trips") != cli["n_trips"]:
            fails.append(f"stored trips {got.get('n_trips')} != CLI trips {cli["n_trips"]} — the "
                         f"precompute changed the answer, not just where it is computed")
        # EVERY PERIOD THE PICKER OFFERS, not just the default window. This is the gap that made
        # Today and 30 days report "not served for this range" while All and 7 days hit the cap.
        print("  every window the picker maps to:")
        for _w in D3.RTT_WINDOWS:
            D3.rtt_refresh(db3, "site-A", "ch29", _w)
            _g, _m = D3._rtt_read(db3, "site-A", "ch29", _w)
            print(f"    window {_w:>4g}d -> {_m.get('state')}  trips={(_g or {}).get('n_trips')}")
            if _m.get("state") != "ok":
                fails.append(f"window {_w:g}d is not served after a full precompute "
                             f"({_m.get('state')!r}) — the picker offers a range nothing fills")
    finally:
        db3.close()

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        print("\n--- CLI output, for context ---")
        print("\n".join(cli["raw"].splitlines()[:14]))
        return 1
    print("OK — CLI and dash resolve the identical era string and report identical trips and "
          "median on one database; a disagreement fails here rather than on a screenshot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
