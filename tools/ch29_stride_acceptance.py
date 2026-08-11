#!/usr/bin/env python3
"""FLOOR_STRIDE acceptance on REAL ch29 frames: stride 0 vs stride N, same video, same engine.

WHY THIS IS POSSIBLE NOW. DEPLOY_floor_stride.md pushed this check to a live window because no ch29
corpus existed; tools/test_floor_stride.py could only prove the MECHANISM against a synthetic
timeline. ch29_tue.mp4 (24.7 min, 2026-08-11) is that corpus.

WHY IT NEEDS NO HAND-TIMED TRUTH. It is a SELF-COMPARISON. Two engines see identical frames; one
reads the panel on every door pass, the other every FLOOR_STRIDE frames and carries the previous
read forward. Stride 0's fresh read at frame i IS the reference for stride N's carried value at
frame i. No labels required, and the question asked is exactly the one that matters: does thinning
the reads change what we would have concluded about the floor?

THE METRIC THAT MATTERS is not pct_attributed on its own — carrying a value forward keeps
attribution trivially high. It is DISAGREEMENT: on frames where both engines report a floor, how
often does the carried value differ from what a fresh read would have said. That is the real cost of
the cadence, and it is invisible to any count of non-null rows.

Run:
  python3 tools/ch29_stride_acceptance.py --video ch29_tue.mp4 --roi-json ch29_roi.json \\
      --templates ch29_templates.npz --stride 12
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _xywh(v):
    return tuple(int(round(float(n))) for n in v)


def build_engine(templates, roi):
    import gpu_door as gd
    door = _xywh(roi["door_roi_frame"])
    cells = roi.get("cells") or {}
    digits = [_xywh(c) for c in cells["digit_cells"]]
    arrow = _xywh(cells["arrow_cell"])
    # SINGLE-PANEL, matching production: the fleet logs ch29 as "SINGLE-PANEL (no agree-or-discard)"
    # because PANEL1_DIGIT_CELLS/PANEL1_ARROW_CELL are not calibrated. Using both panels here would
    # measure a configuration that does not run.
    panels = [(_xywh(roi["panel_rois"][0]), digits, arrow)]
    return gd.DoorFloorEngine(templates, door, panels, floor_tracker=gd.FloorTracker(),
                              door_tracker=gd.DoorTracker())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--roi-json", required=True)
    ap.add_argument("--templates", required=True)
    ap.add_argument("--door-stride", type=int, default=2)
    ap.add_argument("--stride", type=int, action="append",
                    help="FLOOR_STRIDE to test; repeatable. Default 6,12,25,50.")
    ap.add_argument("--limit-frames", type=int, default=0)
    a = ap.parse_args()
    strides = a.stride or [6, 12, 25, 50]

    import cv2
    import gpu_door as gd

    templates = gd.load_templates(a.templates)
    roi = json.load(open(a.roi_json))
    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {a.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0

    # ONE PASS, EVERY STRIDE. read_panel/reconcile are STATELESS per frame — the floor a read
    # returns depends only on that frame's pixels. So a single reference pass that reads on every
    # door-pass frame contains every read any stride would ever make, and each stride is obtained by
    # CARRYING FORWARD from the reads at its own multiples. That is exactly what the engine does, it
    # is arithmetically identical to running N engines, and it costs one pass instead of N.
    #
    # (FloorTracker is stateful, but it only produces `stop`; it does not affect the floor value,
    # attribution or age. Nothing reported below depends on it.)
    eng = build_engine(templates, roi)
    reads = []                      # (frame_index, t, floor) at every door-pass frame
    i = 0
    t0 = __import__("time").time()
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if a.limit_frames and i > a.limit_frames:
            break
        if i % a.door_stride:
            continue
        t = (i - 1) / fps
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        rec = eng.process(gray, t, do_floor=True)
        reads.append((i, t, rec["floor"]))
        if len(reads) % 2000 == 0:
            el = __import__("time").time() - t0
            print(f"  ... {len(reads)} reads, {el/60:.1f} min elapsed", flush=True)
    cap.release()

    n = len(reads)
    if not n:
        raise SystemExit("no frames read")
    print(f"=== ch29 FLOOR_STRIDE acceptance — {os.path.basename(a.video)} ===")
    print(f"  {i} frames decoded @ {fps:.4f}fps; door pass every {a.door_stride} -> {n} door-pass frames")
    print(f"  single-panel (production config); no floor whitelist in roi.json — identical for every")
    print(f"  arm, so the comparison is unaffected by its absence.\n")

    def simulate(stride):
        """What the engine WOULD report at each door-pass frame with this FLOOR_STRIDE.

        Carries the last READ forward — including a read whose floor was None. The engine caches
        every floor_pass result, not just successful ones, so carrying a None is the faithful
        behaviour and pretending otherwise would flatter the result.
        """
        out, last = [], None
        for (fi, t, fl) in reads:
            if stride <= 0 or fi % stride == 0:
                last = (t, fl)
            if last is None:
                out.append((None, None))            # before the first read of this arm
            else:
                out.append((last[1], round(t - last[0], 3)))
        return out

    ref = [(fl, 0.0) for (_fi, _t, fl) in reads]
    ref_att = [f for f, _ in ref if f is not None]
    ref_floors = {f for f in ref_att}
    print(f"{'arm':>10} {'reads':>7} {'work':>6} {'pct_attrib':>11} {'distinct':>9} "
          f"{'age med':>8} {'age p95':>8} {'age max':>8} {'disagree':>9}")
    print(f"{'stride 0':>10} {n:>7} {'100%':>6} {100.0*len(ref_att)/n:>10.1f}% {len(ref_floors):>9} "
          f"{0.0:>8.3f} {0.0:>8.3f} {0.0:>8.3f} {'—':>9}")

    verdict = {}
    for st in strides:
        sim = simulate(st)
        nreads = sum(1 for (fi, _t, _f) in reads if fi % st == 0)
        att = [f for f, _ in sim if f is not None]
        floors = {f for f in att}
        ages = [ag for f, ag in sim if f is not None and ag is not None]
        both = [(r[0], s[0]) for r, s in zip(ref, sim) if r[0] is not None and s[0] is not None]
        dis = sum(1 for x, y in both if x != y)
        dpct = 100.0 * dis / len(both) if both else float("nan")
        p95 = sorted(ages)[int(0.95 * (len(ages) - 1))] if ages else 0.0
        print(f"{'stride ' + str(st):>10} {nreads:>7} {100.0*nreads/n:>5.0f}% "
              f"{100.0*len(att)/n:>10.1f}% {len(floors):>9} "
              f"{statistics.median(ages) if ages else 0:>8.3f} {p95:>8.3f} "
              f"{max(ages) if ages else 0:>8.3f} {dpct:>8.2f}%")
        verdict[st] = {"lost": sorted(ref_floors - floors), "dis": dpct, "n_both": len(both),
                       "reads": nreads}

    print()
    for st in strides:
        v = verdict[st]
        print(f"stride {st}: {v['reads']}/{n} reads ({100.0*v['reads']/n:.0f}% of the OCR work), "
              f"disagreement {v['dis']:.2f}% over {v['n_both']} comparable frames")
        if v["lost"]:
            print(f"  LOST FLOORS vs stride 0: {v['lost']}  <-- attribution DEGRADED")
        else:
            print(f"  every floor seen at stride 0 is still attributed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
