#!/usr/bin/env python3
"""Continuous single-cabin door-cycle scheduler — Tier-1 live metrics.

Job: watch_channel {channel, action: start|stop}. v1 = A (door cycles only). NO
onnxruntime, NO ultralytics, NO occupancy — the cheap openness/diff signal only.
The certified door-CLOSE time is NOT sourced here; it stays on the exported-file
full-fps path. This run gives door-cycle RHYTHM, headway, stop counts, idle
periods and hourly profile at the live frame rate.

DESIGN (satisfies the two guards):
 * NON-BLOCKING start. run_watch(start) spawns a daemon runner thread and returns
   at once, so the agent's single-threaded poll loop keeps HEARTBEATING and can
   receive a later {action: stop}. The runner never touches the agent's shared
   httpx client; it does its own urllib POSTs.
 * NO double-count. A cycle emits only when COMPLETE (detect_cycles requires both
   fall edges) AND SETTLED (close_full is >= SETTLE_S behind the newest sample, so
   the post-close floor is fully sampled and close_full is stable), then de-duped
   by close_full within a tolerance. A trailing partial cannot emit then re-emit.

ANCHORING: live wall-clock. Each sample is stamped at capture time (time.time());
offsets are seconds from run start. Capture->compute latency is bounded and
LOGGED, so depicted time ~= capture time (container PTS is NOT trusted — see the
pulled-clip-PTS finding). BASELINE is seeded ONCE, clean and checkable, from a
short LIVE quiet (doors-shut) grab, then refreshed slowly from confirmed-closed
frames only — never re-established from a busy window.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
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

_REGISTRY: dict[int, "_Runner"] = {}
_REG_LOCK = threading.Lock()


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
    """door_roi for this channel from the survey zones. Returns (camera, roi) or
    (camera, None) if uncalibrated — the caller surfaces needs_calibration."""
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


def _post(url, payload, headers, timeout=10):
    try:
        data = json.dumps(payload).encode()
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _post_bytes(url, body, headers, ctype="image/jpeg", timeout=30):
    try:
        h = {"Content-Type": ctype, **(headers or {})}
        req = urllib.request.Request(url, data=body, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _event_row(tl, c):
    """Exact production event schema (mirrors agent._event_payload). Occupancy
    stays None — v1 has no detector."""
    wt = cycle_wall_times(tl, c)
    return {
        "door_open_start_ts": wt["door_open_start"].isoformat(),
        "door_open_full_ts": wt["door_open_full"].isoformat(),
        "door_close_start_ts": wt["door_close_start"].isoformat(),
        "door_close_full_ts": wt["door_close_full"].isoformat(),
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
    def __init__(self, channel, roi, camera, *, cloud, gw_id, headers,
                 nvr=None, url=None, log=None):
        self.channel = int(channel)
        self.roi = roi
        self.camera = camera
        self.cloud = (cloud or "").rstrip("/")
        self.gw_id = gw_id
        self.headers = headers or {}
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
        # signal buffers (absolute offsets from run start — never re-based)
        self.t0 = None
        self.start_wall = None
        self.offs = deque()               # seconds from t0
        self.raws = deque()               # raw gray-level distance
        self.baseline = None
        self.closed_floor = 0.0
        # emission bookkeeping
        self.emitted = []                 # list of close_full offsets already emitted
        self.cycles = []                  # emitted cycle summaries (for rollups)
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

    def status(self):
        el = (time.time() - self.t0) if self.t0 else 0.0
        return {
            "channel": self.channel, "camera": self.camera, "state": self.state,
            "baseline_confirmed": self.baseline_confirmed, "validation_frame": self.validation_fn,
            "error": self.error, "elapsed_s": round(el, 1), "samples": self.samples,
            "signal_fps": round(self.samples / el, 2) if el > 1 else None,
            "latency_med_s": round(float(np.median(self.lat_recent)), 3) if self.lat_recent else None,
            "latency_max_s": round(self.lat_max, 3),
            "cycles_emitted": len(self.cycles), "alarms": self.alarms[-5:],
            "tier1": self.rollups(),
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
        (self-cancel that passes the check). So we also upload ONE OSD-intact
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
        vmsg = self._upload_seed_frame()
        return True, (f"seed motion-quiet (spread {spread:.1f}, closed_floor {self.closed_floor:.1f}, "
                      f"{len(crops)} frames). {vmsg} baseline_confirmed=False — EYEBALL doors-shut at "
                      f"/validation/{self.gw_id} (a stationary OPEN door passes the motion gate).")

    def _upload_seed_frame(self):
        """Encode the stashed full frame as JPEG and POST it to the validation
        viewer (OSD intact). Reuses the keep_validation_frame endpoint/proof."""
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
            if not ok or not self.cloud:
                return "(validation encode/cloud unavailable;"
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            self.validation_fn = f"ch{self.channel:02d}_{stamp}.jpg"
            url = f"{self.cloud}/api/gw/{self.gw_id}/validation/{self.channel}?requested_start={stamp}&mode=watch-seed"
            code = _post_bytes(url, buf.tobytes(), self.headers)
            return f"validation frame uploaded ({code}) -> /validation/{self.gw_id};"
        except Exception as e:
            self._seed_full = None
            return f"(validation upload failed: {type(e).__name__};"

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

    def maybe_detect(self, now_off):
        """Detect over the window; emit only COMPLETE + SETTLED + quality + hole-free
        + non-duplicate cycles. Returns the list of newly emitted rows."""
        if len(self.raws) < 8:
            return []
        tl, offs = self._timeline()
        newest = float(offs[-1])
        new_rows = []
        for c in detect_cycles(tl.signal, tl):
            # SETTLE: close_full must be safely behind the newest sample
            if newest - c.close_full_s < SETTLE_S:
                continue
            if not _quality_ok(c) or _straddles_hole(c, tl.holes):
                continue
            # DE-DUP by close_full within tolerance (offsets are absolute+stable)
            if any(abs(c.close_full_s - e) < DEDUP_TOL_S for e in self.emitted):
                continue
            self.emitted.append(c.close_full_s)
            row = _event_row(tl, c)
            self.cycles.append({
                "open_full_ts": row["door_open_full_ts"],
                "close_full_ts": row["door_close_full_ts"],
                "dwell_s": round(c.dwell_s, 3), "open_travel_s": round(c.open_travel_s, 3),
                "close_travel_s": round(c.close_travel_s, 3), "transfer_s": round(c.transfer_s, 3),
            })
            new_rows.append(row)
        # prune emitted markers that have slid fully out of the window
        cutoff = newest - WINDOW_S
        self.emitted = [e for e in self.emitted if e >= cutoff]
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
        if not rows or not self.cloud:
            return
        payload = {
            "gateway_id": self.gw_id, "camera": self.camera,
            "declared_tz": str(self.start_wall.tzinfo) if self.start_wall else "local",
            "tz_source": "live-wallclock", "start_ts": self.start_wall.isoformat(),
            "events": rows, "door_signal": [], "tier1": self.rollups(), "mode": "continuous",
            "baseline_confirmed": self.baseline_confirmed,
        }
        code = _post(f"{self.cloud}/api/gw/events", payload, self.headers)
        self._log(f"[watch ch{self.channel}] emitted {len(rows)} cycle(s) -> {code}")

    def _status_post(self):
        st = self.status()
        if self.cloud:
            _post(f"{self.cloud}/api/gw/{self.gw_id}/watch_status", st, self.headers)
        import os
        sf = os.environ.get("WATCH_STATUS_FILE")
        if sf:
            try:
                Path(sf).write_text(json.dumps(st))
            except Exception:
                pass

    # ---- the runner thread ----
    def _run(self):
        # resolve URL if not injected
        if not self.url and self.nvr:
            self.url = _live_url(self.nvr, self.channel)
        if not self.url:
            self.state = "failed"; self.error = "no live URL (ONVIF resolve failed)"
            self._log(f"[watch ch{self.channel}] {self.error}"); self._deregister(); return
        if not self.roi:
            self.state = "failed"; self.error = "needs_calibration (no door_roi)"
            self._log(f"[watch ch{self.channel}] {self.error}"); self._deregister(); return

        self._cap_thread = threading.Thread(target=self._capture_loop, name=f"cap-ch{self.channel}", daemon=True)
        self._cap_thread.start()

        ok, msg = self._seed_baseline()
        self._log(f"[watch ch{self.channel}] {msg}")
        if not ok:
            self.state = "failed"; self.error = msg
            self.stop_event.set(); self._status_post(); self._deregister(); return

        self.state = "running"
        self._status_post()
        period = 1.0 / SIGNAL_HZ
        next_t = time.time()
        last_seq = -1
        last_status = time.time()
        while not self.stop_event.is_set():
            now = time.time()
            crop, ts, seq = self.hub.latest()
            # freshness / stall
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
            # detection
            if self.t0 and (now - self.last_detect) >= DETECT_EVERY_S:
                self.last_detect = now
                rows = self.maybe_detect(self.offs[-1] if self.offs else 0.0)
                if rows:
                    self._emit(rows)
            # periodic status + fps drift alarm (every ~15s)
            if now - last_status >= 15.0:
                el = now - self.t0 if self.t0 else 1
                fps = self.samples / el if el > 0 else 0
                if el > 30 and fps < SIGNAL_FPS_ALARM:
                    self._alarm(f"signal fps {fps:.1f} < {SIGNAL_FPS_ALARM}")
                self._status_post(); last_status = now
            # pace at SIGNAL_HZ
            next_t += period
            sleep = next_t - time.time()
            if sleep > 0:
                self.stop_event.wait(sleep)
            else:
                next_t = time.time()
        self.state = "stopped"
        self._status_post()
        self._deregister()

    def _deregister(self):
        with _REG_LOCK:
            if _REGISTRY.get(self.channel) is self:
                _REGISTRY.pop(self.channel, None)


# =========================================================================
# agent entry point (dispatched from the poll loop — MUST stay non-blocking)
# =========================================================================
def run_watch(job, *, report=None, log=None, cloud=None, gw_id=None, headers=None,
              zones_path=None, nvr=None, **_):
    """Dispatch handler. action=start spawns a daemon runner and returns AT ONCE
    (poll loop keeps heartbeating; a later action=stop is receivable)."""
    log = log or (lambda *a, **k: None)
    report = report or (lambda *a, **k: None)
    action = (job.get("action") or "start").lower()
    channel = int(job.get("channel", 29))

    if action == "confirm":
        # operator eyeballed the /validation seed frame and confirms doors were shut
        with _REG_LOCK:
            r = _REGISTRY.get(channel)
        if not r:
            res = {"status": "not_running", "channel": channel}
            report(job, res); return res
        r.baseline_confirmed = True
        log(f"[watch ch{channel}] baseline CONFIRMED doors-shut by operator")
        res = {"status": "confirmed", "channel": channel}
        report(job, res); return res

    if action == "stop":
        with _REG_LOCK:
            r = _REGISTRY.get(channel)
        if not r:
            res = {"status": "not_running", "channel": channel}
            report(job, res); return res
        st = r.stop()
        log(f"[watch ch{channel}] stopped: {st.get('cycles_emitted')} cycles, "
            f"{st.get('samples')} samples, lat_max {st.get('latency_max_s')}s")
        res = {"status": "stopped", "channel": channel, "final": st}
        report(job, res); return res

    # action == start
    with _REG_LOCK:
        if channel in _REGISTRY:
            res = {"status": "already_running", "channel": channel}
            report(job, res); return res
        camera, roi = _resolve_roi(zones_path, channel)
        r = _Runner(channel, roi, camera, cloud=cloud, gw_id=gw_id, headers=headers,
                    nvr=nvr, url=job.get("url"), log=log)
        _REGISTRY[channel] = r
    r.start()
    log(f"[watch ch{channel}] starting (camera={camera}, roi={'set' if roi else 'MISSING'})")
    res = {"status": "starting", "channel": channel, "camera": camera,
           "calibrated": bool(roi)}
    report(job, res)
    return res


def list_running():
    with _REG_LOCK:
        return {ch: r.status() for ch, r in _REGISTRY.items()}


# =========================================================================
# standalone entry point — run as a SUBPROCESS under the full-deps python.
# The import-light agent cannot import numpy/av/liftlab, so watch_manager.py
# (stdlib-only) launches THIS as a child process. Config comes from env; SIGTERM
# stops gracefully, SIGUSR1 confirms the baseline (operator eyeballed doors-shut).
# =========================================================================
def _runner_from_env(channel):
    import os
    nvr = (os.environ.get("NVR_HOST", ""), os.environ.get("NVR_PORT", "80"),
           os.environ.get("NVR_USER", ""), os.environ.get("NVR_PASS", ""))
    token = os.environ.get("GATEWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    camera, roi = _resolve_roi(os.environ.get("ZONES_PATH", ""), channel)
    return _Runner(channel, roi, camera,
                   cloud=os.environ.get("CLOUD_URL", ""),
                   gw_id=os.environ.get("GATEWAY_ID", "site-A"),
                   headers=headers, nvr=nvr, url=os.environ.get("WATCH_URL") or None,
                   log=lambda *a, **k: print(*a, flush=True))


def main():
    import os
    import signal
    import sys
    channel = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("WATCH_CHANNEL", "29"))
    r = _runner_from_env(channel)
    with _REG_LOCK:
        _REGISTRY[channel] = r
    signal.signal(signal.SIGTERM, lambda *_: r.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: r.stop_event.set())
    try:
        def _confirm(*_):
            r.baseline_confirmed = True
            print(f"[watch ch{channel}] baseline CONFIRMED doors-shut (SIGUSR1)", flush=True)
        signal.signal(signal.SIGUSR1, _confirm)
    except (AttributeError, ValueError):
        pass  # SIGUSR1 not on this platform
    print(f"[watch ch{channel}] scheduler process starting (pid {os.getpid()})", flush=True)
    r._run()  # foreground; returns when stop_event is set
    print("FINAL " + json.dumps(r.status()), flush=True)


if __name__ == "__main__":
    main()
