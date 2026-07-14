#!/usr/bin/env python3
"""Add cheap telemetry to the agent heartbeat: get_throttled (hex), loadavg
(1/5/15), mem free/total. Temp/disk/uptime/version already sent. Anchored,
idempotent, backup-first. Restart the agent after."""
import pathlib
import re
import shutil
import time

p = pathlib.Path("/home/askjitk/liftlab-b3/pi-agent/agent.py")
s = p.read_text()
if '"throttled"' in s and "get_throttled" in s:
    print("agent heartbeat already has throttled telemetry — no change")
    raise SystemExit

anchor = '''    try:
        payload["soc_temp"] = subprocess.check_output(
            ["vcgencmd", "measure_temp"], text=True, timeout=3).strip()
    except Exception:
        pass'''
addition = '''
    try:
        payload["throttled"] = subprocess.check_output(
            ["vcgencmd", "get_throttled"], text=True, timeout=3).strip().split("=")[1]
    except Exception:
        pass
    try:
        _la = os.getloadavg()
        payload["loadavg"] = [round(_la[0], 2), round(_la[1], 2), round(_la[2], 2)]
    except Exception:
        pass
    try:
        _mi = {}
        for _line in open("/proc/meminfo"):
            if _line.startswith(("MemTotal:", "MemAvailable:")):
                _mi[_line.split(":")[0]] = int(_line.split()[1])
        payload["mem_total_mb"] = _mi.get("MemTotal", 0) // 1024
        payload["mem_free_mb"] = _mi.get("MemAvailable", 0) // 1024
    except Exception:
        pass'''

if anchor not in s:
    print("ANCHOR NOT FOUND (soc_temp heartbeat block) — aborting, no write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
s = s.replace(anchor, anchor + addition, 1)
s = re.sub(r'VERSION = "0\.\d+\.\d+"', 'VERSION = "0.6.0"', s, count=1)
p.write_text(s)
print("agent patched: heartbeat telemetry (throttled/loadavg/mem) added, VERSION -> 0.6.0; backup written")
