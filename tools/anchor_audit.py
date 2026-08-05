#!/usr/bin/env python3
"""Anchor each close on the CONTINUOUS control signal, then ask what the tracker's stages did.

Every previous measurement anchored on the tracker's own thresholds, which begs the question: if
`openness` is broken, the interval it defines is not the close, and "openness spent 0 frames
descending" is unfalsifiable. So anchor elsewhere.

bright_f (ROI bright-pixel fraction) is independently established to ramp smoothly over 50-65 frames
on both cameras. Here it defines the close: its 10%->90% crossing across the transition IS the
physical descent, on the same frames, from the same ROI. Then, over exactly those frames, report:

    what the raw edge column did   (door_edge_column, stage 1)
    what openness did              (rolling normalisation, stages 3-4)

This separates "the pixels do not carry the descent" from "the descent was carried and discarded",
and it does so per cycle rather than in aggregate.
"""
import argparse
import csv
import datetime as dt
import sys

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import truth_io

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def load(path):
    rows = []
    for r in csv.DictReader(open(path)):
        def f(k):
            v = r.get(k, "")
            return float(v) if v not in ("", "None", None) else None
        rows.append({"epoch": f("epoch"), "osd": r["osd"], "col": f("col"), "openness": f("openness"),
                     "ref_span": f("ref_span"), "bright": f("bright_f_128"), "emitted": int(r["emitted"])})
    return rows


def corr(xs, ys):
    p = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(p)
    if n < 3:
        return 0.0
    mx = sum(x for x, _ in p) / n
    my = sum(y for _, y in p) / n
    sxy = sum((x - mx) * (y - my) for x, y in p)
    sxx = sum((x - mx) ** 2 for x, _ in p) ** 0.5
    syy = sum((y - my) ** 2 for _, y in p) ** 0.5
    return sxy / (sxx * syy) if sxx and syy else 0.0


def ramp_1090(vals, ts, rising):
    """10%->90% crossing of the largest monotone-ish excursion in the window. -> (t10, t90, lo, hi)."""
    v = [(t, x) for t, x in zip(ts, vals) if x is not None]
    if len(v) < 5:
        return None
    xs = [x for _, x in v]
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-6:
        return None
    a = lo + 0.10 * (hi - lo)
    b = lo + 0.90 * (hi - lo)
    # walk to the extreme that ENDS the window's transition, then back out to the other threshold
    if rising:
        i_end = max(range(len(v)), key=lambda i: v[i][1])
        j = i_end
        while j > 0 and v[j][1] > a:
            j -= 1
        k = j
        while k < i_end and v[k][1] < b:
            k += 1
        return v[j][0], v[k][0], lo, hi
    i_end = min(range(len(v)), key=lambda i: v[i][1])
    j = i_end
    while j > 0 and v[j][1] < b:
        j -= 1
    k = j
    while k < i_end and v[k][1] > a:
        k += 1
    return v[j][0], v[k][0], lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    ap.add_argument("--half", type=float, default=8.0)
    a = ap.parse_args()

    rows = load(a.csv)
    r_bc = corr([r["bright"] for r in rows], [r["col"] for r in rows])
    r_bo = corr([r["bright"] for r in rows], [r["openness"] for r in rows])
    rising = r_bc < 0          # bright rises as the column falls -> bright HIGH means CLOSED
    print(f"=== {a.cam} ===")
    print(f"  corr(bright_f, raw col) = {r_bc:+.3f}   corr(bright_f, openness) = {r_bo:+.3f}")
    print(f"  -> bright_f {'RISES' if rising else 'FALLS'} as the door closes on this camera\n")

    truth = truth_io.load_truth(a.cam, a.truth)
    print(f"  Close anchored on bright_f's 10->90% crossing (+/-{a.half:g}s search window).")
    print(f"  'col moved' / 'openness moved' are measured over EXACTLY those frames.\n")
    print(f"  {'truth osd':>10} {'hand_s':>7} {'bright_s':>9} {'frames':>7} | {'col span':>9} "
          f"{'col path':>9} | {'open span':>10} {'pinned':>7} | {'refspan':>8}  status")
    tot_b, tot_h = [], []
    for t in truth:
        c = t["end_f"] / truth_io.CORPUS[a.cam]["fps"]
        sel = [r for r in rows if abs(r["epoch"] - c) <= a.half]
        if len(sel) < 5:
            continue
        rr = ramp_1090([r["bright"] for r in sel], [r["epoch"] for r in sel], rising)
        if not rr:
            continue
        t10, t90, blo, bhi = rr
        seg = [r for r in sel if min(t10, t90) <= r["epoch"] <= max(t10, t90)]
        if len(seg) < 2:
            continue
        cvals = [r["col"] for r in seg if r["col"] is not None]
        ovals = [r["openness"] for r in seg if r["openness"] is not None]
        # col "path" = total absolute frame-to-frame movement; span = end-to-end. path >> span
        # means the edge detector is hopping between features rather than tracking one leaf.
        path = sum(abs(cvals[i] - cvals[i - 1]) for i in range(1, len(cvals))) if len(cvals) > 1 else 0
        pinned = sum(1 for o in ovals if o in (0.0, 1.0))
        rs = [r["ref_span"] for r in seg if r["ref_span"] is not None]
        hand = t["travel_s"]
        dur = abs(t90 - t10)
        if hand:
            tot_b.append(dur); tot_h.append(hand)
        print(f"  {t['osd']:>10} {(f'{hand:.1f}' if hand else '-'):>7} {dur:>9.2f} "
              f"{len(seg):>7} | {(max(cvals)-min(cvals) if cvals else 0):>9.1f} {path:>9.0f} | "
              f"{(max(ovals)-min(ovals) if ovals else 0):>10.3f} "
              f"{f'{pinned}/{len(ovals)}':>7} | {(sum(rs)/len(rs) if rs else 0):>8.1f}  {t['status']}")
    if tot_h:
        errs = [b - h for b, h in zip(tot_b, tot_h)]
        print(f"\n  bright_f-anchored travel vs hand-timed (n={len(tot_h)}): "
              f"mean err={sum(errs)/len(errs):+.2f}s  min={min(errs):+.2f}s  max={max(errs):+.2f}s")
        print(f"    hand:     " + ", ".join(f"{h:.2f}" for h in tot_h))
        print(f"    bright_f: " + ", ".join(f"{b:.2f}" for b in tot_b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
