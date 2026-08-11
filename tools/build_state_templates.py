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


def sample_windows(windows, n_per_window, split, train_frac=0.4):
    """Frame indices sampled EVENLY WITHIN EACH WINDOW, capped per window.

    The cap is the point: one 4950-frame window would otherwise dominate a median built across
    seventeen, and the template would describe that window's lighting rather than the camera's.
    Every window contributes the same number of frames, long or short, so the template spans the
    lighting states the day actually contained.
    """
    out = []
    for (a_, b_) in windows:
        cut = a_ + int((b_ - a_) * train_frac)
        lo, hi = (a_, cut) if split == "train" else (cut, b_)
        if hi - lo < 2:
            continue
        step = max(1, (hi - lo) // n_per_window)
        out.extend(lo + k * step for k in range(min(n_per_window, (hi - lo) // step)))
    return sorted(set(out))


def build_explicit(cam, video, band_y, band_x, windows, n_per_window, split, osd_base, out_dir):
    """Template from an EXPLICIT band and window list, bypassing truth_io/phantom_periods.

    ch29 needs this: its door-state band was localized by open/closed frame differencing at the
    substream's own 704x576 framing, and the registry's 1920x1080 geometry does NOT scale onto it.
    Passing the derived coordinates in directly is the only way to build a template for a band that
    no stored geometry describes.
    """
    import cv2
    y0, y1 = band_y
    x0, x1 = band_x
    x, w = x0, x1 - x0
    want = sample_windows(windows, n_per_window, split)
    return _build_from(cam, video, (y0, y1), (x, w), want, split, osd_base, out_dir, windows)


def _build_from(cam, video, band_y, roi_x_w, want, split, osd_base, out_dir, windows):
    import cv2
    y0, y1 = band_y
    x, w = roi_x_w
    if len(want) < 3:
        raise SystemExit(f"{cam}: only {len(want)} closed frames available — refusing to build")
    cap = cv2.VideoCapture(os.path.expanduser(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps_meas = cap.get(cv2.CAP_PROP_FPS) or 25.0
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
        "band_y": [int(y0), int(y1)],
        "roi_x_w": [int(x), int(w)],
        "template_wh": list(TOP_WH),
        "source_file": os.path.basename(video),
        "source_fps": round(float(fps_meas), 4),
        "osd_base": osd_base,
        "n_windows": len(windows),
        "windows": [list(wv) for wv in windows],
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


def build(cam, split="train"):
    """The corpus path (ch27/ch30): band and windows come from truth_io + phantom_periods."""
    spec = truth_io.CORPUS[cam]
    x, _, w, _ = spec["roi"]
    want = sorted(set(closed_window_frames(cam, split)))
    wins = [[p["start_f"], p["end_f"]] for p in truth_io.load_phantoms(cam)]
    return _build_from(cam, truth_io.video_path(cam), tuple(TOP_BAND_Y), (x, w), want, split,
                       spec.get("osd_base"), None, wins)


def _pair(v, sep=","):
    return tuple(int(n) for n in v.split(sep))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", action="append")
    ap.add_argument("--out-dir", default="door_state_templates")
    ap.add_argument("--split", default="train", choices=("train", "test"))
    # EXPLICIT path — for a camera whose band is not described by any stored geometry.
    ap.add_argument("--video")
    ap.add_argument("--band-y", help="y0,y1")
    ap.add_argument("--band-x", help="x0,x1")
    ap.add_argument("--windows", help="a-b,c-d,... closed-door frame ranges")
    ap.add_argument("--per-window", type=int, default=6)
    ap.add_argument("--osd-base", default=None)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    print("=== building h3 state templates ===")

    if a.video:
        cam = (a.cam or ["cam"])[0]
        wins = [tuple(int(n) for n in wv.split("-")) for wv in a.windows.split(",")]
        meta = build_explicit(cam, a.video, _pair(a.band_y), _pair(a.band_x), wins,
                              a.per_window, a.split, a.osd_base, a.out_dir)
        path = os.path.join(a.out_dir, f"{cam}.json")
        with open(path, "w") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
        print(f"  {cam}: {meta['n_frames']} closed frames from {meta['source_file']} "
              f"({meta['split']} split of {meta['n_windows']} windows)")
        print(f"      band y{meta['band_y'][0]}-{meta['band_y'][1]} "
              f"x{meta['roi_x_w'][0]}-{meta['roi_x_w'][0] + meta['roi_x_w'][1]}")
        print(f"      LOO NCC median {meta['loo_ncc_median']:.4f} min {meta['loo_ncc_min']:.4f}; "
              f"md5 {meta['md5']}")
        print(f"      -> {path}")
        return 0

    cams = a.cam or sorted(truth_io.CORPUS)
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
