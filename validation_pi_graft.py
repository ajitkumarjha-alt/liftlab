#!/usr/bin/env python3
"""Add keep_validation_frame to pull_playback: when the job param is set, extract
EXACTLY ONE decoded frame (OSD intact) from the pulled clip BEFORE upload/delete
and upload it to the cloud validation viewer. Off by default (privacy); on only
for commissioning proofs. Anchored, idempotent, backup-first.

(analyze_local's validation frame follows once the historical mechanism is fixed —
its NVR pull currently shares the same falsified grammar, so its frame would just
prove 'live' too.)"""
import pathlib
import shutil
import time

p = pathlib.Path("/home/askjitk/liftlab-b3/pi-agent/agent.py")
s = p.read_text()
if "_keep_validation_frame" in s:
    print("agent already has keep_validation_frame — no change")
    raise SystemExit

helpers = '''def _upload_validation(client, ch, reqstart, jpg_path, mode):
    try:
        client.post(f"{CLOUD}/api/gw/{GW_ID}/validation/{ch}",
                    params={"requested_start": reqstart, "mode": mode},
                    content=Path(jpg_path).read_bytes(),
                    headers={**H(), "Content-Type": "image/jpeg"}, timeout=30)
        log(f"validation frame ch{ch} req_start={reqstart} uploaded (view /validation/{GW_ID})")
    except Exception as e:
        log(f"validation upload failed ch{ch}: {e}")


def _keep_validation_frame(client, ch, reqstart, clip, mode):
    """Extract one OSD-intact frame from a just-pulled clip and upload it."""
    vf = WORKDIR / f"val_ch{int(ch):02d}.jpg"
    try:
        subprocess.run(f"ffmpeg -hide_banner -loglevel error -y -i {shlex.quote(str(clip))} "
                       f"-frames:v 1 -vf scale=720:-1 {shlex.quote(str(vf))}",
                       shell=True, timeout=30)
        if vf.exists() and vf.stat().st_size > 1000:
            _upload_validation(client, ch, reqstart, vf, mode)
        else:
            log(f"validation extract produced no frame ch{ch}")
    except Exception as e:
        log(f"validation extract failed ch{ch}: {e}")
    finally:
        vf.unlink(missing_ok=True)


def pull_playback(client, job) -> None:'''

anchor_helpers = "def pull_playback(client, job) -> None:"
anchor_hook = '    report(client, jid, "running", 40, f"uploading {out.stat().st_size >> 20} MB")'
hook_new = ('    if p.get("keep_validation_frame"):\n'
            '        _keep_validation_frame(client, p["channel"], p["start"], out, "pull")\n'
            + anchor_hook)

for a in (anchor_helpers, anchor_hook):
    if a not in s:
        print(f"ANCHOR NOT FOUND: {a[:50]!r} — aborting, no write")
        raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
s = s.replace(anchor_helpers, helpers, 1).replace(anchor_hook, hook_new, 1)
p.write_text(s)
print("agent patched: pull_playback keep_validation_frame added; backup written")
