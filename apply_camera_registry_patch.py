#!/usr/bin/env python3
"""Mount camera_registry_router in main.py. Anchored, idempotent, backup-first."""
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
    ("from calib_run_api import calib_run_router",
     "from calib_run_api import calib_run_router\nfrom camera_registry_api import camera_registry_router"),
    ("app.include_router(calib_run_router)",
     "app.include_router(calib_run_router)\napp.include_router(camera_registry_router)"),
], "camera_registry_router", "main.py")
print("camera_registry_router mount done")
