#!/usr/bin/env python3
"""Derive each camera's horizontal band from where its door ACTUALLY transitions.

WHY. `door_signal_v2.TOP_BAND_Y = (15, 85)` is one hard-coded band applied to both cameras, chosen
on the reasoning that a strip above head height sees the leaf and not the passengers (5e58211). That
reasoning is sound and camera-independent; the COORDINATES are not. Head height in the frame depends
on where the camera is mounted, and ch30 is a corner mount whose doorway occupies mid/lower frame.
A band placed on the wall above the opening still correlates with door state — the whole scene
changes when the leaf arrives — but it cannot TIME the leaf, because the rows it averages are not
the rows the leaf sweeps. That is one mechanism behind ncc_top saturating early (ed42761).

WHAT THIS MEASURES. Nothing about signals or thresholds. For each hand-timed close in the ground
truth, take the door's appearance shortly BEFORE the close starts (open) and shortly AFTER it ends
(closed), and ask, per image row: how much did this row change?

    E[y] = median over closes of  mean_x | closed(y, x) - open(y, x) |

Rows the leaf sweeps change a lot. Rows of fixed wall, ceiling or cabin trim change little — a
passenger walking through moves them on any one close, which is why the reduction across closes is
a MEDIAN and not a mean. E[y] is in gray levels and is directly readable.

The band is then the contiguous run of rows maximising mean E[y] at a fixed height, searched over
the whole frame rather than assumed. Reported alongside is CONSISTENCY: the fraction of closes on
which a row moved in the same direction as the median close. A row with high energy but split
direction is a row passengers cross, not a row the leaf sweeps.

OUTPUT is `tools/band_coords.json`, consumed by door_signal_v2 and position_closedness, plus a
per-row CSV and an annotated frame so the choice is auditable rather than asserted.

USAGE
  python3 tools/band_placement.py --cam ch30 --out-dir tools/band_evidence
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io

# Frames sampled either side of a close to characterise open / closed appearance. The gap keeps the
# leaf's own motion out of both samples: the truth's endpoints carry ~+/-2 frames, and a close that
# is mistimed by a few frames would otherwise put moving leaf into the "open" sample.
LEAD_GAP, LEAD_N = 12, 20        # open  sample: [start_f - GAP - N, start_f - GAP)
TAIL_GAP, TAIL_N = 12, 20        # closed sample: (end_f + GAP, end_f + GAP + N]

BAND_H_DEFAULT = 70              # rows; matches the 70-row height of the incumbent (15, 85)
MIN_CONSISTENCY = 0.6            # a row must agree in sign on this fraction of closes to be trusted

BAND_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "band_coords.json")


def close_rows(cam):
    """Truth closes usable for band derivation: `clean` only — the excluded statuses are excluded
    precisely because an endpoint is unreliable, and both endpoints are load-bearing here."""
    return [t for t in truth_io.load_truth(cam) if t["status"] == "clean"]


def collect(cam, closes):
    """One decode pass. -> {frame_index: gray ROI-column slice}, full frame height."""
    import cv2
    x, _, w, _ = truth_io.CORPUS[cam]["roi"]
    want = set()
    for t in closes:
        want |= set(range(max(1, t["start_f"] - LEAD_GAP - LEAD_N), max(1, t["start_f"] - LEAD_GAP)))
        want |= set(range(t["end_f"] + TAIL_GAP, t["end_f"] + TAIL_GAP + TAIL_N))
    cap = cv2.VideoCapture(truth_io.video_path(cam))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {truth_io.video_path(cam)}")
    got, i, mx = {}, 0, max(want)
    while True:
        ok, fr = cap.read()
        if not ok or i > mx:
            break
        i += 1
        if i in want:
            got[i] = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)[:, x:x + w].astype(np.float32)
    cap.release()
    return got


def per_close_delta(got, t):
    """(signed row delta, coverage) for one close: closed-sample minus open-sample, per row.

    Each side is a MEDIAN over its sampled frames, so one passenger standing through part of the
    sample does not carry the row.
    """
    op = [got[f] for f in range(max(1, t["start_f"] - LEAD_GAP - LEAD_N),
                                max(1, t["start_f"] - LEAD_GAP)) if f in got]
    cl = [got[f] for f in range(t["end_f"] + TAIL_GAP, t["end_f"] + TAIL_GAP + TAIL_N) if f in got]
    if len(op) < 5 or len(cl) < 5:
        return None, (len(op), len(cl))
    o = np.median(np.stack(op), axis=0)
    c = np.median(np.stack(cl), axis=0)
    return (c - o), (len(op), len(cl))


def static_noise(cam, got_extra):
    """Per-row temporal spread while the door is NOT moving.

    A row that changes when nothing is opening or closing is a row carrying something other than the
    leaf: the burnt-in OSD clock, a reflection, a passenger. Sampled from the door-closed windows in
    phantom_periods, which are defined by absence of door motion. Reported next to the transition
    energy so a band is never chosen on movement that is not the door's.
    """
    if len(got_extra) < 5:
        return None
    S = np.stack([got_extra[f] for f in sorted(got_extra)])
    med = np.median(S, axis=0)
    return np.abs(S - med).mean(axis=(0, 2))          # (H,) mean abs deviation per row, gray levels


def collect_static(cam, n_per_window=24):
    """Frames from inside the door-closed windows — the door is stationary in all of them."""
    import cv2
    x, _, w, _ = truth_io.CORPUS[cam]["roi"]
    want = set()
    for p in truth_io.load_phantoms(cam):
        a, b = p["start_f"], p["end_f"]
        if b - a < n_per_window:
            continue
        step = (b - a) // n_per_window
        want |= {a + i * step for i in range(n_per_window)}
    if not want:
        return {}
    cap = cv2.VideoCapture(truth_io.video_path(cam))
    got, i, mx = {}, 0, max(want)
    while True:
        ok, fr = cap.read()
        if not ok or i > mx:
            break
        i += 1
        if i in want:
            got[i] = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)[:, x:x + w].astype(np.float32)
    cap.release()
    return got


def derive(cam, band_h, out_dir):
    closes = close_rows(cam)
    if len(closes) < 3:
        raise SystemExit(f"{cam}: {len(closes)} clean closes — refusing to place a band on that")
    got = collect(cam, closes)
    deltas, used = [], []
    for t in closes:
        d, cov = per_close_delta(got, t)
        if d is None:
            print(f"    skip close {t['start_f']}-{t['end_f']}: only {cov[0]} open / {cov[1]} closed "
                  f"frames resolved")
            continue
        deltas.append(d)
        used.append(t)
    if len(deltas) < 3:
        raise SystemExit(f"{cam}: only {len(deltas)} closes resolved")

    D = np.stack(deltas)                                    # (n_closes, H, W) signed
    row_abs = np.abs(D).mean(axis=2)                        # (n_closes, H) magnitude per row
    E = np.median(row_abs, axis=0)                          # (H,) energy
    row_signed = D.mean(axis=2)                             # (n_closes, H) direction per row
    med_sign = np.sign(np.median(row_signed, axis=0))
    agree = (np.sign(row_signed) == med_sign).mean(axis=0)  # (H,) consistency

    H = len(E)
    N = static_noise(cam, collect_static(cam))
    if N is None:
        N = np.zeros(H)
    # SNR is what a band should maximise, not raw energy: a row that also moves when the door is
    # still contributes change the leaf did not cause.
    snr = E / np.maximum(N, 1.0)
    # Contiguous band of fixed height maximising mean energy, restricted to rows that agree in sign.
    Eg = np.where(agree >= MIN_CONSISTENCY, E, 0.0)
    csum = np.concatenate([[0.0], np.cumsum(Eg)])
    scores = (csum[band_h:] - csum[:-band_h]) / band_h
    y0 = int(np.argmax(scores))
    y1 = y0 + band_h

    inc0, inc1 = 15, 85                                     # the incumbent, for comparison
    def mean_of(a, b):
        return float(E[a:b].mean())

    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/{cam}_row_energy.csv", "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["row", "energy_gray", "static_noise_gray", "snr", "consistency",
                     "in_chosen_band", "in_incumbent_band"])
        for yy in range(H):
            wr.writerow([yy, round(float(E[yy]), 3), round(float(N[yy]), 3), round(float(snr[yy]), 3),
                         round(float(agree[yy]), 3), int(y0 <= yy < y1), int(inc0 <= yy < inc1)])

    print(f"  {cam}: {len(deltas)} clean closes, ROI columns {D.shape[2]}, frame rows {H}")
    print(f"    CHOSEN band y{y0}-{y1}   mean energy {mean_of(y0, y1):.2f} gray, "
          f"median consistency {float(np.median(agree[y0:y1])):.2f}")
    print(f"    incumbent  y{inc0}-{inc1}   mean energy {mean_of(inc0, inc1):.2f} gray, "
          f"median consistency {float(np.median(agree[inc0:inc1])):.2f}")
    print(f"    ratio chosen/incumbent = {mean_of(y0, y1) / max(1e-6, mean_of(inc0, inc1)):.2f}x")
    print(f"    static noise (door still): chosen {float(N[y0:y1].mean()):.2f} gray, "
          f"incumbent {float(N[inc0:inc1].mean()):.2f} gray")
    print(f"    SNR (energy / static noise):  chosen {float(snr[y0:y1].mean()):.2f}, "
          f"incumbent {float(snr[inc0:inc1].mean()):.2f}")
    noisiest = int(np.argmax(N))
    print(f"    noisiest static row y{noisiest} at {N[noisiest]:.2f} gray "
          f"({'INSIDE' if inc0 <= noisiest < inc1 else 'outside'} the incumbent band)")
    peak = int(np.argmax(E))
    print(f"    peak row y{peak} at {E[peak]:.2f} gray; frame-wide median {float(np.median(E)):.2f}")
    return {"cam": cam, "band_y": [y0, y1], "band_h": band_h,
            "mean_energy_gray": round(mean_of(y0, y1), 3),
            "median_consistency": round(float(np.median(agree[y0:y1])), 3),
            "static_noise_gray": round(float(N[y0:y1].mean()), 3),
            "snr": round(float(snr[y0:y1].mean()), 3),
            "incumbent_band_y": [inc0, inc1],
            "incumbent_mean_energy_gray": round(mean_of(inc0, inc1), 3),
            "incumbent_static_noise_gray": round(float(N[inc0:inc1].mean()), 3),
            "incumbent_snr": round(float(snr[inc0:inc1].mean()), 3),
            "peak_row": peak, "peak_energy_gray": round(float(E[peak]), 3),
            "n_closes": len(deltas),
            "closes_used": [[t["start_f"], t["end_f"]] for t in used]}, E, agree, got


def annotate(cam, info, E, got, out_dir):
    """Draw the chosen and incumbent bands on a real open frame, with the energy curve beside it."""
    import cv2
    x, _, w, _ = truth_io.CORPUS[cam]["roi"]
    f = sorted(got)[0]
    img = cv2.cvtColor(got[f].astype(np.uint8), cv2.COLOR_GRAY2BGR)
    y0, y1 = info["band_y"]; i0, i1 = info["incumbent_band_y"]
    cv2.rectangle(img, (0, i0), (w - 1, i1), (0, 0, 255), 1)
    cv2.rectangle(img, (0, y0), (w - 1, y1), (0, 255, 0), 2)
    cv2.putText(img, "incumbent", (3, max(10, i0 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
    cv2.putText(img, "chosen", (3, max(10, y0 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    H = len(E)
    plot = np.zeros((H, 160, 3), dtype=np.uint8)
    mx = float(E.max()) or 1.0
    for yy in range(H):
        cv2.line(plot, (0, yy), (int(155 * E[yy] / mx), yy), (200, 200, 200), 1)
    cv2.rectangle(plot, (0, y0), (159, y1), (0, 255, 0), 1)
    cv2.rectangle(plot, (0, i0), (159, i1), (0, 0, 255), 1)
    cv2.imwrite(f"{out_dir}/{cam}_band.png", np.hstack([img, plot]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", action="append", choices=sorted(truth_io.CORPUS),
                    help="repeatable; default all cameras")
    ap.add_argument("--band-h", type=int, default=BAND_H_DEFAULT)
    ap.add_argument("--out-dir", default="tools/band_evidence")
    ap.add_argument("--write", action="store_true", help=f"write {BAND_JSON}")
    a = ap.parse_args()

    cams = a.cam or sorted(truth_io.CORPUS)
    print("=== band placement from row-wise transition energy ===")
    out = {}
    for cam in cams:
        info, E, agree, got = derive(cam, a.band_h, a.out_dir)
        annotate(cam, info, E, got, a.out_dir)
        out[cam] = info
    if a.write:
        json.dump(out, open(BAND_JSON, "w"), indent=1)
        print(f"  wrote {BAND_JSON}")
    else:
        print("  (dry run — pass --write to commit the coordinates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
