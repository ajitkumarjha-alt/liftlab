#!/usr/bin/env python3
"""Add the 'watch_channel' job dispatch to the live agent (continuous single-cabin
door-cycle scheduler, v1=A). The agent venv is import-light (no numpy/av/liftlab),
so the handler lives in watch_manager.py (STDLIB ONLY), which launches
continuous_scheduler.py as a SUBPROCESS under the full-deps python. NON-BLOCKING:
start Popen()s and returns at once, so the poll loop keeps heartbeating and a later
{action:stop} is receivable. Anchored, idempotent, backup-first."""
import pathlib
import re
import shutil
import time

p = pathlib.Path("/home/askjitk/liftlab-b3/pi-agent/agent.py")
s = p.read_text()
if 'job["type"] == "watch_channel"' in s:
    print("agent already dispatches watch_channel — no change")
    raise SystemExit

# anchor on the analyze_local dispatch (stable; survey grafted after it too)
old = ('                    elif job["type"] == "analyze_local":\n'
       '                        analyze_local(client, job)')
new = (old + '\n'
       '                    elif job["type"] == "watch_channel":\n'
       '                        import watch_manager\n'
       '                        watch_manager.run_watch(\n'
       '                            job, report=report, log=log, cloud=CLOUD, gw_id=GW_ID,\n'
       '                            headers=H(), zones_path=ZONES_PATH,\n'
       '                            nvr=(NVR_HOST, NVR_PORT, NVR_USER, NVR_PASS))')

if old not in s:
    print("ANCHOR NOT FOUND (analyze_local dispatch) — aborting, no write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
s = s.replace(old, new, 1)
s = re.sub(r'VERSION = "0\.\d+\.\d+"', 'VERSION = "0.7.0"', s, count=1)
p.write_text(s)
print("agent patched: watch_channel dispatch added (non-blocking), VERSION -> 0.7.0; backup written")
