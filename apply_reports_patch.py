#!/usr/bin/env python3
"""Mount reports_router in main.py (import + include_router). Anchored, idempotent, backup-first.

Anchored on dash_router because /reports is a human page in the same family as /dash and inherits
the same Caddy basicauth — mounting it beside dash keeps that adjacency visible in main.py rather
than implied.
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
        return True
    for old, _ in edits:
        if old not in s:
            print(f"  {label}: ANCHOR NOT FOUND {old[:46]!r} — SKIP")
            return False
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    for old, new in edits:
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"  {label}: patched")
    return True


okd = patch(f"{APP}/main.py", [
    ("from dash_api import dash_router",
     "from dash_api import dash_router\nfrom reports_api import reports_router"),
    ("app.include_router(dash_router)",
     "app.include_router(dash_router)\napp.include_router(reports_router)"),
], "reports_router", "main.py")
print("reports_router mount " + ("done" if okd else "NOT DONE — anchors missing"))
raise SystemExit(0 if okd else 1)
