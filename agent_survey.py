"""
On-Pi channel survey (commissioning) — imported LAZILY by the agent for the
'survey' job type, so the agent stays import-light.

One ~480px snapshot per channel. DEFAULT mode is 'playback' (a 3 s pull from
~lag seconds ago): the Dahua playback grammar is field-proven to honour the
channel param on this firmware. 'live' (realmonitor sub-stream) is opt-in via
params.snapshot_mode='live' — on some Dahua OEM firmware realmonitor IGNORES the
channel and returns one default camera (observed on this NVR: all 40 channels
came back as the same "42B REFUGE" feed), so it is NOT the default. Either way a
per-channel TRIPWIRE logs mode + masked request URL, so "one camera 40 times" is
visible in journalctl. Per-channel errors are recorded (non-fatal); snapshots
are deleted locally after upload.
"""
import json
import re
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


def _mask(url: str) -> str:
    return re.sub(r"://[^@/]*@", "://***:***@", url or "")


def _grab_frame(url: str, out: Path, timeout: int) -> bool:
    """Grab one frame ~1.5 s in (middle of ~3 s), scaled to 480px wide.
    True only on a non-trivial jpg."""
    transport = "-rtsp_transport tcp " if url.startswith("rtsp://") else ""
    cmd = (f"ffmpeg -hide_banner -loglevel error -y {transport}"
           f"-i {shlex.quote(url)} -ss 1.5 -frames:v 1 -vf scale=480:-1 -q:v 5 "
           f"{shlex.quote(str(out))}")
    try:
        r = subprocess.run(cmd, shell=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and out.exists() and out.stat().st_size > 1000


def _zoned_channels(zones_path) -> set:
    """Channels with a door_roi in camera_zones.json (for the has_zones flag)."""
    try:
        z = json.loads(Path(zones_path).read_text())
    except Exception:
        return set()
    out = set()
    for k, v in z.items():
        if isinstance(v, dict) and v.get("door_roi"):
            m = re.match(r"ch0*(\d+)", str(k))
            if m:
                out.add(int(m.group(1)))
    return out


def run_survey(job, *, client, cloud, gw_id, headers, report, log,
               workdir, zones_path, nvr, playback_url):
    p = job.get("params", {})
    jid = job["id"]
    c_from = int(p.get("from", 1))
    c_to = int(p.get("to", 40))
    lag = int(p.get("playback_lag_s", 180))
    snap_mode = str(p.get("snapshot_mode", "playback")).lower()   # 'playback' | 'live'
    total = max(1, c_to - c_from + 1)
    host, port, user, pw = nvr
    zoned = _zoned_channels(zones_path)
    work = Path(workdir) / "survey"
    work.mkdir(parents=True, exist_ok=True)
    log(f"survey start: gw={gw_id} ch {c_from}..{c_to} snapshot_mode={snap_mode}")

    def post(ch, params, content=None):
        h = dict(headers)
        if content is not None:
            h["Content-Type"] = "image/jpeg"
        try:
            client.post(f"{cloud}/api/gw/{gw_id}/survey/{ch}", params=params,
                        content=content, headers=h, timeout=30)
            return True
        except Exception as e:
            log(f"survey ch{ch} post failed: {e}")
            return False

    ok = err = 0
    for i, ch in enumerate(range(c_from, c_to + 1)):
        report(client, jid, "running", 2 + int(96 * i / total),
               f"ch {ch}/{c_to} (ok {ok}, err {err})")
        out = work / f"ch{ch:02d}.jpg"
        out.unlink(missing_ok=True)
        url = mode = None
        got = False
        if snap_mode == "live":                       # opt-in; may not honour channel
            url = f"rtsp://{user}:{pw}@{host}:{port}/cam/realmonitor?channel={ch}&subtype=1"
            mode = "live"
            got = _grab_frame(url, out, 20)
        if not got:                                    # field-proven per-channel path
            s = datetime.now() - timedelta(seconds=lag)
            e = s + timedelta(seconds=3)
            url = playback_url(ch, s.isoformat(), e.isoformat())
            mode = "playback"
            got = _grab_frame(url, out, 30)
        log(f"survey ch{ch}: mode={mode} got={got} url={_mask(url)}")   # TRIPWIRE
        hz = 1 if ch in zoned else 0
        if got:
            if post(ch, {"has_zones": hz, "mode": mode}, out.read_bytes()):
                ok += 1
            else:
                err += 1
            out.unlink(missing_ok=True)
        else:
            err += 1
            post(ch, {"error": f"no frame ({mode} failed)"})
    report(client, jid, "done", 100, f"survey complete: {ok} ok, {err} errored of {total}")
