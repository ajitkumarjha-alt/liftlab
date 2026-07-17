#!/usr/bin/env python3
"""Mount dash_router in main.py (import + include_router). Anchored, idempotent, backup-first."""
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
    ("from survey_api import survey_router",
     "from survey_api import survey_router\nfrom dash_api import dash_router"),
    ("app.include_router(survey_router)",
     "app.include_router(survey_router)\napp.include_router(dash_router)"),
], "dash_router", "main.py")
print("dash_router mount done")
