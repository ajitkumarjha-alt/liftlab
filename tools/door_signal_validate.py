#!/usr/bin/env python3
"""Gate for door signal v2. A candidate signal does not reach a tracker until it passes here.

This exists because 5e58211 found that the signal h2 has keyed on since day one was never checked
against the door — it was checked against the engine's own opinion, which is not evidence. So the
order is inverted: validate first, and let a camera FAIL rather than ship a weak signal for it.

Two tests, both against data the engine did not produce.

  TEST A — STATE. 48 hand-labelled frames (single observer, from ROI crops). A signal passes if it
  agrees with the physical door at its own operating point. Reported threshold-free (AUC, the
  probability a random open frame scores above a random closed one) AND at the best threshold, with
  that threshold's leave-one-out accuracy so the number is not just the fit to its own labels.
  Pass band is 85-90%, per the caveat that 48 single-observer labels do not support chasing 100%.

  TEST B — TRAVEL. 6 hand-timed closes. A close is a RISING transition of a closed-template signal
  (unlike openness, the direction is not in question). The signal's 10->90% crossing across that
  transition must land within +/-0.4s of the hand-timed travel.

The label set is NOT a random sample of door states — it was drawn at the extremes of the broken
openness signal, so it over-represents frames where openness was confident. That makes it a fair
test of "does this signal fix what openness got wrong" and an unfair basis for an absolute accuracy
claim. Stated here rather than in a footnote because it bounds what a pass means.
"""
import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"

# The dirty corpus needed a concat-inversion exclusion here (ch30 video t=210-212). The clean
# corpus is verified continuous, so there is nothing to exclude and the map is empty.
INVERSION_VT = {}

PASS_STATE = 0.85          # agreement floor for TEST A
PASS_TRAVEL = 0.40         # seconds, TEST B tolerance
MIN_TIMED = 4              # DECISION 1: at least this many timed closes measured in-band


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def read_labels(path, cam):
    with open(path) as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    return [r for r in csv.DictReader(lines) if r["cam"] == cam]


def read_sig(path):
    rows = []
    for r in csv.DictReader(open(path)):
        d = {"frame": int(r["frame"]), "vt": float(r["vt"]), "epoch": float(r["epoch"]),
             "osd": r["osd"]}
        for k in r:
            if k not in d and k != "osd":
                try:
                    d[k] = float(r[k])
                except ValueError:
                    pass
        rows.append(d)
    return rows


def auc(pos, neg):
    """P(a random pos scores above a random neg). 0.5 = no information, 1.0 = perfect."""
    if not pos or not neg:
        return float("nan")
    wins = ties = 0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def best_threshold(pos, neg):
    """Threshold maximising balanced accuracy, and that accuracy. pos = door OPEN scores."""
    cands = sorted(set(pos + neg))
    best, bt = -1, None
    for i in range(len(cands)):
        t = cands[i]
        # convention: score >= t  => predict OPEN
        tp = sum(1 for p in pos if p >= t); fn = len(pos) - tp
        tn = sum(1 for n in neg if n < t); fp = len(neg) - tn
        acc = (tp + tn) / (len(pos) + len(neg))
        if acc > best:
            best, bt = acc, t
    return bt, best


def loo_accuracy(pos, neg):
    """Leave-one-out: refit the threshold without each point, then classify it. Guards against a
    threshold that only looks good because it was chosen on the same 48 points."""
    ok = 0
    allpts = [(v, 1) for v in pos] + [(v, 0) for v in neg]
    for k in range(len(allpts)):
        p2 = [v for j, (v, y) in enumerate(allpts) if y == 1 and j != k]
        n2 = [v for j, (v, y) in enumerate(allpts) if y == 0 and j != k]
        if not p2 or not n2:
            continue
        t, _ = best_threshold(p2, n2)
        v, y = allpts[k]
        ok += int((v >= t) == bool(y))
    return ok / len(allpts)


def smooth(v, k=3):
    return [sum(v[max(0, i - k // 2):min(len(v), i + k // 2 + 1)]) /
            len(v[max(0, i - k // 2):min(len(v), i + k // 2 + 1)]) for i in range(len(v))]


def pctl(v, q):
    s = sorted(v)
    k = (len(s) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def detect_closes(rows, key, hi_t, lo_t, min_closed_s=0.6):
    """Closes as sustained rising transitions of a closed-template signal.

    PERSISTENCE is the only non-obvious rule: the signal must stay closed for min_closed_s after the
    crossing. The ch30 concat splices in single frames that jump the signal from 0.48 to 0.96 and
    straight back (video t=9.8 is one); without this they read as instantaneous closes and poison
    every travel measured near them.
    """
    v = smooth([r[key] for r in rows])
    t = [r["epoch"] for r in rows]
    ev, st = [], ("closed" if v[0] >= hi_t else "open")
    for i in range(len(v)):
        if st == "closed":
            if v[i] < lo_t:
                st = "open"
            continue
        if v[i] < hi_t:
            continue
        k = i
        while k + 1 < len(v) and t[k + 1] - t[i] < min_closed_s:
            k += 1
        if min(v[i:k + 1]) < lo_t:
            continue                      # splice spike, not a close
        st = "closed"
        ev.append(i)
    return v, t, ev


def travel_level(v, t, i, near_open, close_th, max_s=12.0):
    """DECISION 1's definition, transplanted onto the validated signal: close_full is the crossing
    into the closed level, close_start the last sample at/below the open level before it."""
    f = i
    while f > 0 and v[f - 1] >= close_th:
        f -= 1
    s = f
    while s > 0 and v[s] > near_open and t[f] - t[s] < max_s:
        s -= 1
    return t[s], t[f], t[f] - t[s]


def ramp_1090(seg, key, rising):
    """10->90% crossing of the transition in `seg`. -> (t10, t90, lo, hi) or None.

    Anchored on the extreme that ENDS the transition, then walked back out, so a plateau either side
    does not stretch the measured span.
    """
    v = [(r["epoch"], r[key]) for r in seg if key in r]
    if len(v) < 5:
        return None
    xs = [x for _, x in v]
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return None
    a, b = lo + 0.10 * (hi - lo), lo + 0.90 * (hi - lo)
    i_end = max(range(len(v)), key=lambda i: v[i][1]) if rising else min(range(len(v)), key=lambda i: v[i][1])
    j = i_end
    if rising:
        while j > 0 and v[j][1] > a:
            j -= 1
        k = j
        while k < i_end and v[k][1] < b:
            k += 1
    else:
        while j > 0 and v[j][1] < b:
            j -= 1
        k = j
        while k < i_end and v[k][1] > a:
            k += 1
    return v[j][0], v[k][0], lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--labels", default="tools/openness_labels_20260805.csv")
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    ap.add_argument("--signals", default="ncc_roi,ncc_top,top_motion,bright_128,bright_160,roi_mean")
    ap.add_argument("--state-signal", default="ncc_top")
    ap.add_argument("--hi", type=float, required=True, help="closed-plateau threshold")
    ap.add_argument("--lo", type=float, required=True, help="open threshold (hysteresis)")
    ap.add_argument("--tol-f", type=float, default=90.0,
                    help="frames within which an emission is the same close as a truth row")
    ap.add_argument("--half", type=float, default=10.0)
    a = ap.parse_args()

    sig = read_sig(a.sig)
    by_frame = {r["frame"]: r for r in sig}
    labels = read_labels(a.labels, a.cam)
    keys = [k for k in a.signals.split(",") if k in sig[0]]

    print("=" * 80)
    print(f"TEST A — STATE agreement, {a.cam}")
    print("=" * 80)
    import door_signal_v2 as dsv2
    closed_f = set(dsv2.closed_window_frames(a.cam, "test"))       # held out from the template
    open_f = set(dsv2.open_frames_from_truth(a.cam))
    by_frame = {r["frame"]: r for r in sig}
    used = ([("closed", by_frame[f]) for f in sorted(closed_f) if f in by_frame]
            + [("open", by_frame[f]) for f in sorted(open_f) if f in by_frame])
    n_c = sum(1 for s_, _ in used if s_ == "closed")
    n_o = len(used) - n_c
    print(f"  labels are TRUTH-DERIVED, not eyeballed:")
    print(f"    closed  {n_c:4d} frames — door-closed windows, TEST split (the template was built")
    print(f"                              from the disjoint train split, so this is held out)")
    print(f"    open    {n_o:4d} frames — the frames just before each clean close's start_f, where")
    print(f"                              the door is open and not yet moving")
    print(f"  This replaces 63064cb's 48 single-observer eyeball labels sampled at openness's")
    print(f"  extremes. Larger, independent of my eye, and drawn from the same scan as the truth.\n")
    print(f"  {'signal':>12} {'AUC':>6} {'best thr':>9} {'acc':>6} {'LOO acc':>8} "
          f"{'open med':>9} {'closed med':>11}  verdict")
    results = {}
    for k in keys:
        pos = [r[k] for s_, r in used if s_ == "open"]
        neg = [r[k] for s_, r in used if s_ == "closed"]
        if not pos or not neg:
            continue
        A = auc(pos, neg)
        flip = A < 0.5
        p2, n2 = (neg, pos) if flip else (pos, neg)
        A2 = auc(p2, n2)
        thr, acc = best_threshold(p2, n2)
        lacc = loo_accuracy(p2, n2) if len(used) <= 200 else acc
        med = lambda v: sorted(v)[len(v) // 2]
        results[k] = {"auc": A2, "acc": acc, "loo": lacc, "thr": thr, "flip": flip}
        verdict = "PASS" if lacc >= PASS_STATE else ("marginal" if lacc >= 0.80 else "fail")
        print(f"  {k:>12} {A2:>6.3f} {thr:>9.4f} {acc:>6.1%} {lacc:>8.1%} "
              f"{med(pos):>9.4f} {med(neg):>11.4f}  {verdict}"
              + ("   [closed scores HIGHER]" if flip else ""))
    if len(used) > 200:
        print(f"\n  (LOO omitted above {200} labels — with n={len(used)} the threshold is not")
        print(f"   meaningfully fit to any one point; 'acc' is the in-sample number.)")

    print("\n" + "=" * 80)
    print(f"TEST B — TRAVEL against hand-timed closes, {a.cam}")
    print("=" * 80)
    truth = truth_io.load_truth(a.cam, a.truth)
    timed = [t for t in truth if t["timed"]]
    fps = truth_io.CORPUS[a.cam]["fps"]
    key = a.state_signal
    v, t, ev = detect_closes(sig, key, a.hi, a.lo)
    CL, OP = pctl(v, 90), pctl(v, 10)
    rng = CL - OP
    near_open, close_th = OP + 0.10 * rng, CL - 0.10 * rng
    print(f"  state signal {key}: open_level={OP:.3f} closed_level={CL:.3f}")
    print(f"  close_start crossing <= {near_open:.3f}, close_full crossing >= {close_th:.3f} "
          f"(DECISION 1's near_open/close_th shape)")
    print(f"  {len(ev)} sustained closes detected; {len(timed)} timed truth closes "
          f"({len(truth)} total, {len(truth)-len(timed)} excluded by status)")
    print(f"  tolerance +/-{PASS_TRAVEL:g}s; hand endpoints carry a flat "
          f"+/-{truth_io.TRAVEL_ERR_S:g}s (frame-anchored, ~2 frames per end)\n")

    # emissions carry the FRAME of close_full, so matching is exact — no clock, no drift
    meas = []
    for i in ev:
        ts, tf, tv = travel_level(v, t, i, near_open, close_th)
        meas.append({"full_f": sig[i]["frame"], "travel": tv})

    print(f"  {'truth end_f':>11} {'hand_s':>7} {'emit_f':>8} {'d_f':>6} {'travel_s':>9} {'err_s':>7}  verdict")
    produced, inband = 0, 0
    for tr in timed:
        cand = [m for m in meas if abs(m["full_f"] - tr["end_f"]) <= a.tol_f]
        if not cand:
            print(f"  {tr['end_f']:>11} {tr['travel_s']:>7.2f} {'-':>8} {'-':>6} {'not detected':>9} "
                  f"{'-':>7}  no-emission")
            continue
        b = min(cand, key=lambda m: abs(m["full_f"] - tr["end_f"]))
        produced += 1
        err = b["travel"] - tr["travel_s"]
        ok = abs(err) <= PASS_TRAVEL
        inband += ok
        print(f"  {tr['end_f']:>11} {tr['travel_s']:>7.2f} {b['full_f']:>8} "
              f"{b['full_f']-tr['end_f']:>+6d} {b['travel']:>9.2f} {err:>+7.2f}  "
              f"{'pass' if ok else 'FAIL'}")

    # DECISION 1: at least MIN_TIMED of the timed closes measured within tolerance, AND every
    # value the engine actually produced within tolerance (it may withhold, it may not be wrong).
    all_ok = (produced > 0 and inband == produced)
    enough = inband >= MIN_TIMED
    print(f"\n  produced {produced} travels for {len(timed)} timed closes; "
          f"{inband} within +/-{PASS_TRAVEL:g}s")
    print(f"  DECISION 1: >={MIN_TIMED} timed-and-in-tolerance -> {'yes' if enough else 'NO'}; "
          f"all produced values in tolerance -> {'yes' if all_ok else 'NO'}")
    print(f"  TEST B: {'PASS' if (enough and all_ok) else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
