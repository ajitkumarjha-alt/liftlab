#!/usr/bin/env python3
"""Reconstruct each DESCENT at both stages and show which one loses it. Reads openness_trace.py CSV.

openness_stage_audit.py measured occupancy over a whole +/-8s window, which counts closed-state
jitter as "intermediate" and therefore cannot discriminate. This measures the transition itself:

  for a window around a hand-timed close, find the last frame at/above near_open and the first
  frame after it at/below close_th, and report how many analyzed frames lie between them.

That interval IS close_start -> close_full. Doing it on the tracker's `openness` reproduces what
h2 timed. Doing it on the raw edge column normalised against a GLOBAL p2/p98 reference asks what
the SAME frames would have yielded had the reference not moved. The difference between the two
numbers is the cost of the rolling reference, in frames.

Also prints the clipping census: how often `openness` is pinned at exactly 0.0 or 1.0 because the
raw column fell outside the rolling [p10, p90] refs. A metric that is clipped is not measuring.
"""
import argparse
import csv
import datetime as dt
import sys

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"
CLOSE_TH, NEAR_OPEN = 0.10, 0.90


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
                     "ref_lo": f("ref_lo"), "ref_hi": f("ref_hi"), "ref_span": f("ref_span"),
                     "bright": f("bright_f_128"), "emitted": int(r["emitted"])})
    return rows


def pct(vals, q):
    v = sorted(vals)
    k = (len(v) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def descent(sel, key, lo, hi, invert=False):
    """Last frame >= near_open followed by the first frame <= close_th. -> (n_frames, seconds, span).

    Returns the LAST such descent in the window (the close nearest the truth timestamp).
    """
    norm = []
    for r in sel:
        v = r[key]
        norm.append(None if v is None else max(0.0, min(1.0, (v - lo) / (hi - lo))))
    best = None
    i = 0
    while i < len(norm):
        if norm[i] is not None and norm[i] >= NEAR_OPEN:
            j = i + 1
            while j < len(norm) and (norm[j] is None or norm[j] > CLOSE_TH):
                if norm[j] is not None and norm[j] >= NEAR_OPEN:
                    i = j          # a later top — restart the descent from there
                j += 1
            if j < len(norm):
                best = (j - i - 1, sel[j]["epoch"] - sel[i]["epoch"], i, j)
                i = j
        i += 1
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    ap.add_argument("--half", type=float, default=8.0)
    a = ap.parse_args()

    rows = load(a.csv)
    cols = [r["col"] for r in rows if r["col"] is not None]
    g_lo, g_hi = pct(cols, 2), pct(cols, 98)

    # ---- clipping census
    clip_hi = sum(1 for r in rows if r["col"] is not None and r["ref_hi"] is not None and r["col"] >= r["ref_hi"])
    clip_lo = sum(1 for r in rows if r["col"] is not None and r["ref_lo"] is not None and r["col"] <= r["ref_lo"])
    have = sum(1 for r in rows if r["openness"] is not None)
    pinned = sum(1 for r in rows if r["openness"] in (0.0, 1.0))
    print(f"=== {a.cam} ===")
    print(f"  global geometric reference (whole run): p2={g_lo:.1f} p98={g_hi:.1f} span={g_hi-g_lo:.1f}px")
    print(f"  CLIPPING CENSUS over {have} frames with an openness value:")
    print(f"    col >= rolling p90 (openness pinned 1.0): {clip_hi:5d} ({100.0*clip_hi/have:.1f}%)")
    print(f"    col <= rolling p10 (openness pinned 0.0): {clip_lo:5d} ({100.0*clip_lo/have:.1f}%)")
    print(f"    openness exactly 0.0 or 1.0            : {pinned:5d} ({100.0*pinned/have:.1f}%)")

    truth = [r for r in csv.DictReader(open(a.truth)) if r["cam"] == a.cam and r["status"] != "truncated"]
    print(f"\n  DESCENT RECONSTRUCTION (+/-{a.half:g}s around each hand-timed close)")
    print(f"  'frames' = analyzed frames strictly between the last near_open and the first close_th.\n")
    print(f"  {'truth osd':>10} {'hand_s':>7} | {'openness: frames':>17} {'secs':>6} | "
          f"{'rawcol: frames':>15} {'secs':>6} | {'refspan':>8} {'ref_hi':>7} {'colmax':>7}  status")
    agg = []
    for t in truth:
        c = osd_to_epoch(t["close_end_osd"])
        sel = [r for r in rows if abs(r["epoch"] - c) <= a.half]
        if not sel:
            continue
        d_o = descent(sel, "openness", 0.0, 1.0)
        d_c = descent(sel, "col", g_lo, g_hi)
        rs = [r["ref_span"] for r in sel if r["ref_span"] is not None]
        rh = [r["ref_hi"] for r in sel if r["ref_hi"] is not None]
        cm = max(r["col"] for r in sel if r["col"] is not None)
        hand = float(t["travel_s"]) if t["travel_s"] else None
        fo = f"{d_o[0]}" if d_o else "no descent"
        so = f"{d_o[1]:.2f}" if d_o else "-"
        fc = f"{d_c[0]}" if d_c else "no descent"
        sc = f"{d_c[1]:.2f}" if d_c else "-"
        print(f"  {t['close_end_osd']:>10} {(f'{hand:.1f}' if hand else '-'):>7} | {fo:>17} {so:>6} | "
              f"{fc:>15} {sc:>6} | {(sum(rs)/len(rs) if rs else 0):>8.1f} "
              f"{(sum(rh)/len(rh) if rh else 0):>7.1f} {cm:>7.1f}  {t['status']}")
        if hand and d_o and d_c:
            agg.append((hand, d_o[1], d_c[1]))
    if agg:
        print(f"\n  matched to hand-timed travel (n={len(agg)}):")
        print(f"    mean hand={sum(x[0] for x in agg)/len(agg):.2f}s  "
              f"openness-descent={sum(x[1] for x in agg)/len(agg):.2f}s  "
              f"rawcol-descent={sum(x[2] for x in agg)/len(agg):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
