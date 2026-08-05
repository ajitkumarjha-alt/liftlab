#!/usr/bin/env python3
"""Acceptance on the INTEGRATED path: the real DoorFloorEngine, the shipped template artefact.

WHY THIS EXISTS SEPARATELY FROM doorwatch_replay.py. That harness imports a tracker and drives it
directly. It graded the ALGORITHM. It cannot catch an integration defect — a template loaded from
the wrong band, a state mapped wrongly on the way to the wire, a selector that silently picks h2 —
because it never runs the code that does any of those things. This one constructs the engine the way
gpu_analyze.build_door_engine constructs it and calls DoorFloorEngine.door_pass(), which is the
same method the worker calls on every frame.

WHAT IS AND IS NOT THE REAL PATH, stated precisely so the result is not over-claimed:

  REAL:  the template artefact is loaded and md5-verified by gd.load_state_template
         the band and ROI x-slice come from that artefact
         gd.state_closedness computes the signal
         gd.DoorTrackerH3 consumes it
         DoorFloorEngine.door_pass is the caller
         h3 states are mapped through H3_STATE_TO_WIRE exactly as the worker emits them

  NOT:   floor OCR. DoorFloorEngine also reads floor panels, and the corpus has no panel
         calibration or floor templates for these two cameras, so a stub panel is supplied and its
         reads are discarded. The floor half is INDEPENDENT of the door half — it consumes the same
         gray frame and writes different output fields — so stubbing it does not touch what is
         being graded. It does mean this run does not prove the floor path still works.

  NOT:   segment decode, the DOOR_STRIDE cadence loop, HTTP posting, or the fleet's env plumbing.

USAGE
  python3 tools/integrated_acceptance.py --cam ch30 --video ch30_peak.mp4
  python3 tools/integrated_acceptance.py --cam ch27 --video ~/dwrec/rec/ch27_clean.mp4
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io
import doorwatch_replay as dwr

TPL_DIR = "door_state_templates"


def build_engine(cam, tpl_dir):
    """Construct DoorFloorEngine the way gpu_analyze.build_door_engine does, minus floor OCR."""
    import numpy as np
    import gpu_door as gd

    state_tpl, state_meta = gd.load_state_template(os.path.join(tpl_dir, f"{cam}.json"))
    if state_meta["cam"] != cam:
        raise SystemExit(f"template is for {state_meta['cam']}, not {cam}")
    roi = truth_io.CORPUS[cam]["roi"]

    # Stub floor panel: one 8x8 cell with a single 1-glyph template. DoorFloorEngine requires at
    # least one panel; its reads are discarded below. Deliberately tiny so it costs nothing.
    glyph = np.zeros((16, 10), dtype=np.uint8)
    templates = {"1": glyph}
    panels = [((0, 0, 8, 8), [(0, 0, 4, 8)], (4, 0, 4, 8))]

    dtr = gd.DoorTrackerH3()
    eng = gd.DoorFloorEngine(templates, roi, panels, floor_tracker=gd.FloorTracker(),
                             door_tracker=dtr, state_tpl=state_tpl, state_meta=state_meta)
    return eng, dtr, state_meta


def run(cam, video, tpl_dir, stride):
    import cv2
    import gpu_door as gd

    eng, dtr, meta = build_engine(cam, tpl_dir)
    cap = cv2.VideoCapture(os.path.expanduser(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    events, wire_states, open_vals, travel_seen = [], set(), [], []
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if stride > 1 and (i % stride):
            continue
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        t = (i - 1) / fps
        cycle, wire, openness, strength = eng.door_pass(gray, t)
        wire_states.add(wire)
        if openness is not None:
            open_vals.append(openness)
        if cycle:
            ce = cycle["close_full"]
            events.append({"cam": cam, "ts": ce, "close_ts": ce,
                           "close_f": int(round(ce * fps)),
                           "close_travel_s": cycle["close_travel_s"], "raw": cycle})
            travel_seen.append(cycle["close_travel_s"])
    cap.release()

    print(f"=== {cam}  INTEGRATED  DoorFloorEngine.door_pass  {os.path.basename(video)} ===")
    print(f"  video {W}x{H} @ {round(fps,2)}fps  roi={truth_io.CORPUS[cam]['roi']}")
    print(f"  template {meta['md5'][:8]} from {meta['source_file']} ({meta['n_frames']} frames), "
          f"band y{meta['band_y'][0]}-{meta['band_y'][1]}, LOO {meta['loo_ncc_median']}")
    print(f"  wire door_state values seen: {sorted(str(s) for s in wire_states)}")
    print(f"  openness range: {min(open_vals):.3f}..{max(open_vals):.3f}" if open_vals
          else "  openness: none produced")
    print(f"  refractory suppressed {dtr.suppressed}; {dtr.abandoned} descents abandoned")

    # Wire-vocabulary gate. The cloud 400s anything outside this set, so a violation here is a
    # deploy-blocking defect, not a cosmetic one.
    allowed = {"closed", "opening", "open", "closing", None}
    bad = {s for s in wire_states if s not in allowed}
    print(f"  WIRE VOCAB   {'OK' if not bad else 'VIOLATION ' + str(bad)}   "
          f"(door_event_api accepts {{closed,opening,open,closing}} or null)")

    # Null-travel gate. h3 must never emit a travel number.
    non_null = [v for v in travel_seen if v is not None]
    print(f"  NULL TRAVEL  {'OK' if not non_null else 'VIOLATION: ' + str(non_null[:5])}   "
          f"({len(travel_seen)} cycles, all close_travel_s must be null)")
    if events:
        q = events[0]["raw"].get("close_quality") or ""
        print(f"  REASON FLAG  {'OK' if 'not-measured' in q else 'MISSING'}   {q[:70]}...")

    truth = dwr.load_truth("tools/groundtruth_20260805.csv", cam)
    phantoms = dwr.load_phantom("tools/phantom_periods_20260805.csv", cam)
    m, missed, ph, unm, gradable = dwr.score(events, truth, phantoms, 5.0, cam)
    print()
    print(f"  emitted {len(events)} close events; gradable truth closes: {len(gradable)}")
    print(f"  DETECTED   {len(m)}/{len(gradable)}")
    print(f"  MISSED     {len(missed)}")
    print(f"  PHANTOM    {len(ph)}")
    print(f"  UNMATCHED  {len(unm)}")
    return {"detected": len(m), "gradable": len(gradable), "phantom": len(ph),
            "emitted": len(events), "unmatched": len(unm), "wire_bad": bad,
            "non_null_travel": non_null}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True, choices=sorted(truth_io.CORPUS))
    ap.add_argument("--video", required=True)
    ap.add_argument("--tpl-dir", default=TPL_DIR)
    ap.add_argument("--frame-stride", type=int, default=2)
    a = ap.parse_args()
    r = run(a.cam, a.video, a.tpl_dir, a.frame_stride)
    return 0 if (not r["wire_bad"] and not r["non_null_travel"]) else 1


if __name__ == "__main__":
    sys.exit(main())
