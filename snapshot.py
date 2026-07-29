#!/usr/bin/env python3
"""Snapshot grid producer. For each cam receiving relay segments, decode ONE frame from the newest
segment (only when a NEW segment arrives) into SNAP_DIR/{gw}/{cam}.jpg. Runs as a SEPARATE, niced
process (liftlab-snap.service) — NOT inside uvicorn — so HEVC decode can't starve the event ingest.

- Reads segments from LIVE_DIR (/dev/shm/liftlab-live, the capped relay store).
- Writes JPEGs to SNAP_DIR (/run/liftlab-snap, RAM, ~40KB x cams, NOT the capped store).
- Decodes only on a new segment, sequentially -> ~one keyframe decode per cam per segment.
- A cam that stops arriving just stops getting new JPEGs; the JPEG mtime ages, and the page marks
  it STALE from that age (this process does nothing for a dead stream -> no silent frozen frame).
"""
import glob
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

LIVE = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
SNAP = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
PERIOD = float(os.environ.get("SNAP_PERIOD", "2.0"))
WIDTH = os.environ.get("SNAP_WIDTH", "480")
DB_PATH = os.environ.get("GATEWAY_DB", "")            # channel_map source of truth (best-effort)
# never snapshot these leftover/non-stream dirs (relay_diag scratch, verification images)
JUNK = re.compile(r"^chdiag|^_|zonecheck")


def allowed_cams(gw):
    """Set of ch{N} marked is_lift in channel_map, or None if the DB is unavailable (then fall
    back to the JUNK filter). Keeps the snapshotter off relay_diag scratch dirs etc."""
    if not DB_PATH or not os.path.exists(DB_PATH):
        return None
    try:
        db = sqlite3.connect(DB_PATH)
        rows = db.execute("SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1", (gw,)).fetchall()
        db.close()
        return {f"ch{int(r[0])}" for r in rows} if rows else None
    except Exception:
        return None


def newest_seg(camdir: Path):
    segs = glob.glob(str(camdir / "*.ts"))
    if not segs:
        return None
    return max(segs, key=os.path.getmtime)


def decode(seg: str, out: Path) -> bool:
    # temp must NOT end in .jpg: snapmeta globs *.jpg, so a lingering/mid-write temp named
    # <cam>.jpg.tmp.jpg shows up as a phantom camera tile. -f image2 tells ffmpeg the output
    # muxer explicitly, since the .tmp extension no longer implies jpeg.
    tmp = str(out) + ".tmp"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", seg, "-frames:v", "1",
             "-vf", "scale=" + WIDTH + ":-2", "-q:v", "6", "-f", "image2", tmp],
            timeout=10, stdin=subprocess.DEVNULL)
    except Exception:
        return False
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, out)      # atomic: the server never reads a half-written JPEG
        return True
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def main():
    SNAP.mkdir(parents=True, exist_ok=True)
    last = {}
    while True:
        t0 = time.time()
        for gwdir in sorted(LIVE.glob("*")):
            if not gwdir.is_dir():
                continue
            gw = gwdir.name
            (SNAP / gw).mkdir(parents=True, exist_ok=True)
            allowed = allowed_cams(gw)               # channel_map cams, or None -> JUNK filter only
            for camdir in sorted(gwdir.glob("*")):
                if not camdir.is_dir():
                    continue
                cam = camdir.name
                if JUNK.search(cam):
                    continue                         # chdiag*/zonecheck/_* -> never decode (was journal spam)
                if allowed is not None and cam not in allowed:
                    continue                         # not a marked lift cabin
                seg = newest_seg(camdir)
                if not seg:
                    continue
                key = gw + "/" + cam
                if last.get(key) == seg:
                    continue                       # no new segment since last decode
                if decode(seg, SNAP / gw / (cam + ".jpg")):
                    last[key] = seg
        time.sleep(max(0.2, PERIOD - (time.time() - t0)))


if __name__ == "__main__":
    main()
