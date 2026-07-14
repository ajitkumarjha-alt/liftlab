#!/usr/bin/env python3
"""Fix RTSP auth in the agent's playback_url(): URL-encode NVR user+password.
A password with '@' (e.g. lodha@000) otherwise splits the userinfo -> auth
fails -> the NVR returns a default camera for every channel. Anchored,
idempotent, backup-first. (repo liftlab/nvr.py already quotes creds; this fixes
the agent path used by pull_playback and the survey playback fallback.)"""
import pathlib
import shutil
import time

p = pathlib.Path("/home/askjitk/liftlab-b3/pi-agent/agent.py")
s = p.read_text()
if "quote(NVR_PASS" in s:
    print("agent playback_url already url-encodes creds — no change")
    raise SystemExit

edits = []
if "from urllib.parse import quote" not in s:
    edits.append(("import httpx", "import httpx\nfrom urllib.parse import quote"))
edits.append((
    'f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_HOST}:{NVR_PORT}"',
    'f"rtsp://{quote(NVR_USER, safe=\'\')}:{quote(NVR_PASS, safe=\'\')}@{NVR_HOST}:{NVR_PORT}"',
))

for old, _ in edits:
    if old not in s:
        print(f"ANCHOR NOT FOUND: {old[:44]!r} — aborting, no write")
        raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
for old, new in edits:
    s = s.replace(old, new, 1)
p.write_text(s)
print("agent patched: playback_url now url-encodes NVR creds; backup written")
