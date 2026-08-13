#!/usr/bin/env python3
"""Does dash_api._h3_cycle_ts count REAL cycles? Graded against the frame-anchored hand truth.

WHY THIS AND NOT HAND-CHECKING. The corpus already knows the answer: groundtruth_20260805.csv has
one row per real close, anchored to frames in named files, and doorwatch_replay can drive the real
h3 tracker over those same files. So the claim layer can be graded against truth directly rather
than by eye.

WHAT IS BEING GRADED. Not the tracker — the CLAIM. _h3_cycle_ts counts a cycle on every transition
INTO 'closed' from 'closing' or 'open', with no dwell test, so a sub-second chatter burst counts
exactly like a lift that stood open and shut. This runs that function verbatim over the wire-state
stream the engine would have written, at dwell thresholds 0/1/2/3s, and compares the count against
the hand-timed closes.

WHAT A DWELL THRESHOLD MEANS HERE: the state preceding the close must have HELD that long before the
close is counted. 0 is the shipped behaviour.

  python3 tools/dwell_validation.py                       # all three corpus cameras
  python3 tools/dwell_validation.py --cam ch27
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The corpus, as tools/weekly_stopwatch.md LEG 1 runs it.
CORPUS = {
    "ch30": {"video": "~/projects/liftlab/ch30_peak.mp4", "roi": (2, 2, 335, 446)},
    "ch27": {"video": "~/dwrec/rec/ch27_clean.mp4", "roi": (125, 3, 238, 397)},
}


def emitted_rows(wire_states):
    """The gw_door_event stream: emit-on-change over door_state.

    The corpus replay has no panel, so floor and direction are constant — which is exactly the
    isolation we want. Every row here is a door_state transition and nothing else, so what this
    grades is the door-state half of the claim, uncontaminated by the arrow reader.
    """
    out, prev = [], "__init__"
    for t, st in wire_states:
        if st != prev:
            out.append({"ts": t, "door_state": st})
            prev = st
    return out


def cycle_ts(rows, min_dwell_s=0.0):
    """dash_api._h3_cycle_ts, verbatim, plus an optional dwell test on the PRECEDING state."""
    out, prev, prev_ts = [], None, None
    for r in rows:
        st = r["door_state"]
        if st == "closed" and prev in ("closing", "open"):
            if min_dwell_s <= 0 or (prev_ts is not None and r["ts"] - prev_ts >= min_dwell_s):
                out.append(r["ts"])
        if st != prev:
            prev, prev_ts = st, r["ts"]
    return out


def grade(claimed_ts, truth, fps, tol=5.0):
    """-> (matched, missed, false). One truth close may match at most one claim."""
    tr = sorted((row["end_f"] - 1) / fps for row in truth)
    used, matched, false = set(), 0, 0
    for c in sorted(claimed_ts):
        hit = None
        for k, t in enumerate(tr):
            if k in used:
                continue
            if abs(t - c) <= tol:
                hit = k
                break
        if hit is None:
            false += 1
        else:
            used.add(hit)
            matched += 1
    return matched, len(tr) - len(used), false


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", action="append")
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--tol", type=float, default=5.0)
    ap.add_argument("--truth", default="tools/groundtruth_20260805.csv")
    a = ap.parse_args()
    cams = a.cam or sorted(CORPUS)

    import doorwatch_replay as dr

    print("DWELL VALIDATION — dash_api._h3_cycle_ts graded against frame-anchored hand truth.")
    print("A cycle is CLAIMED on a transition into 'closed'; the dwell test asks how long the")
    print("preceding state held. 0.0s is what the compliance panel ships today.\n")
    verdict = {}
    for cam in cams:
        spec = CORPUS[cam]
        video = os.path.expanduser(spec["video"])
        if not os.path.exists(video):
            print(f"=== {cam}: SKIPPED — {video} not present ===\n")
            continue
        truth = dr.load_truth(a.truth, cam)
        ev, diag = dr.replay_h3(cam, video, list(spec["roi"]), 0.0, a.stride)
        rows = emitted_rows(diag["wire_states"])
        fps = diag["fps"]
        print(f"=== {cam} — {os.path.basename(video)} @ {fps}fps, stride {a.stride} ===")
        print(f"  hand-timed real closes: {len(truth)}   engine cycle EVENTS (tracker): {len(ev)}")
        print(f"  gw_door_event rows the emit gate would write (door_state only): {len(rows)}")
        print(f"  {'dwell':>6} {'claimed':>8} {'matched':>8} {'missed':>7} {'FALSE':>6}  verdict")
        best = None
        for d in (0.0, 1.0, 2.0, 3.0):
            cts = cycle_ts(rows, d)
            m, miss, fa = grade(cts, truth, fps, a.tol)
            flag = ""
            if best is None and fa == 0:
                best = d
                flag = "  <- first threshold with NO false cycles"
            print(f"  {d:>5.1f}s {len(cts):>8} {m:>8} {miss:>7} {fa:>6}{flag}")
        verdict[cam] = best
        print()
    print("SUMMARY")
    for cam, b in verdict.items():
        print(f"  {cam}: lowest dwell with zero false cycles = "
              + (f"{b:.1f}s" if b is not None else "NONE of 0/1/2/3s eliminated them"))
    print("\nThe threshold the truth supports goes into _h3_cycle_ts — the CLAIM layer. The raw")
    print("gw_door_event stream stays lossless; a row is evidence, a cycle is an assertion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
