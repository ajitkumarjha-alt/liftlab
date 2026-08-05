#!/usr/bin/env python3
"""Compare travel-estimator FAMILIES against the frame-anchored truth, on one signal dump.

63064cb reported that four estimator families disagreed by over a second on the same six closes,
and that the disagreement — not any one family's score — was the finding. That was on a spliced
corpus with n=6, so it could not distinguish "the signal cannot time travel" from "the test is
underpowered". This runs the same families against the clean corpus so the question is answerable,
and reports which family comes closest rather than quietly keeping the best one.

The families, all reading the same per-frame signal dump:

  level      near_open -> close_th crossing of the closed-template NCC. DECISION 1's own definition,
             transplanted onto the validated signal.
  r1090      10% -> 90% crossing within the detected ramp. The literal form of the original spec.
             Note it is definitionally ~0.8x a first-motion-to-fully-closed hand timing on a linear
             ramp, so a systematic negative bias here is expected, not evidence of a bad signal.
  foot       derivative-based: walk back from the closed plateau while the signal is still rising,
             tolerating short flats, to find the foot of the ramp.
  motion     duration of sustained above-baseline motion in the top band. This is the only family
             whose definition matches the hand timing exactly (first motion -> motion stops), and on
             a camera where top-band motion is a validated state signal it is the natural candidate.

Verdict per family is DECISION 1: at least MIN_TIMED timed closes measured within tolerance, and
every value the family actually produced within tolerance.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io
from door_signal_validate import (MIN_TIMED, PASS_TRAVEL, detect_closes, pctl, read_sig, smooth,
                                  travel_level)


def est_level(v, t, i, ctx):
    return travel_level(v, t, i, ctx["near_open"], ctx["close_th"])[2]


def est_r1090(v, t, i, ctx):
    s, f, _ = travel_level(v, t, i, ctx["near_open"], ctx["close_th"])
    js = next((j for j in range(len(t)) if t[j] >= s), 0)
    jf = next((j for j in range(len(t)) if t[j] >= f), len(t) - 1)
    seg = v[js:jf + 1]
    if len(seg) < 3:
        return None
    lo, hi = min(seg), max(seg)
    if hi - lo < 1e-6:
        return None
    a, b = lo + 0.10 * (hi - lo), lo + 0.90 * (hi - lo)
    t10 = next((t[js + k] for k, x in enumerate(seg) if x >= a), None)
    t90 = next((t[js + k] for k, x in enumerate(seg) if x >= b), None)
    return None if (t10 is None or t90 is None) else t90 - t10


def est_foot(v, t, i, ctx, eps=0.004, flat_run=4):
    f = i
    while f > 0 and v[f - 1] >= ctx["close_th"]:
        f -= 1
    j, flat, foot = f, 0, f
    while j > 0 and t[f] - t[j] < 12.0:
        if v[j] - v[j - 1] > eps:
            flat, foot = 0, j - 1
        else:
            flat += 1
            if flat >= flat_run:
                break
        j -= 1
    return t[f] - t[foot]


def est_motion(v, t, i, ctx):
    m, mt = ctx["motion"], ctx["mtime"]
    thr = ctx["m_thr"]
    f = i
    while f > 0 and v[f - 1] >= ctx["close_th"]:
        f -= 1
    a = f
    while a > 0 and m[a] >= thr and mt[f] - mt[a] < 12.0:
        a -= 1
    b = f
    while b < len(m) - 1 and m[b] >= thr and mt[b] - mt[f] < 6.0:
        b += 1
    return mt[b] - mt[a]


FAMILIES = {"level": est_level, "r1090": est_r1090, "foot": est_foot, "motion": est_motion}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--state-signal", default="ncc_top")
    ap.add_argument("--motion-signal", default="top_motion")
    ap.add_argument("--hi", type=float, required=True)
    ap.add_argument("--lo", type=float, required=True)
    ap.add_argument("--tol-f", type=float, default=90.0)
    ap.add_argument("--truth", default=truth_io.DEFAULT_TRUTH)
    a = ap.parse_args()

    sig = read_sig(a.sig)
    timed = [t for t in truth_io.load_truth(a.cam, a.truth) if t["timed"]]
    v, t, ev = detect_closes(sig, a.state_signal, a.hi, a.lo)
    CL, OP = pctl(v, 90), pctl(v, 10)
    rng = CL - OP
    m = [r[a.motion_signal] for r in sig]
    ctx = {"near_open": OP + 0.10 * rng, "close_th": CL - 0.10 * rng,
           "motion": smooth(m, 3), "mtime": t,
           "m_thr": pctl(m, 25) + 0.25 * (pctl(m, 95) - pctl(m, 25))}

    print(f"=== {a.cam}: {len(ev)} detected closes, {len(timed)} timed truth closes, "
          f"tolerance +/-{PASS_TRAVEL:g}s ===")
    print(f"  {'family':>8} {'produced':>9} {'in-band':>8} {'mean|err|':>10} {'max|err|':>9} "
          f"{'bias':>7}  DECISION 1")
    detail = {}
    for name, fn in FAMILIES.items():
        rows = []
        for tr in timed:
            cand = [i for i in ev if abs(sig[i]["frame"] - tr["end_f"]) <= a.tol_f]
            if not cand:
                rows.append((tr, None))
                continue
            i = min(cand, key=lambda j: abs(sig[j]["frame"] - tr["end_f"]))
            try:
                val = fn(v, t, i, ctx)
            except Exception:
                val = None
            rows.append((tr, val))
        errs = [val - tr["travel_s"] for tr, val in rows if val is not None]
        produced = len(errs)
        inband = sum(1 for e in errs if abs(e) <= PASS_TRAVEL)
        ok = produced > 0 and inband == produced and inband >= MIN_TIMED
        detail[name] = rows
        print(f"  {name:>8} {produced:>9} {inband:>8} "
              f"{(sum(abs(e) for e in errs)/produced if produced else float('nan')):>10.2f} "
              f"{(max(abs(e) for e in errs) if produced else float('nan')):>9.2f} "
              f"{(sum(errs)/produced if produced else float('nan')):>+7.2f}  "
              f"{'PASS' if ok else 'FAIL'}")

    best = max(FAMILIES, key=lambda n: sum(
        1 for tr, val in detail[n] if val is not None and abs(val - tr["travel_s"]) <= PASS_TRAVEL))
    print(f"\n  closest family: {best}")
    print(f"  {'truth end_f':>11} {'hand_s':>7} " + " ".join(f"{n:>9}" for n in FAMILIES))
    for k, tr in enumerate(timed):
        cells = []
        for n in FAMILIES:
            val = detail[n][k][1]
            cells.append("        -" if val is None else f"{val:>9.2f}")
        print(f"  {tr['end_f']:>11} {tr['travel_s']:>7.2f} " + " ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
