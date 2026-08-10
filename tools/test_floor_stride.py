#!/usr/bin/env python3
"""FLOOR_STRIDE: fewer floor reads must mean fewer REDUNDANT reads, not fewer attributed floors.

WHAT THIS PROVES AND WHAT IT DOES NOT.

PROVES (mechanism, deterministically):
  * a carried-forward frame reports the previous read's floor with a truthful floor_age_s;
  * a frame that reads reports floor_age_s == 0.0;
  * FloorTracker is fed ONLY real reads — a carried value never invents dwell;
  * `stop` is never emitted from a carried frame;
  * the floor sequence at stride N is the stride-1 sequence SUBSAMPLED — no floor is invented,
    and a floor change is observed within N frames of when stride-1 would have seen it.

DOES NOT PROVE: the live attribution rate on ch29. That needs ch29 frames, and no ch29 corpus
exists. The acceptance query for the live window is in DEPLOY_floor_stride.md. This test is the
floor under that measurement, not a substitute for it.

The panel reader is stubbed with a scripted floor-vs-frame timeline, so "what the camera shows"
is known exactly and attribution can be checked against ground truth rather than against itself.
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
import gpu_door as gd


class _StubReader:
    """Reads a floor off a scripted timeline keyed by frame index carried in the crop's [0,0] pixel."""
    arrow_labels = ("up", "down")

    def __init__(self, timeline):
        self.timeline = timeline
        self.calls = 0

    def read_panel(self, crop_img):
        self.calls += 1
        i = int(crop_img[0, 0])
        return {"status": "ok", "floor": self.timeline[i], "direction": None,
                "score": 0.9, "cells": None, "shift": 0}


def build(timeline):
    eng = gd.DoorFloorEngine.__new__(gd.DoorFloorEngine)      # bypass template/geometry setup
    rdr = _StubReader(timeline)
    eng.readers = [((0, 0, 4, 4), rdr)]
    eng.door_roi = (0, 0, 8, 8)
    eng.state_tpl = None
    eng.state_band_y = eng.state_roi_x_w = None
    eng.door = gd.DoorTracker()
    eng.floor = gd.FloorTracker()
    eng.hash = "stub"
    eng._last_floor = None
    return eng, rdr


def run(timeline, stride, fps=25.0):
    """-> (per-frame reported floors, per-frame floor_age_s, reader call count)"""
    eng, rdr = build(timeline)
    floors, ages = [], []
    for i in range(len(timeline)):
        frame = np.full((8, 8), i, dtype=np.uint8)            # the index IS the pixel
        rec = eng.process(frame, i / fps, do_floor=(stride <= 0 or i % stride == 0))
        floors.append(rec["floor"])
        ages.append(rec["floor_age_s"])
    return floors, ages, rdr.calls


def main():
    fps = 25.0
    # The car sits at a floor for 40 frames (1.6s at 25fps), then changes. Six stops.
    # Kept under 256 frames because the stub encodes the frame index in a uint8 pixel.
    DWELL = 40
    timeline = []
    for f in ["3", "4", "5", "6", "7", "8"]:
        timeline += [f] * DWELL
    fails = []

    base_floors, base_ages, base_calls = run(timeline, 0)
    if base_calls != len(timeline):
        fails.append(f"stride 0 should read every frame: {base_calls}/{len(timeline)}")
    if any(a != 0.0 for a in base_ages):
        fails.append("stride 0 produced a non-zero floor_age_s")
    if base_floors != timeline:
        fails.append("stride 0 did not report the timeline verbatim")

    for stride in (2, 6, 12, 25):
        floors, ages, calls = run(timeline, stride)
        expected_calls = sum(1 for i in range(len(timeline)) if i % stride == 0)
        if calls != expected_calls:
            fails.append(f"stride {stride}: {calls} reads, expected {expected_calls}")
        # every reported floor must be one the camera ACTUALLY showed at or before that frame
        for i, (got, age) in enumerate(zip(floors, ages)):
            src = i - int(round(age * fps))
            if got is None:
                fails.append(f"stride {stride}: frame {i} reported no floor"); break
            if src < 0 or timeline[src] != got:
                fails.append(f"stride {stride}: frame {i} reported {got!r} with age {age}s -> "
                             f"frame {src}, which showed {timeline[src] if 0 <= src else 'n/a'!r}")
                break
            if age > (stride - 1) / fps + 1e-9:
                fails.append(f"stride {stride}: frame {i} age {age}s exceeds the stride bound")
                break
        # ATTRIBUTION: every DISTINCT floor the camera showed must still be attributed
        if set(f for f in floors if f) != set(timeline):
            missing = set(timeline) - set(f for f in floors if f)
            fails.append(f"stride {stride}: LOST floors {sorted(missing)} — attribution degraded")
        # and a change must be seen within `stride` frames of the true change
        for i in range(1, len(timeline)):
            if timeline[i] != timeline[i - 1]:
                seen = next((j for j in range(i, min(i + stride + 1, len(floors)))
                             if floors[j] == timeline[i]), None)
                if seen is None:
                    fails.append(f"stride {stride}: change at {i} not observed within {stride} frames")
                    break
        print(f"  stride {stride:>2}: {calls:>3} reads ({calls / len(timeline):.0%} of stride-0), "
              f"distinct floors attributed {len(set(f for f in floors if f))}/{len(set(timeline))}, "
              f"max age {max(ages):.3f}s")

    # NEGATIVE CONTROL — the attribution check must be CAPABLE of failing, or the passes above
    # mean nothing. At a stride longer than the dwell, the camera can change floor entirely between
    # reads and that floor is never attributed. This asserts the check FIRES, and it also locates
    # the knob's limit: FLOOR_STRIDE must stay comfortably under the shortest dwell of interest.
    too_long = DWELL + 20
    floors_bad, _, _ = run(timeline, too_long)
    lost = set(timeline) - set(f for f in floors_bad if f)
    print(f"  negative control (stride {too_long} > dwell {DWELL}): lost {sorted(lost) or 'nothing'}")
    if not lost:
        fails.append("negative control did not lose a floor — the attribution assertion has no teeth")

    # FloorTracker must never be fed a carried value: stops only from real reads
    eng, rdr = build(timeline)
    carried_stops = 0
    for i in range(len(timeline)):
        frame = np.full((8, 8), i, dtype=np.uint8)
        do = (i % 12 == 0)
        rec = eng.process(frame, i / fps, do_floor=do)
        if rec["stop"] is not None and not do:
            carried_stops += 1
    if carried_stops:
        fails.append(f"{carried_stops} stops emitted from carried-forward frames (invented dwell)")

    print()
    print("FAIL: " + "; ".join(fails) if fails else "FLOOR_STRIDE: ALL ASSERTIONS PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
