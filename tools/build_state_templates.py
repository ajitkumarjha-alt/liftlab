#!/usr/bin/env python3
"""Build the per-camera CLOSED-DOOR STATE TEMPLATE artefact that DoorTrackerH3 needs in production.

WHY AN ARTEFACT AT ALL. h3 does not read an edge column; it reads NCC against a template of the
closed leaf. In the offline harness that template is rebuilt on every run from the corpus's
hand-marked door-closed windows. Production has no corpus and no hand marks, so the template has to
be BUILT ONCE, SHIPPED, AND VERSIONED — otherwise the engine's zero point drifts silently between
deploys and `door_version` stops meaning anything.

WHAT IS IN THE ARTEFACT. The template pixels (PNG, base64), plus everything needed to answer "where
did this come from and is it still the right one":

  cam, band_y, template_wh   the geometry the template is only valid for
  source_file, source_fps    the recording it was cut from
  source_frames              every frame index that went into the median, in order
  n_frames, split            how many, and which split of the door-closed windows
  build_date, builder        provenance
  tracker                    the engine revision this template is for
  md5                        of the decoded PNG bytes — what the loader verifies

BAND. The state band is y15-85 on BOTH cameras, and it is stored per camera rather than taken from
a module constant so it travels with the pixels it describes. This is NOT the band in
tools/band_coords.json: that one is the TRAVEL band, derived by transition energy in 093340d, and it
is unused by a state-only engine. ch30's travel band (y242-312) has better door signal and worse SNR
(5.20 vs 10.24), and no acceptance run has ever been done with ch30's STATE on it. Moving it is a
measurement, not an edit.

USAGE
  python3 tools/build_state_templates.py --cam ch27 --cam ch30 --out-dir door_state_templates
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import truth_io
from door_signal_v2 import TEMPLATE_WH, TOP_BAND_Y, closed_window_frames

TOP_WH = (TEMPLATE_WH[0], max(8, TEMPLATE_WH[1] // 4))     # (96, 32)
BUILDER = "tools/build_state_templates.py"


def build(cam, split="train"):
    import cv2
    spec = truth_io.CORPUS[cam]
    x, _, w, _ = spec["roi"]
    y0, y1 = TOP_BAND_Y
    want = sorted(set(closed_window_frames(cam, split)))
    if len(want) < 3:
        raise SystemExit(f"{cam}: only {len(want)} closed frames available — refusing to build")
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
            g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            got[i] = cv2.resize(g[y0:y1, x:x + w], TOP_WH)
    cap.release()
    used = sorted(got)
    if len(used) < 3:
        raise SystemExit(f"{cam}: only {len(used)} closed frames resolved from {len(want)} requested")
    tpl = np.median(np.stack([got[f] for f in used]), axis=0).astype(np.uint8)

    ok, buf = cv2.imencode(".png", tpl)
    if not ok:
        raise SystemExit(f"{cam}: PNG encode failed")
    png = buf.tobytes()

    # Leave-one-out agreement of the template against the frames it was NOT built from, so the
    # artefact carries a number saying how well it represents a closed door rather than only saying
    # that it was built.
    loo = []
    for k, f in enumerate(used):
        others = np.median(np.stack([got[g] for j, g in enumerate(used) if j != k]),
                           axis=0).astype(np.uint8)
        a = got[f].astype(np.float32); b = others.astype(np.float32)
        a = a - a.mean(); b = b - b.mean()
        da, db = float(np.sqrt((a * a).sum())), float(np.sqrt((b * b).sum()))
        loo.append(0.0 if (da < 1e-6 or db < 1e-6) else float((a * b).sum() / (da * db)))

    return {
        "schema": 1,
        "cam": cam,
        "tracker": "h3-state",
        "band_y": list(TOP_BAND_Y),
        "roi_x_w": [int(x), int(w)],
        "template_wh": list(TOP_WH),
        "source_file": spec["file"],
        "source_fps": spec["fps"],
        "split": split,
        "n_frames": len(used),
        "source_frames": used,
        "loo_ncc_median": round(float(np.median(loo)), 4),
        "loo_ncc_min": round(float(min(loo)), 4),
        "build_date": dt.date.today().isoformat(),
        "builder": BUILDER,
        "md5": hashlib.md5(png).hexdigest(),
        "png_b64": base64.b64encode(png).decode("ascii"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", action="append", choices=sorted(truth_io.CORPUS))
    ap.add_argument("--out-dir", default="door_state_templates")
    ap.add_argument("--split", default="train", choices=("train", "test"))
    a = ap.parse_args()
    cams = a.cam or sorted(truth_io.CORPUS)
    os.makedirs(a.out_dir, exist_ok=True)
    print("=== building h3 state templates ===")
    for cam in cams:
        meta = build(cam, a.split)
        path = os.path.join(a.out_dir, f"{cam}.json")
        with open(path, "w") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
        print(f"  {cam}: {meta['n_frames']} closed frames from {meta['source_file']} "
              f"({meta['split']} split), band y{meta['band_y'][0]}-{meta['band_y'][1]}")
        print(f"      LOO NCC median {meta['loo_ncc_median']:.4f} min {meta['loo_ncc_min']:.4f}; "
              f"md5 {meta['md5']}")
        print(f"      -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
