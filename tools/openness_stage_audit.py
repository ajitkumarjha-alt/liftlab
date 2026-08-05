#!/usr/bin/env python3
"""Which stage of the door signal path destroys the descent? Reads openness_trace.py's CSV.

The claim under test is DECISION 2's: the tracker "never observes the descent". That is measured
here as INTERMEDIATE OCCUPANCY — the number of analyzed frames a cycle spends strictly between the
tracker's own two thresholds (close_th=0.10 and near_open=0.90). A 2.4s close at 12.5fps should
present ~30 such frames. The h2 cycles present 0-2.

The point of this script is that occupancy is computed at EVERY stage of the path with the SAME
band, so the stage where it collapses is the stage that is lying:

  raw col   -> normalised against a GLOBAL, whole-run p2/p98 reference  (what the geometry supports)
  openness  -> normalised against the tracker's ROLLING p10/p90 refs    (what the tracker uses)
  bright_f  -> min-max over the window                                  (threshold-free control)

If raw col shows ~30 intermediate frames where openness shows 0, the leaf positions were recovered
and then thrown away, and the defect is in the normalisation, not the edge metric or the camera.
"""
import argparse
import csv
import datetime as dt
import sys

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import truth_io

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"

CLOSE_TH, NEAR_OPEN = 0.10, 0.90       # DoorTracker defaults — the band the state machine needs


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def load(path):
    rows = []
    for r in csv.DictReader(open(path)):
        def f(k):
            v = r.get(k, "")
            return float(v) if v not in ("", "None", None) else None
        rows.append({"epoch": f("epoch"), "vt": f("vt"), "osd": r["osd"], "col": f("col"),
                     "strength": f("strength"), "ref_lo": f("ref_lo"), "ref_hi": f("ref_hi"),
                     "ref_span": f("ref_span"), "openness": f("openness"),
                     "bright": f("bright_f_128"), "centroid": f("col_centroid"),
                     "state_before": r["state_before"], "state_after": r["state_after"],
                     "emitted": int(r["emitted"]), "kept": int(r["kept"])})
    return rows


def pct(vals, q):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def band_count(vals, lo, hi):
    """Frames strictly inside the tracker's decision band, after min-max normalisation to [lo,hi]."""
    v = [x for x in vals if x is not None]
    if len(v) < 2:
        return 0, 0.0
    a, b = min(v), max(v)
    if b - a < 1e-9:
        return 0, 0.0
    n = sum(1 for x in v if CLOSE_TH < (x - a) / (b - a) < NEAR_OPEN)
    return n, b - a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    ap.add_argument("--half", type=float, default=8.0)
    ap.add_argument("--fps", type=float, default=12.5)
    a = ap.parse_args()

    rows = load(a.csv)
    cols = [r["col"] for r in rows if r["col"] is not None]
    g_lo, g_hi = pct(cols, 2), pct(cols, 98)     # global geometric reference for this camera/ROI
    print(f"=== {a.cam}  {len(rows)} analyzed frames ===")
    print(f"  raw edge col: global p2={g_lo:.1f} p98={g_hi:.1f}  span={g_hi-g_lo:.1f}px "
          f"(min {min(cols):.0f} max {max(cols):.0f})")

    spans = [r["ref_span"] for r in rows if r["ref_span"] is not None]
    print(f"  tracker ROLLING ref span (px): p05={pct(spans,5):.1f} p25={pct(spans,25):.1f} "
          f"p50={pct(spans,50):.1f} p75={pct(spans,75):.1f} p95={pct(spans,95):.1f}")
    starved = sum(1 for s in spans if s < 0.5 * (g_hi - g_lo))
    print(f"  frames whose rolling span is < HALF the geometric span: {starved}/{len(spans)} "
          f"({100.0*starved/max(1,len(spans)):.1f}%)  [min_span_col guard = 6.0px, so these all PASS]")

    # ---- run-wide intermediate occupancy, same band at each stage
    def occ(key, lo=None, hi=None):
        v = [r[key] for r in rows if r[key] is not None]
        if lo is None:
            lo, hi = min(v), max(v)
        return sum(1 for x in v if CLOSE_TH < (x - lo) / (hi - lo) < NEAR_OPEN), len(v)

    n_o, t_o = occ("openness", 0.0, 1.0)
    n_c, t_c = occ("col", g_lo, g_hi)
    n_b, t_b = occ("bright")
    print(f"\n  WHOLE-RUN intermediate occupancy (fraction of frames strictly in the "
          f"{CLOSE_TH:.2f}-{NEAR_OPEN:.2f} band):")
    print(f"    raw col   (global p2/p98 ref) : {n_c:5d}/{t_c} = {100.0*n_c/t_c:5.1f}%")
    print(f"    openness  (tracker rolling)   : {n_o:5d}/{t_o} = {100.0*n_o/t_o:5.1f}%")
    print(f"    bright_f  (control)           : {n_b:5d}/{t_b} = {100.0*n_b/t_b:5.1f}%")

    # ---- per hand-timed close
    truth = truth_io.load_truth(a.cam, a.truth)
    print(f"\n  PER HAND-TIMED CLOSE (+/-{a.half:g}s window). 'inter' = analyzed frames in the band;")
    print(f"  a {'travel'} of T seconds should show ~{a.fps:.0f}*T of them.\n")
    print(f"  {'truth osd':>10} {'hand_s':>7} {'expect':>7} | {'col':>5} {'openness':>8} {'bright':>7} "
          f"| {'colspan':>8} {'refspan':>8} {'brightspan':>10}  status")
    tot = {"col": 0, "openness": 0, "bright": 0, "expect": 0}
    for t in truth:
        if t["status"] == "truncated":
            continue
        c = t["end_f"] / truth_io.CORPUS[a.cam]["fps"]
        sel = [r for r in rows if abs(r["epoch"] - c) <= a.half]
        if not sel:
            continue
        nc, sc = band_count([r["col"] for r in sel], None, None)
        no = sum(1 for r in sel if r["openness"] is not None and CLOSE_TH < r["openness"] < NEAR_OPEN)
        nb, sb = band_count([r["bright"] for r in sel], None, None)
        rs = [r["ref_span"] for r in sel if r["ref_span"] is not None]
        hand = t["travel_s"]
        exp = f"{a.fps*hand:.0f}" if hand else "-"
        if hand:
            tot["expect"] += a.fps * hand
        tot["col"] += nc; tot["openness"] += no; tot["bright"] += nb
        print(f"  {t['osd']:>10} {(f'{hand:.1f}' if hand else '-'):>7} {exp:>7} | "
              f"{nc:>5} {no:>8} {nb:>7} | {sc:>8.1f} "
              f"{(sum(rs)/len(rs) if rs else 0):>8.1f} {sb:>10.4f}  {t['status']}")
    print(f"\n  TOTALS over hand-timed closes: raw col {tot['col']} intermediate frames, "
          f"openness {tot['openness']}, bright {tot['bright']}")

    # ---- frozen-frame census (the corpus caveat, quantified rather than assumed)
    frozen = 0
    for i in range(1, len(rows)):
        p, q = rows[i - 1], rows[i]
        if (p["bright"] is not None and q["bright"] is not None
                and abs(p["bright"] - q["bright"]) < 1e-6 and p["col"] == q["col"]):
            frozen += 1
    print(f"\n  frozen frames (identical bright_f AND identical col to the previous analyzed frame): "
          f"{frozen}/{len(rows)} ({100.0*frozen/len(rows):.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
