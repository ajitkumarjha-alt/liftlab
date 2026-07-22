#!/usr/bin/env python3
"""Mount calib_run_router in main.py (import + include_router), after calib_cells_router. Anchored,
idempotent, backup-first — same shape as apply_calib_cells_patch.py."""
import pathlib
import shutil
import time

APP = "/opt/liftlab-b3/cloud"


def patch(path, edits, marker, label):
    p = pathlib.Path(path)
    s = p.read_text()
    if marker in s:
        print(f"  {label}: already patched — skip")
        return
    for old, _ in edits:
        if old not in s:
            print(f"  {label}: ANCHOR NOT FOUND {old[:46]!r} — SKIP")
            return
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    for old, new in edits:
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"  {label}: patched")


patch(f"{APP}/main.py", [
    ("from calib_cells_api import calib_cells_router",
     "from calib_cells_api import calib_cells_router\nfrom calib_run_api import calib_run_router"),
    ("app.include_router(calib_cells_router)",
     "app.include_router(calib_cells_router)\napp.include_router(calib_run_router)"),
], "calib_run_router", "main.py")
print("calib_run_router mount done")
