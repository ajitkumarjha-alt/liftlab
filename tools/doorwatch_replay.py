#!/usr/bin/env python3
"""Replay the door-cycle engine offline over recorded video and diff it against hand-timed truth.

WHY. h2 emits phantom close events — reflections sweeping a mirror-polished door panel as the car
departs flip the state classifier, and several of those phantoms land inside the [0.5, 30]s filter
that is supposed to keep garbage out of C27. It also MISSES real closes and clips travel low. None
of that was visible from the database, because the database is the engine's own opinion. The only
way to grade an engine is against something it did not produce: video, timed by hand.

This harness runs the REPO's tracker (unmodified — h2 as-is for the baseline, h3 later) over an mp4,
maps video position to OSD wall-clock, and scores the result against two CSVs:

  groundtruth_*.csv       real closes seen on video, with hand-timed travel where measurable
  phantom_periods_*.csv   windows verified door-CLOSED; ANY event emitted inside one is a phantom

The baseline table this prints for h2 is the acceptance test for h3. A fix that does not move these
numbers is not a fix.

USAGE
  python3 tools/doorwatch_replay.py --video ch30_full.mp4 --cam ch30 --roi X,Y,W,H \\
      --osd-base 12:04:48 [--skip-before 30] [--frame-stride 1]

The ROI is FRAME pixels (x,y,w,h) in the video's own resolution. gpu_door's own note warns the
marked ROI is 1920x1080 desk-rig while the GPU sees a 704x576 substream, so if the recording is a
different size from the calibration, pass --roi-scale or a ROI already in the video's pixels. Getting
this wrong does not error — it silently produces a nonsense edge column — so the harness prints the
ROI, the frame size, and the openness distribution, and REFUSES to score if openness never moves.
"""
import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def epoch_to_osd(ep):
    return dt.datetime.fromtimestamp(ep, IST).strftime("%H:%M:%S")


def load_truth(path, cam):
    """Frame-anchored truth. The old wall-clock loader mapped OSD through an --osd-base offset; on
    ch30 that offset drifted ~4s and inverted at a concat seam, which faked a phantom count
    (3013116). Frames of a named file have neither problem."""
    return truth_io.load_truth(cam, path)


def load_phantom(path, cam):
    return truth_io.load_phantoms(cam, path)


def replay(video, roi, osd_base_ep, skip_before, stride, roi_scale):
    """-> (events, diag). events are gw_door_event-shaped dicts with OSD-mapped ts."""
    import cv2
    import numpy as np
    import gpu_door

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    x, y, w, h = [int(round(v * roi_scale)) for v in roi]
    tr = gpu_door.DoorTracker()
    events, opens = [], []
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if stride > 1 and (i % stride):
            continue
        vt = (i - 1) / fps
        if vt < skip_before:
            continue
        t = vt                                     # SECONDS INTO THE FILE — the truth's own anchor
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        col, strength = gpu_door.door_edge_column(gray[y:y + h, x:x + w])
        if col is None:
            continue
        opens.append(tr.openness(col) if hasattr(tr, "openness") else None)
        cyc = tr.update(t, col, strength)
        if cyc:
            for c in (cyc if isinstance(cyc, list) else [cyc]):
                if not isinstance(c, dict):
                    continue
                ct = c.get("close_travel_s")
                ce = c.get("close_full_ts") or c.get("close_ts") or c.get("ts") or t
                events.append({"cam": None, "ts": c.get("ts", ce), "close_ts": ce,
                               "close_f": int(round(ce * fps)),
                               "close_travel_s": ct, "raw": c})
    cap.release()
    ov = [o for o in opens if o is not None]
    diag = {"fps": round(fps, 2), "frames": n, "size": f"{W}x{H}", "roi": (x, y, w, h),
            "openness_n": len(ov),
            "openness_min": round(min(ov), 3) if ov else None,
            "openness_max": round(max(ov), 3) if ov else None,
            "openness_span": round(max(ov) - min(ov), 3) if ov else None}
    return events, diag


def score(events, truth, phantoms, tol, cam):
    """Match emissions to truth closes IN FRAMES. `tol` is seconds, converted via the file's own fps.

    A phantom is any emission inside a door-closed window. Those windows are door-state, not
    occupancy — an emission there is a phantom whoever is standing in the cabin.
    """
    fps = truth_io.CORPUS[cam]["fps"]
    gradable = list(truth)
    used, matches, phantom_hits, unmatched = set(), [], [], []
    for e in sorted(events, key=lambda x: x["close_f"]):
        inside = truth_io.in_phantom(phantoms, e["close_f"])
        if inside:
            phantom_hits.append((e, inside))
            continue
        best, bd = None, None
        for i, t in enumerate(gradable):
            if i in used:
                continue
            d = abs(e["close_f"] - t["end_f"])
            if bd is None or d < bd:
                best, bd = i, d
        if best is not None and bd <= tol * fps:
            used.add(best)
            matches.append((e, gradable[best], bd / fps))
        else:
            unmatched.append(e)
    missed = [t for i, t in enumerate(gradable) if i not in used]
    return matches, missed, phantom_hits, unmatched, gradable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--roi", required=True, help="x,y,w,h in the video's own pixels")
    ap.add_argument("--roi-scale", type=float, default=1.0)
    ap.add_argument("--skip-before", type=float, default=0.0)
    ap.add_argument("--frame-stride", type=int, default=1)
    ap.add_argument("--tol", type=float, default=5.0)
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    ap.add_argument("--phantoms", default="tools/phantom_periods_20260805.csv")
    a = ap.parse_args()

    roi = [float(v) for v in a.roi.split(",")]
    truth = load_truth(a.truth, a.cam)
    phantoms = load_phantom(a.phantoms, a.cam)
    ev, diag = replay(a.video, roi, 0.0, a.skip_before, a.frame_stride, a.roi_scale)

    import gpu_door
    print(f"=== {a.cam}  tracker={gpu_door.TRACKER_LOGIC}  {os.path.basename(a.video)} ===")
    print(f"  video {diag['size']} @ {diag['fps']}fps  roi={diag['roi']}  "
          f"openness span={diag['openness_span']} (min {diag['openness_min']} max {diag['openness_max']})")
    if not diag["openness_span"]:
        print("  REFUSING TO SCORE: openness never moved. The ROI is almost certainly wrong for this")
        print("  video's resolution — a bad ROI yields a flat edge column and silently scores 0/0.")
        return 2

    m, missed, ph, unm, gradable = score(ev, truth, phantoms, a.tol, a.cam)
    print(f"  emitted {len(ev)} close events; hand-timed closes gradable: {len(gradable)}")
    print()
    print(f"  {'DETECTED':<10} {len(m)}/{len(gradable)}")
    print(f"  {'MISSED':<10} {len(missed)}")
    print(f"  {'PHANTOM':<10} {len(ph)}   (events inside verified door-CLOSED windows)")
    print(f"  {'UNMATCHED':<10} {len(unm)}  (emitted, no truth within {a.tol:g}s, not in a phantom window)")
    print()
    if m:
        print(f"  {'truth end_f':>11} {'emit_f':>9} {'d_s':>6} {'hand_s':>7} {'engine_s':>9} {'err_s':>7}  status")
        for e, t, d in m:
            hs = f"{t['travel_s']:.2f}" if t["travel_s"] is not None else "-"
            es = f"{e['close_travel_s']:.2f}" if e["close_travel_s"] is not None else "-"
            er = (f"{e['close_travel_s'] - t['travel_s']:+.2f}"
                  if (t["travel_s"] is not None and e["close_travel_s"] is not None) else "-")
            print(f"  {t['end_f']:>11} {e['close_f']:>9} {d:>6.1f} {hs:>7} {es:>9} {er:>7}  {t['status']}")
        errs = [e["close_travel_s"] - t["travel_s"] for e, t, _ in m
                if t["travel_s"] is not None and e["close_travel_s"] is not None]
        if errs:
            print(f"    travel error: n={len(errs)} mean={sum(errs)/len(errs):+.2f}s "
                  f"min={min(errs):+.2f}s max={max(errs):+.2f}s")
    if missed:
        print(f"\n  MISSED: " + ", ".join(f"f{t['end_f']}({t['status']})" for t in missed))
    if ph:
        print(f"\n  PHANTOMS inside verified-closed windows:")
        for e, p in ph:
            ct = f"{e['close_travel_s']:.2f}s" if e["close_travel_s"] is not None else "-"
            print(f"    f{e['close_f']}  travel={ct}   [{p['note']}]")
    if unm:
        def _t(e):
            v = e["close_travel_s"]
            return f"{v:.2f}" if v is not None else "-"
        print("\n  UNMATCHED emissions: " +
              ", ".join(f"f{e['close_f']}({_t(e)}s)" for e in unm[:12]) +
              (f" ... +{len(unm)-12} more" if len(unm) > 12 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -- RUN PARAMETERS for the CLEAN corpus -------------------------------------
# Truth and phantom windows are frame-anchored to these files, so there is no --osd-base to get
# wrong. ROIs are unchanged (both calib frame_wh match the 704x576 recordings).
#
#   python3 tools/doorwatch_replay.py --cam ch27 --video ~/dwrec/rec/ch27_clean.mp4 \
#       --roi 125,3,238,397 --frame-stride 2
#   python3 tools/doorwatch_replay.py --cam ch30 --video ch30_peak.mp4 \
#       --roi 2,2,335,446 --frame-stride 2
