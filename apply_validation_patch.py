#!/usr/bin/env python3
"""Mount validation_router in main.py. Anchored, idempotent, backup-first."""
import pathlib
import shutil
import time

APP = "/opt/liftlab-b3/cloud"
p = pathlib.Path(f"{APP}/main.py")
s = p.read_text()
if "validation_router" in s:
    print("  main.py: already patched — skip")
else:
    a1 = "from events_api import events_router"
    a2 = "app.include_router(events_router)"
    if a1 in s and a2 in s:
        shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
        s = s.replace(a1, a1 + "\nfrom validation_api import validation_router", 1)
        s = s.replace(a2, a2 + "\napp.include_router(validation_router)", 1)
        p.write_text(s)
        print("  main.py: patched (validation_router)")
    else:
        print("  main.py: ANCHOR NOT FOUND — SKIP")
print("validation mount done")
