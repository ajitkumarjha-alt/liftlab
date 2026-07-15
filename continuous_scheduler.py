#!/usr/bin/env python3
"""Continuous single-cabin door-cycle scheduler — Tier-1 live metrics (v1=A).

Runs as a SUBPROCESS under the B4 venv (numpy/av/cv2/liftlab), launched by the
import-light agent via watch_manager.py — the SAME out-of-process shape
analyze_local uses (PYTHONPATH=B4_DIR, cwd=B4_DIR, job as JSON on STDIN).

TOKEN BOUNDARY (matches analyze_local exactly): the gateway token is STRIPPED from
this child's env. So this child NEVER posts. It EMITS newline-delimited JSON on
STDOUT — {"kind":"events"|"status"|"validation"} — and the PARENT agent (which
owns the Bearer credential) does every POST. Human logs/tracebacks go to STDERR
(captured to a logfile); STDOUT is the structured channel ONLY. If the parent dies,
STDOUT breaks and the child stops (auto-reap).

v1 = door cycles only. NO onnxruntime/ultralytics/occupancy — cheap openness/diff
signal. The certified door-CLOSE time is NOT sourced here (exported-file full-fps
path). This gives door-cycle RHYTHM, headway, stop counts, idle, hourly profile.

NO double-count: a cycle emits only when COMPLETE (detect_cycles needs both fall
edges) AND SETTLED (close_full >= SETTLE_S behind newest), de-duped by close_full.
ANCHORING: live wall-clock; capture->compute latency bounded+logged. BASELINE
seeded once from a quiet doors-shut grab + an OSD-intact validation frame the
operator must eyeball (baseline_confirmed stays False until action:confirm).
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import numpy as np

# --- approved reuse map: the proven pipeline internals, imported not forked ---
from liftlab.doors import (_crop_gray, _normalize_openness, detect_cycles,
                           DoorCycle, MOTION_FLOOR)
from liftlab.stitch import (StitchedTimeline, cycle_wall_times,
                            OPEN_TRAVEL_MIN, OPEN_TRAVEL_MAX,
                            CLOSE_TRAVEL_MIN, CLOSE_TRAVEL_MAX, DATA_HOLE_S)

# --- tunables (Q1/Q2/Q3 as confirmed) ---
SIGNAL_HZ = 10.0            # Q2: pinned at 10 Hz
SIGNAL_FPS_ALARM = 6.0      # Q2: alarm if achieved rate drifts under ~6
WINDOW_S = 300.0           # Q3: 5-min rolling detection window
SETTLE_S = 3.0             # Q3: close_full must be this far behind newest before emit
DEDUP_TOL_S = 1.0          # Q3: cycles within this close_full distance are the same
DOWNSCALE = 4              # matches doors.py default ROI downscale
DETECT_EVERY_S = 1.0       # run detection at most this often
BASELINE_ALPHA = 0.02      # slow EMA refresh from confirmed-closed frames only
LATENCY_ALARM_S = 1.0      # freshness alarm threshold
STALL_S = 5.0             # capture considered stalled if freshest older than this
SEED_FRAMES = 40          # ~a few seconds of live frames to seed the baseline
SEED_QUIET_SPREAD = MOTION_FLOOR   # seed rejected if raw spread exceeds this (moving/active)
SEG_PAD_S = 6.0           # samples kept each side of a committed segment for re-measure
SEED_RETRY_S = 120.0      # if no persisted baseline and the cabin is busy: retry seed this often

_REGISTRY: dict[int, "_Runner"] = {}
_REG_LOCK = threading.Lock()
_STDOUT_BROKEN = threading.Event()   # set if the parent (stdout reader) went away
_OUT_LOCK = threading.Lock()
_NDJSON = None                       # private fd to the parent; set by main() only


# =========================================================================
# child->parent channel: NDJSON on a PRIVATE stdout fd. The child NEVER posts.
# =========================================================================
def _isolate_ndjson_channel():
    """Make the parent's pipe a channel ONLY _emit_out can write. Dup the real
    stdout (fd 1 -> the pipe) aside, then point fd 1 AND sys.stdout at stderr, so
    NO native library (PyAV/ffmpeg, OpenCV) and no stray print() can corrupt the
    NDJSON the parent parses as events over a multi-hour run. Called by main() only
    (never at import), so the smoke-import is unaffected."""
    global _NDJSON
    import os
    real = os.dup(1)             # the pipe to the parent
    os.dup2(2, 1)                # fd 1 -> stderr: native/stray fd-1 writes hit the log
    sys.stdout = sys.stderr      # Python-level prints also go to stderr
    _NDJSON = os.fdopen(real, "w", buffering=1)


def _emit_out(obj):
    line = json.dumps(obj)
    ch = _NDJSON if _NDJSON is not None else sys.stdout   # fallback for tests
    with _OUT_LOCK:
        try:
            ch.write(line + "\n")
            ch.flush()
        except (BrokenPipeError, ValueError, OSError):
            _STDOUT_BROKEN.set()   # parent gone -> stop the run (auto-reap)


def _log_err(msg):
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# =========================================================================
# helpers
# =========================================================================
def _live_url(nvr, channel):
    host, port, user, pw = nvr
    import onvif_resolve
    cm = onvif_resolve.resolve_map(host, user, pw, cache_path=f"/tmp/onvif_map_ch{channel}.json")
    raw = onvif_resolve.uri_for(cm, int(channel), prefer=1)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(user, safe='')}:{quote(pw, safe='')}@", 1)


def _resolve_roi(zones_path, channel):
    """door_roi for this channel from ZONES_PATH (B4_DIR/camera_zones.json — same
    source analyze_local uses). Returns (camera, roi) or (camera, None) if
    uncalibrated (caller surfaces needs_calibration)."""
    try:
        z = json.loads(Path(zones_path).read_text())
    except Exception:
        return f"ch{int(channel):02d}", None
    for key in (f"ch{int(channel):02d}", f"ch{channel}", str(channel), f"channel_{channel}"):
        node = z.get(key)
        if isinstance(node, dict) and node.get("door_roi"):
            return key, tuple(node["door_roi"])
    # channel_map indirection: {channel_map: {"29": "cam-name"}, "cam-name": {door_roi}}
    cmap = z.get("channel_map", {})
    cam = cmap.get(str(channel)) or cmap.get(f"ch{int(channel):02d}")
    if cam and isinstance(z.get(cam), dict) and z[cam].get("door_roi"):
        return cam, tuple(z[cam]["door_roi"])
    return f"ch{int(channel):02d}", None


def _event_row(tl, c):
    """Production event schema. Includes the DERIVED close_travel_s (and
    open_travel_s), computed exactly like app.py:211 — the /events renderer reads
    the close_travel_s COLUMN (ev.get('close_travel_s') at ingest), so omitting the
    key stores NULL and renders '—'. This is THE number the project measures.
    Occupancy stays None — v1 has no detector."""
    wt = cycle_wall_times(tl, c)
    return {
        "door_open_start_ts": wt["door_open_start"].isoformat(),
        "door_open_full_ts": wt["door_open_full"].isoformat(),
        "door_close_start_ts": wt["door_close_start"].isoformat(),
        "door_close_full_ts": wt["door_close_full"].isoformat(),
        "open_travel_s": round(c.open_travel_s, 3),
        "close_travel_s": round(c.close_travel_s, 3),
        "plateau": round(c.plateau, 4),
        "ramp_residual": round(c.residual, 4),
        "floor": None, "floor_source": "unknown",
        "boarded": None, "alighted": None,
    }


def _quality_ok(c: DoorCycle) -> bool:
    return (OPEN_TRAVEL_MIN < c.open_travel_s < OPEN_TRAVEL_MAX
            and c.transfer_s >= 0
            and CLOSE_TRAVEL_MIN < c.close_travel_s < CLOSE_TRAVEL_MAX)


def _straddles_hole(c: DoorCycle, holes) -> bool:
    for h0, h1 in holes:
        if c.open_start_s < h1 and h0 < c.close_full_s:
            return True
    return False


def _overlaps(a0, a1, b0, b1):
    """Do time intervals [a0,a1] and [b0,b1] overlap? Commitment matches on this,
    not on an exact edge value — a re-emit's edges drift by seconds across window
    slides but its [open,close] still overlaps the committed interval, while a
    genuinely distinct opening (a real reopen after the close) does not."""
    return a0 < b1 and b0 < a1


# =========================================================================
# frame hub — the latest-frame-drop hand-off
# =========================================================================
class _FrameHub:
    """Holds only the FRESHEST (crop, capture_ts). Any unconsumed prior is
    discarded — that discard is the drop. Fed by the capture thread (or, in
    tests, directly)."""
    def __init__(self):
        self._lock = threading.Lock()
        self._crop = None
        self._ts = 0.0
        self._seq = 0

    def put(self, crop, ts):
        with self._lock:
            self._crop, self._ts, self._seq = crop, ts, self._seq + 1

    def latest(self):
        with self._lock:
            return self._crop, self._ts, self._seq


# =========================================================================
# the runner
# =========================================================================
class _Runner:
    def __init__(self, channel, roi, camera, *, gw_id="site-A",
                 nvr=None, url=None, log=None, cloud=None, headers=None):
        self.channel = int(channel)
        self.roi = roi
        self.camera = camera
        self.gw_id = gw_id
        self.nvr = nvr
        self.url = url                    # optional pre-resolved / injected URL
        self._log = log or (lambda *a, **k: None)
        self.stop_event = threading.Event()
        self.hub = _FrameHub()
        self.state = "seeding"
        self.error = None
        # Q1 seed provenance: one OSD-intact validation frame + eyeball flag
        self._want_full = threading.Event()
        self._seed_full = None            # one full BGR frame stashed during seed
        self.baseline_confirmed = False   # operator must eyeball doors-shut
        self.validation_fn = None
        self.confirmed_at = None          # when the baseline was operator-confirmed (persisted)
        self.baseline_source = None       # "persisted" | "live-seed"
        self._pending_confirm = False     # set by SIGUSR1; applied+persisted in the run loop
        import os as _os
        self.run_dir = Path(_os.environ.get("WATCH_RUN_DIR", "/home/askjitk/liftlab-watch"))
        self.baseline_path = self.run_dir / f"baseline_ch{self.channel}.npz"
        # signal buffers (absolute offsets from run start — never re-based)
        self.t0 = None
        self.start_wall = None
        self.offs = deque()               # seconds from t0
        self.raws = deque()               # raw gray-level distance
        self.baseline = None
        self.closed_floor = 0.0
        # commitment bookkeeping — freeze by [open,close] INTERVAL (drift-robust)
        self.committed = []               # committed (open_start_s, close_full_s) intervals
        self.cycles = []                  # committed cycle summaries (for rollups)
        # health
        self.samples = 0
        self.lat_recent = deque(maxlen=200)
        self.lat_max = 0.0
        self.last_detect = 0.0
        self.alarms = []
        self._cap_thread = None
        self._run_thread = None

    # ---- lifecycle ----
    def start(self):
        self._run_thread = threading.Thread(target=self._run, name=f"watch-ch{self.channel}", daemon=True)
        self._run_thread.start()

    def stop(self, timeout=8.0):
        self.stop_event.set()
        if self._run_thread:
            self._run_thread.join(timeout)
        self.state = "stopped"
        return self.status()

    def _health(self):
        """SoC temp, LIVE vs STICKY throttle (bits 0-3 now / 16-19 since-boot), free
        mem, and the child's own RSS (memory-growth watch). All for the soak gate."""
        import subprocess
        h = {}
        try:
            raw = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=3).strip().split("=")[1]
            val = int(raw, 16)
            h["throttled_hex"] = raw
            h["throttle_live"] = [n for b, n in {0: "undervolt", 1: "freqcap", 2: "throttled", 3: "templimit"}.items() if val & (1 << b)]
            h["throttle_sticky"] = [n for b, n in {16: "undervolt", 17: "freqcap", 18: "throttled", 19: "templimit"}.items() if val & (1 << b)]
        except Exception:
            h["throttled_hex"] = None
        try:
            h["soc_temp"] = float(subprocess.check_output(["vcgencmd", "measure_temp"], text=True, timeout=3).split("=")[1].split("'")[0])
        except Exception:
            pass
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    h["mem_avail_mb"] = int(line.split()[1]) // 1024
                    break
        except Exception:
            pass
        try:
            for line in open("/proc/self/status"):
                if line.startswith("VmRSS:"):
                    h["rss_mb"] = int(line.split()[1]) // 1024
                    break
        except Exception:
            pass
        return h

    def status(self):
        el = (time.time() - self.t0) if self.t0 else 0.0
        return {
            "channel": self.channel, "camera": self.camera, "state": self.state,
            "baseline_confirmed": self.baseline_confirmed, "validation_frame": self.validation_fn,
            "baseline_source": self.baseline_source, "confirmed_at": self.confirmed_at,
            "error": self.error, "elapsed_s": round(el, 1), "samples": self.samples,
            "signal_fps": round(self.samples / el, 2) if el > 1 else None,
            "latency_med_s": round(float(np.median(self.lat_recent)), 3) if self.lat_recent else None,
            "latency_max_s": round(self.lat_max, 3),
            "cycles_emitted": len(self.cycles), "alarms": self.alarms[-5:],
            # last few full cycle timings so CORRECTNESS is eyeball-able off the Pi
            # status file (not just the tier1 aggregates): check close_travel_s ~2-2.5s,
            # dwell_s sane — a stable loop emitting nonsense cycles fails differently
            # than a drifting one, and health metrics won't show it.
            "recent_cycles": self.cycles[-5:],
            "tier1": self.rollups(), "health": self._health(),
        }

    # ---- capture thread (real live stream) ----
    def _capture_loop(self):
        import av
        x, y, w, h = self.roi
        while not self.stop_event.is_set():
            try:
                cont = av.open(self.url, options={"rtsp_transport": "tcp", "stimeout": "5000000"})
                s = next(v for v in cont.streams if v.type == "video")
                s.thread_type = "AUTO"
                for frame in cont.decode(s):
                    if self.stop_event.is_set():
                        break
                    try:
                        crop = _crop_gray(frame, x, y, w, h, DOWNSCALE)
                    except Exception:
                        continue
                    self.hub.put(crop, time.time())
                    # stash ONE full OSD-intact frame for the seed validation proof
                    if self._want_full.is_set() and self._seed_full is None:
                        try:
                            self._seed_full = frame.to_ndarray(format="bgr24")
                        except Exception:
                            pass
                cont.close()
            except Exception as e:
                self.error = f"capture: {type(e).__name__}: {str(e)[:80]}"
                self._alarm(self.error)
                if self.stop_event.wait(2.0):
                    break  # backoff, then reopen

    # ---- seed (Q1: clean, checkable, live, quiet) ----
    def _seed_baseline(self):
        """Collect SEED_FRAMES fresh crops, verify the window is QUIET (doors not
        moving), set baseline=median, closed_floor=median distance. If not quiet,
        FAIL and surface — never guess a baseline from a busy window.

        The motion-quiet gate proves doors-not-MOVING, but a stationary OPEN door
        is also quiet and would pass while seeding the OPEN appearance as 'closed'
        (self-cancel that passes the check). So we also emit ONE OSD-intact
        validation frame from the seed window and leave baseline_confirmed=False:
        the operator must EYEBALL doors-shut at /validation/<gw> before trusting it."""
        self._want_full.set()             # ask capture to stash one full frame
        crops, deadline = [], time.time() + 20.0
        last_seq = -1
        while len(crops) < SEED_FRAMES and time.time() < deadline and not self.stop_event.is_set():
            crop, ts, seq = self.hub.latest()
            if crop is not None and seq != last_seq:
                crops.append(crop); last_seq = seq
            time.sleep(1.0 / SIGNAL_HZ)
        if len(crops) < max(8, SEED_FRAMES // 2):
            return False, f"seed: only {len(crops)} frames (capture not delivering)"
        stack = np.stack(crops)
        baseline = np.median(stack, axis=0)
        raw = np.abs(stack - baseline).mean(axis=(1, 2))
        spread = float(raw.max() - raw.min())
        if spread > SEED_QUIET_SPREAD:
            return False, (f"seed NOT quiet (raw spread {spread:.1f} > {SEED_QUIET_SPREAD} gray levels) "
                           f"— doors were moving/active; retry during a visibly doors-shut still moment")
        self.baseline = baseline.astype(np.float32)
        self.closed_floor = float(np.median(raw))
        vmsg = self._emit_seed_frame()
        return True, (f"seed motion-quiet (spread {spread:.1f}, closed_floor {self.closed_floor:.1f}, "
                      f"{len(crops)} frames). {vmsg} baseline_confirmed=False — EYEBALL doors-shut at "
                      f"/validation/{self.gw_id} (a stationary OPEN door passes the motion gate).")

    def _emit_seed_frame(self):
        """Encode the stashed full frame as JPEG and EMIT it (base64) for the
        PARENT to POST to the validation viewer. The child does not post."""
        self._want_full.clear()
        deadline = time.time() + 3.0
        while self._seed_full is None and time.time() < deadline:
            time.sleep(0.1)
        if self._seed_full is None:
            return "(no validation frame captured;"
        try:
            import cv2
            img = self._seed_full
            h, w = img.shape[:2]
            if w > 720:
                img = cv2.resize(img, (720, max(1, int(h * 720 / w))), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img)
            self._seed_full = None
            if not ok:
                return "(validation encode failed;"
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            self.validation_fn = f"ch{self.channel:02d}_{stamp}.jpg"
            _emit_out({"kind": "validation", "channel": self.channel,
                       "jpg_b64": base64.b64encode(buf.tobytes()).decode(),
                       "requested_start": stamp, "mode": "watch-seed"})
            return f"validation frame emitted to parent -> /validation/{self.gw_id};"
        except Exception as e:
            self._seed_full = None
            return f"(validation emit failed: {type(e).__name__};"

    # ---- durable baseline: reuse a CONFIRMED baseline across restarts (option b) ----
    def _save_baseline(self):
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            np.savez(str(self.baseline_path), baseline=self.baseline,
                     closed_floor=np.float32(self.closed_floor),
                     roi=np.asarray(self.roi, dtype=np.int64),
                     confirmed_at=np.float64(self.confirmed_at or time.time()))
            self._log(f"[watch ch{self.channel}] confirmed baseline persisted -> {self.baseline_path.name}")
        except Exception as e:
            self._log(f"[watch ch{self.channel}] baseline persist failed: {type(e).__name__}: {e}")

    def _load_baseline(self):
        """Reuse a previously-CONFIRMED baseline so a restart (incl. a reboot at peak)
        needs no self-seed. Reused ONLY if the ROI still matches (camera unmoved).
        baseline_confirmed carries its original confirmed_at provenance."""
        try:
            if not self.baseline_path.exists():
                return False
            d = np.load(str(self.baseline_path), allow_pickle=False)
            if tuple(int(x) for x in d["roi"]) != tuple(int(x) for x in self.roi):
                self._log(f"[watch ch{self.channel}] persisted baseline ROI != current — will re-seed")
                return False
            self.baseline = d["baseline"].astype(np.float32)
            self.closed_floor = float(d["closed_floor"])
            self.confirmed_at = float(d["confirmed_at"])
            self.baseline_confirmed = True
            self.baseline_source = "persisted"
            return True
        except Exception as e:
            self._log(f"[watch ch{self.channel}] baseline load failed: {type(e).__name__}: {e}")
            return False

    # ---- per-sample ingest (also the unit-test entry point) ----
    def step(self, crop, ts):
        if self.t0 is None:
            self.t0 = ts
            self.start_wall = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        raw = float(np.abs(crop - self.baseline).mean())
        # refresh baseline ONLY from confirmed-closed frames (tracks lighting drift,
        # never absorbs an open door)
        if raw < self.closed_floor + MOTION_FLOOR:
            self.baseline = ((1 - BASELINE_ALPHA) * self.baseline + BASELINE_ALPHA * crop).astype(np.float32)
        off = ts - self.t0
        self.offs.append(off)
        self.raws.append(raw)
        self.samples += 1
        # trim to window
        while self.offs and (off - self.offs[0]) > WINDOW_S:
            self.offs.popleft(); self.raws.popleft()

    def _timeline(self):
        offs = np.asarray(self.offs, dtype=np.float64)
        sig = _normalize_openness(np.asarray(self.raws, dtype=np.float32))
        dh = np.diff(offs)
        holes = [(float(offs[i]), float(offs[i + 1])) for i in np.where(dh > DATA_HOLE_S)[0]]
        return StitchedTimeline(offs, sig, self.start_wall, holes, []), offs

    def _seg_remeasure(self, off_lo, off_hi):
        """Re-detect the cycle in [off_lo, off_hi] with SEGMENT-LOCAL normalization:
        the STABLE closed_floor as the 0-level (NOT the segment/window median, which
        self-cancels on an open-dominated slice) and this segment's own plateau as the
        1-level. Deterministic and window-independent — this is what kills the ~6s
        close disagreement. Returns a DoorCycle (offsets absolute-from-t0) or None."""
        offs = np.asarray(self.offs, dtype=np.float64)
        raws = np.asarray(self.raws, dtype=np.float32)
        m = (offs >= off_lo) & (offs <= off_hi)
        if int(m.sum()) < 8:
            return None
        so, sr = offs[m], raws[m]
        lo = float(self.closed_floor)
        hi = float(np.percentile(sr, 98))
        if hi - lo < MOTION_FLOOR:
            return None
        sig = np.clip((sr - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)
        cycles = detect_cycles(sig, StitchedTimeline(so, sig, self.start_wall, [], []))
        return max(cycles, key=lambda c: c.plateau) if cycles else None

    def maybe_detect(self, now_off):
        """Detect opens on the window, RE-MEASURE each settled one segment-locally for a
        deterministic close, and COMMIT it as an [open,close] INTERVAL. A candidate whose
        interval OVERLAPS an already-committed interval is the same physical opening
        re-detected — edges drift by seconds across slides (rise_lo/extrapolation
        cascade) but the intervals still overlap — so it is skipped. Interval-overlap is
        robust to that drift where an exact open-edge value key was not. One physical
        opening -> one committed cycle. Returns the newly emitted rows."""
        if len(self.raws) < 8:
            return []
        tl, offs = self._timeline()
        raws = np.asarray(self.raws, dtype=np.float32)
        newest = float(offs[-1])
        new_rows = []
        for c in detect_cycles(tl.signal, tl):   # DETECTION only; the window VALUE isn't trusted
            # cheap pre-check: the (drifty) window interval already overlaps a committed one
            if any(_overlaps(c.open_start_s, c.close_full_s, io, ic) for io, ic in self.committed):
                continue
            if newest - c.close_full_s < SETTLE_S:
                continue                                       # close not settled yet
            if not _quality_ok(c) or _straddles_hole(c, tl.holes):
                continue
            # the door must have RETURNED TO CLOSED after the close (not a mid-wobble
            # dip): post-close samples near the closed floor for SETTLE_S, else wait.
            post = raws[(offs > c.close_full_s) & (offs <= c.close_full_s + SETTLE_S)]
            if len(post) >= 3 and float(np.median(post)) > self.closed_floor + MOTION_FLOOR:
                continue
            # re-measure segment-locally -> deterministic close, then commit the INTERVAL
            commit = self._seg_remeasure(c.open_start_s - SEG_PAD_S, c.close_full_s + SEG_PAD_S)
            if commit is None or not _quality_ok(commit):
                commit = c                                     # fall back to the window cycle
            if any(_overlaps(commit.open_start_s, commit.close_full_s, io, ic) for io, ic in self.committed):
                continue                                       # stable interval already committed
            self.committed.append((commit.open_start_s, commit.close_full_s))
            row = _event_row(tl, commit)                       # tl.wall_of maps absolute offsets
            self.cycles.append({
                "open_full_ts": row["door_open_full_ts"],
                "close_full_ts": row["door_close_full_ts"],
                "dwell_s": round(commit.dwell_s, 3), "open_travel_s": round(commit.open_travel_s, 3),
                "close_travel_s": round(commit.close_travel_s, 3), "transfer_s": round(commit.transfer_s, 3),
            })
            new_rows.append(row)
        # prune committed intervals whose close slid fully out of the window
        cutoff = newest - WINDOW_S
        self.committed = [(io, ic) for io, ic in self.committed if ic >= cutoff]
        return new_rows

    # ---- Tier-1 rollups from emitted cycles ----
    def rollups(self):
        cy = self.cycles
        opens = [datetime.fromisoformat(c["open_full_ts"]) for c in cy]
        headways = [round((opens[i] - opens[i - 1]).total_seconds(), 1) for i in range(1, len(opens))]
        # idle periods = inter-open gaps beyond 3x median headway (or >120s)
        idle_thr = max(120.0, 3 * float(np.median(headways))) if headways else 120.0
        idle = [g for g in headways if g > idle_thr]
        hourly = {}
        for o in opens:
            hourly[o.strftime("%Y-%m-%dT%H")] = hourly.get(o.strftime("%Y-%m-%dT%H"), 0) + 1
        dwells = [c["dwell_s"] for c in cy]
        return {
            "stop_count": len(cy),
            "headway_median_s": round(float(np.median(headways)), 1) if headways else None,
            "headway_p90_s": round(float(np.percentile(headways, 90)), 1) if headways else None,
            "dwell_median_s": round(float(np.median(dwells)), 2) if dwells else None,
            "idle_periods": len(idle), "idle_longest_s": round(max(idle), 1) if idle else 0,
            "hourly_profile": hourly,
        }

    def _alarm(self, msg):
        stamp = f"{round((time.time()-self.t0),1) if self.t0 else 0}s: {msg}"
        self.alarms.append(stamp)
        self._log(f"[watch ch{self.channel}] ALARM {msg}")

    def _emit(self, rows):
        """Emit derived events for the PARENT to Bearer-POST. NO imagery, NO token.
        The payload is EXACTLY analyze_local's 7-key shape, so it rides the existing,
        proven /api/gw/events ingest with zero schema change. tier1/baseline_confirmed
        travel in status() (read off the Pi), never in this POST."""
        if not rows:
            return
        payload = {
            "gateway_id": self.gw_id, "camera": self.camera,
            "declared_tz": str(self.start_wall.tzinfo) if self.start_wall else "local",
            "tz_source": "live-wallclock", "start_ts": self.start_wall.isoformat(),
            "events": rows, "door_signal": [],
        }
        _emit_out({"kind": "events", "payload": payload})
        self._log(f"[watch ch{self.channel}] emitted {len(rows)} cycle(s) to parent")

    def _status_post(self):
        st = self.status()
        _emit_out({"kind": "status", "status": st})
        import os
        sf = os.environ.get("WATCH_STATUS_FILE")
        if sf:
            try:
                Path(sf).write_text(json.dumps(st))
            except Exception:
                pass

    # ---- the runner thread / foreground loop ----
    def _run(self):
        # resolve URL if not injected
        if not self.url and self.nvr:
            try:
                self.url = _live_url(self.nvr, self.channel)
            except Exception as e:
                self.state = "failed"; self.error = f"onvif resolve: {type(e).__name__}: {str(e)[:80]}"
                self._log(f"[watch ch{self.channel}] {self.error}"); self._status_post(); self._deregister(); return
        if not self.url:
            self.state = "failed"; self.error = "no live URL (ONVIF resolve failed)"
            self._log(f"[watch ch{self.channel}] {self.error}"); self._status_post(); self._deregister(); return
        if not self.roi:
            self.state = "failed"; self.error = "needs_calibration (no door_roi)"
            self._log(f"[watch ch{self.channel}] {self.error}"); self._status_post(); self._deregister(); return

        self._cap_thread = threading.Thread(target=self._capture_loop, name=f"cap-ch{self.channel}", daemon=True)
        self._cap_thread.start()

        # (b) reuse a persisted CONFIRMED baseline if present (unless a re-seed is forced),
        # so a restart/reboot at PEAK does not need a doors-shut moment to self-seed.
        import os as _os
        reseed = _os.environ.get("WATCH_RESEED") == "1"
        if not reseed and self._load_baseline():
            from datetime import datetime as _dt
            when = _dt.fromtimestamp(self.confirmed_at).strftime("%Y-%m-%d %H:%M") if self.confirmed_at else "?"
            self.state = "running"
            self._log(f"[watch ch{self.channel}] reusing persisted baseline (confirmed {when}); no re-seed")
        else:
            # (a) live seed, RETRY-until-quiet instead of dying on a busy cabin
            self.baseline_source = "live-seed"
            while not self.stop_event.is_set():
                ok, msg = self._seed_baseline()
                self._log(f"[watch ch{self.channel}] {msg}")
                if ok:
                    self.state = "running"
                    break
                self.state = "seeding-retry"; self.error = msg
                self._status_post()             # ALIVE + retrying (not dead) — visible on /pihealth
                if self.stop_event.wait(SEED_RETRY_S):
                    break
            if self.state != "running":
                self._deregister(); return
        self.error = None
        self._status_post()
        period = 1.0 / SIGNAL_HZ
        next_t = time.time()
        last_seq = -1
        last_status = time.time()
        while not self.stop_event.is_set() and not _STDOUT_BROKEN.is_set():
            now = time.time()
            if self._pending_confirm:                          # operator confirmed (SIGUSR1)
                self._pending_confirm = False
                self.baseline_confirmed = True
                self.confirmed_at = time.time()
                self._save_baseline()                          # survives restarts (option b)
                self._log(f"[watch ch{self.channel}] baseline CONFIRMED + persisted")
            crop, ts, seq = self.hub.latest()
            age = now - ts if ts else 999
            if ts and seq != last_seq:
                lat = now - ts
                self.lat_recent.append(lat); self.lat_max = max(self.lat_max, lat)
                if lat > LATENCY_ALARM_S:
                    self._alarm(f"latency {lat:.2f}s > {LATENCY_ALARM_S}s (stale frame)")
                self.step(crop, ts)
                last_seq = seq
            elif age > STALL_S:
                self._alarm(f"capture stalled (freshest {age:.1f}s old)")
            if self.t0 and (now - self.last_detect) >= DETECT_EVERY_S:
                self.last_detect = now
                rows = self.maybe_detect(self.offs[-1] if self.offs else 0.0)
                if rows:
                    self._emit(rows)
            if now - last_status >= 15.0:
                el = now - self.t0 if self.t0 else 1
                fps = self.samples / el if el > 0 else 0
                if el > 30 and fps < SIGNAL_FPS_ALARM:
                    self._alarm(f"signal fps {fps:.1f} < {SIGNAL_FPS_ALARM}")
                self._status_post(); last_status = now
            next_t += period
            sleep = next_t - time.time()
            if sleep > 0:
                self.stop_event.wait(sleep)
            else:
                next_t = time.time()
        if _STDOUT_BROKEN.is_set():
            self._log(f"[watch ch{self.channel}] parent stdout closed — stopping (auto-reap)")
        self.state = "stopped"
        self._status_post()
        self._deregister()

    def _deregister(self):
        with _REG_LOCK:
            if _REGISTRY.get(self.channel) is self:
                _REGISTRY.pop(self.channel, None)


# =========================================================================
# in-process handler (kept for tests / non-agent callers; the agent uses the
# subprocess path via watch_manager). NON-BLOCKING start.
# =========================================================================
def run_watch(job, *, report=None, log=None, gw_id=None, zones_path=None, nvr=None, **_):
    log = log or (lambda *a, **k: None)
    report = report or (lambda *a, **k: None)
    p = job.get("params") if isinstance(job.get("params"), dict) else {}
    action = (p.get("action") or job.get("action") or "start").lower()
    channel = int(p.get("channel", job.get("channel", 29)))

    if action == "confirm":
        with _REG_LOCK:
            r = _REGISTRY.get(channel)
        if not r:
            res = {"status": "not_running", "channel": channel}; report(job, res); return res
        r.baseline_confirmed = True
        res = {"status": "confirmed", "channel": channel}; report(job, res); return res

    if action == "stop":
        with _REG_LOCK:
            r = _REGISTRY.get(channel)
        if not r:
            res = {"status": "not_running", "channel": channel}; report(job, res); return res
        res = {"status": "stopped", "channel": channel, "final": r.stop()}; report(job, res); return res

    with _REG_LOCK:
        if channel in _REGISTRY:
            res = {"status": "already_running", "channel": channel}; report(job, res); return res
        camera, roi = _resolve_roi(zones_path, channel)
        r = _Runner(channel, roi, camera, gw_id=gw_id, nvr=nvr,
                    url=(p.get("url") or job.get("url")), log=log)
        _REGISTRY[channel] = r
    r.start()
    res = {"status": "starting", "channel": channel, "camera": camera, "calibrated": bool(roi)}
    report(job, res)
    return res


# =========================================================================
# standalone entry point — run as a SUBPROCESS under the B4 python.
# Job arrives as JSON on STDIN (matches analyze_local's runner contract).
# STDOUT = NDJSON to the parent; STDERR = human logs. SIGTERM stops; SIGUSR1
# confirms the baseline (operator eyeballed doors-shut).
# =========================================================================
def _runner_from_job(job):
    import os
    ns = job.get("nvr_settings") or {}
    nvr = (ns.get("host") or os.environ.get("NVR_HOST", ""),
           str(ns.get("port") or os.environ.get("NVR_PORT", "80")),
           ns.get("user") or os.environ.get("NVR_USER", ""),
           ns.get("password") or os.environ.get("NVR_PASS", ""))
    zones = job.get("zones_path") or os.environ.get("ZONES_PATH", "")
    channel = int(job.get("channel", 29))
    camera, roi = _resolve_roi(zones, channel)
    return channel, _Runner(channel, roi, camera, gw_id=job.get("gateway_id", "site-A"),
                            nvr=nvr, url=job.get("url") or None, log=_log_err)


def main():
    import os
    import signal
    _isolate_ndjson_channel()   # FIRST: fd 1 becomes private NDJSON; all else -> stderr
    # belt-and-suspenders: quiet the native decoders so the logfile isn't spammed
    try:
        import av
        av.logging.set_level(av.logging.ERROR)
    except Exception:
        pass
    try:
        import cv2
        cv2.setLogLevel(0)   # SILENT
    except Exception:
        pass
    raw = sys.stdin.read()
    try:
        job = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _emit_out({"kind": "status", "status": {"state": "failed", "error": f"bad job json: {e}"}})
        return
    channel, r = _runner_from_job(job)
    with _REG_LOCK:
        _REGISTRY[channel] = r
    signal.signal(signal.SIGTERM, lambda *_: r.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: r.stop_event.set())
    try:
        def _confirm(*_):
            r._pending_confirm = True     # minimal handler; run loop confirms + persists
        signal.signal(signal.SIGUSR1, _confirm)
    except (AttributeError, ValueError):
        pass  # SIGUSR1 not on this platform
    _log_err(f"[watch ch{channel}] scheduler process starting (pid {os.getpid()})")
    r._run()  # foreground; returns when stop_event set or parent stdout closes
    _emit_out({"kind": "status", "status": r.status()})


if __name__ == "__main__":
    main()
