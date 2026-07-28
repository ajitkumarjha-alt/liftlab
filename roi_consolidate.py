#!/usr/bin/env python3
"""Consolidate pre-consolidation calibration (sibling _calib_* files) into roi.json.

Cameras calibrated BEFORE the roi.json consolidation (ch29, likely ch30) keep their real
calibration in sibling files — _calib_rois.json (door/panels/frame, keys `door_roi`/`panels`,
list forms) and _calib_fitcells.json / _calib_cells.json (cells as "x,y,w,h;…" STRINGS; fitcells
SUPERSEDES cells — it is the final calibration step production built from). The registry's
_geometry() only reads roi.json, so a newborn roi.json (e.g. created by a zones POST) shadows all
of it and the fleet starts the worker door=off.

    sudo python3 roi_consolidate.py                 # report-only scan of every camera
    sudo python3 roi_consolidate.py --merge ch29    # consolidate ONE camera from its own _calib files

The merge preserves everything already in roi.json (zones, provenance), lets the _calib door value
WIN over any hand-seeded one (printed, never silent), writes atomically and keeps the dir's
ownership. Each gap camera gets its own explicit --merge run — deliberately no bulk mode; every
migration deserves its own eyes on the printed values. After a merge, the registry payload hash
changes and the fleet restarts that worker on its next poll (~30s): verify door=ON in the fleet log.
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("CALIB_BASE", "/var/lib/liftlab/calib"))
GW = os.environ.get("GW", "site-A")
NEED = ("door_roi_frame", "panel_rois", "cells")


def parse_cells_str(s):
    """door_calib's string forms: "x,y,w,h;x,y,w,h" -> [[x,y,w,h], ...]."""
    out = []
    for part in str(s or "").split(";"):
        nums = [p for p in part.split(",") if p.strip()]
        if len(nums) == 4:
            out.append([int(round(float(v))) for v in nums])
    return out


def load(p):
    try:
        v = json.loads(Path(p).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(base_gw):
    print(f"=== consolidation-gap scan ({base_gw}) — calib artifacts vs roi.json keys ===")
    rows = []
    for camdir in sorted(p for p in base_gw.iterdir() if p.is_dir()):
        cam = camdir.name
        has_rois = (camdir / "_calib_rois.json").exists()
        has_cells = any((camdir / n).exists() for n in ("_calib_fitcells.json", "_calib_cells.json"))
        r = load(camdir / "roi.json")
        missing = [k for k in NEED if not r.get(k)]
        if not missing:
            verdict = "OK (consolidated)"
        elif has_rois or has_cells:
            verdict = f"GAP: roi.json missing {missing} but calib artifacts EXIST -> run --merge {cam}"
        else:
            verdict = f"roi.json missing {missing}; no calib artifacts (never calibrated) -> wizard, not merge"
        print(f"  {cam:6s} rois={'Y' if has_rois else '-'} cells={'Y' if has_cells else '-'} "
              f"roi.json keys={len(r)} -> {verdict}")
        rows.append((cam, verdict))
    return rows


def merge(cam):
    d = BASE / GW / cam
    roi_p = d / "roi.json"
    rois = load(d / "_calib_rois.json")
    if not (rois.get("door_roi") and rois.get("panels") and rois.get("frame_wh")):
        sys.exit(f"{cam}: _calib_rois.json missing door_roi/panels/frame_wh — nothing safe to merge")
    cells_src, cells_name = None, None
    for name in ("_calib_fitcells.json", "_calib_cells.json"):   # fitcells first: it supersedes
        c = load(d / name)
        if c.get("digit_cells") and c.get("arrow_cell"):
            cells_src, cells_name = c, name
            break
    if not cells_src:
        sys.exit(f"{cam}: no usable digit_cells/arrow_cell in _calib_fitcells.json or _calib_cells.json")

    roi = load(roi_p)
    door = [int(v) for v in rois["door_roi"]]
    if roi.get("door_roi_frame") and list(roi["door_roi_frame"]) != door:
        print(f"NOTE: existing/seeded door {roi['door_roi_frame']} != _calib_rois {door} — "
              f"_calib wins (the confirmed source)")
    digit_cells = parse_cells_str(cells_src["digit_cells"])
    arrow = parse_cells_str(cells_src["arrow_cell"])
    if not digit_cells or not arrow:
        sys.exit(f"{cam}: cells unparsable from {cells_name}")
    print(f"door_roi_frame: {door}")
    print(f"panel_rois:     {rois['panels']}")
    print(f"frame_wh:       {rois['frame_wh']}")
    print(f"cells ({cells_name}): digit_cells={digit_cells} arrow_cell={arrow[0]}")

    roi.update({
        "door_roi_frame": door,
        "panel_rois": [[int(v) for v in p] for p in rois["panels"]],
        "frame_wh": [int(v) for v in rois["frame_wh"]],
        "cells": {"digit_cells": digit_cells, "arrow_cell": arrow[0]},
        "consolidated_note": (f"pre-consolidation calibration merged from _calib_rois.json + "
                              f"{cells_name} (fitcells supersedes cells); prior keys preserved"),
    })
    roi.pop("door_seed_note", None)
    tmp = str(roi_p) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(roi, f, indent=2)
    os.replace(tmp, roi_p)
    if hasattr(os, "chown"):                       # keep the file owned like its dir (absent on Windows)
        st = os.stat(d)
        os.chown(roi_p, st.st_uid, st.st_gid)

    final = load(roi_p)
    print(f"{cam} roi.json keys now: {sorted(final.keys())}")
    print(f"GPU_DOOR-complete: {all(final.get(k) for k in NEED)} | "
          f"zones intact: {bool(final.get('zone_cabin'))}")
    print("registry hash changes on the fleet's next poll (~30s) — verify door=ON in the fleet log")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", default="", metavar="CAM",
                    help="consolidate this camera's _calib files into its roi.json (default: scan only)")
    a = ap.parse_args()
    if a.merge:
        merge(a.merge)
        print()
    scan(BASE / GW)


if __name__ == "__main__":
    main()
