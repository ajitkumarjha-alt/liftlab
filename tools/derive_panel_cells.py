#!/usr/bin/env python3
"""Panel cell structure from the FRAMES — COLUMN evidence only. The vertical extent is NOT derived.

READ THIS BEFORE TRUSTING ANY NUMBER BELOW. The row band this tool reports is UNSTABLE IN SAMPLE
SIZE, which means it is not a measurement. Measured 2026-08-13 over 300/600/1200 sampled frames:

  ch34 rows  (0,97)+(125,140)  ->  (36,80)  ->  (0,140)      three different answers
  ch32 rows  (0,39)+(60,80)    ->  (0,33)+(73,80)  ->  (0,26) monotonically shrinking
  ch37 rows  (0,120) throughout                              never resolves at all

The cause is the threshold, and it is a design fault rather than noise: "active = above 40% of the
PEAK column energy" is relative, so as more frames are added the energy floor rises and a fixed
fraction-of-peak carves a different band every time. Columns survive it better because the gaps
between glyphs are genuinely dark; rows do not, because a display has no horizontal gap to find.

WHAT IS USABLE: the COLUMN runs, where they converge. ch32 settles at (25,44) and (50,70) across
600 and 1200 frames; ch37 at (18,34) and (42,75). ch34 shows no column gap at any sample size —
there is nothing to derive, and the tool says so rather than proposing the whole panel as one cell.

WHAT IS NOT: any y extent, on any camera. The rects below carry the panel's own y range unchanged,
which is deliberately WRONG as a cell and is there so that nobody mistakes it for a derived one.

So this tool contributes evidence to /calibrate, not geometry. A proposal is not a calibration, and
an unstable proposal is not even a proposal.

Original purpose, retained:

WHY DERIVED AND NOT MEASURED BY EYE. The cells are the geometry every floor read depends on, and a
cell that is a few pixels off does not fail loudly — it reads a different glyph confidently. The
operator supplied the PANEL rect (verified legible by eye); this finds the lit sub-regions inside it
from the video itself, so the boundaries come from where the LEDs actually are.

THE SIGNAL. A floor indicator changes as the lift moves, so across a capture the DIGIT pixels vary
and the background does not. Per-pixel standard deviation over sampled frames therefore lights up
exactly the glyph area. Columns of that map segment the digits; the arrow is the group that sits
apart from the digit row (the operator's rects put it at one end).

WHAT THIS DOES NOT DO: it does not name the glyphs. It proposes cell RECTANGLES for a human to
confirm on /calibrate, which is where the labelling happens. A proposal is not a calibration.

  python3 tools/derive_panel_cells.py --cam ch32 --video ch32_cal.mp4 --panel 85,55,155,135
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np


def segments(mask, min_run):
    """Contiguous True runs -> [(start, end_exclusive)]."""
    out, run = [], None
    for i, v in enumerate(mask):
        if v and run is None:
            run = i
        elif not v and run is not None:
            if i - run >= min_run:
                out.append((run, i))
            run = None
    if run is not None and len(mask) - run >= min_run:
        out.append((run, len(mask)))
    return out


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--panel", required=True, help="x0,y0,x1,y1 in frame px")
    ap.add_argument("--stride", type=int, default=25)
    ap.add_argument("--max-frames", type=int, default=1200)
    ap.add_argument("--col-frac", type=float, default=0.40,
                    help="a column/row BELOW this fraction of peak energy is a SEPARATOR; cells are "
                         "the runs between separators. 0.25 was too low — nothing inside a lit "
                         "display falls that far, so every column read as active and the panel came "
                         "back as one cell.")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    x0, y0, x1, y1 = (int(v) for v in a.panel.split(","))

    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {a.video}")
    crops, i = [], 0
    while len(crops) < a.max_frames:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % a.stride:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        crops.append(g[y0:y1, x0:x1].astype(np.float32))
    cap.release()
    if len(crops) < 20:
        raise SystemExit(f"only {len(crops)} frames sampled — too few to see the panel vary")
    st = np.stack(crops)
    var = st.std(axis=0)                      # per-pixel variation: the glyphs move, the panel does not
    mx = st.max(axis=0)                       # ever-lit: catches a digit that never changed in the sample

    # SEGMENT ON THE GAPS, NOT ON "ACTIVITY". The first version marked a column active above 25% of
    # peak energy and took the runs — which returned ONE cell spanning the whole panel on all three
    # cameras, because the energy floor inside a lit display never drops near zero. What separates
    # glyphs is the VALLEY between them, so the low-energy columns are the separators and the cells
    # are what lies between. Same for rows, which is how the true display band is found inside a
    # panel rect that is larger than the display.
    col = var.sum(axis=0)
    row = var.sum(axis=1)
    ca = col >= a.col_frac * col.max()        # NOT a separator
    ra = row >= a.col_frac * row.max()
    cols = segments(ca, 3)
    rows = segments(ra, 3)

    print(f"=== {a.cam} — panel ({x0},{y0})-({x1},{y1}) = {x1 - x0}x{y1 - y0}px, "
          f"{len(crops)} frames sampled every {a.stride} ===")
    print(f"  per-pixel std: max {var.max():.1f}  mean {var.mean():.1f}   "
          f"(a static panel would be flat — this is what proves the crop contains the display)")
    if var.max() < 5.0:
        print("  REFUSING: the panel crop barely varies across the capture. Either the rect is off")
        print("  the display, or the lift never changed floor in the sample. Not a cell proposal.")
        return 2
    print(f"  column runs between separators: {cols}")
    print(f"  row runs between separators:    {rows}")
    if len(cols) == 1 and cols[0][1] - cols[0][0] >= 0.9 * (x1 - x0):
        print("  NOTE: one column run spanning the panel — no vertical gap between glyphs at this")
        print("  resolution. Cell boundaries CANNOT be derived from the pixels here; the panel rect")
        print("  is a single region and the split must be placed by eye on /calibrate.")

    # DIGITS: the tall row band with the most energy; cells are its column runs.
    if not rows or not cols:
        print("  REFUSING: no contiguous active band found.")
        return 2
    ry0, ry1 = max(rows, key=lambda r: row[r[0]:r[1]].sum())
    digits = [(x0 + c0, y0 + ry0, c1 - c0, ry1 - ry0) for c0, c1 in cols]
    print(f"  digit row band: y{y0 + ry0}-{y0 + ry1} ({ry1 - ry0}px tall)")
    for k, d in enumerate(digits):
        print(f"    digit cell {k}: x={d[0]} y={d[1]} w={d[2]} h={d[3]}")
    # ARROW: a row band separate from the digit band, if one exists.
    other = [r for r in rows if r != (ry0, ry1)]
    arrow = None
    if other:
        ay0, ay1 = max(other, key=lambda r: row[r[0]:r[1]].sum())
        ax = segments(ca, 2)
        if ax:
            arrow = (x0 + ax[0][0], y0 + ay0, ax[-1][1] - ax[0][0], ay1 - ay0)
        print(f"  arrow cell (separate row band): {arrow}")
    else:
        print("  NO separate arrow band found in this panel rect. The arrow may share the digit")
        print("  row, or sit outside the supplied rect — this needs an eye on /calibrate, and the")
        print("  two-template rule means an unlabelled arrow yields NO direction rather than a wrong one.")

    out = {"cam": a.cam, "panel_roi": [x0, y0, x1 - x0, y1 - y0],
           "digit_cells": [list(d) for d in digits],
           "arrow_cell": (list(arrow) if arrow else None),
           "n_frames_sampled": len(crops), "var_max": round(float(var.max()), 2),
           "PROVISIONAL": "cell RECTANGLES proposed from pixel variance; glyph labelling and "
                          "confirmation happen on /calibrate. A proposal is not a calibration."}
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)
        print(f"  -> {a.out}")
    else:
        print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
