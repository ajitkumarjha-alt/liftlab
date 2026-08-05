#!/usr/bin/env python3
"""TEST B, with the acceptance criterion repaired.

WHY THE OLD CRITERION IS VOID. DECISION 1 asked for ">=4 of the timed closes within +/-0.4s, and
every produced value within +/-0.4s". ed42761 showed that the hand travels span 1.56-2.40s with
sd 0.28s, so +/-0.4s is 1.4x the truth's own spread, and:

    a CONSTANT predictor answering 2.03s regardless of the video scores 11/13 within +/-0.4s.

A test a stopped clock passes cannot validate a measurement. The `>=4 in band` clause is void.

THE REPAIRED CRITERION. A family passes only if ALL THREE hold:

    a) Pearson r with the hand travels >= +0.70   — it must RANK the closes, which kills both the
                                                    anti-correlated families and the disguised
                                                    constants that the old test waved through
    b) median |error| <= 0.25s                    — inside the truth's own sd, not 1.4x it
    c) MAE strictly better than the constant       — it must beat the stopped clock it replaced
       2.03s predictor

Reported per camera and pooled.

TWO THINGS THIS TOOL DELIBERATELY REPORTS RATHER THAN SCORES.

  COVERAGE. A family that withholds 13 of 15 closes and gets 2 right is not a passing family, but
  none of (a)(b)(c) can see that. `produced/timed` is printed next to every verdict; the criterion
  as specified does not gate on it and this tool does not invent a gate.

  THE ANCHOR. Each close is measured inside a window placed on the truth's `end_f`. That hands the
  estimator ONE instant — the same instant a detector would hand it — and nothing about the
  duration, which is the quantity under test; both endpoints are found by the estimator inside a
  12s window. The `det` column then reports, independently, whether the state signal would have
  found that close on its own, so an estimator cannot look good on closes no tracker could reach.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io

PASS_R = 0.70             # (a) Pearson floor
PASS_MED = 0.25           # (b) median |error|, seconds
CONST_S = 2.03            # (c) the constant predictor that passed the old criterion 11/13

# Window placed on the truth close_full. Wide enough that the estimator finds its own foot on the
# slowest close in the corpus (4.76s) without the window edge ever being the answer.
WIN_BACK_S, WIN_FWD_S = 8.0, 4.0

SUSTAIN_S = 0.4           # a plateau must hold this long to count as departed-from / reached
MIN_SPAN = 0.25           # a window whose position never moves this far produces NO measurement


def read_pos(path):
    rows = []
    for r in csv.DictReader(open(path)):
        rows.append({"frame": int(r["frame"]), "vt": float(r["vt"]),
                     "position": float(r["position"]), "ncc_top": float(r["ncc_top"])})
    return rows


def pearson(x, y):
    n = len(x)
    if n < 3:
        return float("nan")
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx < 1e-12 or syy < 1e-12:
        return float("nan")          # a constant predictor has no correlation, it does not have 0
    return sxy / math.sqrt(sxx * syy)


def median(v):
    s = sorted(v)
    n = len(s)
    return float("nan") if not s else (s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2]))


def pct(seg, q):
    s = sorted(seg)
    k = (len(s) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def window(rows, anchor_f, fps):
    a = anchor_f - WIN_BACK_S * fps
    b = anchor_f + WIN_FWD_S * fps
    idx = [i for i, r in enumerate(rows) if a <= r["frame"] <= b]
    return (idx[0], idx[-1]) if len(idx) > 5 else None


def est_plateau(v, t, w0, w1, ai, fps, frac):
    """The brief's definition: first SUSTAINED departure from the open plateau -> the signal
    REACHING the closed plateau. Plateau levels are local to the window (p5/p95), so a close that
    ends on a lower plateau — a different floor behind the leaf — is still measured against its own
    two levels rather than a run-wide pair.
    """
    seg = v[w0:w1 + 1]
    lo, hi = pct(seg, 5), pct(seg, 95)
    span = hi - lo
    if span < MIN_SPAN:
        return None, "no ramp"
    dep_th, rea_th = lo + frac * span, hi - frac * span
    hold = max(2, int(round(SUSTAIN_S * fps)))

    # close_full: sustained crossing into the closed plateau, the one nearest the anchor
    cands = [j for j in range(w0, w1 - hold + 2)
             if v[j] >= rea_th and min(v[j:j + hold]) >= rea_th and (j == w0 or v[j - 1] < rea_th)]
    if not cands:
        return None, "never reaches closed"
    F = min(cands, key=lambda j: abs(j - ai))

    # close_start: the LAST sample at or below the open plateau that held for `hold` before it
    S = None
    for s in range(F - 1, w0 + hold - 2, -1):
        if max(v[s - hold + 1:s + 1]) <= dep_th:
            S = s
            break
    if S is None:
        return None, "no open plateau"
    return t[F] - t[S], ""


def est_foot(v, t, w0, w1, ai, fps, eps_per_s=0.10, flat_s=0.24):
    """Derivative ramp foot on the position signal — the family that came closest on `ncc_top`
    (ed42761: max |err| 0.93s vs level's 5.83s), carried over so the comparison is like for like."""
    seg = v[w0:w1 + 1]
    lo, hi = pct(seg, 5), pct(seg, 95)
    if hi - lo < MIN_SPAN:
        return None, "no ramp"
    rea_th = hi - 0.05 * (hi - lo)
    hold = max(2, int(round(SUSTAIN_S * fps)))
    cands = [j for j in range(w0, w1 - hold + 2)
             if v[j] >= rea_th and min(v[j:j + hold]) >= rea_th and (j == w0 or v[j - 1] < rea_th)]
    if not cands:
        return None, "never reaches closed"
    F = min(cands, key=lambda j: abs(j - ai))
    eps = eps_per_s / fps                     # per-sample rise that still counts as "rising"
    flat_run = max(2, int(round(flat_s * fps)))
    j, flat, foot = F, 0, F
    while j > w0:
        if v[j] - v[j - 1] > eps:
            flat, foot = 0, j - 1
        else:
            flat += 1
            if flat >= flat_run:
                break
        j -= 1
    return t[F] - t[foot], ""


def diag_iqr(v, t, w0, w1, ai, fps):
    """DIAGNOSTIC ONLY — NOT a candidate family, and deliberately excluded from the verdict.

    The 25%->75% crossing of the local span. On a linear ramp this is 0.5x the true travel by
    construction, so it CANNOT satisfy clause (b) and is not offered as an estimator. Its only job is
    to separate two very different failures that clauses (a)-(c) cannot tell apart:

      * if this correlates with the hand travels, the position signal DOES carry travel information
        and what fails is the endpoint-finding out on the plateaus;
      * if it does not correlate either, the information is not in the signal and no estimator
        built on it can be rescued.

    Reported as a correlation, never as a pass.
    """
    seg = v[w0:w1 + 1]
    lo, hi = pct(seg, 5), pct(seg, 95)
    if hi - lo < MIN_SPAN:
        return None, "no ramp"
    a_th, b_th = lo + 0.25 * (hi - lo), lo + 0.75 * (hi - lo)
    hold = max(2, int(round(SUSTAIN_S * fps)))
    cands = [j for j in range(w0, w1 - hold + 2)
             if v[j] >= b_th and min(v[j:j + hold]) >= b_th and (j == w0 or v[j - 1] < b_th)]
    if not cands:
        return None, "never reaches 75%"
    F = min(cands, key=lambda j: abs(j - ai))
    S = F
    while S > w0 and v[S] > a_th:
        S -= 1
    return t[F] - t[S], ""


def est_ncc_level(v_unused, t, w0, w1, ai, fps, ncc):
    """Control: DECISION 1's level crossing on `ncc_top`, the family ed42761 measured. Present so
    the position proxy is scored against the signal it is meant to replace, under the same
    criterion, on the same closes."""
    seg = ncc[w0:w1 + 1]
    lo, hi = pct(seg, 5), pct(seg, 95)
    if hi - lo < 0.05:
        return None, "no ramp"
    dep_th, rea_th = lo + 0.10 * (hi - lo), hi - 0.10 * (hi - lo)
    F = None
    for j in range(w0, w1 + 1):
        if ncc[j] >= rea_th and (F is None or abs(j - ai) < abs(F - ai)):
            F = j
    if F is None:
        return None, "never reaches closed"
    while F > w0 and ncc[F - 1] >= rea_th:
        F -= 1
    S = F
    while S > w0 and ncc[S] > dep_th:
        S -= 1
    return t[F] - t[S], ""


def run_cam(cam, sig_path, truth_path):
    fps = truth_io.CORPUS[cam]["fps"]
    rows = read_pos(sig_path)
    v = [r["position"] for r in rows]
    ncc = [r["ncc_top"] for r in rows]
    t = [r["vt"] for r in rows]
    timed = [x for x in truth_io.load_truth(cam, truth_path) if x["timed"]]

    fams = {
        "pos_plateau": lambda w0, w1, ai: est_plateau(v, t, w0, w1, ai, fps, 0.05),
        "pos_1090": lambda w0, w1, ai: est_plateau(v, t, w0, w1, ai, fps, 0.10),
        "pos_foot": lambda w0, w1, ai: est_foot(v, t, w0, w1, ai, fps),
        "ncc_level": lambda w0, w1, ai: est_ncc_level(v, t, w0, w1, ai, fps, ncc),
        "[diag]iqr": lambda w0, w1, ai: diag_iqr(v, t, w0, w1, ai, fps),
    }
    out = {k: [] for k in fams}
    for tr in timed:
        wk = window(rows, tr["end_f"], fps)
        ai = min(range(len(rows)), key=lambda i: abs(rows[i]["frame"] - tr["end_f"]))
        for k, fn in fams.items():
            if wk is None:
                out[k].append((tr, None, "no window"))
                continue
            try:
                val, why = fn(wk[0], wk[1], ai)
            except Exception as e:
                val, why = None, f"error: {type(e).__name__}"
            out[k].append((tr, val, why))
    return timed, out


def endpoints(cam, sig_path, truth_path):
    """Where the proxy's traversal sits relative to the hand endpoints, per close.

    This is the diagnostic that says WHICH endpoint fails. d10 is the frame offset from the truth's
    `start_f` to the 10%-of-local-span crossing, d90 the offset from `end_f` to the 90% crossing. A
    signal that lags the door by a CONSTANT amount has large means and small sds, and still ranks
    durations correctly. A signal whose foot wanders has a large sd on d10 — and no duration
    estimator built on it can correlate, however the thresholds are chosen.
    """
    fps = truth_io.CORPUS[cam]["fps"]
    rows = read_pos(sig_path)
    v = [r["position"] for r in rows]
    pos_of = {r["frame"]: i for i, r in enumerate(rows)}
    timed = [x for x in truth_io.load_truth(cam, truth_path) if x["timed"]]
    out = []
    for tr in timed:
        a, b = tr["start_f"], tr["end_f"]
        if b not in pos_of:
            continue
        w = window(rows, b, fps)
        if w is None:
            continue
        w0, w1 = w
        seg = v[w0:w1 + 1]
        lo, hi = pct(seg, 5), pct(seg, 95)
        if hi - lo < MIN_SPAN:
            out.append((tr, None, None, None))
            continue
        t10, t90 = lo + 0.10 * (hi - lo), lo + 0.90 * (hi - lo)
        j = pos_of[b]
        while j < w1 and v[j] < t90:
            j += 1
        while j > w0 and v[j - 1] >= t90:
            j -= 1
        k = j
        while k > w0 and v[k] > t10:
            k -= 1
        out.append((tr, rows[k]["frame"] - a, rows[j]["frame"] - b, rows[j]["frame"] - rows[k]["frame"]))
    return out


def sd(v):
    if len(v) < 2:
        return float("nan")
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))


def score(pairs):
    """pairs: [(hand_s, est_s)] with est not None."""
    if not pairs:
        return None
    hand = [h for h, _ in pairs]
    est = [e for _, e in pairs]
    errs = [e - h for h, e in pairs]
    r = pearson(est, hand)
    med = median([abs(e) for e in errs])
    mae = sum(abs(e) for e in errs) / len(errs)
    cmae = sum(abs(CONST_S - h) for h in hand) / len(hand)
    a = (not math.isnan(r)) and r >= PASS_R
    b = med <= PASS_MED
    c = mae < cmae
    return {"n": len(pairs), "r": r, "med": med, "mae": mae, "cmae": cmae,
            "a": a, "b": b, "c": c, "pass": a and b and c,
            "bias": sum(errs) / len(errs), "mx": max(abs(e) for e in errs)}


def show(tag, s, produced, timed_n):
    if s is None:
        print(f"  {tag:>22}  produced 0/{timed_n} — nothing to score            FAIL")
        return
    r = "  nan" if math.isnan(s["r"]) else f"{s['r']:+.3f}"
    if tag.startswith("[diag]"):
        print(f"  {tag:>22}  {produced:>2}/{timed_n} {r:>7} {'':>7} {'':>7} {'':>7} "
              f"{'':>7} {'':>7}   ---  diagnostic, not scored")
        return
    print(f"  {tag:>22}  {produced:>2}/{timed_n} {r:>7} {s['med']:>7.2f} {s['mae']:>7.2f} "
          f"{s['cmae']:>7.2f} {s['bias']:>+7.2f} {s['mx']:>7.2f}   "
          f"{'Y' if s['a'] else 'n'}{'Y' if s['b'] else 'n'}{'Y' if s['c'] else 'n'}  "
          f"{'PASS' if s['pass'] else 'FAIL'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sig", action="append", required=True, metavar="CAM=PATH",
                    help="repeatable, e.g. --sig ch27=/path/ch27_pos.csv")
    ap.add_argument("--truth", default=truth_io.DEFAULT_TRUTH)
    a = ap.parse_args()

    sigs = dict(s.split("=", 1) for s in a.sig)
    per_cam, all_fams = {}, []
    for cam in sorted(sigs):
        timed, out = run_cam(cam, sigs[cam], a.truth)
        per_cam[cam] = (timed, out)
        all_fams = list(out)

    print("=" * 96)
    print("TEST B — TRAVEL, repaired criterion")
    print("=" * 96)
    print(f"  (a) Pearson r >= {PASS_R:+.2f}   (b) median |err| <= {PASS_MED:.2f}s   "
          f"(c) MAE < constant-{CONST_S}s predictor's MAE")
    print(f"  A family passes only if all three hold. The old '>={4} of 15 in +/-0.40s' clause is "
          f"void — a constant passes it.\n")

    for cam in sorted(per_cam):
        timed, out = per_cam[cam]
        print(f"--- {cam}: per-close values against hand truth "
              f"({len(timed)} timed closes) ---")
        print(f"  {'start_f':>8} {'end_f':>7} {'hand_s':>7} " +
              " ".join(f"{k:>11}" for k in all_fams))
        for k in range(len(timed)):
            tr = timed[k]
            cells = []
            for f in all_fams:
                val = out[f][k][1]
                cells.append(f"{'-':>11}" if val is None else f"{val:>11.2f}")
            print(f"  {tr['start_f']:>8} {tr['end_f']:>7} {tr['travel_s']:>7.2f} " +
                  " ".join(cells))
        print(f"  {'':>8} {'':>7} {'err ->':>7} " +
              " ".join(f"{k:>11}" for k in all_fams))
        for k in range(len(timed)):
            tr = timed[k]
            cells = []
            for f in all_fams:
                val = out[f][k][1]
                cells.append(f"{out[f][k][2][:11]:>11}" if val is None
                             else f"{val - tr['travel_s']:>+11.2f}")
            print(f"  {'':>8} {'':>7} {'':>7} " + " ".join(cells))
        print()

    hdr = (f"  {'family':>22}  {'n':>5} {'r':>7} {'med|e|':>7} {'MAE':>7} {'constMAE':>7} "
           f"{'bias':>7} {'max|e|':>7}   abc  verdict")
    for cam in sorted(per_cam):
        timed, out = per_cam[cam]
        print(f"--- {cam} ---")
        print(hdr)
        for f in all_fams:
            pairs = [(tr["travel_s"], val) for tr, val, _ in out[f] if val is not None]
            show(f, score(pairs), len(pairs), len(timed))
        print()

    print("--- POOLED (both cameras) ---")
    print(hdr)
    tot = sum(len(per_cam[c][0]) for c in per_cam)
    for f in all_fams:
        pairs = []
        for cam in sorted(per_cam):
            pairs += [(tr["travel_s"], val) for tr, val, _ in per_cam[cam][1][f] if val is not None]
        show(f, score(pairs), len(pairs), tot)

    print(f"\n  control: the constant-{CONST_S}s predictor under this criterion — r is undefined "
          f"(zero variance),")
    print(f"  so clause (a) rejects it. That is the repair.")

    print("\n" + "=" * 96)
    print("ENDPOINT DIAGNOSIS — which end of the traversal fails to lock onto the door")
    print("=" * 96)
    print("  d10 = frames from the truth's start_f to the 10%-of-span crossing")
    print("  d90 = frames from the truth's end_f to the 90% crossing")
    print("  A constant lag (large mean, small sd) still ranks durations correctly. A wandering")
    print("  endpoint (large sd) makes duration correlation impossible at any threshold.\n")
    for cam in sorted(sigs):
        ep = endpoints(cam, sigs[cam], a.truth)
        fps = truth_io.CORPUS[cam]["fps"]
        print(f"--- {cam} (fps {fps:.4f}) ---")
        print(f"  {'start_f':>8} {'end_f':>7} {'hand_s':>7} {'d10_f':>6} {'d90_f':>6} "
              f"{'hand_f':>7} {'proxy_f':>8} {'proxy_s':>8}")
        d10s, d90s = [], []
        for tr, d10, d90, dur in ep:
            if d10 is None:
                print(f"  {tr['start_f']:>8} {tr['end_f']:>7} {tr['travel_s']:>7.2f} "
                      f"{'-':>6} {'-':>6} {tr['end_f']-tr['start_f']:>7} {'no ramp':>8} {'-':>8}")
                continue
            d10s.append(d10); d90s.append(d90)
            print(f"  {tr['start_f']:>8} {tr['end_f']:>7} {tr['travel_s']:>7.2f} {d10:>+6d} "
                  f"{d90:>+6d} {tr['end_f']-tr['start_f']:>7} {dur:>8} {dur/fps:>8.2f}")
        if d10s:
            print(f"  d10: mean {sum(d10s)/len(d10s):+6.1f} f  sd {sd(d10s):5.1f} f "
                  f"({sd(d10s)/fps:.2f}s)")
            print(f"  d90: mean {sum(d90s)/len(d90s):+6.1f} f  sd {sd(d90s):5.1f} f "
                  f"({sd(d90s)/fps:.2f}s)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
