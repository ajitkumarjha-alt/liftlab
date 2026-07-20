#!/usr/bin/env python3
"""Mount door_event_router in main.py (import + include_router), after ops_router. Anchored,
idempotent, backup-first — same shape as apply_calib_label_patch.py."""
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
    ("from ops_api import ops_router",
     "from ops_api import ops_router\nfrom door_event_api import door_event_router"),
    ("app.include_router(ops_router)",
     "app.include_router(ops_router)\napp.include_router(door_event_router)"),
], "door_event_router", "main.py")
print("door_event_router mount done")
