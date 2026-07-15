#!/usr/bin/env python3
"""Mount live_router in main.py (import + include_router). Anchored, idempotent,
backup-first. Run as the liftlab owner. No Caddy change: /api/gw/* is already
basicauth-exempt (token) and /live/* inherits basicauth via the existing proxy."""
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
            print(f"  {label}: ANCHOR NOT FOUND {old[:46]!r} — SKIP (no write)")
            return
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    for old, new in edits:
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"  {label}: patched")


patch(f"{APP}/main.py", [
    ("from events_api import events_router",
     "from events_api import events_router\nfrom live_api import live_router"),
    ("app.include_router(events_router)",
     "app.include_router(events_router)\napp.include_router(live_router)"),
], "live_router", "main.py")

print("live_router mount done")
