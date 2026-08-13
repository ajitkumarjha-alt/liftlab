#!/usr/bin/env python3
"""Does this template actually describe a CLOSED door? LOO cannot answer that; separation can.

THE FAILURE THIS EXISTS FOR, in the operator's own words: "my first scan used an open frame and
failed, don't repeat". A template built entirely from OPEN frames is perfectly self-consistent —
its leave-one-out NCC is excellent, because every frame it was built from looks like every other.
LOO measures whether the template represents ITS OWN inputs. It says nothing about whether those
inputs were the door state you meant.

What convicts is SEPARATION: score the template against frames it was NOT built from, split into
the declared closed windows and everything outside them. A good template scores high inside and
visibly lower outside. A template built from the wrong state scores high on the wrong side, or
shows no separation at all — and either way the number says so before anything is deployed.

Also verifies a named ANCHOR frame the operator confirmed by eye, because a distribution can be
right in aggregate and wrong where it matters.

  python3 tools/verify_state_template.py --cam ch34 --video ch34_cal.mp4 --anchor-s 200
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import numpy as np


def ncc(crops, tpl):
    T = tpl.astype(np.float32).ravel()
    T = T - T.mean()
    tn = float(np.sqrt((T * T).sum()))
    out = np.zeros(len(crops))
    for a in range(0, len(crops), 4000):
        C = crops[a:a + 4000].astype(np.float32).reshape(-1, T.size)
        C -= C.mean(axis=1, keepdims=True)
        nn = np.sqrt((C * C).sum(axis=1))
        nn[nn < 1e-6] = np.inf
        out[a:a + len(C)] = (C @ T) / (nn * tn)
    return out


def q(v, p):
    return float(np.percentile(v, p)) if len(v) else float("nan")


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--tpl-dir", default="door_state_templates")
    ap.add_argument("--stride", type=int, default=6)
    ap.add_argument("--anchor-s", type=float, action="append", default=[])
    a = ap.parse_args()

    meta = json.load(open(os.path.join(a.tpl_dir, f"{a.cam}.json")))
    png = base64.b64decode(meta["png_b64"])
    tpl = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    y0, y1 = meta["band_y"]
    x, w = meta["roi_x_w"]
    wh = tuple(meta["template_wh"])
    wins = [tuple(v) for v in meta["windows"]]
    built = set(meta["source_frames"])

    cap = cv2.VideoCapture(os.path.expanduser(a.video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {a.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    idx, crops = [], []
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % a.stride:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        idx.append(i)
        crops.append(cv2.resize(g[y0:y1, x:x + w], wh))
    cap.release()
    idx = np.array(idx)
    crops = np.stack(crops)
    s = ncc(crops, tpl)

    inwin = np.array([any(lo <= f <= hi for lo, hi in wins) for f in idx])
    heldout = np.array([f not in built for f in idx])
    ci = s[inwin & heldout]                 # closed, NOT used to build the template
    op = s[~inwin]                          # outside every declared closed window

    print(f"=== {a.cam} — {os.path.basename(a.video)} @ {fps:.4f}fps, every {a.stride}th frame ===")
    print(f"  template: {meta['n_frames']} frames, band y{y0}-{y1} x{x}-{x + w}, md5 {meta['md5'][:8]}")
    print(f"  LOO NCC median {meta['loo_ncc_median']} min {meta['loo_ncc_min']}   "
          f"(self-consistency only — cannot see a wrong-state template)")
    print(f"  sampled {len(idx)} frames: {int((inwin & heldout).sum())} held-out in-window, "
          f"{int((~inwin).sum())} outside every window")
    print(f"  HELD-OUT CLOSED  p05 {q(ci, 5):.3f}  p25 {q(ci, 25):.3f}  p50 {q(ci, 50):.3f}  "
          f"min {ci.min() if len(ci) else float('nan'):.3f}")
    print(f"  OUTSIDE WINDOWS  p10 {q(op, 10):.3f}  p25 {q(op, 25):.3f}  p50 {q(op, 50):.3f}  "
          f"p90 {q(op, 90):.3f}")
    # NOT "closed p05 minus outside p90". That reads as a separation score and is not one: the
    # outside pool is mostly CLOSED — only the declared windows are verified, and a lift spends most
    # of its day shut — so its p90 is high by construction and the difference comes out near zero or
    # negative on a perfectly good template. ch34 scored -0.028 and ch37 -0.004 while both are
    # excellent. A reader glancing at a negative "SEPARATION" would draw the opposite conclusion, so
    # the line is gone. What discriminates is the closed floor against the outside LOW tail.
    print(f"  discrimination: closed p05 {q(ci, 5):.3f} vs outside p10 {q(op, 10):.3f} "
          f"= {q(ci, 5) - q(op, 10):+.3f}   (outside p50 {q(op, 50):.3f} is high because most of "
          f"the day is genuinely closed and undeclared)")
    # The outside-window tail is NOT all open: a lift is closed most of the day, and only the
    # DECLARED windows are verified. So p90 outside is expected to be high; what must hold is that
    # the closed p05 sits above the bulk of the outside distribution, and that a real open state
    # exists somewhere in the tail at all.
    print(f"  outside-window frames scoring below the closed p05: "
          f"{100.0 * float((op < q(ci, 5)).mean()):.1f}%  "
          f"(a lift is CLOSED most of the day, so this is a minority by construction — "
          f"0% would mean the template cannot tell the states apart)")
    verdict = []
    if q(ci, 50) < 0.90:
        verdict.append("held-out closed median below 0.90 — the template does not describe its own "
                       "declared windows")
    if float((op < q(ci, 5)).mean()) < 0.02:
        verdict.append("almost NOTHING outside the windows scores below the closed floor — no "
                       "discrimination, which is what a wrong-state template looks like")
    for t in a.anchor_s:
        f = int(round(t * fps))
        k = int(np.argmin(np.abs(idx - f)))
        inw = any(lo <= idx[k] <= hi for lo, hi in wins)
        print(f"  ANCHOR t={t:g}s -> frame {idx[k]} : NCC {s[k]:.3f}  "
              f"{'inside' if inw else 'OUTSIDE'} a declared window")
        if s[k] < q(ci, 5):
            verdict.append(f"the operator-verified CLOSED anchor at t={t:g}s scores {s[k]:.3f}, "
                           f"below the held-out closed p05 ({q(ci, 5):.3f})")
    print()
    if verdict:
        for v in verdict:
            print(f"  REJECT: {v}")
        return 1
    print("  ACCEPT: the template describes the declared closed state and discriminates against "
          "the rest of the capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
