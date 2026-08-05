#!/usr/bin/env python3
"""One reader for the frame-anchored ground truth. Five tools used to each parse the old
wall-clock schema themselves; that is why a schema change broke all five at once.

WHAT CHANGED AND WHY IT MATTERS. The old truth was `close_end_osd` — a wall-clock instant read off
the burnt-in OSD — plus a per-row `travel_err_s`. Matching an emission to it meant mapping video
position to wall clock through an `--osd-base` offset, and that mapping was neither exact nor
monotonic: ch30 carried a ~4s segment-overlap drift and a concat inversion, which between them
faked a phantom count (3013116) and forced a drift correction that made a detection score partly
circular (63064cb).

The clean corpus is anchored in FRAMES of a named file. There is no offset to fit and no clock to
drift, so matching is exact. OSD is retained for display only — never for matching.

`travel_err_s` is gone by design. Frame-anchored endpoints carry roughly +/-2 frames per end, so
tools that need an error term use the flat TRAVEL_ERR_S below rather than a per-row column.
"""
from __future__ import annotations

import csv
import datetime as dt
import os

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"

# Flat hand-timing error: ~+/-2 frames (+/-0.08s) at each end of a frame-anchored close.
TRAVEL_ERR_S = 0.16

# The clean corpus. fps/frames are the measured values, not the nominal 25 — ch27 in particular
# runs at 24.8910, and using 25 would bias every derived time by ~0.4%.
CORPUS = {
    "ch27": {"file": "ch27_clean.mp4", "path": "~/dwrec/rec/ch27_clean.mp4",
             "fps": 24.8910, "frames": 70260, "osd_base": "16:05:33",
             "roi": (125, 3, 238, 397)},
    "ch30": {"file": "ch30_peak.mp4", "path": "~/projects/liftlab/ch30_peak.mp4",
             "fps": 24.9945, "frames": 27257, "osd_base": "16:59:30",
             "roi": (2, 2, 335, 446)},
}

DEFAULT_TRUTH = "tools/groundtruth_20260805.csv"
DEFAULT_PHANTOMS = "tools/phantom_periods_20260805.csv"


def video_path(cam):
    return os.path.expanduser(CORPUS[cam]["path"])


def _rows(path):
    """CSV rows with '#' comment lines dropped. The header must be line 1; comments follow it."""
    with open(path) as fh:
        lines = list(fh)
    if not lines:
        return []
    keep = [lines[0]] + [ln for ln in lines[1:] if not ln.lstrip().startswith("#")]
    return [r for r in csv.DictReader(keep) if r.get("cam") and not r["cam"].startswith("#")]


def frame_to_s(cam, f):
    return f / CORPUS[cam]["fps"]


def frame_to_osd(cam, f):
    base = CORPUS[cam]["osd_base"]
    h, m, s = (int(x) for x in base.split(":"))
    t = dt.datetime(int(DAY[:4]), int(DAY[5:7]), int(DAY[8:10]), h, m, s, tzinfo=IST)
    return (t + dt.timedelta(seconds=frame_to_s(cam, f))).strftime("%H:%M:%S")


def load_truth(cam, path=DEFAULT_TRUTH, gradable_only=False):
    """Ground-truth closes for one camera, frame-anchored.

    Each row: start_f, end_f (close start / close full), travel_s (None when not timed), status.
    `timed` marks the rows a travel test may score — status 'clean' with a travel value. Rows
    flagged *_excluded are real closes but deliberately not timed; they still count as closes for
    detection, which is why they are returned rather than filtered out by default.
    """
    out = []
    for r in _rows(path):
        if r["cam"] != cam:
            continue
        if gradable_only and r["status"] != "clean":
            continue
        a, b = int(r["start_f"]), int(r["end_f"])
        tv = float(r["travel_s"]) if r["travel_s"] else None
        out.append({"cam": cam, "file": r["file"], "start_f": a, "end_f": b,
                    "travel_s": tv, "status": r["status"],
                    "err_s": TRAVEL_ERR_S if tv is not None else None,
                    "timed": tv is not None and r["status"] == "clean",
                    "start_s": frame_to_s(cam, a), "end_s": frame_to_s(cam, b),
                    "osd": frame_to_osd(cam, b)})
    return sorted(out, key=lambda x: x["start_f"])


def load_phantoms(cam, path=DEFAULT_PHANTOMS):
    """Door-closed windows. NOT occupancy-verified: an emission inside one is a phantom regardless
    of who is in the cabin, because a door that is not moving is not closing."""
    return [{"cam": cam, "start_f": int(r["start_f"]), "end_f": int(r["end_f"]),
             "note": r["note"], "osd": frame_to_osd(cam, int(r["start_f"]))}
            for r in _rows(path) if r["cam"] == cam]


def in_phantom(phantoms, frame):
    return next((p for p in phantoms if p["start_f"] <= frame <= p["end_f"]), None)


def match_closes(emitted_frames, truth, tol_s, cam):
    """Greedy nearest match of emissions to truth closes, in FRAMES (no clock, no drift).

    An emission matches the nearest unused truth close whose end_f is within tol_s. Returns
    (matches, missed, unmatched); matches carry the frame offset so a systematic bias is visible
    rather than hidden inside a tolerance.
    """
    tol_f = tol_s * CORPUS[cam]["fps"]
    used, matches, unmatched = set(), [], []
    for ef in sorted(emitted_frames):
        best, bd = None, None
        for i, t in enumerate(truth):
            if i in used:
                continue
            d = abs(ef - t["end_f"])
            if bd is None or d < bd:
                best, bd = i, d
        if best is not None and bd <= tol_f:
            used.add(best)
            matches.append({"emit_f": ef, "truth": truth[best], "d_f": ef - truth[best]["end_f"],
                            "d_s": (ef - truth[best]["end_f"]) / CORPUS[cam]["fps"]})
        else:
            unmatched.append(ef)
    missed = [t for i, t in enumerate(truth) if i not in used]
    return matches, missed, unmatched
