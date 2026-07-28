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
from concurrent.futures import TimeoutError as _FuturesTimeout

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
SEG_DUR_S = float(os.environ.get("SEG_DUR_S", "2"))
SEG_BUDGET_MS = SEG_DUR_S * 1000                 # real-time budget: one segment's worth of wall time
PREFETCH_N = int(os.environ.get("PREFETCH_N", "2"))   # fetch this many segments AHEAD while tracking (overlap)
# ANALYZE_FPS: subsample decoded frames before tracking. 0/unset = process EVERY frame (~25fps, current
# behavior). The 7-cam fleet needs ~10fps/cam to fit the GPU (see re-bench). CHANGING THIS CHANGES WHAT
# GETS COUNTED (tracking continuity / dwell) -> it is a COUNTING_VERSION bump; don't touch mid-validation.
ANALYZE_FPS = float(os.environ.get("ANALYZE_FPS", "0"))
CALIB_W, CALIB_H = 1920, 1080
# CH29'S polygons (desk-rig 2026-07-11, geometry-verified 2026-07-15). Built-in fallback for ch29
# ONLY — scaling one lift's polygons onto another camera's optics counts wrong (the ch16 undercount).
ZONE_CABIN = [[630, 870], [932, 747], [1042, 733], [1308, 1056], [587, 1056], [548, 914]]
ZONE_LANDING = [[514, 394], [834, 322], [722, 529], [732, 684], [761, 776], [618, 827]]
# Zones travel with the camera via the registry (roi.json), like door geometry (baa0ca7). The fleet
# sets ZONE_LANDING/ZONE_CABIN (JSON [[x,y],...]) + ZONE_FRAME ("w,h" the polygons were drawn at).
# No zones and not ch29 -> counting OFF, loudly; the door/floor pass is independent and unaffected.


def _parse_zone_env(s):
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return None
    ok = (isinstance(v, list) and len(v) >= 3
          and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in v))
    return [[float(x), float(y)] for x, y in v] if ok else None


_ZL_ENV = _parse_zone_env(os.environ.get("ZONE_LANDING", "") or "null")
_ZC_ENV = _parse_zone_env(os.environ.get("ZONE_CABIN", "") or "null")
try:
    ZONE_FRAME_W, ZONE_FRAME_H = [float(x) for x in os.environ.get("ZONE_FRAME", "").split(",")]
except ValueError:
    ZONE_FRAME_W, ZONE_FRAME_H = CALIB_W, CALIB_H
if _ZL_ENV and _ZC_ENV:
    ZONES_SOURCE = "registry"
elif CAM == "ch29":
    ZONES_SOURCE = "builtin-ch29"
else:
    ZONES_SOURCE = "none"
HDRS = {"Authorization": "Bearer " + TOKEN}
BASE = f"{CLOUD}/api/gw/{GW}/live/{CAM}"

# ---- GPU_DOOR: opt-in door/floor pass (gpu_door engine) emitting the SEPARATE gw_door_event stream.
# ADDITIVE — never touches counting/transit. Geometry is FRAME px from calibration (env for now; the
# zones store later). Panel1 needs its OWN cells (different in-panel x) for the agree-or-discard check;
# without them it runs single-panel (flagged). Every event is version-stamped (templates hash + geometry).
GPU_DOOR = os.environ.get("GPU_DOOR", "0") == "1"
DOOR_ROI_FRAME = os.environ.get("DOOR_ROI_FRAME", "")            # "x,y,w,h" leaf ROI
PANEL_ROIS = os.environ.get("PANEL_ROIS", "")                   # "x,y,w,h;x,y,w,h" panel0[;panel1]
DIGIT_CELLS = os.environ.get("DIGIT_CELLS", "")                 # within-panel px, panel0 (from door_calib --cells)
ARROW_CELL = os.environ.get("ARROW_CELL", "")
PANEL1_DIGIT_CELLS = os.environ.get("PANEL1_DIGIT_CELLS", "")   # panel1's OWN cells (needs a panel1 anchor read)
PANEL1_ARROW_CELL = os.environ.get("PANEL1_ARROW_CELL", "")
DOOR_MIN_SCORE = float(os.environ.get("DOOR_MIN_SCORE", "0.55"))
DOOR_BLANK_RANGE = int(os.environ.get("DOOR_BLANK_RANGE", "40"))
DOOR_SHIFT = int(os.environ.get("DOOR_SHIFT", "2"))            # rigid ±Npx cell-alignment search (jitter absorb)
DOOR_MARGIN = float(os.environ.get("DOOR_MARGIN", "0.05"))    # top-2 glyph gap < this -> ambiguous no_read
DOOR_BLANK_MIN = float(os.environ.get("DOOR_BLANK_MIN", "0.45"))   # blank floor for a DIM cell (not lit)
DOOR_SHIFT_FLOOR = float(os.environ.get("DOOR_SHIFT_FLOOR", "0.40"))  # reject a shift whose cells avg below this
DOOR_LIT_RANGE = float(os.environ.get("DOOR_LIT_RANGE", "120"))   # contrast >= this = LIT digit; blank must not delete it
DOOR_BLANK_STRONG = float(os.environ.get("DOOR_BLANK_STRONG", "0.90"))  # blank this high wins even in a lit cell (edge)...
DOOR_BLANK_LIT_MARGIN = float(os.environ.get("DOOR_BLANK_LIT_MARGIN", "0.15"))  # ...but only if it beats the glyph by this
DOOR_CONFUSE_BAND = float(os.environ.get("DOOR_CONFUSE_BAND", "0"))   # >0 = diff-region tiebreak for close pairs (3/5,8/6)
DOOR_DISC_MIN = float(os.environ.get("DOOR_DISC_MIN", "0.10"))    # min |diff-region projection| to act on
DOOR_STRIDE = max(1, int(os.environ.get("DOOR_STRIDE", "2")))   # run the door pass every Nth decoded frame
DOOR_HB_S = float(os.environ.get("DOOR_HB_S", "60"))           # emit a row at least this often (liveness)
FLOORCHECK_PER_HR = int(os.environ.get("FLOORCHECK_PER_HR", "30"))   # sampled reads+crop -> /floorcheck
FLOOR_ORDER = [s.strip() for s in os.environ.get("FLOOR_ORDER", "").split(",") if s.strip()]  # FloorTracker._idx
# Valid-floor whitelist. Defaults to FLOOR_ORDER (the ordered floor list IS the valid set); an
# explicit FLOOR_ALPHABET overrides. Referenced by build_door_engine — it was USED there without
# ever being defined (a latent NameError that would have crashed GPU_DOOR on the next deploy), and
# the engine call had lost its valid_floors= wiring entirely. Both fixed here.
FLOOR_ALPHABET = [s.strip() for s in os.environ.get("FLOOR_ALPHABET", "").split(",") if s.strip()] or FLOOR_ORDER
# DoorTracker time guards — the fix for implausible close_travel (0.08s sub-frame, 4641s gap-span).
DOOR_MAX_GAP_S = float(os.environ.get("DOOR_MAX_GAP_S", "15"))       # frame jump mid-cycle -> abandon
DOOR_MIN_CLOSE_S = float(os.environ.get("DOOR_MIN_CLOSE_S", "0.3"))  # close below this -> withheld
DOOR_MAX_CLOSE_S = float(os.environ.get("DOOR_MAX_CLOSE_S", "30"))   # close above this -> withheld
TEMPLATES_REFETCH_S = float(os.environ.get("TEMPLATES_REFETCH_S", "600"))  # re-pull npz; reload if hash changed
# --- liveness: progress-based, because "active (running)" told us nothing during the 5h44m hang ---
WD_STALL_S = float(os.environ.get("WD_STALL_S", "120"))    # no segment processed this long -> dump + exit
WD_GRACE_S = float(os.environ.get("WD_GRACE_S", "180"))    # cold start: model load + CUDA init
SOCK_TIMEOUT_S = float(os.environ.get("SOCK_TIMEOUT_S", "30"))   # default for sockets we don't own
FETCH_WAIT_S = float(os.environ.get("FETCH_WAIT_S", "60"))  # cap on waiting for a prefetch future
# OUTPUT-ATTESTING WATCHDOG (the Thu-18:00 lesson). The progress watchdog only proves the loop turns;
# it cannot see a counting path that has silently died while segments + door analysis keep flowing.
# So attest OUTPUT: if the lift is demonstrably IN USE (doors cycling) yet no transit has been posted
# for a long span, the YOLO/tracker path is wedged (a shared CUDA/tracker event returned [] forever on
# both workers at once, no exception) -> dump stacks and exit for the fleet to restart. Same shape as
# the relay's alive-but-not-delivering restart.
TRANSIT_STALL_S = float(os.environ.get("TRANSIT_STALL_S", "1800"))    # no transit for this long...
TRANSIT_STALL_OPENS = int(os.environ.get("TRANSIT_STALL_OPENS", "20"))  # ...while >= this many door opens = wedged
# A raising det.track (the OTHER wedge mode) must be VISIBLE and self-heal, not silently propagate or
# spin. Count consecutive failures; past this many, dump + exit.
TRACK_FAIL_MAX = int(os.environ.get("TRACK_FAIL_MAX", "50"))
# DISCONTINUITY GUARD (third instance of the pattern: relay ffmpeg, door pairing, now counting).
# Persistent tracker + counter state must NOT survive a segment-clock gap it cannot account for. The
# Thu-23 silence: the stream starved, segments arrived minutes apart, then recovered — and the
# ByteTrack/ZoneCounter state carried a corruption across that gap that survived full recovery, so
# transits stayed zero for 59h. On a wall-clock jump between processed segments larger than this,
# rebuild the tracker (fresh ByteTrack) and counter, and abandon any open episode — any crossing
# in-progress across a minutes-long gap is already meaningless. NORMAL operation (~2s cadence) never
# trips this, so it is behaviour-preserving and NOT a counting_version change.
SEG_GAP_RESET_S = float(os.environ.get("SEG_GAP_RESET_S", "30"))
TEMPLATES_URL = f"{CLOUD}/api/gw/{GW}/templates/{CAM}"


def log(m):
    print(f"[gpu-analyze] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}", flush=True)


# LIVENESS (see gpu_watchdog.py). harden_sockets MUST run before the Session below is built —
# setdefaulttimeout only affects sockets created after it. This is a floor under the libraries we
# don't call directly; our own HTTP calls all carry explicit timeouts already.
try:
    import gpu_watchdog as _wd
    _wd.harden_sockets(SOCK_TIMEOUT_S)
except Exception as _e:                       # never let liveness plumbing stop the analyzer
    _wd = None
    print(f"[gpu-analyze] watchdog unavailable: {_e}", flush=True)


def wd_phase(name):
    """Breadcrumb for the stall report — no-op if the watchdog module is missing."""
    if _wd is not None:
        _wd.phase(name)


# Pooled keep-alive session: urllib does a fresh TCP+TLS handshake PER segment (~3-4 RTTs of setup
# before a byte moves) — that is the 952ms fetch. requests.Session reuses the connection, so connect
# collapses to ~0 after the first. Degrade to urllib if requests is somehow absent (never crash the run).
try:
    import requests as _requests
    _SESSION = _requests.Session()
    _SESSION.headers.update({**HDRS, "Connection": "keep-alive"})   # ask to keep the socket warm
    # ONE shared session (module-level) with an explicit pool sized for the prefetch concurrency, so warm
    # connections are REUSED across segments AND across the prefetch threads (Session is thread-safe here).
    _adapter = _requests.adapters.HTTPAdapter(pool_connections=4,
                                              pool_maxsize=max(4, PREFETCH_N + 2), max_retries=0)
    _SESSION.mount("https://", _adapter)
    _SESSION.mount("http://", _adapter)
    _HAS_SESSION = True
except Exception:
    _SESSION = None
    _HAS_SESSION = False

_KA_PROBED = [False]


def http_get(url, timeout=15):
    return http_get_timed(url, timeout)[0]


def http_get_timed(url, timeout=15):
    """Fetch bytes + split the cost: (body, connect_ms, transfer_ms). connect_ms = time to response
    headers (TCP+TLS+TTFB) — with keep-alive it drops to ~0 on a reused connection, which is the whole
    point; transfer_ms = body read. Non-2xx -> urllib.error.HTTPError so the caller's 404 path is unchanged."""
    if _HAS_SESSION:
        t0 = time.time()
        r = _SESSION.get(url, timeout=timeout, stream=True)   # returns once headers are in
        t1 = time.time()
        if not _KA_PROBED[0]:   # PROVE client-vs-server ONCE: does the server keep the connection alive?
            _KA_PROBED[0] = True
            try:
                ver = getattr(getattr(r, "raw", None), "version", None)   # 11 = HTTP/1.1
            except Exception:
                ver = None
            log(f"keep-alive probe: server Connection={r.headers.get('Connection')!r} "
                f"Keep-Alive={r.headers.get('Keep-Alive')!r} http_ver={ver}. If connect stays ~200ms AND "
                f"this is 'close'/None, Caddy is closing the socket per request (server-side fix needed).")
        if r.status_code >= 400:
            code = r.status_code
            r.close()
            raise urllib.error.HTTPError(url, code, "http error", None, None)
        body = r.content                                       # read the body
        r.close()                                              # release the connection back to the pool for reuse
        t2 = time.time()
        return body, (t1 - t0) * 1000, (t2 - t1) * 1000
    t0 = time.time()
    req = urllib.request.Request(url, headers=HDRS)
    with urllib.request.urlopen(req, timeout=timeout) as r:   # no pooling: whole fetch counts as transfer
        body = r.read()
    return body, 0.0, (time.time() - t0) * 1000


SLOW_POST_MS = float(os.environ.get("SLOW_POST_MS", "2000"))   # log any POST slower than this


def http_post_json(url, obj, timeout=10, what=""):
    """POST with an explicit timeout AND a breadcrumb. `what` names the call in the watchdog's stall
    report, so a hang inside a POST identifies itself even before the traceback is read. A POST that
    merely runs SLOW (but returns) is logged too — that's the early warning the 07:50 hang never gave."""
    import json
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={**HDRS, "Content-Type": "application/json"}, method="POST")
    wd_phase(f"POST {what or url}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    finally:
        ms = (time.time() - t0) * 1000
        if ms > SLOW_POST_MS:
            log(f"SLOW POST {what or url}: {ms:.0f}ms (timeout={timeout}s)")
        wd_phase("loop")


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
    dc = ep.get("det_counts") or []
    det_max = max(dc) if dc else 0                          # most people YOLO saw in any single frame
    det_mean = sum(dc) / len(dc) if dc else 0.0
    n_ids = len(ep.get("ids") or ())                        # distinct tracks the tracker established
    cf = ep.get("confs") or []                              # confidences of the ACCEPTED detections (>= CONF)
    c_min = min(cf) if cf else 0.0
    c_mean = sum(cf) / len(cf) if cf else 0.0
    c_max = max(cf) if cf else 0.0
    log(f"episode attempt ({reason}): boarded={ep['b']} alighted={ep['a']} imgs={len(ep['imgs'])} "
        f"span={ep['ts_end'] - ep['ts_start']:.0f}s")
    # DETECTION AUDIT: the counted transits can only be as good as what YOLO+tracker saw. If a crowd of
    # five shows det_max=2, the people were never detected (occlusion); if det_max=5 but distinct_ids=2,
    # the tracker merged them. Either way it's a detection problem, not a guard-tuning one. The conf range
    # tells which lever: confs hugging CONF -> lowering CONF may recover them; confs high -> truly occluded.
    log(f"episode dets: per-frame max={det_max} mean={det_mean:.1f} over {len(dc)} frames; "
        f"distinct track_ids={n_ids}; conf min/mean/max={c_min:.2f}/{c_mean:.2f}/{c_max:.2f}")
    try:
        st = http_post_json(f"{CLOUD}/api/gw/{GW}/validation_item/{CAM}",
                            {"ts_start": ep["ts_start"], "ts_end": ep["ts_end"],
                             "machine_boarded": ep["b"], "machine_alighted": ep["a"], "images": ep["imgs"],
                             "det_max": det_max, "det_mean": round(det_mean, 1), "distinct_ids": n_ids,
                             "det_frames": len(dc), "conf_min": round(c_min, 2),
                             "conf_mean": round(c_mean, 2), "conf_max": round(c_max, 2),
                             "counting_version": counting.COUNTING_VERSION},  # verdict is valid only for this logic
                            what="validation_item")
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


def make_counter(W, H):
    """ZoneCounter with THIS camera's zones scaled to the decode size — or None when the camera has
    no zones (counting OFF beats counting with another lift's polygons; door pass unaffected)."""
    if ZONES_SOURCE == "none":
        return None
    if ZONES_SOURCE == "registry":
        zl, zc, fw, fh = _ZL_ENV, _ZC_ENV, ZONE_FRAME_W, ZONE_FRAME_H
    else:
        zl, zc, fw, fh = ZONE_LANDING, ZONE_CABIN, CALIB_W, CALIB_H
    sx, sy = W / fw, H / fh
    log(f"zones[{ZONES_SOURCE}] drawn {fw:.0f}x{fh:.0f} -> frame {W}x{H} (sx={sx:.3f} sy={sy:.3f})")
    return counting.ZoneCounter(scale_zone(zl, sx, sy), scale_zone(zc, sx, sy))


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


def _rel_times(pts, n, seg_dur):
    """Per-frame seconds relative to the segment start. Uses REAL container PTS (handles variable fps /
    irregular 19-82-frame segments) when every frame carries a monotonic PTS; else uniform over the
    nominal segment duration. Absolute time is anchored on wall-clock by the caller (seg_wall) — PTS is
    used ONLY for intra-segment spacing, so pulled-clip PTS absolute drift can't corrupt the timeline."""
    if n and all(p is not None for p in pts) and pts[-1] > pts[0]:
        p0 = pts[0]
        return [p - p0 for p in pts]
    return [i * (seg_dur / n) for i in range(n)] if n else []


def decode_segment(data):
    """HEVC segment bytes -> (BGR frames, per-frame relative-seconds). NVDEC if USE_NVDEC=1 (L4 has
    hevc_cuvid, but rawvideo loses PTS -> uniform), else CPU via PyAV (704x576 CPU decode is cheap)."""
    if USE_NVDEC:
        fr = _decode_nvdec(data)
        if fr is not None:
            return fr, _rel_times([None] * len(fr), len(fr), SEG_DUR_S)   # rawvideo: no PTS -> uniform
        log("NVDEC decode unavailable/failed — CPU fallback")
    import av
    frames, pts = [], []
    try:
        c = av.open(io.BytesIO(data))
        tb = c.streams.video[0].time_base
        for f in c.decode(video=0):
            frames.append(f.to_ndarray(format="bgr24"))
            pts.append(float(f.pts * tb) if (f.pts is not None and tb) else None)
        c.close()
    except Exception as e:
        log(f"decode failed: {e}")
    return frames, _rel_times(pts, len(frames), SEG_DUR_S)


def _parse_xywh_list(s):
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out


def _hash8(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def build_door_engine(prefetched_tpl=None):
    """Fetch templates (unless handed a set) + build the DoorFloorEngine from the calibrated geometry.
    Returns (engine, door_version) on success, else (None, reason). NEVER raises into the worker."""
    import gpu_door as gd
    if not (DOOR_ROI_FRAME and PANEL_ROIS and DIGIT_CELLS and ARROW_CELL):
        return None, "geometry missing (need DOOR_ROI_FRAME, PANEL_ROIS, DIGIT_CELLS, ARROW_CELL)"
    tpl = prefetched_tpl
    if tpl is None:
        try:
            tpl = gd.fetch_templates(TEMPLATES_URL, headers=HDRS)
        except Exception as e:
            return None, f"template fetch failed: {type(e).__name__}: {str(e)[:80]}"
    try:
        droi = tuple(int(v) for v in DOOR_ROI_FRAME.split(","))
        prois = _parse_xywh_list(PANEL_ROIS)
        panels = [(prois[0], _parse_xywh_list(DIGIT_CELLS), _parse_xywh_list(ARROW_CELL)[0])]
        if len(prois) >= 2 and PANEL1_DIGIT_CELLS and PANEL1_ARROW_CELL:
            panels.append((prois[1], _parse_xywh_list(PANEL1_DIGIT_CELLS), _parse_xywh_list(PANEL1_ARROW_CELL)[0]))
            mode = "2-panel agree-or-discard"
        else:
            mode = ("SINGLE-PANEL (no agree-or-discard) — set PANEL1_DIGIT_CELLS/PANEL1_ARROW_CELL "
                    "from a panel1 anchor read to enable the free confidence check")
        ft = gd.FloorTracker(floor_order=FLOOR_ORDER or None)
        dtr = gd.DoorTracker(max_gap_s=DOOR_MAX_GAP_S, min_close_s=DOOR_MIN_CLOSE_S,
                             max_close_s=DOOR_MAX_CLOSE_S)
        eng = gd.DoorFloorEngine(tpl, droi, panels, min_score=DOOR_MIN_SCORE, blank_range=DOOR_BLANK_RANGE,
                                 floor_tracker=ft, door_tracker=dtr, shift_search=DOOR_SHIFT, margin_min=DOOR_MARGIN,
                                 blank_min=DOOR_BLANK_MIN, shift_floor=DOOR_SHIFT_FLOOR,
                                 lit_range=DOOR_LIT_RANGE, blank_strong=DOOR_BLANK_STRONG,
                                 blank_lit_margin=DOOR_BLANK_LIT_MARGIN, confuse_band=DOOR_CONFUSE_BAND,
                                 disc_min=DOOR_DISC_MIN, valid_floors=(FLOOR_ALPHABET or None))
    except Exception as e:
        return None, f"geometry/engine error: {type(e).__name__}: {str(e)[:80]}"
    geom_sig = _hash8("|".join([DOOR_ROI_FRAME, PANEL_ROIS, DIGIT_CELLS, ARROW_CELL,
                                PANEL1_DIGIT_CELLS, PANEL1_ARROW_CELL]))
    version = f"{eng.hash[:8]}+{geom_sig}"          # templates hash + geometry hash = comparability boundary
    log(f"GPU_DOOR: {len(panels)} panel(s) [{mode}]; templates_hash={eng.hash[:12]}; door_version={version}")
    if FLOOR_ALPHABET:
        log(f"GPU_DOOR floor whitelist: {len(FLOOR_ALPHABET)} valid floors {FLOOR_ALPHABET[:6]}"
            f"{'...' if len(FLOOR_ALPHABET) > 6 else ''} — off-alphabet reads flagged, not counted")
    else:
        log("GPU_DOOR floor whitelist: NONE (set FLOOR_ORDER/FLOOR_ALPHABET to reject impossible floors)")
    log(f"GPU_DOOR time guards: max_gap={DOOR_MAX_GAP_S}s (abandon mid-cycle across a stream gap); "
        f"close_travel plausible [{DOOR_MIN_CLOSE_S},{DOOR_MAX_CLOSE_S}]s (else emitted null)")
    return eng, version


def post_door_event(rec, version, thash):
    payload = {"cam": CAM, "ts": rec["t"], "floor": rec["floor"], "direction": rec["direction"],
               "door_state": rec["door_state"], "openness": rec["openness"], "read_conf": rec["read_conf"],
               "panels_agreed": rec["panels_agreed"], "reason": rec["reason"], "candidates": rec.get("candidates"),
               "close_travel_s": rec.get("close_travel_s"), "door_version": version, "templates_hash": thash}
    try:
        http_post_json(f"{CLOUD}/api/gw/{GW}/door_event", payload, what="door_event")
    except Exception as e:
        log(f"door_event POST failed: {e}")


def post_floorcheck(rec, frame_bgr, panel0_roi, version):
    """Sampled read WITH the panel0 crop -> /floorcheck, so accuracy is eyeballable before Tier-2 trusts it."""
    import cv2

    import gpu_door as gd
    b64 = None
    try:
        ok, buf = cv2.imencode(".jpg", gd.crop(frame_bgr, panel0_roi))
        if ok:
            b64 = base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    payload = {"cam": CAM, "ts": rec["t"], "floor": rec["floor"], "direction": rec["direction"],
               "read_conf": rec["read_conf"], "panels_agreed": rec["panels_agreed"], "reason": rec["reason"],
               "door_version": version, "crop_jpeg_b64": b64}
    try:
        http_post_json(f"{CLOUD}/api/gw/{GW}/floorcheck", payload, what="floorcheck")
    except Exception as e:
        log(f"floorcheck POST failed: {e}")


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
    if ZONES_SOURCE == "none":
        log(f"ZONES: NONE for {CAM} — transit counting OFF (door pass unaffected). Draw zones and "
            f"save them into roi.json so the registry carries them; ch29's built-ins on another "
            f"camera's optics are the undercount, not a fallback.")
    else:
        log(f"ZONES: {ZONES_SOURCE}")
    posted = 0
    val_state = get_val_state()
    last_val_poll = time.time()
    episode = None                                # current door-open episode being validated
    recent = deque(maxlen=FRAME_BUF)              # ring of recent frames -> multi-frame capture
    recent_dets = deque(maxlen=FRAME_BUF)         # parallel ring: (n_dets, id_tuple, conf_tuple) per frame -> detection audit
    dropped = 0                                   # segments never processed (pruned/lag) — running count
    segments = 0                                  # segments decoded + processed
    last_transit_ts = 0.0
    last_transit_post_wall = time.time()          # wall clock of the last successful transit POST
    door_opens_since_transit = 0                   # door-open transitions observed since that POST
    door_prev_state = None                         # for edge-detecting door opens
    track_fail_streak = 0                          # consecutive det.track exceptions
    last_seg_wall = None                            # wall time of the last PROCESSED segment (gap guard)
    gap_resets = 0                                  # tracker/counter rebuilds on a discontinuity
    started = time.time()
    last_drop_log = time.time()
    last_idle_log = 0.0
    last_hb = 0.0
    # rejected-crossing telemetry: would-be crossings the guards killed, so disp_frac/min_frames can be
    # tuned from the real distribution. rej_hist buckets the achieved frac in 0.05 steps [0..0.35, then .35+].
    rej_disp = 0                                  # rejected: reached dest + dwelled, but moved < disp_frac
    rej_dwell = 0                                 # rejected: too few frames in dest (fast walk-through / lost track)
    rej_in = 0                                    # rejected would-be BOARDINGS (landing->cabin)
    rej_out = 0                                   # rejected would-be ALIGHTINGS (cabin->landing)
    rej_hist = [0] * 8                            # frac buckets: [0,.05)…[.30,.35) then [.35,∞)
    # per-segment processing time vs the real-time budget (SEG_BUDGET_MS). If total > budget sustained,
    # one L4 cannot keep pace with even one camera -> the drop counter climbs and the 7-cam plan breaks.
    # ARCHITECTURE GATE (can the GPU own door timing?): fetch/decode/track are split so the bottleneck
    # is attributable — at ~7% GPU util the cost is FETCH (cross-region pull + retention window), NOT
    # compute, and fetch is fixable (deeper cloud retention + prefetch) while compute is not. drop_frac
    # / drop_rate quantify how often a 2s segment is lost (mid-close = a silently wrong door event).
    proc_times = deque(maxlen=60)                 # rolling per-segment NON-OVERLAPPED wall ms (the throughput cost)
    fetch_times = deque(maxlen=60)                # rolling fetch DURATION ms (overlapped w/ track via prefetch)
    connect_times = deque(maxlen=60)              # rolling connect ms (TCP+TLS+TTFB) — ~0 with keep-alive = fixed
    transfer_times = deque(maxlen=60)             # rolling body-transfer ms
    decode_times = deque(maxlen=60)               # rolling HEVC decode ms
    track_times = deque(maxlen=60)                # rolling YOLO track ms         (the only true GPU-compute cost)
    last_timing_log = 0.0
    from concurrent.futures import ThreadPoolExecutor
    fetch_ex = ThreadPoolExecutor(max_workers=max(2, PREFETCH_N + 1), thread_name_prefix="prefetch")
    prefetched = {}                               # name -> Future(http_get_timed) — fetch N+1 while tracking N
    def _prefetch(nm):
        if nm not in prefetched:
            prefetched[nm] = fetch_ex.submit(http_get_timed, f"{BASE}/{nm}")
    log(f"fetch: {'pooled keep-alive (requests)' if _HAS_SESSION else 'urllib (NO pooling)'}, "
        f"prefetch={PREFETCH_N} ahead; analyze_fps={'all(~25)' if ANALYZE_FPS<=0 else ANALYZE_FPS}")
    if ANALYZE_FPS > 0:
        log(f"WARNING: ANALYZE_FPS={ANALYZE_FPS} subsamples frames -> changes what gets counted. "
            f"COUNTING_VERSION must reflect this (comparability boundary); current={counting.COUNTING_VERSION}")
    log(f"validation mode: {val_state}")
    log(f"output watchdog: restart if >= {TRANSIT_STALL_OPENS} door opens with no transit for "
        f"{TRANSIT_STALL_S:.0f}s (counting-path wedge); det.track self-heals after {TRACK_FAIL_MAX} fails")

    # GPU_DOOR state (all no-ops unless enabled). Separate stream; independent of counting.
    door_gd = None
    door_eng = None
    door_version = ""
    door_thash = ""
    door_prev_key = None
    door_last_emit = 0.0
    last_fc_ts = 0.0
    last_tpl_refetch = time.time()
    panel0_roi = None
    if GPU_DOOR:
        import gpu_door as door_gd
        door_eng, dv = build_door_engine()
        if door_eng is None:
            log(f"GPU_DOOR DISABLED: {dv}")
        else:
            door_version, door_thash = dv, door_eng.hash
            panel0_roi = _parse_xywh_list(PANEL_ROIS)[0]
            log(f"GPU_DOOR live: stride={DOOR_STRIDE} (~{25 // DOOR_STRIDE}fps), heartbeat<= {DOOR_HB_S}s, "
                f"floorcheck={FLOORCHECK_PER_HR}/hr, templates refetch {TEMPLATES_REFETCH_S:.0f}s")

    def _mean(d):
        return round(sum(d) / len(d), 1) if d else None

    def heartbeat():                              # so a DEAD worker is visible on /ops, not silent
        try:
            _tot = segments + dropped
            http_post_json(f"{CLOUD}/api/gw/{GW}/analyzer_status",
                           {"cam": CAM, "counting_version": counting.COUNTING_VERSION,
                            "zones": ZONES_SOURCE,   # registry | builtin-ch29 | none (counting OFF)
                            "uptime_s": time.time() - started, "segments": segments, "dropped": dropped,
                            "posted": posted, "last_transit_ts": last_transit_ts, "mode": val_state,
                            # output-attestation signal (self-flag): doors opening but no transit posting
                            # is the wedge; surfaced here so /ops shows it before the self-restart fires.
                            "door_opens_since_transit": door_opens_since_transit,
                            "s_since_transit_post": round(time.time() - last_transit_post_wall, 0),
                            "rej_disp": rej_disp, "rej_dwell": rej_dwell,
                            "rej_in": rej_in, "rej_out": rej_out,
                            "rej_hist": ",".join(str(x) for x in rej_hist),
                            "proc_ms": _mean(proc_times), "fetch_ms": _mean(fetch_times),
                            "connect_ms": _mean(connect_times), "transfer_ms": _mean(transfer_times),
                            "decode_ms": _mean(decode_times), "track_ms": _mean(track_times),
                            "seg_budget_ms": SEG_BUDGET_MS, "analyze_fps": ANALYZE_FPS or None,
                            # the two gate numbers (since process start): fraction of segments lost, and
                            # a per-hour drop rate. dropped mid-close = a lost/wrong door event.
                            "drop_frac": round(dropped / _tot, 4) if _tot else 0.0,
                            "drop_rate_hr": round(dropped / ((time.time() - started) / 3600.0), 2)
                                            if time.time() - started > 60 else None},
                           what="analyzer_status")
        except Exception as e:
            log(f"heartbeat POST failed: {e}")

    # ARM LAST: everything above (model load, CUDA init, template fetch, engine build) is startup,
    # and the grace window covers it. From here on, a segment must complete every WD_STALL_S or the
    # watchdog dumps every thread's stack and exits for systemd. Progress is measured as SEGMENTS
    # PROCESSED — not loop turns, not "process alive", both of which stayed true through the hang.
    if _wd is not None:
        _wd.arm(stall_secs=WD_STALL_S, grace=WD_GRACE_S)

    while True:
        wd_phase("playlist")
        segs = playlist_segments()
        new = [s for s in segs if s not in seen]
        if time.time() - last_hb > HEARTBEAT_S:   # heartbeat even when idle (no traffic != dead)
            heartbeat(); last_hb = time.time()
        # GPU_DOOR: re-pull the templates periodically; reload the engine ONLY if the content hash
        # changed, so a door_calib --build propagates to the GPU without a redeploy (content-hash cache).
        if door_eng is not None and time.time() - last_tpl_refetch > TEMPLATES_REFETCH_S:
            last_tpl_refetch = time.time()
            try:
                new_tpl = door_gd.fetch_templates(TEMPLATES_URL, headers=HDRS)
                if door_gd.templates_hash(new_tpl) != door_thash:
                    neweng, ndv = build_door_engine(prefetched_tpl=new_tpl)
                    if neweng is not None:
                        door_eng, door_version, door_thash = neweng, ndv, neweng.hash
                        door_prev_key = None      # force a fresh emit under the new version (boundary)
                        log(f"GPU_DOOR templates changed -> reloaded (version {ndv})")
            except Exception as e:
                log(f"templates refetch failed: {e}")
        if not new:
            if episode and time.time() - episode["ts_end"] > EPISODE_GAP_S:
                post_episode(episode, "gap"); episode = None
            # STARVATION IS NOT A HANG. The playlist fetch above returned, so the loop is turning and
            # the network works — there is simply nothing upstream to process. Refresh the watchdog:
            # restarting cannot conjure segments, and a restart-loop through a relay outage would bury
            # the real signal (upstream) under a fake one (worker). Log it so idleness stays VISIBLE
            # rather than silent — silence is what cost us 5h44m.
            if _wd is not None:
                _wd.progress("idle(no new segs)")
            if segs and time.time() - last_idle_log > 60:
                log(f"idle: playlist has {len(segs)} segs, none new (upstream not advancing?)")
                last_idle_log = time.time()
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
        for s in new[:PREFETCH_N]:                # PRIME the pipeline: kick the first fetches concurrently
            _prefetch(s)
        for idx, name in enumerate(new):
            nxt = idx + PREFETCH_N                 # keep the pipeline full: fetch PREFETCH_N ahead
            if nxt < len(new):
                _prefetch(new[nxt])
            seg_t0 = time.time()                  # measured AFTER prefetch: captures NON-OVERLAPPED work
            _prefetch(name)                        # (no-op if already prefetched)
            try:
                # BOUNDED wait. Future.result() with no timeout was the one genuinely unbounded
                # block left in the loop: http_get_timed carries its own 15s, but if a prefetch
                # thread died in a way that never resolved the future, the main loop waited
                # forever — silent, GPU idle, unit "active". Exactly the 07:50 signature.
                wd_phase(f"fetch-wait {name}")
                data, connect_ms, transfer_ms = prefetched.pop(name).result(timeout=FETCH_WAIT_S)
            except _FuturesTimeout:
                log(f"segment {name} prefetch WAIT EXCEEDED {FETCH_WAIT_S:.0f}s — abandoning (watchdog will "
                    f"dump+exit if this repeats); the fetch thread is wedged")
                seen.add(name); dropped += 1
                continue
            except urllib.error.HTTPError as e:
                if e.code == 404:                 # pruned off the rolling window before we fetched it
                    seen.add(name); dropped += 1
                    continue                       # do NOT retry old segs; move toward live
                log(f"segment {name} HTTP {e.code}"); continue
            except Exception as e:
                log(f"segment {name} fetch failed: {type(e).__name__}: {e}"); continue
            fetch_ms = connect_ms + transfer_ms   # fetch DURATION (ran overlapped w/ the prior track)
            dec_t0 = time.time()
            wd_phase(f"decode {name}")
            frames, rel = decode_segment(data)
            decode_ms = (time.time() - dec_t0) * 1000       # HEVC decode cost
            if not frames:
                seen.add(name); continue
            segments += 1
            track_ms = 0.0
            if ctr is None and ZONES_SOURCE != "none":
                H, W = frames[0].shape[:2]
                ctr = make_counter(W, H)
            seg_wall = time.time()                # approx wall time of this segment's arrival
            # DISCONTINUITY GUARD: a large jump since the last processed segment means the stream
            # starved/recovered. Rebuild the tracker + counter and drop any open episode so no
            # corruption carries across the gap (the Thu-23 durable-wedge fix).
            if last_seg_wall is not None and (seg_wall - last_seg_wall) > SEG_GAP_RESET_S:
                gap_resets += 1
                log(f"SEGMENT-CLOCK GAP {seg_wall - last_seg_wall:.0f}s (> {SEG_GAP_RESET_S:.0f}s) — "
                    f"rebuilding tracker + counter, dropping open episode (reset #{gap_resets}). "
                    f"State must not survive a discontinuity it cannot account for.")
                try:
                    det = counting.YoloDetector(weights=MODEL, conf=CONF, tracker="bytetrack.yaml", device=DEVICE)
                except Exception as e:
                    log(f"detector rebuild failed: {type(e).__name__}: {e} — keeping the old one")
                ctr = None                        # rebuilt below with the same zones
                episode = None                    # abandon (transits already posted independently)
                recent.clear(); recent_dets.clear()
                door_opens_since_transit = 0
            last_seg_wall = seg_wall
            if ctr is None and ZONES_SOURCE != "none":   # rebuild after a gap reset (or first segment)
                H, W = frames[0].shape[:2]
                ctr = make_counter(W, H)
            before = len(ctr.transits) if ctr else 0
            n_fr = len(frames)
            # ANALYZE_FPS subsampling: track every `stride`-th frame. 0/unset -> stride 1 (all frames,
            # current behavior). Cuts the dominant track cost ~proportionally to enable the 7-cam fleet.
            stride = 1
            if ANALYZE_FPS > 0 and n_fr > 0:
                stride = max(1, round((n_fr / SEG_DUR_S) / ANALYZE_FPS))
            for i, fr in enumerate(frames):
                # GPU_DOOR pass — its OWN cadence (DOOR_STRIDE), independent of the YOLO stride, so it
                # runs even when counting subsamples. Cheap (Sobel + small-cell NCC) vs YOLO. Emits the
                # gw_door_event stream on state-change (+ liveness heartbeat); samples N/hr to /floorcheck.
                if door_eng is not None and i % DOOR_STRIDE == 0:
                    d_off = seg_wall - ((rel[-1] - rel[i]) if rel else 0.0)
                    wd_phase(f"door-pass {name} fr{i}")
                    try:
                        drec = door_eng.process(fr, d_off)
                    except Exception as e:
                        drec = None
                        log(f"door process error: {type(e).__name__}: {str(e)[:80]}")
                    if drec is not None:
                        # OUTPUT-ATTEST: a door opening means the lift is in use, so a transit SHOULD
                        # follow. Count opens since the last posted transit; many opens with none posted
                        # = the counting path is wedged while door analysis (independent CV) runs on.
                        if drec.get("door_state") == "open" and door_prev_state != "open":
                            door_opens_since_transit += 1
                        door_prev_state = drec.get("door_state")
                        should, door_prev_key = door_gd.door_event_changed(door_prev_key, drec)
                        if should or (d_off - door_last_emit) >= DOOR_HB_S:
                            post_door_event(drec, door_version, door_thash)
                            door_last_emit = d_off
                        if FLOORCHECK_PER_HR > 0 and (d_off - last_fc_ts) >= (3600.0 / FLOORCHECK_PER_HR):
                            post_floorcheck(drec, fr, panel0_roi, door_version)
                            last_fc_ts = d_off
                if ctr is None:
                    continue                      # no zones for this camera — counting OFF (door pass above ran)
                if i % stride != 0:
                    continue                      # subsampled out (analyze_fps); keeps decode, skips track
                if val_state == "validating":
                    recent.append(fr)             # buffer frames so a transit can grab a sequence
                tr_t0 = time.time()
                wd_phase(f"track {name} fr{i}")
                try:
                    dets = det.track(fr)
                    track_fail_streak = 0
                except Exception as e:
                    # Do NOT swallow silently (the Thu-18:00 failure was invisible). Log, count, and if
                    # it keeps failing the tracker is wedged — dump + exit so the fleet restarts.
                    track_fail_streak += 1
                    log(f"det.track FAILED ({track_fail_streak}/{TRACK_FAIL_MAX}): {type(e).__name__}: {str(e)[:100]}")
                    if track_fail_streak >= TRACK_FAIL_MAX:
                        log(f"det.track failed {track_fail_streak}x consecutively — tracker wedged, exiting for restart")
                        import faulthandler as _fh
                        _fh.dump_traceback(all_threads=True)
                        os._exit(1)
                    dets = []
                track_ms += (time.time() - tr_t0) * 1000     # YOLO inference — the cost that must fit the budget
                frame_off = seg_wall - ((rel[-1] - rel[i]) if rel else 0.0)   # REAL per-frame time (not 25fps assumed)
                if val_state == "validating":                # DETECTION AUDIT: what did YOLO actually see?
                    ids = tuple(d.track_id for d in dets)
                    cfs = tuple(d.conf for d in dets)
                    recent_dets.append((len(dets), ids, cfs)) # run-up buffer -> seeds an episode opened later
                    if episode is not None:
                        episode["det_counts"].append(len(dets))
                        episode["ids"].update(ids)
                        episode["confs"].extend(cfs)
                pre = len(ctr.transits)
                pre_rej = len(ctr.rejections)
                ctr.update(dets, offset_s=frame_off)         # real per-frame wall time (see decode_segment rel)
                for rj in ctr.rejections[pre_rej:]:   # would-be crossings the guards killed
                    bi = min(int(rj.achieved_frac / 0.05 + 1e-9), 7)   # +eps: 0.35/0.05 is 6.999… in float
                    rej_hist[bi] += 1
                    if rj.reason == "displacement":
                        rej_disp += 1
                    else:
                        rej_dwell += 1
                    if rj.origin == "landing":    # landing->cabin would have been a BOARDING
                        rej_in += 1
                    else:
                        rej_out += 1
                    log(f"REJECT {rj.reason} tid={rj.track_id} {rj.origin}->{rj.dest} "
                        f"frac={rj.achieved_frac:.2f} dwell={rj.dwell_frames}")
                for t in ctr.transits[pre:]:      # transits detected ON this frame
                    try:                          # POST the count (both modes; idempotent, cloud dedups)
                        http_post_json(f"{CLOUD}/api/gw/{GW}/transit",
                                       {"cam": CAM, "ts": t.offset_s, "direction": t.direction, "track_id": t.track_id},
                                       what="transit")
                        posted += 1
                        last_transit_ts = t.offset_s
                        last_transit_post_wall = time.time()   # OUTPUT attested — reset the wedge counters
                        door_opens_since_transit = 0
                    except Exception as e:
                        log(f"transit POST failed (no double-count on retry): {e}")
                    # EPISODE = the door-open record, built in BOTH modes. VALIDATING -> attach imagery +
                    # the detection audit for review (cloud stores 'pending'). LIVE -> NO imagery, no audit
                    # (the counts are validated -> cloud stores 'auto'); a live camera never makes a review item.
                    now = t.offset_s
                    if episode and (now - episode["ts_end"] > EPISODE_GAP_S
                                    or now - episode["ts_start"] > EPISODE_MAX_S):
                        post_episode(episode, "gap/max"); episode = None
                    if episode is None:
                        _validating = val_state == "validating"
                        episode = {"ts_start": now, "ts_end": now, "b": 0, "a": 0, "imgs": [],
                                   # seed the detection audit from the run-up frames — validating only
                                   "det_counts": [n for n, _, _ in recent_dets] if _validating else [],
                                   "ids": set(i for _, idt, _ in recent_dets for i in idt) if _validating else set(),
                                   "confs": [c for _, _, cfs in recent_dets for c in cfs] if _validating else []}
                        log(f"episode opened at {now:.0f} ({val_state})")
                    episode["ts_end"] = now
                    episode["b" if t.direction == "in" else "a"] += 1
                    if val_state == "validating":   # imagery ONLY when validating (privacy + no review when live)
                        for j in capture_seq(recent, VAL_SEQ_PER_TRANSIT):
                            if len(episode["imgs"]) < VAL_MAX_IMGS:
                                episode["imgs"].append(j)
            if ctr is not None:
                b, a = ctr.counts()
                if len(ctr.transits) > before:
                    log(f"{name}: +{len(ctr.transits)-before} transits (cum boarded={b} alighted={a}, posted={posted})")
            seg_ms = (time.time() - seg_t0) * 1000          # NON-OVERLAPPED wall: max(fetch_wait, 0)+decode+track+post
            proc_times.append(seg_ms); track_times.append(track_ms)
            fetch_times.append(fetch_ms); decode_times.append(decode_ms)
            connect_times.append(connect_ms); transfer_times.append(transfer_ms)
            if time.time() - last_timing_log > 30 and proc_times:
                pm = sum(proc_times) / len(proc_times); tm = sum(track_times) / len(track_times)
                fm = sum(fetch_times) / len(fetch_times); dm = sum(decode_times) / len(decode_times)
                cm = sum(connect_times) / len(connect_times); xm = sum(transfer_times) / len(transfer_times)
                ratio = pm / SEG_BUDGET_MS
                _tot = segments + dropped
                dfrac = dropped / _tot if _tot else 0.0
                verdict = "OVER-BUDGET (cannot keep pace)" if ratio > 1.0 else "within budget"
                bound = "FETCH-bound" if fm > tm else "COMPUTE-bound (GPU)"
                # fetch split proves the fix: connect~0 => keep-alive working, cost is transfer; connect high => still handshaking
                log(f"seg timing: throughput={pm:.0f}ms (was fetch+track serial) [decode={dm:.0f} track={tm:.0f} n={n_fr}fr] "
                    f"vs budget={SEG_BUDGET_MS:.0f}ms -> {ratio:.2f}x {verdict}; {bound} "
                    f"fetch={fm:.0f}ms[connect={cm:.0f} transfer={xm:.0f}]; drop_frac={dfrac:.3%} dropped_total={dropped}")
                last_timing_log = time.time()
            seen.add(name)
            # THE liveness signal: one fully-processed segment (fetched, decoded, tracked, door-passed,
            # posted). Placed here and nowhere else on purpose — an idle poll, a 404 skip, or a loop
            # spinning without doing work must NOT look like health, or the watchdog re-learns the same
            # lie systemd told us.
            if _wd is not None:
                _wd.progress(name)
            wd_phase("loop")
            # OUTPUT-ATTESTING WEDGE CHECK. Streams are fresh (we just processed a segment) and the door
            # pass shows the lift IN USE, yet no transit has posted for TRANSIT_STALL_S across
            # >= TRANSIT_STALL_OPENS door opens -> the YOLO/tracker path is wedged (the Thu-18:00 mode:
            # returns [] forever, no exception, door analysis unaffected). Restart rather than run blind.
            if (door_opens_since_transit >= TRANSIT_STALL_OPENS
                    and time.time() - last_transit_post_wall >= TRANSIT_STALL_S):
                log(f"COUNTING WEDGED: {door_opens_since_transit} door opens and NO transit posted in "
                    f"{time.time() - last_transit_post_wall:.0f}s while segments flow — YOLO/tracker path "
                    f"is producing nothing. Dumping stacks and exiting for restart.")
                import faulthandler as _fh
                _fh.dump_traceback(all_threads=True)
                os._exit(1)
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
