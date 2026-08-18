#!/usr/bin/env python3
"""A camera with a door band and NO panel geometry must run door-only, not come up door=off.

ch32/ch34/ch37 came up `door=off (geometry incomplete)` on 2026-08-13 after a deploy note that
promised "door state only; floor=NULL/no_read". The note described behaviour the code could not
perform: build_door_engine demanded DOOR_ROI_FRAME **and** PANEL_ROIS **and** DIGIT_CELLS **and**
ARROW_CELL, and DoorFloorEngine raised outright on an empty panel list.

The tempting shortcut was to invent panel geometry to satisfy the constructor. That would produce
CONFIDENT WRONG floor reads — valid_floors is None by default, so any assembled digit string is
accepted — which is precisely what the n>=20 correct-or-abstain gate exists to prevent. A NULL floor
carrying the reason 'no_panel_geometry' is the honest output for an uncalibrated panel.
"""
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main():
    fails = []
    import gpu_door as gd

    print("=== 1. the engine constructs with NO panels — both engines ===")
    # ch32/ch34/ch37 will run h3, so the h3 path is the one that must work. h2 is included because
    # door-only is orthogonal to the tracker and a mode that only works on one of them is a trap.
    tpl = np.full((32, 96), 128, np.uint8)
    meta = {"band_y": [15, 120], "roi_x_w": [240, 165], "template_wh": [96, 32],
            "md5": "0" * 32, "cam": "ch32"}
    eng = eng_h2 = None
    try:
        eng = gd.DoorFloorEngine({}, (240, 0, 165, 576), [], door_tracker=gd.DoorTrackerH3(),
                                 floor_tracker=gd.FloorTracker(), state_tpl=tpl, state_meta=meta)
        eng_h2 = gd.DoorFloorEngine({}, (240, 0, 165, 576), [], door_tracker=gd.DoorTracker(),
                                    floor_tracker=gd.FloorTracker())
        print(f"  h3 constructed; door_only={eng.door_only}")
        print(f"  h2 constructed; door_only={eng_h2.door_only}")
    except Exception as e:
        print(f"  RAISED: {type(e).__name__}: {e}")
        fails.append("DoorFloorEngine still refuses an empty panel list — a camera with a good door "
                     "band and no cells cannot run at all")

    if eng is not None:
        print("\n=== 2. the floor half returns NULL with a reason that names the cause ===")
        fr = eng.floor_pass(np.zeros((576, 704), np.uint8), 1.0)
        print(f"  floor={fr['floor']!r} direction={fr['direction']!r} reason={fr['reason']!r} "
              f"stop={fr['stop']!r}")
        if fr["floor"] is not None or fr["direction"] is not None:
            fails.append("door-only mode invented a floor or a direction")
        if fr["reason"] != "no_panel_geometry":
            fails.append(f"reason is {fr['reason']!r}, not 'no_panel_geometry' — a NULL floor here "
                         "would be indistinguishable from a failed OCR on a real panel")
        if fr["stop"] is not None:
            fails.append("a stop was reported without a panel to read it from")

        print("\n=== 3. the DOOR half still works — that is the whole point ===")
        frame = np.random.default_rng(3).integers(0, 255, (576, 704), dtype=np.uint8)
        for name, e in (("h3", eng), ("h2", eng_h2)):
            rec = e.process(frame, 1.0, do_floor=True)
            print(f"  {name}: door_state={rec['door_state']!r} floor={rec['floor']!r} "
                  f"reason={rec['reason']!r} floor_age_s={rec['floor_age_s']}")
            if "door_state" not in rec:
                fails.append(f"{name}: process() produced no door_state in door-only mode")
            if rec["floor"] is not None:
                fails.append(f"{name}: process() reported a floor in door-only mode")
            if rec["reason"] != "no_panel_geometry":
                fails.append(f"{name}: reason {rec['reason']!r} does not name the cause")

    print("\n=== 3b. build_door_engine() with a door band only and NO templates endpoint ===")
    # THE TEST THE OPERATOR SPECIFIED, and the one that would have caught gates 3-5 before deploy.
    # No panel geometry, and the templates URL points at a closed port so any fetch attempt fails
    # loudly rather than silently succeeding against a real endpoint.
    import importlib
    for k, v in (("ANALYSIS_TOKEN", "t"), ("CLOUD", "http://127.0.0.1:1"), ("CAM", "ch32"),
                 ("GW", "site-A"), ("GPU_DOOR", "1"),
                 ("DOOR_ROI_FRAME", "240,15,165,105")):
        os.environ[k] = v
    for k in ("PANEL_ROIS", "DIGIT_CELLS", "ARROW_CELL",
              "PANEL1_DIGIT_CELLS", "PANEL1_ARROW_CELL"):
        os.environ.pop(k, None)
    import gpu_analyze
    importlib.reload(gpu_analyze)
    eng2, reason = gpu_analyze.build_door_engine()
    print(f"  engine: {eng2 is not None}   reason/version: {str(reason)[:96]}")
    if eng2 is None:
        fails.append(f"build_door_engine returned None with a door band and no templates "
                     f"endpoint: {reason!r} — this is the 404 gate, still closed")
    else:
        print(f"  door_only={eng2.door_only}  door_version={reason}")
        if not eng2.door_only:
            fails.append("the engine built but is not in door-only mode")
        if not str(reason).startswith("notpl000"):
            fails.append(f"door_version {reason!r} does not declare the absent template set — a "
                         "hash of an empty dict reads as if templates exist")

    print("\n=== 3c. no OTHER unconditional floor-side dependency in the build path ===")
    ga = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "gpu_analyze.py")).read()
    body = ga[ga.index("def build_door_engine"):]
    body = body[:body.index("\ndef ", 10)]
    for pat, why in (
            ("tpl = gd.fetch_templates", "the templates fetch must sit behind `elif tpl is None`"),
            ("_parse_xywh_list(PANEL_ROIS)[0]", "indexing [0] of an empty PANEL_ROIS raises")):
        if pat in body and "elif tpl is None" not in body:
            fails.append(f"{why} ({pat!r} still unconditional)")
    if "_parse_xywh_list(PANEL_ROIS)[0]" in ga:
        fails.append("PANEL_ROIS is still indexed [0] somewhere — empty in door-only mode")
    if "not getattr(door_eng, \"door_only\", False)" not in ga:
        fails.append("the PERIODIC template refetch is not guarded — a door-only camera would 404 "
                     "against the templates endpoint every TEMPLATES_REFETCH_S forever")
    if "panel0_roi is not None and FLOORCHECK_PER_HR" not in ga:
        fails.append("floorcheck posting is not guarded — a door-only camera has no panel to crop")
    print("  templates fetch, periodic refetch, panel0_roi index and floorcheck all guarded")

    print("\n=== 4. the gate requires the door band, and ONLY the door band ===")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "gpu_analyze.py")).read()
    if re.search(r"if not \(DOOR_ROI_FRAME and PANEL_ROIS", src):
        fails.append("build_door_engine still demands all four fields — a door band alone still "
                     "yields door=off")
    if "if not DOOR_ROI_FRAME:" not in src:
        fails.append("the door band is no longer a hard requirement; it must be")
    if "_door_only = not (PANEL_ROIS and DIGIT_CELLS and ARROW_CELL)" not in src:
        fails.append("no door-only determination in build_door_engine")
    print("  door band required; panel fields optional and their absence names the mode")

    print("\n=== 5. NOTHING invents panel geometry to satisfy the constructor ===")
    for bad in ("panels = [((0, 0, 1, 1)", "placeholder", "dummy_panel"):
        if bad in src:
            fails.append(f"gpu_analyze contains {bad!r} — fabricated geometry produces confident "
                         "WRONG floor reads, which is what the OCR gate exists to prevent")
    print("  no fabricated panel/cell geometry anywhere in the build path")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — a door band alone runs the door engine; the floor is NULL with a reason that names "
          "why, and no geometry is invented to get there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
