#!/usr/bin/env python3
"""STARVED, NOT SILENT — the fourth health signal, and the gap that found it.

THE DEFECT, reported on the Monday review of 2026-09-07. PL2B (ch30) had been counting 47-115
boardings/day since the Sep 2 restart against ~500 before, while its door cycles ran at 601/day.
0.11 boardings per door cycle against 0.67-1.0 on every other lift. The health line said nothing
for five days and was right by its own rules: the camera was not silent. Fresh heartbeat, advancing
segments, transits every few minutes. All three existing signals ask "is anything ARRIVING", and
the answer was yes.

What no absolute threshold can see is that far too LITTLE was arriving — a lift's demand varies by
an order of magnitude across a week, which is the same measured reason the docstring of
health_check.py rejects an hourly transit alarm as crying wolf. The quantity that does not vary
that way is the RATIO of what was counted to what the doors did, and it is a property of the
counter rather than of the traffic.

The properties this file locks down, in the order they can go wrong:

  1. A STARVED CAMERA IS REPORTED. ch30's ratio collapses while everything else about it is
     healthy, and the line says so.
  2. A RECOVERED CAMERA IS NOT. PL4A sat near zero for two days after a restart and recovered on
     its own; a check that keeps convicting it teaches the reader to ignore the line.
  3. A QUIET CAMERA IS NOT A STARVED ONE. Hours with a handful of door opens produce a ratio built
     on noise; judging them is exactly the wolf-crying the module rejects.
  4. NO BASELINE IS AN UNKNOWN, NOT A CLEAN BILL. A camera without enough history says so.
  5. IT IS NOT A BREACH. Nothing is down — the lift runs, the camera works, the number is wrong.
  6. THE SENTENCE SAYS IT IS NOT SILENCE. Every other named camera on this line means "nothing is
     arriving"; this one means "too little is", and if the phrase does not say so the reader goes
     and checks a stream that is fine.
  7. IT DOES NOT RUN ON THE TICK. evaluate() is called every ~10 minutes and this walks days of
     door rows through a window function; the signal is 6+ hours wide.
"""
import json
import os
import sqlite3
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE camera_registry (gateway_id TEXT, cam TEXT, enabled INTEGER, stride INTEGER,
  analyze_fps REAL, note TEXT, updated_at REAL);
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
  marked_at REAL);
CREATE TABLE analyzer_status (gateway_id TEXT, cam TEXT, ts REAL, segments INTEGER,
  dropped INTEGER, posted INTEGER, last_transit_ts REAL, mode TEXT, counting_version TEXT);
CREATE TABLE transit_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, direction TEXT, track_id INTEGER);
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, floor TEXT, door_state TEXT, door_version TEXT);
CREATE TABLE validation_item (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts_start REAL, counting_version TEXT);
CREATE TABLE relay_status (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
  streams_alive INTEGER);
"""

DV = "260d4a0fh3-stateT5471cb+495e8f48"

# ch29 healthy all along · ch30 = PL2B, starved for the last day · ch27 = PL4A, starved two days
# ago and RECOVERED · ch32 too quiet to judge at all.
CAMS = ["ch27", "ch29", "ch30", "ch32"]
OPENS_PER_HOUR = {"ch27": 30, "ch29": 30, "ch30": 30, "ch32": 3}
HEALTHY_RATIO = 0.80
STARVED_RATIO = 0.11


def build(path, now):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i, c in enumerate(CAMS):
        con.execute("INSERT INTO camera_registry (gateway_id,cam,enabled,stride) "
                    "VALUES ('site-A',?,1,2)", (c,))
        con.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,segments,dropped,posted,mode,"
                    "counting_version) VALUES ('site-A',?,?,?,0,0,'live','v-cabin-1')",
                    (c, now - 20, 1000 + 10 * i))
    con.execute("INSERT INTO relay_status (gateway_id,ts,streams_alive) VALUES ('site-A',?,4)",
                (now - 30,))

    def ratio_for(cam, hours_ago):
        if cam == "ch30":                       # PL2B: starved from ~20h ago and still starved
            return STARVED_RATIO if hours_ago <= 20 else HEALTHY_RATIO
        if cam == "ch27":                       # PL4A: starved 48-96h ago, recovered since
            return STARVED_RATIO if 48 <= hours_ago <= 96 else HEALTHY_RATIO
        return HEALTHY_RATIO

    # Nine days of hourly activity, so the 7-day baseline has plenty of judgeable active hours.
    tid = 0
    for hours_ago in range(9 * 24, -1, -1):
        base = now - hours_ago * 3600.0
        hod = datetime.fromtimestamp(base, IST).hour
        if not (7 <= hod < 23):
            continue                            # off-hours are not judged; do not fabricate them
        for cam in CAMS:
            n_open = OPENS_PER_HOUR[cam]
            for k in range(n_open):
                t = base + k * (3000.0 / max(n_open, 1))
                # closed -> open is the transition the check counts; the closed row in between is
                # what makes each one a separate open rather than one long one.
                con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,door_state,door_version)"
                            " VALUES ('site-A',?,?, 'closed',?)", (cam, t, DV))
                con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,door_state,door_version)"
                            " VALUES ('site-A',?,?, 'open',?)", (cam, t + 5, DV))
            for k in range(int(round(n_open * ratio_for(cam, hours_ago)))):
                tid += 1
                con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                            "VALUES ('site-A',?,?, 'in', ?)",
                            (cam, base + k * 60.0 + 7, tid))
            con.execute("INSERT INTO validation_item (gateway_id,cam,ts_start,counting_version) "
                        "VALUES ('site-A',?,?, 'v-cabin-1')", (cam, base + 10))
    # Every camera has a transit inside the one-hour rule, so nothing BREACHES and the only thing
    # the line can be saying is DEGRADED.
    for cam in CAMS:
        con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                    "VALUES ('site-A',?,?, 'in', 99999)", (cam, now - 300))
    con.commit()
    con.close()


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    # A fixed weekday mid-morning: inside active hours, so the recent window is judgeable.
    now = datetime(2026, 9, 7, 10, 30, tzinfo=IST).timestamp()
    build(path, now)
    os.environ["GATEWAY_DB"] = path
    import importlib
    import health_check as H
    importlib.reload(H)
    H.DB_PATH = path

    db = H._db(path)
    res = H.evaluate(db, "site-A", now)
    st = res.get("starvation") or {}

    print("=== 1. per camera ===")
    for cam in CAMS:
        d = (st.get("cams") or {}).get(cam) or {}
        print(f"  {cam}: state={str(d.get('state')):12s} baseline={d.get('baseline')} "
              f"recent={d.get('recent_median')} under={d.get('n_under')}/{d.get('n_recent_hours')} "
              f"thin={d.get('n_thin_hours')} base_hrs={d.get('n_base_hours')}")

    # 1. THE STARVED CAMERA IS REPORTED.
    if "ch30" not in (res.get("degraded") or []):
        fails.append("ch30 is counting an eighth of its own baseline while posting normally and "
                     "was NOT reported — this is the gap the check exists to close")
    # 2. THE RECOVERED ONE IS NOT.
    if "ch27" in (res.get("degraded") or []):
        fails.append("ch27 recovered on its own two days ago and is still being convicted — a "
                     "check that will not clear teaches the reader to ignore the line")
    # 3. A QUIET CAMERA IS NOT A STARVED ONE.
    if "ch32" in (res.get("degraded") or []):
        fails.append("ch32's hours are too thin to judge and it was reported anyway — a ratio "
                     "built on three door opens is arithmetic on noise")
    d32 = (st.get("cams") or {}).get("ch32") or {}
    if d32.get("state") not in ("no baseline",):
        fails.append(f"a camera with only thin hours reported {d32.get('state')!r} rather than "
                     f"saying it has no baseline — unknown is not a clean bill of health")
    if "ch29" in (res.get("degraded") or []):
        fails.append("the healthy camera was reported")

    print("\n=== 2. the line ===")
    print("  " + res["line"][:300])
    line = res["line"]
    if "[DEGRADED:" not in line:
        fails.append("the degraded finding is not on the health line at all")
    for want, why in (
            ("STARVED, NOT SILENT", "the phrase does not distinguish itself from silence — every "
                                    "other named camera on this line means nothing is arriving"),
            ("ch30", "the phrase does not name the camera"),
            ("boardings per door open", "the phrase does not say what the ratio is"),
            ("baseline", "the phrase does not give the baseline it is being compared against"),
            ("first at 2026-09-", "the phrase does not say when the collapse started, so a "
                                  "sustained fault reads the same as an intermittent one"),
            ("Check the counting worker, not the stream",
             "the phrase does not point at the right box")):
        if want not in line:
            fails.append(why)
    # 5. NOT A BREACH.
    # THE TWO NUMBERS IN THE SENTENCE MUST AGREE. "32% of baseline" beside "14 of 18 hours below
    # 40%" is two claims about one camera that the reader has to reconcile; a median on both sides
    # cannot disagree with the per-hour trigger.
    d30x = (st.get("cams") or {}).get("ch30") or {}
    pct = 100.0 * (d30x.get("recent_median") or 0) / max(d30x.get("baseline") or 1, 1e-9)
    print(f"  reported {pct:.0f}% of baseline · trigger fires under "
          f"{100 * H.DEGRADED_FRAC:.0f}%")
    if pct >= 100 * H.DEGRADED_FRAC:
        fails.append(f"the phrase reports {pct:.0f}% of baseline while the trigger fires below "
                     f"{100 * H.DEGRADED_FRAC:.0f}% — the sentence contradicts its own rule")
    print(f"  ok={res['ok']} (must stay True: nothing is DOWN) · degraded={res.get('degraded')}")
    if not res["ok"]:
        fails.append("a degraded camera made the line report BREACH — nothing is down: the lift "
                     "runs, the camera works, and the number is wrong")
    if "BREACH" in line:
        fails.append("the line says BREACH for a fleet where every camera is posting")

    print("\n=== 3. off-hours are not judged ===")
    d30 = (st.get("cams") or {}).get("ch30") or {}
    if not (d30.get("n_offhours_skipped") or 0) and not (d30.get("n_thin_hours") or 0):
        # the fixture only writes active hours, so this is a shape check on the payload
        if "n_offhours_skipped" not in d30:
            fails.append("the payload does not record how many hours were skipped as off-hours")
    print(f"  ch30 skipped {d30.get('n_offhours_skipped')} off-hour(s), "
          f"{d30.get('n_thin_hours')} thin hour(s)")

    print("\n=== 4. it does not recompute on every tick ===")
    calls = {"n": 0}
    _real = H.starvation_compute

    def counted(*a, **k):
        calls["n"] += 1
        return _real(*a, **k)
    H.starvation_compute = counted
    try:
        H.evaluate(db, "site-A", now + 60)
        H.evaluate(db, "site-A", now + 120)
        n_cached = calls["n"]
        print(f"  two ticks 1 minute apart: {n_cached} recomputation(s)")
        if n_cached:
            fails.append(f"the scan ran {n_cached}x on ticks minutes apart — evaluate() is called "
                         f"every ~10 minutes and this walks days of door rows")
        # a THRESHOLD change must invalidate it: a cached verdict answers a different question
        H.DEGRADED_FRAC = 0.9
        H.evaluate(db, "site-A", now + 180)
        print(f"  after changing the threshold: {calls['n']} recomputation(s)")
        if calls["n"] != 1:
            fails.append("changing a threshold did not invalidate the cache — the change would "
                         "look applied when it was not")
    finally:
        H.starvation_compute = _real
        H.DEGRADED_FRAC = 0.40

    print("\n=== 5. an era crossing inside the window is named, not hidden ===")
    con = sqlite3.connect(path)
    con.execute("UPDATE validation_item SET counting_version='v-cabin-2' WHERE cam='ch30' "
                "AND ts_start >= ?", (now - 5 * 86400,))
    con.execute("DELETE FROM starvation_check")
    con.commit()
    con.close()
    res2 = H.evaluate(db, "site-A", now + 240)
    d = ((res2.get("starvation") or {}).get("cams") or {}).get("ch30") or {}
    print(f"  ch30 counting builds in window: {d.get('counting_versions')} · "
          f"named in the line: {'counting builds' in res2['line']}")
    if (d.get("counting_versions") or 0) < 2:
        fails.append("a counting-build change inside the window was not detected")
    elif "counting builds" not in res2["line"]:
        fails.append("a counting-build change inside the window is not named — a rebuilt counter "
                     "can move this ratio on its own, and that is a different finding from a "
                     "starved camera")
    db.close()

    print("\n=== 6. a fleet with no door rows says so rather than convicting anyone ===")
    p2 = os.path.join(tmp, "bare.db")
    c2 = sqlite3.connect(p2)
    c2.executescript(SCHEMA)
    for c in CAMS:
        c2.execute("INSERT INTO camera_registry (gateway_id,cam,enabled,stride) "
                   "VALUES ('site-A',?,1,2)", (c,))
        c2.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,segments,mode) "
                   "VALUES ('site-A',?,?,1,'live')", (c, now - 20))
        c2.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                   "VALUES ('site-A',?,?, 'in', 1)", (c, now - 300))
    c2.commit(); c2.close()
    db2 = H._db(p2)
    r2 = H.evaluate(db2, "site-A", now)
    db2.close()
    states = {c: ((r2.get("starvation") or {}).get("cams") or {}).get(c, {}).get("state")
              for c in CAMS}
    print(f"  states={states} degraded={r2.get('degraded')}")
    if r2.get("degraded"):
        fails.append("a fleet with no door history was convicted of starvation")
    if "[DEGRADED:" in r2["line"]:
        fails.append("the line reports DEGRADED for a fleet it cannot judge")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK — a starved camera is reported and a recovered one is not, a quiet hour is not "
          "judged,\n     no baseline is an unknown rather than a clean bill, it is not a breach, "
          "and the\n     sentence says it is not silence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
