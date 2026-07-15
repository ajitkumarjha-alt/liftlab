#!/usr/bin/env python3
"""Patch events_api.py gw_events: DERIVE close_travel_s at ingest when the child
didn't send it but both close timestamps are present. The durable store must NEVER
hold NULL close_travel_s while it has both timestamps — that's THE number the
project measures. Fixes the currently-running old child (no close key) AND is
belt-and-suspenders for any future emitter. Backup-first, idempotent, and a
PRE-WRITE compile check so a bad anchor can never write a broken events_api.py."""
import os
import pathlib
import re
import shutil
import time

MARKER = "close_travel_s DERIVED at ingest"
p = pathlib.Path(os.environ.get("EVENTS_API", "/opt/liftlab-b3/cloud/events_api.py"))
s = p.read_text()
if MARKER in s:
    print("events_api.py already derives close_travel_s at ingest — skip")
    raise SystemExit

# anchor: the per-event loop `for <var> in payload.events:`
m = re.search(r'\n([ \t]+)for (\w+) in payload\.events\s*:[ \t]*\n', s)
if not m:
    print("ANCHOR NOT FOUND ('for <ev> in payload.events:') — no write. "
          "Paste gw_events so the anchor can be adjusted.")
    raise SystemExit

indent, var = m.group(1), m.group(2)
bi = indent + "    "  # loop body indent
inject = (
    f"{bi}# {MARKER}: never store NULL close_travel_s when both close ts present\n"
    f"{bi}if {var}.get('close_travel_s') is None and {var}.get('door_close_full_ts') and {var}.get('door_close_start_ts'):\n"
    f"{bi}    try:\n"
    f"{bi}        from datetime import datetime as _cdt\n"
    f"{bi}        {var}['close_travel_s'] = round((_cdt.fromisoformat({var}['door_close_full_ts']) - _cdt.fromisoformat({var}['door_close_start_ts'])).total_seconds(), 3)\n"
    f"{bi}    except Exception:\n"
    f"{bi}        pass\n"
)
new_s = s[:m.end()] + inject + s[m.end():]

try:
    compile(new_s, str(p), "exec")
except SyntaxError as e:
    print(f"patched content does NOT compile ({e}) — aborting, NO write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
p.write_text(new_s)
print(f"events_api.py patched: close_travel_s derived at ingest (loop var '{var}'); backup written")
