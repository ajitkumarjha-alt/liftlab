#!/usr/bin/env python3
"""GPU transit analyzer (liftlab-gpu, L4). Pulls ch29 SUB segments from liftlab-cloud over HTTPS
with a Bearer token (NO gcloud/GCS — the box has no SA scopes), decodes HEVC, runs YOLO11n+ByteTrack,
counts landing<->cabin transits (the thing the Pi's 2fps couldn't do), and POSTs boarded/alighted
back to the cloud. Idempotent + resumable so it survives Spot PREEMPTION cleanly:
  - live ephemeral stream: on restart, rejoin the live edge (old segments are already gone).
  - durable state lives in the cloud (deduped transit_event + gw_event), not here.
  - each transit POST is idempotent (cloud dedups) -> no double counts, no half-written rows.

Env: CLOUD_URL, GW, CAM, ANALYSIS_TOKEN, MODEL (yolo11n.pt), STATE_DIR (systemd StateDirectory),
     CONF, DEVICE (cuda), POLL_S.  Zones: desk-rig 1920x1080, PER-AXIS scaled to the sub frame
     (verified 2026-07-16: sub is a full-frame resample, per-axis transfers).
"""
import io
import os
import sys
import time
import urllib.request

import numpy as np

import counting

CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
TOKEN = os.environ["ANALYSIS_TOKEN"]
MODEL = os.environ.get("MODEL", "yolo11n.pt")
CONF = float(os.environ.get("CONF", "0.35"))
DEVICE = os.environ.get("DEVICE", "cuda")
POLL_S = float(os.environ.get("POLL_S", "1.0"))
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/liftlab-gpu")
CALIB_W, CALIB_H = 1920, 1080
ZONE_CABIN = [[630, 870], [932, 747], [1042, 733], [1308, 1056], [587, 1056], [548, 914]]
ZONE_LANDING = [[514, 394], [834, 322], [722, 529], [732, 684], [761, 776], [618, 827]]
HDRS = {"Authorization": "Bearer " + TOKEN}
BASE = f"{CLOUD}/api/gw/{GW}/live/{CAM}"


def log(m):
    print(f"[gpu-analyze] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}", flush=True)


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, obj, timeout=10):
    import json
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={**HDRS, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def playlist_segments():
    """Segment filenames currently in the cloud's live playlist (ordered)."""
    try:
        m = http_get(f"{BASE}/index.m3u8").decode("utf-8", "ignore")
    except Exception as e:
        log(f"playlist fetch failed: {e}")
        return []
    return [ln.strip() for ln in m.splitlines() if ln.strip().endswith(".ts")]


def scale_zone(poly, sx, sy):
    return [[x * sx, y * sy] for x, y in poly]


USE_NVDEC = os.environ.get("USE_NVDEC", "0") == "1"
_NVDEC_WH = {}


def _decode_nvdec(data):
    """NVDEC via ffmpeg hevc_cuvid (keeps HEVC decode off the 4 vCPU — the ByteTrack cap). Needs the
    frame dims, probed once via PyAV header. Returns BGR frames, or None to fall back to CPU."""
    import subprocess
    import av
    wh = _NVDEC_WH.get("wh")
    if wh is None:
        try:
            c = av.open(io.BytesIO(data)); vs = c.streams.video[0]; wh = (vs.width, vs.height); c.close()
            _NVDEC_WH["wh"] = wh
        except Exception:
            return None
    W, H = wh
    try:
        p = subprocess.run(["ffmpeg", "-hwaccel", "cuda", "-c:v", "hevc_cuvid", "-i", "pipe:0",
                            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                           input=data, capture_output=True, timeout=20)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    fsz = W * H * 3
    buf = p.stdout
    return [np.frombuffer(buf[i:i + fsz], np.uint8).reshape(H, W, 3) for i in range(0, len(buf) - fsz + 1, fsz)]


def decode_segment(data):
    """HEVC segment bytes -> list of BGR frames. NVDEC if USE_NVDEC=1 (L4 has hevc_cuvid), else CPU
    via PyAV (704x576 CPU decode is cheap; not the cap — ByteTrack CPU-assoc is)."""
    if USE_NVDEC:
        fr = _decode_nvdec(data)
        if fr is not None:
            return fr
        log("NVDEC decode unavailable/failed — CPU fallback")
    import av
    frames = []
    try:
        c = av.open(io.BytesIO(data))
        for f in c.decode(video=0):
            frames.append(f.to_ndarray(format="bgr24"))
        c.close()
    except Exception as e:
        log(f"decode failed: {e}")
    return frames


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    cursor_path = os.path.join(STATE_DIR, f"cursor_{CAM}")
    seen = set()
    if os.path.exists(cursor_path):
        try:
            seen = set(open(cursor_path).read().split())
        except Exception:
            pass
    log(f"start: {BASE}  model={MODEL} device={DEVICE}  (resume: {len(seen)} segs known)")

    det = counting.YoloDetector(weights=MODEL, conf=CONF, tracker="bytetrack.yaml", device=DEVICE)
    log(f"detector on device={DEVICE} — verify with nvidia-smi (non-zero GPU-Util = actually on the L4)")
    ctr = None                                   # ZoneCounter, built once we know the frame size
    posted = 0

    while True:
        segs = playlist_segments()
        new = [s for s in segs if s not in seen]
        if not new:
            time.sleep(POLL_S)
            continue
        for name in new:
            try:
                data = http_get(f"{BASE}/{name}")
            except Exception as e:
                log(f"segment {name} fetch failed: {e}")
                continue
            frames = decode_segment(data)
            if not frames:
                seen.add(name); continue
            if ctr is None:
                H, W = frames[0].shape[:2]
                sx, sy = W / CALIB_W, H / CALIB_H
                ctr = counting.ZoneCounter(scale_zone(ZONE_LANDING, sx, sy), scale_zone(ZONE_CABIN, sx, sy))
                log(f"frame {W}x{H} -> zones scaled sx={sx:.3f} sy={sy:.3f}")
            before = len(ctr.transits)
            seg_wall = time.time()                # approx wall time of this segment's arrival
            n_fr = len(frames)
            for i, fr in enumerate(frames):
                dets = det.track(fr)
                ctr.update(dets, offset_s=seg_wall - (n_fr - i) * 0.04)   # ~25fps back-stamp
            # POST any NEW transits (idempotent by cam+track_id+direction; cloud dedups)
            for t in ctr.transits[before:]:
                try:
                    http_post_json(f"{CLOUD}/api/gw/{GW}/transit",
                                   {"cam": CAM, "ts": t.offset_s, "direction": t.direction, "track_id": t.track_id})
                    posted += 1
                except Exception as e:
                    log(f"transit POST failed (will not double-count on retry): {e}")
            b, a = ctr.counts()
            if len(ctr.transits) > before:
                log(f"{name}: +{len(ctr.transits)-before} transits (cum boarded={b} alighted={a}, posted={posted})")
            seen.add(name)
        # persist cursor (rolling: keep the last ~40 seg names)
        try:
            with open(cursor_path, "w") as f:
                f.write(" ".join(sorted(seen)[-40:]))
        except Exception:
            pass
        # bound in-memory transit list (they're durable in the cloud now)
        if ctr and len(ctr.transits) > 2000:
            ctr.transits = ctr.transits[-500:]


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
