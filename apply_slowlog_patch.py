#!/usr/bin/env python3
"""Mount the slow-request middleware in main.py. Anchored, idempotent, backup-first.

Ordering note: this is mounted as HTTP middleware, so it wraps every router already included. It
does not need to be placed relative to any particular include_router — only after `app` exists.
"""
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
    ("from camera_registry_api import camera_registry_router",
     "from camera_registry_api import camera_registry_router\n"
     "from slowlog import banner as slowlog_banner, slow_request_middleware"),
    ("app.include_router(camera_registry_router)",
     "app.include_router(camera_registry_router)\n"
     "app.middleware(\"http\")(slow_request_middleware)\n"
     "slowlog_banner()"),
], "slow_request_middleware", "main.py")
print("slow-request logging mount done")
