#!/usr/bin/env python3
"""ROUND TRIP TIME — door CLOSED at G to the next door OPEN at G, per lift, per era.

RTT is what the whole MEP-02 sheet resolves to (RTT -> interval -> compliance), so this measures it
directly from gw_door_event rather than inferring it from the coefficients that are supposed to sum
to it.

THE CLAIM RULE IS THE PANEL'S OWN. A cycle here is a transition INTO 'closed' from 'closing' or
'open' — dash_api._h3_cycle_ts, verbatim — so cycles and RTT can never disagree about what a cycle
is. That was the brief's condition and it is the reason this file does not define its own.

THE DWELL THRESHOLD IS 0, ON EVIDENCE. tools/dwell_validation.py graded thresholds 0/1/2/3s against
the frame-anchored hand truth and found ZERO phantoms at every one — no claimed cycle falls inside a
verified door-CLOSED window, so the truth offers no evidence that any is false — while every
threshold above 0 destroyed REAL closes (ch27 13 matched -> 11 at 1s -> 5 at 3s; ch30 8 -> 0 at 3s).
Real closes on that corpus have short preceding dwells. So RTT ships at dwell=0 AND carries the same
caveat the cycle counts carry, on every surface. Changing DWELL_S here without re-running that
validation would put RTT on a claim rule the panel does not use.

A G STOP IS A CYCLE AT G, NOT A FLOOR READ AT G. A car passing G reads 'G' on the panel without the
doors ever opening; counting that as a stop would shorten every RTT that contains one. The floor is
taken from the rows of the cycle itself.

PLAUSIBILITY IS REPORTED, NOT DROPPED. Outside [30, 600]s the trip goes to an anomaly bucket with a
reason, and the anomaly RATE is printed beside n — it is a quality metric on floor attribution, not
noise to be swept up. A high rate means the G reads are wrong, which is a finding about the
instrument and not about the lift.

  python3 tools/rtt.py --db /var/lib/liftlab/gateway.db --cam ch29
  python3 tools/rtt.py --db ... --cam ch29 --era 260d4a0f --hours 168
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DWELL_S = 0.0                     # see the module docstring; do not change without re-validating
RTT_MIN_S, RTT_MAX_S = 30.0, 600.0
PEAK_WINDOWS = {"AM peak": (8, 10), "PM peak": (18, 20)}
SHEET_OPEN_S, SHEET_CLOSE_S = 3.00, 2.00        # MEP-02 v28, mirrored from liftlab_report.eras
DWELL_CAVEAT = ("cycle counts include transitions no hand-timed close corroborates; a minimum-dwell "
                "filter was tested and rejected (every threshold above 0s destroyed real closes)")


def cycles_with_floor(rows):
    """-> [(ts_closed, floor_at_that_cycle)] using _h3_cycle_ts's rule, verbatim.

    The floor carried is the last non-NULL floor seen at or before the close, within the same
    open->closed run: that is the floor the car was AT while its doors were open.
    """
    out, prev, cur_floor = [], None, None
    for r in rows:
        st, fl = r["door_state"], r["floor"]
        if fl is not None:
            cur_floor = fl
        if st == "closed" and prev in ("closing", "open"):
            out.append((r["ts"], cur_floor))
            cur_floor = None                      # a new run starts with no floor asserted
        if st != prev:
            prev = st
    return out


def opens_with_floor(rows):
    """-> [(ts_open, floor)] for transitions INTO 'open'. The door OPENING at G ends a round trip."""
    out, prev, cur_floor = [], None, None
    for r in rows:
        st, fl = r["door_state"], r["floor"]
        if fl is not None:
            cur_floor = fl
        if st == "open" and prev != "open":
            out.append((r["ts"], cur_floor))
        if st != prev:
            prev = st
    return out


def trips(rows, home="G"):
    """Round trips: door CLOSED at `home` -> the next door OPEN at `home`.

    Both ends must be STOPS at home — a cycle whose floor is home, and an open whose floor is home —
    so a car merely passing the ground floor cannot end a trip.
    """
    closes = [(t, f) for t, f in cycles_with_floor(rows)]
    opens = [(t, f) for t, f in opens_with_floor(rows)]
    out = []
    oi = 0
    for t0, f0 in closes:
        if f0 != home:
            continue
        while oi < len(opens) and opens[oi][0] <= t0:
            oi += 1
        j = oi
        while j < len(opens):
            t1, f1 = opens[j]
            if f1 == home:
                out.append((t0, t1, t1 - t0))
                break
            j += 1
    return out


def classify(dt):
    if dt < RTT_MIN_S:
        return "short (<30s) — likely a phantom G, or the doors reopened at G"
    if dt > RTT_MAX_S:
        return "long (>600s) — likely a missed G read, or the car was parked"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--era", default="", help="door_version prefix; default = the newest present")
    ap.add_argument("--hours", type=float, default=168.0)
    ap.add_argument("--home", default="G")
    ap.add_argument("--banks", default="lift_banks.json")
    ap.add_argument("--sheet-interval", type=float, default=None,
                    help="the sheet's computed interval for this lift's bank, seconds")
    ap.add_argument("--bank-lifts", type=int, default=None,
                    help="lifts in the bank, so measured RTT can be expressed as an interval")
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    import time as _t
    t1 = _t.time()
    t0 = t1 - a.hours * 3600

    # ERA SCOPING ON THE FULL door_version, NOT AN 8-CHAR PREFIX.
    #
    # The prefix is the TEMPLATES hash; the engine tag and the levels tag come AFTER it. So
    # `LIKE '260d4a0f%'` matched three different eras on ch29 — '260d4a0f+…' (untagged, pre-tagging),
    # '260d4a0fh2+…' and '260d4a0fh2Laa52+…' — 99,026 rows pooled across three instruments, and it
    # would pool h3 in too the moment it appeared, since h3 keeps the same templates hash. My first
    # version did exactly that while printing a single era name, which is worse than not scoping at
    # all: it looks scoped.
    versions = [r["door_version"] for r in db.execute(
        "SELECT door_version, COUNT(*) n FROM gw_door_event WHERE cam=? AND ts>=? AND ts<? "
        "AND door_version IS NOT NULL GROUP BY door_version ORDER BY MAX(ts) DESC", (a.cam, t0, t1))]
    if a.era:
        matched = [v for v in versions if v.startswith(a.era)]
        if len(matched) > 1:
            print(f"=== RTT — {a.cam} ===")
            print(f"  --era {a.era!r} matches {len(matched)} DIFFERENT door_versions:")
            for v in matched:
                print(f"    {v}")
            print("  These are different instruments. The prefix is the TEMPLATES hash; the engine")
            print("  and levels tags follow it, so a prefix does not identify an era. Pass one in")
            print("  full, e.g. --era " + matched[0])
            return 2
        era_full = matched[0] if matched else a.era
    else:
        era_full = versions[0] if versions else ""
    era = era_full
    rows = db.execute("SELECT ts, floor, door_state, door_version FROM gw_door_event "
                      "WHERE cam=? AND ts>=? AND ts<? AND door_version = ? ORDER BY ts, id",
                      (a.cam, t0, t1, era_full)).fetchall()
    print(f"=== RTT — {a.cam}, era {era or '(none)'}, last {a.hours:g}h ===")
    if len(versions) > 1:
        print(f"  ({len(versions)} eras present in range; this is scoped to the newest. Others: "
              + ", ".join(versions[1:4]) + (" …" if len(versions) > 4 else "") + ")")
    print(f"  {len(rows)} gw_door_event rows; home floor = {a.home!r}")
    if not rows:
        print("  no rows — nothing to measure")
        return 1
    engine = "h3-state" if any("h3-state" in (r["door_version"] or "") for r in rows) else "h2/edge"
    attributed = sum(1 for r in rows if r["floor"] is not None)
    print(f"  engine {engine}; floor attributed on {attributed}/{len(rows)} rows "
          f"({100.0 * attributed / len(rows):.0f}%)")
    print(f"  claim rule: dash_api._h3_cycle_ts, dwell={DWELL_S:g}s")
    print(f"  CAVEAT: {DWELL_CAVEAT}")

    def stats(v):
        v = sorted(v)
        return (len(v), statistics.median(v), v[min(len(v) - 1, int(0.85 * len(v)))])

    tr = trips(rows, a.home)
    good, anomalies = [], Counter()
    per_hour = {}
    for ts, _te, dt in tr:
        why = classify(dt)
        h = datetime.fromtimestamp(ts, IST).hour
        b = per_hour.setdefault(h, {"ok": [], "anom": 0})
        if why:
            anomalies[why] += 1
            b["anom"] += 1
        else:
            good.append((ts, dt))
            b["ok"].append(dt)
    n_all = len(tr)
    print(f"\n  round trips found: {n_all}   plausible: {len(good)}   "
          f"anomalies: {n_all - len(good)}"
          + (f" ({100.0 * (n_all - len(good)) / n_all:.0f}%)" if n_all else ""))
    for why, c in anomalies.most_common():
        print(f"    {c:>5}  {why}")
    if n_all and (n_all - len(good)) / n_all > 0.25:
        print("    ANOMALY RATE ABOVE 25% — this is a statement about FLOOR ATTRIBUTION, not about")
        print("    the lift. Treat the medians below as provisional until it comes down.")
    if not good:
        print("\n  no plausible round trips — nothing to report")
        return 1

    print(f"\n  {'hour':>5} {'n':>5} {'median s':>9} {'p85 s':>8} {'anom':>5}")
    for h in range(24):
        b = per_hour.get(h)
        if not b or not b["ok"]:
            continue
        n, med, p85 = stats(b["ok"])
        print(f"  {h:02d}:00 {n:>5} {med:>9.1f} {p85:>8.1f} {b['anom']:>5}")

    print(f"\n  {'window':>10} {'n':>5} {'median s':>9} {'p85 s':>8}")
    n, med, p85 = stats([d for _t_, d in good])
    print(f"  {'all-day':>10} {n:>5} {med:>9.1f} {p85:>8.1f}")
    for name, (lo, hi) in PEAK_WINDOWS.items():
        v = [d for ts, d in good if lo <= datetime.fromtimestamp(ts, IST).hour < hi]
        if v:
            n, med, p85 = stats(v)
            print(f"  {name:>10} {n:>5} {med:>9.1f} {p85:>8.1f}")
    print("  (THE PEAK TRAP: the sheet's coefficients describe a PEAK design condition, not an "
          "all-day average. Peak and all-day are separate lines; the ratio is itself a finding.)")

    # ── THE FREE VALIDATION: measured RTT vs RTT rebuilt from its own components ─────────────
    # If a directly-measured RTT and one summed from the coefficients disagree, one instrument is
    # wrong and we would rather know before publishing than after. It is reported even when rough.
    print("\n  COMPONENT CHECK — measured RTT vs the sum of its parts")
    dwell_v = []                                  # door-open duration per stop, measured here
    prev_open_ts, prev = None, None
    for r in rows:
        st = r["door_state"]
        if st == "open" and prev != "open":
            prev_open_ts = r["ts"]
        elif st in ("closing", "closed") and prev == "open" and prev_open_ts is not None:
            d = r["ts"] - prev_open_ts
            if 0 < d < 300:
                dwell_v.append(d)
            prev_open_ts = None
        if st != prev:
            prev = st
    stops_per_trip = None
    if good:
        # stops in a trip = cycles between the trip's endpoints, averaged
        cyc = [t for t, _f in cycles_with_floor(rows)]
        per = [sum(1 for c in cyc if ts < c <= ts + d) for ts, d in good]
        stops_per_trip = statistics.median(per) if per else None
    if dwell_v:
        dn, dmed, _dp = stats(dwell_v)
        print(f"    measured door-open dwell per stop: median {dmed:.1f}s over {dn} stops")
    else:
        print("    measured door-open dwell: none resolvable from these rows")
    if stops_per_trip is not None:
        print(f"    measured stops per round trip: median {stops_per_trip:.0f}")
    _n, med_rtt, _p = stats([d for _t_, d in good])
    if dwell_v and stops_per_trip:
        door_part = stops_per_trip * (dmed + SHEET_OPEN_S + SHEET_CLOSE_S)
        print(f"    door+dwell component: {stops_per_trip:.0f} stops x "
              f"({dmed:.1f} dwell + {SHEET_OPEN_S:.1f} open + {SHEET_CLOSE_S:.1f} close) "
              f"= {door_part:.0f}s of a measured {med_rtt:.0f}s RTT "
              f"({100.0 * door_part / med_rtt:.0f}%)")
        print(f"    remainder = {med_rtt - door_part:.0f}s, which must be TRAVEL + lost time.")
    print("    THE COMPARISON CANNOT BE COMPLETED. Closing the loop needs a travel figure, and")
    print("    there is none: h3 emits close_travel_s=NULL by design, the h2 travel numbers were")
    print("    invalidated 2026-08-05, and tools/weekly_stopwatch.md LEG 2 — the hand-timed sample")
    print("    that would supply it — has never run. So the parts can be measured and the sum")
    print("    cannot be checked. That is a gap in the evidence, not a rough answer.")

    # ASSUMPTION BESIDE OBSERVATION, no verdict.
    if a.sheet_interval is not None:
        print(f"\n  sheet computed interval for this bank: {a.sheet_interval:.1f}s")
        if a.bank_lifts:
            n, med, _p = stats([d for _t_, d in good])
            print(f"  measured RTT median {med:.1f}s over {a.bank_lifts} lifts "
                  f"-> implied interval {med / a.bank_lifts:.1f}s")
        else:
            print("  lifts-in-bank not supplied, so measured RTT cannot be expressed as an interval "
                  "(interval = RTT / lifts). Pass --bank-lifts.")
        print("  Both figures are shown; no verdict is drawn here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
