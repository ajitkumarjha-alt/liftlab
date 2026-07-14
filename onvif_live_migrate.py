#!/usr/bin/env python3
"""Migrate pull_playback + analyze_local off the falsified cam/playback?channel=
builder onto the ONVIF live channel map (this NVR only selects channels via the
ONVIF /ch/stream path; historical is unavailable — see NVR_CAPABILITIES.md).

Patches (anchored, idempotent, backup-first):
  agent.py            : add _onvif_live_url(); pull_playback + analyze_local capture
                        LIVE from the ONVIF-resolved per-channel URI; VERSION -> 0.5.0
  analyze_runner.py   : add a live_url capture branch (import subprocess)
No historical semantics remain in these paths; a "window" duration = live capture_s.
"""
import pathlib
import re
import shutil
import time

AGENT = "/home/askjitk/liftlab-b3/pi-agent/agent.py"
RUNNER = "/home/askjitk/liftlab-b4/analyze_runner.py"


def backup(p):
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")


# ---------------- agent.py ----------------
ap = pathlib.Path(AGENT)
a = ap.read_text()
if "_onvif_live_url" in a:
    print("agent.py already migrated — skip")
else:
    helper = '''def _onvif_live_url(channel, prefer=1):
    """ONVIF-resolved LIVE per-channel URI (creds injected). This NVR ignores
    ?channel=N on constructed RTSP; only the ONVIF /ch/stream path selects a
    channel, and historical is unavailable (see NVR_CAPABILITIES.md)."""
    try:
        import onvif_resolve
        cmap = onvif_resolve.resolve_map(NVR_HOST, NVR_USER, NVR_PASS,
                                         cache_path=str(WORKDIR / f"onvif_map_{GW_ID}.json"))
        raw = onvif_resolve.uri_for(cmap, int(channel), prefer=prefer)
    except Exception as e:
        log(f"onvif resolve failed: {e}")
        return None
    if not raw:
        return None
    from urllib.parse import quote as _q, urlparse as _up
    if _up(raw).username:
        return raw
    return raw.replace("rtsp://", f"rtsp://{_q(NVR_USER, safe='')}:{_q(NVR_PASS, safe='')}@", 1)


def pull_playback(client, job) -> None:'''

    pp_url_old = '    url = playback_url(p["channel"], p["start"], p["end"])'
    pp_url_new = ('    url = _onvif_live_url(p["channel"])\n'
                  '    if not url:\n'
                  '        report(client, jid, "failed", 0, "no ONVIF live URI for channel")\n'
                  '        return')

    al_old = ('''    else:
        runner_job["start"] = p["start"]
        runner_job["end"] = p["end"]
        runner_job["nvr_settings"] = {"host": NVR_HOST, "port": int(NVR_PORT),
                                      "user": NVR_USER, "password": NVR_PASS}''')
    al_new = ('''    else:
        _live = _onvif_live_url(p.get("channel", 1))
        if not _live:
            report(client, jid, "failed", 0, "no ONVIF live URI for channel (analyze_local)")
            return
        runner_job["live_url"] = _live
        runner_job["capture_s"] = int(p.get("capture_s", 300))''')

    for anc in ("def pull_playback(client, job) -> None:", pp_url_old, al_old):
        if anc not in a:
            print(f"agent ANCHOR NOT FOUND: {anc[:44]!r} — aborting, no write")
            raise SystemExit
    backup(ap)
    a = a.replace("def pull_playback(client, job) -> None:", helper, 1)
    a = a.replace(pp_url_old, pp_url_new, 1)
    a = a.replace(al_old, al_new, 1)
    # ensure a live capture is time-bounded (-t); add only if not already present
    if "-c copy -an -t" not in a:
        a = a.replace("-c copy -an", "-c copy -an -t {int(dur)}", 1)
    a = re.sub(r'VERSION = "0\.\d+\.\d+"', 'VERSION = "0.5.0"', a, count=1)
    ap.write_text(a)
    print("agent.py migrated: pull_playback + analyze_local -> ONVIF live; VERSION -> 0.5.0")


# ---------------- analyze_runner.py ----------------
rp = pathlib.Path(RUNNER)
r = rp.read_text()
if 'job.get("live_url")' in r:
    print("analyze_runner.py already has live_url branch — skip")
else:
    r_old = '''    else:
        ns_d = job.get("nvr_settings")'''
    r_new = '''    elif job.get("live_url"):                      # ONVIF live capture (historical dead)
        dur = int(job.get("capture_s", 300))
        local = work / f"ch{channel:02d}_live_{datetime.now():%Y%m%d%H%M%S}.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-rtsp_transport", "tcp",
               "-i", job["live_url"], "-t", str(dur), "-c", "copy", "-an", str(local)]
        try:
            rr = subprocess.run(cmd, capture_output=True, text=True, timeout=dur * 2 + 60)
        except subprocess.TimeoutExpired:
            return _emit({"status": "failed", "error": "live capture timed out"})
        if rr.returncode != 0 or not local.exists() or local.stat().st_size < 100000:
            return _emit({"status": "failed", "error": f"live capture: {(rr.stderr or '')[:120]}"})
        acquired = "captured live via ONVIF"
    else:
        ns_d = job.get("nvr_settings")'''
    if r_old not in r:
        print(f"runner ANCHOR NOT FOUND: {r_old[:44]!r} — aborting, no write")
        raise SystemExit
    backup(rp)
    r = r.replace(r_old, r_new, 1)
    if "\nimport subprocess\n" not in r:
        r = r.replace("import shutil", "import shutil\nimport subprocess", 1)
    rp.write_text(r)
    print("analyze_runner.py migrated: live_url capture branch added")

print("\nDone. Restart the agent:  systemctl restart liftlab-agent")
