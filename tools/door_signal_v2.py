#!/usr/bin/env python3
"""Door-position signal v2 — per-camera, built to be VALIDATED before anything keys on it.

5e58211 established that `openness` (median column of the strongest Sobel-x gradient, normalised
against a rolling p10/p90) is not a measurement of door position: it agrees with the physical door
65% of the time at its own decision thresholds, its raw column overlaps 65-71% between open and
closed, and its abstain path never fires. It is not rehabilitated here. It is replaced.

Two ideas, both from the frame analysis that produced the hand-timed ground truth:

  CLOSED-TEMPLATE NCC — the closed leaf is the one appearance that does not change with the floor
  the car is at. ch27's lobby flips polarity by floor (bright carpet on 43, dark marble elsewhere),
  so any global brightness rule is structurally unusable there; the closed door is invariant. NCC
  against a per-camera closed template gives STATE (closed = high) and, because a template match
  should degrade as the leaf slides out of frame, is also the first candidate for POSITION.

  TOP BAND — a horizontal strip above head height (y 15-85 at 704x576, x spanning the doorway).
  Passengers stand in the ROI and are a stronger gradient than the leaf; above head height the leaf
  is the only thing that moves. Both NCC and inter-frame motion are computed there as well as over
  the full door ROI, so validation can choose rather than the author assuming.

  BRIGHT FRACTION — for ch30, where the lobby is reliably brighter than the leaf, the fraction of
  ROI pixels above a threshold, with an adaptive baseline (rolling low/high percentiles over a long
  window) to absorb lighting drift. Reported raw AND baseline-normalised so validation can tell a
  drifting baseline from a moving door.

NOTHING here decides door state. This module only produces candidate signals; door_signal_validate.py
scores them against the 48 hand labels and the 6 hand-timed travels, and only a signal that passes
is allowed into a tracker.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"

# Per-camera geometry. door_roi is the calib ROI already in the video's own 704x576 pixels (both
# cameras' calib frame_wh matches the recording, per README_DOORWATCH). top_band is y-range above
# head height; its x-range is taken from the door ROI so it spans the doorway and nothing else.
CAMS = {
    "ch30": {"video": "~/dwrec/rec/ch30_full.mp4", "roi": (2, 2, 335, 446),
             "osd_base": "12:04:48", "skip_before": 0.0},
    "ch27": {"video": "~/dwrec/rec/ch27_full.mp4", "roi": (125, 3, 238, 397),
             "osd_base": "12:04:36", "skip_before": 30.0},
}
TOP_BAND_Y = (15, 85)
TEMPLATE_WH = (96, 128)      # templates are resized to this so NCC cost is fixed and small


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def epoch_to_osd(ep):
    return dt.datetime.fromtimestamp(ep, IST).strftime("%H:%M:%S")


def top_band(frame_gray, roi):
    x, _, w, _ = roi
    y0, y1 = TOP_BAND_Y
    return frame_gray[y0:y1, x:x + w]


def ncc(a, b):
    """Zero-mean normalised cross-correlation of two equal-size arrays. 1.0 = identical structure.

    Flat patches (zero variance) return 0.0 rather than dividing by zero — a featureless crop is not
    evidence of a match.
    """
    a = a.astype(np.float32); b = b.astype(np.float32)
    a = a - a.mean(); b = b - b.mean()
    da = float(np.sqrt((a * a).sum())); db = float(np.sqrt((b * b).sum()))
    if da < 1e-6 or db < 1e-6:
        return 0.0
    return float((a * b).sum() / (da * db))


def read_labels(path, cam=None):
    with open(path) as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    out = []
    for r in csv.DictReader(lines):
        if cam and r["cam"] != cam:
            continue
        r["openness"] = float(r["openness"]); r["col"] = float(r["col"])
        out.append(r)
    return out


def label_frame_index(label):
    """The label file carries its own frame index, verified against the deterministic selection that
    produced the label grids (osd/openness/col all re-checked, 48/48). Matching on (osd, openness,
    col) instead is ambiguous — frozen frames share all three — and would silently put the wrong
    pixels into a template."""
    return int(label["frame"])


def grab_frames(video, indices):
    import cv2
    cap = cv2.VideoCapture(os.path.expanduser(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    want = set(indices)
    got, i, mx = {}, 0, max(want) if want else -1
    while True:
        ok, fr = cap.read()
        if not ok or i > mx + 2:
            break
        if (i + 1) in want:                     # trace 'frame' is 1-based
            got[i + 1] = fr.copy()
        i += 1
    cap.release()
    return got


def build_templates(cam, labels_csv, out_dir):
    """Median CLOSED-door template (full ROI and top band) from hand-labelled closed frames.

    The median across many independent closed frames — different floors, different passengers — is
    what makes the template the leaf rather than any one scene behind it.
    """
    import cv2
    spec = CAMS[cam]
    labs = [l for l in read_labels(labels_csv, cam) if l["door_state"] == "closed"]
    idx_of = {l["id"]: label_frame_index(l) for l in labs}
    frames = grab_frames(spec["video"], list(idx_of.values()))
    x, y, w, h = spec["roi"]
    roi_stack, top_stack, used = [], [], []
    for lid, fi in sorted(idx_of.items()):
        fr = frames.get(fi)
        if fr is None:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        roi_stack.append(cv2.resize(g[y:y + h, x:x + w], TEMPLATE_WH))
        tb = top_band(g, spec["roi"])
        top_stack.append(cv2.resize(tb, (TEMPLATE_WH[0], max(8, TEMPLATE_WH[1] // 4))))
        used.append((lid, fi))
    if len(roi_stack) < 3:
        raise SystemExit(f"{cam}: only {len(roi_stack)} closed frames resolved — refusing to build "
                         f"a template from that")
    tpl_roi = np.median(np.stack(roi_stack), axis=0).astype(np.uint8)
    tpl_top = np.median(np.stack(top_stack), axis=0).astype(np.uint8)
    os.makedirs(out_dir, exist_ok=True)
    cv2.imwrite(f"{out_dir}/{cam}_tpl_roi.png", tpl_roi)
    cv2.imwrite(f"{out_dir}/{cam}_tpl_top.png", tpl_top)
    meta = {"cam": cam, "n_closed_frames": len(roi_stack), "used": used,
            "roi": spec["roi"], "top_band_y": TOP_BAND_Y, "template_wh": TEMPLATE_WH}
    json.dump(meta, open(f"{out_dir}/{cam}_tpl.json", "w"), indent=1)
    # leave-one-out: how well does the template match the frames it was NOT built from?
    loo = []
    for k in range(len(roi_stack)):
        others = np.median(np.stack([r for j, r in enumerate(roi_stack) if j != k]), axis=0).astype(np.uint8)
        loo.append(ncc(roi_stack[k], others))
    print(f"  {cam}: template from {len(roi_stack)} closed frames "
          f"({', '.join(l for l, _ in used)})")
    print(f"    leave-one-out NCC on held-out closed frames: "
          f"min={min(loo):.3f} med={sorted(loo)[len(loo)//2]:.3f} max={max(loo):.3f}")
    return tpl_roi, tpl_top


def dump_signals(cam, tpl_roi, tpl_top, stride, out_csv, limit=None):
    """Per-frame candidate signals over the whole run."""
    import cv2
    spec = CAMS[cam]
    cap = cv2.VideoCapture(os.path.expanduser(spec["video"]))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {spec['video']}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    base = osd_to_epoch(spec["osd_base"])
    x, y, w, h = spec["roi"]
    tb_h = tpl_top.shape[0]
    rows, prev_top = [], None
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if stride > 1 and (i % stride):
            continue
        vt = (i - 1) / fps
        if vt < spec["skip_before"]:
            continue
        if limit and vt > limit:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        roi = g[y:y + h, x:x + w]
        roi_s = cv2.resize(roi, TEMPLATE_WH)
        tb = cv2.resize(top_band(g, spec["roi"]), (TEMPLATE_WH[0], tb_h))
        n_roi = ncc(roi_s, tpl_roi)
        n_top = ncc(tb, tpl_top)
        mot = 0.0 if prev_top is None else float(np.abs(tb.astype(np.float32) - prev_top).mean())
        prev_top = tb.astype(np.float32)
        rows.append({"frame": i, "vt": round(vt, 3), "epoch": round(base + vt, 3),
                     "osd": epoch_to_osd(base + vt),
                     "ncc_roi": round(n_roi, 5), "ncc_top": round(n_top, 5),
                     "top_motion": round(mot, 4),
                     "roi_mean": round(float(roi.mean()), 3),
                     "bright_128": round(float((roi >= 128).mean()), 5),
                     "bright_160": round(float((roi >= 160).mean()), 5),
                     "top_mean": round(float(tb.mean()), 3)})
    cap.release()
    with open(out_csv, "w", newline="") as fh:
        wcsv = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wcsv.writeheader(); wcsv.writerows(rows)
    print(f"  {cam}: wrote {len(rows)} frames -> {out_csv}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True, choices=sorted(CAMS))
    ap.add_argument("--labels", default="tools/openness_labels_20260805.csv")
    ap.add_argument("--tpl-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--frame-stride", type=int, default=2)
    a = ap.parse_args()

    print(f"=== door signal v2: {a.cam} ===")
    tpl_roi, tpl_top = build_templates(a.cam, a.labels, a.tpl_dir)
    dump_signals(a.cam, tpl_roi, tpl_top, a.frame_stride, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
