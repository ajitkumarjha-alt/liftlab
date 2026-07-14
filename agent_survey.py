"""
On-Pi channel survey (commissioning) — imported LAZILY by the agent for the
'survey' job type, so the agent stays import-light.

Channel selection: this NVR ignores ?channel=N on constructed RTSP and returns a
single default camera under EVERY grammar we tried (proven by grammar_probe).
ONVIF is authoritative — it hands us path-based per-channel URIs
(rtsp://host:554/<channel>/<stream>?...). So the survey resolves the channel->URI
map via ONVIF (cached per gateway) and grabs one ~480px snapshot per channel from
the sub-stream. Per-channel errors are recorded (non-fatal); a per-channel TRIPWIRE
logs mode + masked URL so a wrong-camera regression is visible in journalctl.
"""
import json
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import onvif_resolve


def _mask(url: str) -> str:
    m = re.match(r"^(\w+://)([^/]*)(/.*)?$", url or "")
    if not m:
        return url or ""
    scheme, authority, rest = m.group(1), m.group(2), m.group(3) or ""
    if "@" in authority:
        authority = "***:***@" + authority.rsplit("@", 1)[1]
    return scheme + authority + rest


def _inject_creds(uri: str, user: str, pw: str) -> str:
    if not uri or urlparse(uri).username:
        return uri
    return uri.replace("rtsp://", f"rtsp://{quote(user, safe='')}:{quote(pw, safe='')}@", 1)


def _grab_frame(url: str, out: Path, timeout: int) -> bool:
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
    total = max(1, c_to - c_from + 1)
    host, port, user, pw = nvr
    zoned = _zoned_channels(zones_path)
    work = Path(workdir) / "survey"
    work.mkdir(parents=True, exist_ok=True)

    # Resolve per-channel URIs via ONVIF (path-based selection; cached per gateway).
    cache = Path(workdir) / f"onvif_map_{gw_id}.json"
    try:
        cmap = onvif_resolve.resolve_map(host, user, pw, cache_path=str(cache),
                                         refresh=bool(p.get("refresh_onvif", False)))
        log(f"survey onvif: {len(cmap)} channels resolved (cache {cache})")
    except Exception as e:
        report(client, jid, "failed", 0, f"ONVIF resolve failed: {type(e).__name__}: {e}")
        return

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
        raw = onvif_resolve.uri_for(cmap, ch, prefer=2)     # sub-stream for thumbnails
        if not raw:
            err += 1
            log(f"survey ch{ch}: no ONVIF URI")
            post(ch, {"error": "no ONVIF URI for channel"})
            continue
        url = _inject_creds(raw, user, pw)
        got = _grab_frame(url, out, 20)
        log(f"survey ch{ch}: mode=onvif got={got} url={_mask(url)}")   # TRIPWIRE
        hz = 1 if ch in zoned else 0
        if got:
            if post(ch, {"has_zones": hz, "mode": "onvif"}, out.read_bytes()):
                ok += 1
            else:
                err += 1
            out.unlink(missing_ok=True)
        else:
            err += 1
            post(ch, {"error": "no frame (onvif uri)"})
    report(client, jid, "done", 100, f"survey complete: {ok} ok, {err} errored of {total}")
