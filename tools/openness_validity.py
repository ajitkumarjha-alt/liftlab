#!/usr/bin/env python3
"""Is `openness` a measurement of door position at all? Scores the hand labels + the traces.

Everything upstream of this asked HOW the descent is lost. This asks the prior question the h3
design assumed away: whether the signal the gate would key on tracks the door. Three tests, each
falsifiable and each run against something the engine did not produce:

  1. AGREEMENT AT THE EXTREMES. Frames sampled at the tracker's own thresholds (openness>=0.95 =
     "fully open", <=0.05 = "fully closed"), hand-labelled from the ROI crop. If openness measures
     the door, its extremes agree with the door. Disagreement here is not noise — near_open and
     close_th are exactly where the state machine commits.

  2. SEPARABILITY OF THE RAW COLUMN. If a raw edge column of N px means "open" at one moment and
     "closed" at another, no normalisation of it can be repaired — the information is not there.
     Reported as the overlap between the col distributions at the two openness extremes.

  3. REFERENCE STABILITY. openness = clip((col - p10)/(p90 - p10)) over a rolling 600-sample
     window. Reports how far that denominator sits below the run's own geometric range, and how
     many frames are consequently pinned at exactly 0.0 or 1.0.

Usage:
  python3 tools/openness_validity.py --labels tools/openness_labels_20260805.csv \\
      --trace ch30=/path/ch30_all.csv --trace ch27=/path/ch27_all.csv
"""
import argparse
import csv
import sys


def read_labels(path):
    rows = []
    with open(path) as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    for r in csv.DictReader(lines):
        r["openness"] = float(r["openness"])
        r["col"] = float(r["col"])
        rows.append(r)
    return rows


def read_trace(path):
    rows = []
    for r in csv.DictReader(open(path)):
        if r["openness"] in ("", "None"):
            continue
        rows.append({"openness": float(r["openness"]), "col": float(r["col"]),
                     "ref_span": float(r["ref_span"]) if r["ref_span"] not in ("", "None") else None,
                     "strength": float(r["strength"])})
    return rows


def pct(v, q):
    v = sorted(v)
    k = (len(v) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="tools/openness_labels_20260805.csv")
    ap.add_argument("--trace", action="append", default=[], help="cam=path/to/trace.csv")
    a = ap.parse_args()

    labels = read_labels(a.labels)
    traces = {}
    for t in a.trace:
        cam, path = t.split("=", 1)
        traces[cam] = read_trace(path)

    print("=" * 78)
    print("TEST 1 — does openness agree with the door at the tracker's own thresholds?")
    print("=" * 78)
    grand = {"agree": 0, "disagree": 0, "uncertain": 0}
    for cam in sorted({r["cam"] for r in labels}):
        rs = [r for r in labels if r["cam"] == cam]
        print(f"\n  {cam}:")
        for band, want, name in (("H", "open", "openness>=0.95  tracker says FULLY OPEN"),
                                 ("L", "closed", "openness<=0.05  tracker says FULLY CLOSED")):
            sel = [r for r in rs if r["id"].startswith(band)]
            ok = sum(1 for r in sel if r["door_state"] == want)
            bad = sum(1 for r in sel if r["door_state"] not in (want, "uncertain"))
            unc = sum(1 for r in sel if r["door_state"] == "uncertain")
            grand["agree"] += ok; grand["disagree"] += bad; grand["uncertain"] += unc
            print(f"    {name}: door really {want} in {ok}/{len(sel)}, "
                  f"really {'closed' if want=='open' else 'open'} in {bad}, uncertain {unc}")
    n = grand["agree"] + grand["disagree"] + grand["uncertain"]
    print(f"\n  BOTH CAMERAS, {n} labelled frames at the decision thresholds:")
    print(f"    openness agrees with the physical door : {grand['agree']:3d}  "
          f"({100.0*grand['agree']/n:.0f}%)")
    print(f"    openness states the OPPOSITE           : {grand['disagree']:3d}  "
          f"({100.0*grand['disagree']/n:.0f}%)")
    print(f"    uncertain (passenger fills doorway)    : {grand['uncertain']:3d}  "
          f"({100.0*grand['uncertain']/n:.0f}%)")
    print(f"\n    A coin flip scores 50%. The state machine commits at exactly these values.")

    print("\n" + "=" * 78)
    print("TEST 2 — is the raw edge column separable between open and closed?")
    print("=" * 78)
    for cam in sorted({r["cam"] for r in labels}):
        rs = [r for r in labels if r["cam"] == cam and r["door_state"] in ("open", "closed")]
        op = [r["col"] for r in rs if r["door_state"] == "open"]
        cl = [r["col"] for r in rs if r["door_state"] == "closed"]
        if not op or not cl:
            continue
        lo_ov, hi_ov = max(min(op), min(cl)), min(max(op), max(cl))
        n_ov = sum(1 for v in op + cl if lo_ov <= v <= hi_ov)
        print(f"\n  {cam}: col when door OPEN   n={len(op):2d}  "
              f"min={min(op):6.1f} med={pct(op,50):6.1f} max={max(op):6.1f}")
        print(f"        col when door CLOSED n={len(cl):2d}  "
              f"min={min(cl):6.1f} med={pct(cl,50):6.1f} max={max(cl):6.1f}")
        print(f"        overlapping range [{lo_ov:.1f}, {hi_ov:.1f}] contains "
              f"{n_ov}/{len(op)+len(cl)} of all labelled frames "
              f"({100.0*n_ov/(len(op)+len(cl)):.0f}%)")

    print("\n" + "=" * 78)
    print("TEST 3 — reference stability and clipping")
    print("=" * 78)
    for cam, rows in sorted(traces.items()):
        cols = [r["col"] for r in rows]
        geo = pct(cols, 98) - pct(cols, 2)
        spans = [r["ref_span"] for r in rows if r["ref_span"] is not None]
        pinned = sum(1 for r in rows if r["openness"] in (0.0, 1.0))
        weak = sum(1 for r in rows if r["strength"] < 0.30)
        print(f"\n  {cam}: {len(rows)} frames")
        print(f"    geometric range of col (p2..p98)      : {geo:.1f} px")
        print(f"    rolling reference span, median        : {pct(spans,50):.1f} px "
              f"({100.0*pct(spans,50)/geo:.0f}% of geometric)")
        print(f"    frames with span < half geometric     : "
              f"{sum(1 for s in spans if s < geo/2)}/{len(spans)} "
              f"({100.0*sum(1 for s in spans if s < geo/2)/len(spans):.0f}%)")
        print(f"    frames pinned at exactly 0.0 or 1.0   : {pinned} "
              f"({100.0*pinned/len(rows):.0f}%)")
        print(f"    frames rejected by min_strength=0.30  : {weak} "
              f"({100.0*weak/len(rows):.1f}%)  <- the abstain path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
