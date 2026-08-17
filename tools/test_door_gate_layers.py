#!/usr/bin/env python3
"""BOTH gates, together. Relaxing one of two changes nothing, and that is how this survived a fix.

592276a relaxed the WORKER's geometry check so a door band alone runs the engine. ch32/ch34/ch37
still came up `door=off` — because gpu_fleet has its OWN gate that decides GPU_DOOR before the
worker is spawned, and it still demanded all four fields. Two gates for one decision, a fix aimed
squarely at it, and the observable behaviour was identical afterwards.

The log line is why it survived: "geometry incomplete" was printed for four different absences, so
the message an operator read while looking at their freshly-drawn door band said only that
something was missing. This asserts BOTH layers agree and that the reason NAMES the field.
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def main():
    fails = []
    src_fleet = open(os.path.join(ROOT, "gpu_fleet.py")).read()
    src_worker = open(os.path.join(ROOT, "gpu_analyze.py")).read()

    print("=== 1. neither layer demands panel fields for the door engine ===")
    if re.search(r'GPU_DOOR"\]\s*=\s*"1" if \(geom\.get\("door_roi_frame"\)\s*and\s*geom\.get\("panel_rois"\)',
                 src_fleet):
        fails.append("gpu_fleet still requires all four fields for GPU_DOOR=1")
    if re.search(r"if not \(DOOR_ROI_FRAME and PANEL_ROIS", src_worker):
        fails.append("gpu_analyze still requires all four fields")
    print("  fleet: door_roi_frame gates GPU_DOOR;  worker: DOOR_ROI_FRAME is the only requirement")

    print("\n=== 2. the fleet's decision, driven directly ===")
    import importlib.util
    spec = importlib.util.spec_from_file_location("_fleet_probe", os.path.join(ROOT, "gpu_fleet.py"))
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass                                   # module-level config may exit without env; we only
    except Exception as e:                     # need _door_state_str, which is pure
        print(f"  (module import raised {type(e).__name__}; testing the string helper by source)")
        m = None

    cases = [
        ([], [], "ON door+floor", "everything calibrated"),
        ([], ["panel_rois", "digit_cells", "arrow_cell"], "ON door-only", "door band only — ch32/34/37"),
        ([], ["digit_cells", "arrow_cell"], "ON door-only", "panel drawn, cells not placed"),
        (["door_roi_frame"], ["panel_rois"], "off", "no door band"),
    ]
    if m and hasattr(m, "_door_state_str"):
        for dm, pm, want, why in cases:
            got = m._door_state_str(dm, pm)
            ok = got.startswith(want)
            print(f"  {'OK ' if ok else 'BAD'}  {why:38s} -> {got[:72]}")
            if not ok:
                fails.append(f"{why}: got {got[:60]!r}, expected to start {want!r}")
            # the reason must NAME the field, not just say something is missing
            if dm and "door_roi_frame" not in got:
                fails.append(f"{why}: the reason does not name door_roi_frame")
            # Panel fields are named only when they are the REASON — i.e. in the door-only case.
            # With no door band the engine is off regardless of the panel, and listing panel fields
            # there would be noise pointing away from the one thing to fix.
            if pm and not dm and not all(k in got for k in pm):
                fails.append(f"{why}: the reason does not name every missing panel field: {pm}")
        # Match the EMITTED string, not the docstring that quotes it to explain why it was wrong.
        # (My first version failed on its own explanatory comment — the same shape as the census
        # test that matched a comment quoting `sorted(seen)`.)
        emitted = re.sub(r'"""(?:.|\n)*?"""', "", src_fleet)
        if "geometry incomplete" in emitted:
            fails.append("the nameless 'geometry incomplete' string is still emitted somewhere")
    else:
        fails.append("could not import gpu_fleet to drive _door_state_str")

    print("\n=== 3. the two layers cannot disagree about what door-only means ===")
    if "no_panel_geometry" not in src_worker:
        fails.append("the worker has no no_panel_geometry reason")
    if "no_panel_geometry" not in src_fleet:
        fails.append("the fleet's log does not mention the reason the worker will report, so an "
                     "operator reading the supervisor cannot predict what the rows will say")
    print("  both name no_panel_geometry, so the supervisor line predicts the row reason")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — both gates require only the door band, and the supervisor's line names the field "
          "that decided it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
