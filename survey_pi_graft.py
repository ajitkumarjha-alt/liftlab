#!/usr/bin/env python3
"""Add the 'survey' job-type dispatch to the live agent. The handler lives in
agent_survey.py (imported lazily), so this is a tiny, safe, additive patch.
Anchored, idempotent, backup-first."""
import pathlib
import re
import shutil
import time

p = pathlib.Path("/home/askjitk/liftlab-b3/pi-agent/agent.py")
s = p.read_text()
if 'job["type"] == "survey"' in s:
    print("agent already dispatches survey — no change")
    raise SystemExit

old = ('                    elif job["type"] == "analyze_local":\n'
       '                        analyze_local(client, job)')
new = (old + '\n'
       '                    elif job["type"] == "survey":\n'
       '                        import agent_survey\n'
       '                        agent_survey.run_survey(\n'
       '                            job, client=client, cloud=CLOUD, gw_id=GW_ID, headers=H(),\n'
       '                            report=report, log=log, workdir=WORKDIR, zones_path=ZONES_PATH,\n'
       '                            nvr=(NVR_HOST, NVR_PORT, NVR_USER, NVR_PASS), playback_url=playback_url)')

if old not in s:
    print("ANCHOR NOT FOUND (analyze_local dispatch) — aborting, no write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
s = s.replace(old, new, 1)
s = re.sub(r'VERSION = "0\.3\.\d+"', 'VERSION = "0.4.0"', s, count=1)
p.write_text(s)
print("agent patched: survey dispatch added, VERSION -> 0.4.0; backup written")
