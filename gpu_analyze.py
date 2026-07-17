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
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque

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
VAL_POLL_S = float(os.environ.get("VAL_POLL_S", "30"))     # how often to re-check this cam's mode
EPISODE_GAP_S = float(os.environ.get("EPISODE_GAP_S", "8"))  # transits >this apart = different opening
EPISODE_MAX_S = float(os.environ.get("EPISODE_MAX_S", "25"))  # force-close an episode this long (safety)
VAL_MAX_IMGS = int(os.environ.get("VAL_MAX_IMGS", "8"))    # per-episode image cap (room for a few transits)
VAL_SEQ_PER_TRANSIT = int(os.environ.get("VAL_SEQ_PER_TRANSIT", "3"))  # frames spanning EACH crossing
FRAME_BUF = int(os.environ.get("FRAME_BUF", "40"))        # ring of recent frames (~1.6s @25fps)
MAX_BEHIND = int(os.environ.get("MAX_BEHIND", "3"))        # if >this new segs queued, jump to live edge
HEARTBEAT_S = float(os.environ.get("HEARTBEAT_S", "30"))   # analyzer heartbeat to the cloud
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


def get_val_state():
    """This camera's mode from the cloud: 'validating' (snap + review) or 'live' (counts only)."""
    try:
        return json.loads(http_get(f"{CLOUD}/api/gw/{GW}/validation_mode/{CAM}").decode())["state"]
    except Exception:
        return "validating"                       # default: validate an unknown/new camera


def jpeg_b64(fr, width=480):
    """Downscaled JPEG of a validation frame (privacy: small, short-lived, deleted after verdict)."""
    import cv2
    h, w = fr.shape[:2]
    if w > width:
        fr = cv2.resize(fr, (width, int(h * width / w)))
    ok, buf = cv2.imencode(".jpg", fr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


def capture_seq(recent, k):
    """k frames evenly spanning the recent-frame ring (first..crossing) so MOVEMENT — hence
    DIRECTION — is visible. A single still can't distinguish boarding from alighting; a sequence
    can (person moving toward the cabin vs toward the landing)."""
    n = len(recent)
    if n == 0:
        return []
    idxs = list(range(n)) if n <= k else [round(x * (n - 1) / (k - 1)) for x in range(k)]
    out = []
    for i in idxs:
        j = jpeg_b64(recent[i])
        if j:
            out.append(j)
    return out


def post_episode(ep, reason=""):
    """Post a door-open episode for review. Logs the ATTEMPT and the RESULT so a silent failure
    (rejected POST, live-mode skip, exception) can't hide."""
    if not ep:
        return
    if ep["b"] + ep["a"] == 0:
        log(f"episode closed EMPTY (0 transits, {reason}) — not posting")
        return
    log(f"episode attempt ({reason}): boarded={ep['b']} alighted={ep['a']} imgs={len(ep['imgs'])} "
        f"span={ep['ts_end'] - ep['ts_start']:.0f}s")
    try:
        st = http_post_json(f"{CLOUD}/api/gw/{GW}/validation_item/{CAM}",
                            {"ts_start": ep["ts_start"], "ts_end": ep["ts_end"],
                             "machine_boarded": ep["b"], "machine_alighted": ep["a"], "images": ep["imgs"],
                             "counting_version": counting.COUNTING_VERSION})   # verdict is valid only for this logic
        log(f"episode POST -> HTTP {st}")
    except Exception as e:
        log(f"episode POST FAILED: {type(e).__name__}: {getattr(e, 'code', '')} {str(e)[:120]}")


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
    val_state = get_val_state()
    last_val_poll = time.time()
    episode = None                                # current door-open episode being validated
    recent = deque(maxlen=FRAME_BUF)              # ring of recent frames -> multi-frame capture
    dropped = 0                                   # segments never processed (pruned/lag) — running count
    segments = 0                                  # segments decoded + processed
    last_transit_ts = 0.0
    started = time.time()
    last_drop_log = time.time()
    last_hb = 0.0
    log(f"validation mode: {val_state}")

    def heartbeat():                              # so a DEAD worker is visible on /ops, not silent
        try:
            http_post_json(f"{CLOUD}/api/gw/{GW}/analyzer_status",
                           {"cam": CAM, "counting_version": counting.COUNTING_VERSION,
                            "uptime_s": time.time() - started, "segments": segments, "dropped": dropped,
                            "posted": posted, "last_transit_ts": last_transit_ts, "mode": val_state})
        except Exception as e:
            log(f"heartbeat POST failed: {e}")

    while True:
        segs = playlist_segments()
        new = [s for s in segs if s not in seen]
        if time.time() - last_hb > HEARTBEAT_S:   # heartbeat even when idle (no traffic != dead)
            heartbeat(); last_hb = time.time()
        if not new:
            if episode and time.time() - episode["ts_end"] > EPISODE_GAP_S:
                post_episode(episode, "gap"); episode = None
            time.sleep(POLL_S)
            continue
        # STAY NEAR LIVE: if we've fallen behind, skip the old queued segments (they're about to be
        # pruned -> would 404 anyway). lag = how many segs behind the newest playlist entry we are.
        lag = len(new)
        if lag > MAX_BEHIND:
            for s in new[:-MAX_BEHIND]:
                seen.add(s)
            dropped += lag - MAX_BEHIND
            new = new[-MAX_BEHIND:]
        if time.time() - last_drop_log > 60:
            log(f"segment lag={lag} behind live, dropped_total={dropped} (skipped-to-live + pruned)")
            last_drop_log = time.time()
        for name in new:
            try:
                data = http_get(f"{BASE}/{name}")
            except urllib.error.HTTPError as e:
                if e.code == 404:                 # pruned off the rolling window before we fetched it
                    seen.add(name); dropped += 1
                    continue                       # do NOT retry old segs; move toward live
                log(f"segment {name} HTTP {e.code}"); continue
            except Exception as e:
                log(f"segment {name} fetch failed: {type(e).__name__}: {e}"); continue
            frames = decode_segment(data)
            if not frames:
                seen.add(name); continue
            segments += 1
            if ctr is None:
                H, W = frames[0].shape[:2]
                sx, sy = W / CALIB_W, H / CALIB_H
                ctr = counting.ZoneCounter(scale_zone(ZONE_LANDING, sx, sy), scale_zone(ZONE_CABIN, sx, sy))
                log(f"frame {W}x{H} -> zones scaled sx={sx:.3f} sy={sy:.3f}")
            before = len(ctr.transits)
            seg_wall = time.time()                # approx wall time of this segment's arrival
            n_fr = len(frames)
            for i, fr in enumerate(frames):
                if val_state == "validating":
                    recent.append(fr)             # buffer frames so a transit can grab a sequence
                dets = det.track(fr)
                pre = len(ctr.transits)
                ctr.update(dets, offset_s=seg_wall - (n_fr - i) * 0.04)   # ~25fps back-stamp
                for t in ctr.transits[pre:]:      # transits detected ON this frame
                    try:                          # POST the count (both modes; idempotent, cloud dedups)
                        http_post_json(f"{CLOUD}/api/gw/{GW}/transit",
                                       {"cam": CAM, "ts": t.offset_s, "direction": t.direction, "track_id": t.track_id})
                        posted += 1
                        last_transit_ts = t.offset_s
                    except Exception as e:
                        log(f"transit POST failed (no double-count on retry): {e}")
                    if val_state == "validating":  # capture THIS frame into the door-open episode
                        now = t.offset_s
                        if episode and (now - episode["ts_end"] > EPISODE_GAP_S
                                        or now - episode["ts_start"] > EPISODE_MAX_S):
                            post_episode(episode, "gap/max"); episode = None
                        if episode is None:
                            episode = {"ts_start": now, "ts_end": now, "b": 0, "a": 0, "imgs": []}
                            log(f"episode opened at {now:.0f}")
                        episode["ts_end"] = now
                        episode["b" if t.direction == "in" else "a"] += 1
                        for j in capture_seq(recent, VAL_SEQ_PER_TRANSIT):   # sequence -> direction
                            if len(episode["imgs"]) < VAL_MAX_IMGS:
                                episode["imgs"].append(j)
            b, a = ctr.counts()
            if len(ctr.transits) > before:
                log(f"{name}: +{len(ctr.transits)-before} transits (cum boarded={b} alighted={a}, posted={posted})")
            seen.add(name)
        # close a stale validation episode (door shut) + refresh this cam's mode periodically
        if episode and time.time() - episode["ts_end"] > EPISODE_GAP_S:
            post_episode(episode, "gap-between-segments"); episode = None
        if time.time() - last_val_poll > VAL_POLL_S:
            ns = get_val_state()
            if ns != val_state:
                log(f"validation mode: {val_state} -> {ns}")
                if ns == "live" and episode:
                    post_episode(episode, "mode->live flush"); episode = None
            val_state = ns
            last_val_poll = time.time()
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
