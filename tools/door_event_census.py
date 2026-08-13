#!/usr/bin/env python3
"""WHAT IS FLAPPING in gw_door_event, per camera, per hour — evidence before any emission change.

THE QUESTION. ch29 emitted 1196-5505 gw_door_event rows/hour on the night of 2026-08-12, against a
DOOR_HB_S liveness floor of 60/hr. The emit gate is:

    key = (floor, direction, door_state);  emit if cycle or key != prev_key

so every row is either a completed cycle or a CHANGE in one of those three fields. This census reads
the rows back and says WHICH field changed, how often, and between which values — because "the door
stream is noisy" is not a finding and "direction flipped up<->down 4,900 times without the floor
moving" is.

WHY IT MATTERS DOWNSTREAM. ch29's cycle and reopen counts on the compliance panel are derived from
this stream (_h3_cycle_ts walks door_state transitions; _h3_reopens counts open->closing->open). If
door_state is chattering, those counts are inflated by the same factor and the panel is quoting it.
The census reports the cycle count both raw and after a debounce, so the size of that exposure is a
number rather than a worry.

USAGE
  python3 tools/door_event_census.py --db /var/lib/liftlab/gateway.db --cam ch29 --hours 1
  python3 tools/door_event_census.py --db ... --cam ch29 --from '2026-08-12T23:00+05:30' --to '...'
  python3 tools/door_event_census.py --db ... --all-cams --hours 1        # the comparison
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _ts(s):
    return datetime.fromisoformat(s).timestamp()


def busiest_hour(db, cam, t0, t1):
    """The hour with the most rows — the census should describe the worst case, not an average."""
    rows = db.execute(
        "SELECT CAST(ts/3600 AS INT) h, COUNT(*) n FROM gw_door_event "
        "WHERE cam=? AND ts>=? AND ts<? GROUP BY h ORDER BY n DESC LIMIT 1", (cam, t0, t1)).fetchone()
    if not rows:
        return None
    h0 = rows[0] * 3600.0
    return h0, h0 + 3600.0, rows[1]


def census(db, cam, t0, t1):
    # floor_age_s is a recent column; a gateway that predates it must still be censusable, and the
    # absence is worth reporting rather than crashing on.
    have_age = any(r[1] == "floor_age_s" for r in db.execute("PRAGMA table_info(gw_door_event)"))
    age_col = "COALESCE(floor_age_s, -1)" if have_age else "-1"
    rows = db.execute(
        "SELECT ts, floor, direction, door_state, close_travel_s, door_version, reason, read_conf, "
        f"{age_col} fage FROM gw_door_event "
        "WHERE cam=? AND ts>=? AND ts<? ORDER BY ts, id", (cam, t0, t1)).fetchall()
    n = len(rows)
    out = {"cam": cam, "n": n, "t0": t0, "t1": t1,
           "hours": max(1e-9, (t1 - t0) / 3600.0)}
    if not n:
        return out
    out["rate_hr"] = n / out["hours"]
    out["eras"] = Counter(str(r[5] or "")[:8] for r in rows)
    out["engine"] = ("h3-state" if any("h3-state" in str(r[5] or "") for r in rows) else "h2/edge")

    # WHICH FIELD MOVED. Attribute every row to the field(s) that differ from the previous row —
    # this is the emit gate run backwards, so the totals say exactly why each row exists.
    changed = Counter()
    pairs = {"floor": Counter(), "direction": Counter(), "door_state": Counter()}
    prev = None
    n_cycle_rows = 0
    for r in rows:
        cur = (r[1], r[2], r[3])
        if r[4] is not None:
            n_cycle_rows += 1
        if prev is None:
            changed["first row"] += 1
            prev = cur
            continue
        moved = [nm for nm, a, b in zip(("floor", "direction", "door_state"), prev, cur) if a != b]
        if not moved:
            # emitted with an unchanged key => a completed cycle or the DOOR_HB_S liveness beat
            changed["no key change (cycle or heartbeat)"] += 1
        else:
            changed["+".join(moved)] += 1
            for nm in moved:
                i = ("floor", "direction", "door_state").index(nm)
                pairs[nm][f"{prev[i]!r} -> {cur[i]!r}"] += 1
        prev = cur
    out["changed"] = changed
    out["pairs"] = pairs
    out["n_cycle_rows"] = n_cycle_rows

    # DOOR-STATE CHATTER, measured as dwell: how long the state held before flipping back. Real door
    # motion dwells for seconds; a classifier sitting on its threshold flips in frame time.
    dwell = []
    last_state, last_t = rows[0][3], rows[0][0]
    for r in rows[1:]:
        if r[3] != last_state:
            dwell.append(r[0] - last_t)
            last_state, last_t = r[3], r[0]
    out["n_state_flips"] = len(dwell)
    if dwell:
        d = sorted(dwell)
        out["dwell"] = {"p10": d[len(d) // 10], "p50": d[len(d) // 2],
                        "p90": d[min(len(d) - 1, 9 * len(d) // 10)],
                        "under_1s": sum(1 for x in d if x < 1.0),
                        "under_0_5s": sum(1 for x in d if x < 0.5)}

    # FLOOR-READ instability while the door never moved: the other way this stream inflates.
    fl = [r for r in rows if r[1] is not None]
    out["floor_values"] = Counter(str(r[1]) for r in fl)
    out["floor_age_seen"] = have_age and any(r[8] >= 0 for r in rows)
    out["has_age_col"] = have_age
    return out


def panel_cycles(db, cam, t0, t1, min_dwell_s=0.0):
    """The COMPLIANCE PANEL's own cycle rule, applied verbatim, raw and with a dwell filter.

    dash_api._h3_cycle_ts counts a cycle on every transition INTO 'closed' from 'closing' or 'open',
    with NO dwell test. So a sub-second chatter burst open->closed counts exactly like a lift that
    stood open for twenty seconds and shut. The conservation check in that function's docstring
    (entries to 'closing' == exits) is reassuring about bookkeeping and says nothing about whether
    the transitions were door motion: chatter conserves too.

    min_dwell_s requires the PRECEDING state to have held that long before the close is counted.
    This is a measurement of exposure, not a corrected count — it says how much of the panel's number
    rests on transitions too brief to be a door.
    """
    rows = db.execute("SELECT ts, door_state FROM gw_door_event WHERE cam=? AND ts>=? AND ts<? "
                      "ORDER BY ts, id", (cam, t0, t1)).fetchall()
    n, prev, prev_ts = 0, None, None
    for ts, st in rows:
        if st != prev:
            if st == "closed" and prev in ("closing", "open"):
                if min_dwell_s <= 0 or (prev_ts is not None and ts - prev_ts >= min_dwell_s):
                    n += 1
            prev, prev_ts = st, ts
    return n


def cycles(db, cam, t0, t1, debounce_s=0.0):
    """open->closed transitions, raw and debounced — the compliance panel's cycle count, and what it
    would be if states shorter than `debounce_s` were treated as chatter rather than as motion."""
    rows = db.execute("SELECT ts, door_state FROM gw_door_event WHERE cam=? AND ts>=? AND ts<? "
                      "ORDER BY ts, id", (cam, t0, t1)).fetchall()
    seq = []
    for ts, st in rows:
        if st is None:
            continue
        if not seq or seq[-1][1] != st:
            seq.append((ts, st))
    if debounce_s > 0:
        keep, i = [], 0
        while i < len(seq):
            j = i + 1
            while j < len(seq) and (seq[j][0] - seq[i][0]) < debounce_s:
                j += 1                              # collapse everything inside the window
            keep.append(seq[i])
            i = j
        seq = [s for k, s in enumerate(keep) if k == 0 or keep[k - 1][1] != s[1]] or keep
    n = 0
    seen_open = False
    for _ts, st in seq:
        if st == "open":
            seen_open = True
        elif st == "closed" and seen_open:
            n += 1
            seen_open = False
    return n


def report(c, db):
    print(f"\n=== {c['cam']} — {c['n']} rows over {c['hours']:.2f}h ===")
    if not c["n"]:
        print("  no rows in range")
        return
    print(f"  rate: {c['rate_hr']:.0f} rows/hr   engine: {c['engine']}   "
          f"era(s): {', '.join(f'{k}({v})' for k, v in c['eras'].most_common(3))}")
    print(f"  DOOR_HB_S=60 gives a floor of 60/hr -> this is {c['rate_hr'] / 60:.0f}x the liveness floor")
    print("  WHICH FIELD MOVED (the emit gate, run backwards):")
    for k, v in c["changed"].most_common():
        print(f"    {v:>7}  {100.0 * v / c['n']:>5.1f}%  {k}")
    for nm in ("door_state", "direction", "floor"):
        top = c["pairs"][nm].most_common(4)
        if top:
            print(f"  top {nm} transitions: " + " · ".join(f"{k} x{v}" for k, v in top))
    if c.get("dwell"):
        d = c["dwell"]
        print(f"  door_state flips: {c['n_state_flips']}  dwell p10/p50/p90 = "
              f"{d['p10']:.2f}/{d['p50']:.2f}/{d['p90']:.2f}s   "
              f"under 1s: {d['under_1s']} ({100.0 * d['under_1s'] / max(1, c['n_state_flips']):.0f}%)  "
              f"under 0.5s: {d['under_0_5s']}")
        print("    (a real door dwells for SECONDS; sub-second flips are a classifier on its "
              "threshold, not a door)")
    print(f"  rows carrying close_travel_s: {c['n_cycle_rows']}  "
          f"floors seen: {dict(c['floor_values'].most_common(6))}")
    raw = panel_cycles(db, c["cam"], c["t0"], c["t1"], 0.0)
    d1 = panel_cycles(db, c["cam"], c["t0"], c["t1"], 1.0)
    d2 = panel_cycles(db, c["cam"], c["t0"], c["t1"], 2.0)
    print(f"  COMPLIANCE EXPOSURE (dash_api._h3_cycle_ts's own rule): cycles raw={raw}  "
          f"prev-state held >=1s: {d1}  >=2s: {d2}"
          + (f"   -> {100.0 * (raw - d2) / raw:.0f}% of the panel's count rests on a state that "
             f"held under 2s" if raw else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cam", action="append")
    ap.add_argument("--all-cams", action="store_true")
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to", dest="to")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)

    cams = a.cam or []
    if a.all_cams or not cams:
        cams = [r[0] for r in db.execute("SELECT DISTINCT cam FROM gw_door_event ORDER BY cam")]

    print("gw_door_event CENSUS — every row is a (floor,direction,door_state) change or a cycle.")
    for cam in cams:
        if a.frm:
            t0, t1 = _ts(a.frm), _ts(a.to) if a.to else _ts(a.frm) + a.hours * 3600
        else:
            span = db.execute("SELECT MIN(ts), MAX(ts) FROM gw_door_event WHERE cam=?",
                              (cam,)).fetchone()
            if not span or span[0] is None:
                print(f"\n=== {cam} — no rows at all ===")
                continue
            bh = busiest_hour(db, cam, span[0], span[1])
            if bh is None:
                continue
            t0, t1, _n = bh
        report(census(db, cam, t0, t1), db)
    print("\nWindow chosen: busiest hour per camera unless --from/--to given "
          "(a census of the average hour would describe a problem nobody has).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
