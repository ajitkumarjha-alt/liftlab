#!/usr/bin/env python3
"""Does the deployed door-state template still match the LIVE pixels? Run this on the GPU box.

THE QUESTION IT SETTLES. A calib snapshot and a capture can disagree for two very different reasons:
the snapshot is rescaled (a coordinate problem, fixable by conversion) or the camera's framing has
actually changed (a pixel problem, which invalidates the template). Ratios on a drawn rectangle
cannot tell those apart. Scoring the template against live frames can.

It pulls segments from the SAME playlist the worker decodes — not the snapshot route — so what it
measures is what the engine sees.

  python3 tools/check_live_band.py --cam ch32 --tpl door_state_templates/ch32.json \\
      --base http://127.0.0.1:9090/api/gw/site-A/live/ch32 --token "$ANALYSIS_TOKEN"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request

import numpy as np


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True)
    ap.add_argument("--tpl", required=True)
    ap.add_argument("--base", required=True, help="the worker's BASE: .../live/{cam}")
    ap.add_argument("--token", default="")
    ap.add_argument("--segments", type=int, default=3)
    ap.add_argument("--shift", type=int, default=200, help="+/- x offsets to search")
    a = ap.parse_args()

    meta = json.load(open(a.tpl))
    tpl = cv2.imdecode(np.frombuffer(base64.b64decode(meta["png_b64"]), np.uint8),
                       cv2.IMREAD_GRAYSCALE)
    y0, y1 = meta["band_y"]
    x, w = meta["roi_x_w"]
    wh = tuple(meta["template_wh"])
    hdr = {"Authorization": f"Bearer {a.token}"} if a.token else {}

    def get(u):
        return urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=20).read()

    names = [ln.strip() for ln in get(f"{a.base}/index.m3u8").decode("utf-8", "ignore").splitlines()
             if ln.strip().endswith(".ts")][-a.segments:]
    frames = []
    import tempfile, os
    for n in names:
        p = os.path.join(tempfile.mkdtemp(), n)
        open(p, "wb").write(get(f"{a.base}/{n}"))
        cap = cv2.VideoCapture(p)
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            frames.append(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY))
        cap.release()
    if not frames:
        raise SystemExit("no frames decoded from the live playlist")
    H, W = frames[0].shape
    print(f"=== {a.cam} — {len(frames)} LIVE frames from the worker's own playlist, {W}x{H} ===")
    print(f"  template band: y{y0}-{y1} x{x}-{x + w}  (built on {meta['source_file']})")
    if (W, H) != (704, 576):
        print(f"  NOTE: live frame is {W}x{H}, not 704x576 — the stream profile has changed and every")
        print(f"  stored geometry is in the old frame's pixels.")

    T = tpl.astype(np.float32).ravel(); T -= T.mean()
    tn = float(np.sqrt((T * T).sum()))

    def score_at(xoff):
        xs = x + xoff
        if xs < 0 or xs + w > W:
            return None
        v = []
        for f in frames[::5]:
            c = cv2.resize(f[y0:y1, xs:xs + w], wh).astype(np.float32).ravel()
            c -= c.mean()
            nn = float(np.sqrt((c * c).sum()))
            v.append(0.0 if nn < 1e-6 else float((c @ T) / (nn * tn)))
        return float(np.percentile(v, 90))          # the door is closed most of the time

    here = score_at(0)
    print(f"  NCC at the stored band (p90 over live frames): {here:.3f}")
    best, bestoff = here, 0
    for off in range(-a.shift, a.shift + 1, 4):
        s = score_at(off)
        if s is not None and s > best:
            best, bestoff = s, off
    print(f"  best over x offsets +/-{a.shift}: {best:.3f} at offset {bestoff:+d}px "
          f"(band x{x + bestoff}-{x + bestoff + w})")
    print()
    if here >= 0.90 and abs(bestoff) <= 8:
        print("  VERDICT: the template still matches the live pixels at its stored band. The framing")
        print("  has NOT changed; any disagreement with the calib snapshot is a COORDINATE problem")
        print("  (the snapshot is rescaled), not a pixel one. Save the ROI in FRAME coords.")
        return 0
    if best >= 0.90 and abs(bestoff) > 8:
        print(f"  VERDICT: the framing HAS MOVED by about {bestoff:+d}px in x. The template still")
        print("  describes the door, but at the wrong offset — rebuild it from live-consistent")
        print("  footage, and do not save an ROI derived from the old capture.")
        return 1
    print("  VERDICT: the template does not match the live pixels at ANY offset in range. Either the")
    print("  camera was re-aimed/zoomed, or this is not the same view. Recapture and rebuild.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
