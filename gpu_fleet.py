#!/usr/bin/env python3
"""GPU fleet supervisor — one gpu_analyze worker per ENABLED camera, from the cloud registry.

Adding a camera is a toggle on /dash. This process polls /api/gw/{gw}/cameras and converges: start
a worker for a camera that is enabled and not running, stop one that is no longer enabled, restart
one that died. gpu_analyze stays exactly as it is — single-camera, CAM from env — because a worker
that only ever thinks about one camera is a worker whose failures stay local to that camera.

WHY A SUPERVISOR AND NOT systemd TEMPLATE UNITS (liftlab-gpu@ch29). Templates would need this
process to hold root and call systemctl, and would put the fleet's state in systemd's enablement
rather than in the registry — two sources of truth for "what is running", which is exactly the
divergence that has already cost this project two outages. Supervising child processes directly is
also the pattern already proven on the Pi by relay_soak.sh, including the bits that matter: bounded
polls, a heartbeat, and a supervisor watchdog.

THE SAFETY RULE, and it is the whole design:

    A FAILED OR EMPTY POLL CHANGES NOTHING.

An unreachable cloud, a 500, a timeout, a truncated body, an empty camera list — every one of them
leaves the running set exactly as it is. The registry can only ever take effect when it is READ
SUCCESSFULLY AND IS NON-EMPTY. This is not defensive habit; it is the specific lesson from the
segment-age stall detector, where treating missing telemetry as "nothing is delivering" would have
restarted all seven streams on a cloud blip. Here the equivalent mistake is worse: it would stop
collecting data and look like a working fleet with nothing enabled.

An empty list is treated as "cannot read intent", not "run nothing". Stopping every camera because
a query returned zero rows — a fresh table, a bad migration, a wrong gateway id — is a total data
outage caused by a schema mistake. Explicitly disabling a camera is how you stop one.

Env: CLOUD_URL, GW, ANALYSIS_TOKEN (or GATEWAY_TOKEN), FLEET_POLL_S, WORKER_PY, WORKER_SCRIPT.
"""
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
GW = os.environ.get("GW", "site-A")
TOKEN = os.environ.get("ANALYSIS_TOKEN") or os.environ.get("GATEWAY_TOKEN") or ""
POLL_S = float(os.environ.get("FLEET_POLL_S", "30"))
HTTP_TIMEOUT_S = float(os.environ.get("FLEET_HTTP_TIMEOUT_S", "15"))
WORKER_PY = os.environ.get("WORKER_PY", sys.executable)
WORKER_SCRIPT = os.environ.get("WORKER_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             "gpu_analyze.py"))
START_GRACE_S = float(os.environ.get("FLEET_START_GRACE_S", "20"))   # let a worker load its model
RESTART_BACKOFF_S = float(os.environ.get("FLEET_RESTART_BACKOFF_S", "30"))
LOOP_STALL_S = float(os.environ.get("FLEET_LOOP_STALL_S", "180"))
# OUTPUT FLOOR (2026-07-30, second silent counting wedge in 24h, both mid-peak). The worker's own
# detectors need door opens or learned history; this is the supervisor's crude backstop that needs
# neither: a worker whose STREAM IS FRESH (its cursor file is advancing — segments are being
# processed) but which has POSTED NOTHING for FLOOR_S gets restarted regardless of door state.
# Evidence is two files the worker maintains in STATE_DIR: cursor_{cam} (mtime advances only when
# segments flow through the loop) and transit_last_{cam} (touched on every successful transit POST).
# Guards, each one load-bearing:
#   - active hours only (IST): a quiet building at 3am is not a wedge; restart-looping every camera
#     all night buries real journal signal. Default 07-22 covers both study peaks.
#   - worker uptime must exceed FLOOR_S: a fresh restart starts with a stale transit_last, and
#     without this guard the floor would restart-loop its own restarts forever.
#   - zones must be configured: a camera with counting OFF will never post; restarting it is noise.
#   - stream freshness: a starved stream is the relay watchdog's problem; a restart here cannot
#     conjure segments and would mask the real (upstream) outage.
# Crude by design — it converts a 4-hour data hole into a ~FLOOR_S one. 0 disables.
FLOOR_S = float(os.environ.get("FLEET_TRANSIT_FLOOR_S", "900"))
FLOOR_STREAM_FRESH_S = float(os.environ.get("FLEET_FLOOR_STREAM_FRESH_S", "120"))
FLOOR_H0, FLOOR_H1 = (int(x) for x in os.environ.get("FLEET_FLOOR_HOURS_IST", "7,22").split(","))
STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/liftlab-gpu")
IST_OFF_S = 5.5 * 3600.0


def log(m):
    print(f"[gpu-fleet] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}", flush=True)


_procs = {}          # cam -> {proc, started, cfg, restarts, last_exit}
_last_good = None    # the last SUCCESSFULLY read registry — the only thing we ever act on
_hb = [time.monotonic()]
HASH_HEARTBEAT_POLLS = int(os.environ.get("FLEET_HASH_HEARTBEAT_POLLS", "10"))  # ~5 min at 30s
_SELF_MD5 = ""       # md5 of this file AS LOADED, filled at startup
_SELF_WARNED = False


def fetch_registry():
    """The registry, or None. None means 'do not act' — every failure mode returns it."""
    # NO-CACHE, EXPLICITLY. urllib does not cache, but nothing between here and the app is
    # guaranteed not to. A stale body is indistinguishable from an unchanged registry unless we
    # both forbid caching and CHECK — see the freshness test in the poll loop.
    req = urllib.request.Request(f"{CLOUD}/api/gw/{GW}/cameras",
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Cache-Control": "no-cache, no-store, max-age=0",
                                          "Pragma": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            import json
            d = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        log(f"registry poll failed ({type(e).__name__}: {str(e)[:80]}) — keeping the current fleet")
        return None
    cams = d.get("cameras")
    if not isinstance(cams, list) or not cams:
        # NOT "run nothing". A fresh table or a wrong gateway id must not stop live cameras.
        log(f"registry returned {len(cams) if isinstance(cams, list) else 'no'} cameras — treating as "
            f"UNREADABLE, not as 'disable everything'. Fleet unchanged.")
        return None
    out = {}
    for c in cams:
        cam = str(c.get("cam", ""))
        if cam:
            g = c.get("geometry") or {}
            lv = c.get("door_levels") or {}
            out[cam] = {"enabled": bool(c.get("enabled")), "stride": int(c.get("stride") or 2),
                        "analyze_fps": float(c.get("analyze_fps") or 0),
                        # Which door engine this camera runs. The rollout is per camera and lives in
                        # the REGISTRY, never in a deploy script — that is what lets ch27/ch30 move
                        # to h3 while ch16/29/32/34/37 stay on h2 without a code change, and what
                        # lets the next camera join by editing data. Absent/unknown -> h2.
                        "door_tracker": str(c.get("door_tracker") or "h2").strip().lower(),
                        # Floor-OCR cadence in frames, 0 = every door pass (current behaviour).
                        # Per camera because the cost scales with that camera's alphabet size.
                        "floor_stride": int(c.get("floor_stride") or 0),
                        # per-camera DoorTracker levels (close_th recalibration). Values only; the
                        # worker validates ranges — the registry already did on the way in.
                        "door_levels": {k: float(v) for k, v in sorted(lv.items())
                                        if isinstance(v, (int, float))},
                        # Door geometry AND counting zones travel with the camera, from roi.json via
                        # the registry, so a fleet-spawned worker needs no unit-file edits for either.
                        "geometry": {k: str(v) for k, v in sorted(g.items()) if k in
                                     ("door_roi_frame", "panel_rois", "digit_cells", "arrow_cell",
                                      "zone_landing", "zone_cabin", "zone_frame")}}
    # `t` is the SERVER's stamp on this response (camera_registry_api.cameras_get). It was being
    # discarded, and it is the one field that can prove a body is fresh rather than replayed.
    return {"cams": out, "hash": d.get("hash"), "t": d.get("t")}


def _door_state_str(door_missing, panel_missing):
    """What the door engine will do, and WHICH field decided it.

    "geometry incomplete" was printed for four different absences, so the line told an operator that
    something was missing without saying what — while they were looking at the very field it wanted,
    already drawn and served. Same nameless-absence defect the dashboard work removed from three
    panels: an absence must always say which kind of absence it is.
    """
    if door_missing:
        return ("off (no door_roi_frame — draw the door band at /calib-roi; nothing else is "
                "required to run the door engine)")
    if panel_missing:
        return ("ON door-only (have door_roi_frame; missing " + ", ".join(panel_missing)
                + " — floor will be NULL with reason no_panel_geometry until those are calibrated)")
    return "ON door+floor"


def start(cam, cfg):
    env = dict(os.environ, CAM=cam, GW=GW,
               DOOR_STRIDE=str(cfg["stride"]), PYTHONUNBUFFERED="1",
               # SET EXPLICITLY ON EVERY WORKER, never left to inherit. If this were only set for h3
               # cameras, a fleet process started with DOOR_TRACKER=h3 in its own environment would
               # hand h3 to every camera it spawned — including the five with no template, which
               # would then fall back to h2 and LOOK fine while the registry said otherwise.
               DOOR_TRACKER=str(cfg.get("door_tracker") or "h2"),
               FLOOR_STRIDE=str(cfg.get("floor_stride") or 0))
    # Geometry from the registry. Env names are gpu_analyze's own, and an ABSENT key is unset rather
    # than blank: gpu_analyze treats "" as "not configured" and falls back, whereas a stale value
    # inherited from the fleet's environment would silently configure this camera from another
    # lift's panel — the exact failure the built-in PANEL_ROIS default already caused once.
    geom = cfg.get("geometry") or {}
    for key, envname in (("door_roi_frame", "DOOR_ROI_FRAME"), ("panel_rois", "PANEL_ROIS"),
                         ("digit_cells", "DIGIT_CELLS"), ("arrow_cell", "ARROW_CELL"),
                         # Zones: same absent-means-UNSET rule — a stale inherited value would count
                         # this camera with another lift's polygons, the exact ch16 undercount this
                         # plumbing exists to end.
                         ("zone_landing", "ZONE_LANDING"), ("zone_cabin", "ZONE_CABIN"),
                         ("zone_frame", "ZONE_FRAME"),
                         # The valid-floor whitelist is per CAMERA — ch16's list is 58 floors and
                         # ch29's is 97, and they barely overlap. It was previously settable only in
                         # /etc/liftlab-gpu.env, i.e. ONE value for the whole fleet, which is why it
                         # was never set at all.
                         ("floor_alphabet", "FLOOR_ALPHABET")):
        if geom.get(key):
            env[envname] = geom[key]
        else:
            env.pop(envname, None)
    # THE DOOR BAND IS THE ONLY REQUIREMENT, at this layer too. This demanded all four fields, so a
    # camera with a calibrated door band and no panel cells was spawned with GPU_DOOR=0 and the door
    # engine never started — even after 592276a relaxed the WORKER's gate to match. Two gates, one
    # relaxed, and the symptom was unchanged: ch32/ch34/ch37 still came up door=off on 2026-08-17
    # with their geometry saved and served. Relaxing one of two gates changes nothing, and the log
    # line said the same thing either way, which is how it survived a fix aimed straight at it.
    #
    # Panel fields absent -> the worker runs its door-only path and reports floor=NULL with reason
    # 'no_panel_geometry'. Panel fields present -> unchanged behaviour.
    _door_missing = [k for k in ("door_roi_frame",) if not geom.get(k)]
    _panel_missing = [k for k in ("panel_rois", "digit_cells", "arrow_cell") if not geom.get(k)]
    env["GPU_DOOR"] = "0" if _door_missing else "1"
    # Per-camera DoorTracker levels from the registry (ch29's close_th recalibration). Same
    # absent-means-UNSET rule as geometry: a level not set for THIS camera must fall back to the
    # worker default, never inherit another camera's tuning from the fleet environment.
    for key, envname in (("near_open", "DOOR_NEAR_OPEN"), ("close_th", "DOOR_CLOSE_TH"),
                         ("close_start_th", "DOOR_CLOSE_START_TH"),
                         ("close_debounce_s", "DOOR_CLOSE_DEBOUNCE_S")):
        v = (cfg.get("door_levels") or {}).get(key)
        if v is not None:
            env[envname] = str(v)
        else:
            env.pop(envname, None)
    # ANALYZE_FPS is only SET when the registry asks for subsampling. Passing "0.0" is behaviourally
    # identical to unset (gpu_analyze treats <=0 as "every frame"), but it surfaces as a literal 0.0
    # on /ops next to workers showing "all(~25)", and two renderings of the same setting read as two
    # different configurations. Omitting it keeps one visible meaning for "not subsampled".
    if float(cfg.get("analyze_fps") or 0) > 0:
        env["ANALYZE_FPS"] = str(cfg["analyze_fps"])
    else:
        env.pop("ANALYZE_FPS", None)
    try:
        p = subprocess.Popen([WORKER_PY, WORKER_SCRIPT], env=env, stdin=subprocess.DEVNULL,
                             cwd=os.path.dirname(WORKER_SCRIPT) or ".")
    except OSError as e:
        log(f"{cam}: FAILED to start: {e}")
        return
    _procs[cam] = {"proc": p, "started": time.time(), "cfg": dict(cfg),
                   "restarts": _procs.get(cam, {}).get("restarts", 0), "last_exit": None,
                   # CODE VERSION THIS WORKER IS ACTUALLY RUNNING. Recorded at spawn, because after
                   # this instant the file on disk can change under us and the process cannot.
                   "code_md5": _worker_md5()}
    zones = ("registry" if (geom.get("zone_landing") and geom.get("zone_cabin"))
             else ("builtin-ch29" if cam == "ch29" else "NONE — counting OFF (save zones into roi.json)"))
    lv = cfg.get("door_levels") or {}
    log(f"{cam}: door_tracker={cfg.get('door_tracker', 'h2')} (registry) — the worker logs which "
        f"engine it actually resolved, including any fallback to h2")
    log(f"{cam}: started pid={p.pid} stride={cfg['stride']} analyze_fps={cfg['analyze_fps']} "
        f"door={_door_state_str(_door_missing, _panel_missing)} "
        f"zones={zones}"
        + (f" door_levels={lv} (NON-DEFAULT — this worker's door era moves)" if lv else ""))


def stop(cam, why):
    rec = _procs.pop(cam, None)
    if not rec:
        return
    p = rec["proc"]
    log(f"{cam}: stopping ({why}) pid={p.pid}")
    p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        log(f"{cam}: did not exit in 15s — killing")
        p.kill()


def converge(reg):
    want = {c: cfg for c, cfg in reg["cams"].items() if cfg["enabled"]}
    for cam in [c for c in _procs if c not in want]:
        stop(cam, "disabled in the registry")
    for cam, cfg in want.items():
        rec = _procs.get(cam)
        if rec is None:
            start(cam, cfg)
        elif rec["cfg"] != cfg:
            # A config change needs a restart: gpu_analyze reads DOOR_STRIDE/ANALYZE_FPS once, at
            # import. Say so explicitly — a silent restart looks like a crash in the journal.
            log(f"{cam}: config changed {rec['cfg']} -> {cfg} — restarting the worker")
            stop(cam, "config change")
            start(cam, cfg)


def _self_md5():
    """md5 of THIS file on disk. cycle_stale_code covers the WORKERS; nothing covers the
    supervisor, so a gpu_fleet.py deployed without a unit restart keeps running the old code
    indefinitely and silently — which is exactly how a supervisor can be blind to registry fields
    it was never taught to parse."""
    import hashlib
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


def check_self_stale():
    """Loud, once per transition. This process CANNOT safely restart itself — systemd owns that —
    so the honest action is to be unmissable in the journal rather than to act."""
    global _SELF_WARNED
    disk = _self_md5()
    if disk and _SELF_MD5 and disk != _SELF_MD5 and not _SELF_WARNED:
        _SELF_WARNED = True
        log(f"SUPERVISOR CODE IS STALE — running {_SELF_MD5[:8]}, {os.path.abspath(__file__)} on "
            f"disk is {disk[:8]}. cycle_stale_code covers the workers, NOT this process. A registry "
            f"field added in the newer code is INVISIBLE to the running supervisor until "
            f"`systemctl restart liftlab-gpu-fleet`.")


def _worker_md5():
    """md5 of the worker script AS IT IS ON DISK RIGHT NOW."""
    import hashlib
    try:
        with open(WORKER_SCRIPT, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


def cycle_stale_code():
    """Restart any worker whose running code differs from the file on disk.

    WHY. A worker is a separate process started from WORKER_SCRIPT; replacing that file changes what
    the NEXT spawn runs and nothing about the ones already running. A deploy that copies the new
    gpu_analyze.py and restarts only the unit is therefore not guaranteed to replace worker code —
    on 2026-08-10 a deploy needed stop + pkill + start to actually take effect, and the operator had
    no way to tell from the outside which binary each worker was executing.

    This closes it from the supervisor side: the md5 each worker was SPAWNED with is recorded, and a
    mismatch against disk cycles that worker. It is deliberately indifferent to WHY they differ —
    orphaned children from an unclean supervisor death, a mid-flight deploy, or a hand-edited file
    all present identically and all want the same action.

    Cameras are cycled ONE PER POLL. Seven simultaneous restarts would drop every stream at once and
    reload seven TensorRT engines into the same GPU; staggered, the fleet re-deploys itself within a
    few polls with one camera dark at a time.
    """
    disk = _worker_md5()
    if not disk:
        return
    for cam in list(_procs):
        rec = _procs[cam]
        if rec["proc"].poll() is not None:
            continue                              # exited — reap() owns it
        was = rec.get("code_md5") or ""
        if was and was != disk:
            log(f"{cam}: WORKER CODE IS STALE — running {was[:8]}, {WORKER_SCRIPT} on disk is "
                f"{disk[:8]}. Cycling it so the deployed code is the code that runs. "
                f"(One camera per poll; the rest follow.)")
            cfg = rec["cfg"]
            stop(cam, "stale worker code (deploy)")
            start(cam, cfg)
            return                                # one per poll, deliberately


def reap():
    """Restart workers that exited. Backoff so a worker that cannot start (bad geometry, missing
    model) does not spin — it stays visible in the journal instead of flooding it."""
    for cam in list(_procs):
        rec = _procs[cam]
        rc = rec["proc"].poll()
        if rc is None:
            continue
        if rec["last_exit"] is None:
            rec["last_exit"] = time.time()
            rec["restarts"] += 1
            log(f"{cam}: worker exited rc={rc} after {time.time() - rec['started']:.0f}s "
                f"(restart #{rec['restarts']} in {RESTART_BACKOFF_S:.0f}s)")
            continue
        if time.time() - rec["last_exit"] >= RESTART_BACKOFF_S:
            cfg = rec["cfg"]
            _procs.pop(cam, None)
            start(cam, cfg)
            if cam in _procs:
                _procs[cam]["restarts"] = rec["restarts"]


def output_floor():
    """Restart any worker that is demonstrably processing segments but has posted nothing for
    FLOOR_S during active hours. See the FLOOR_S comment block for why each guard exists."""
    if FLOOR_S <= 0:
        return
    hour_ist = int(((time.time() + IST_OFF_S) % 86400.0) // 3600.0)
    if not (FLOOR_H0 <= hour_ist < FLOOR_H1):
        return
    now = time.time()
    for cam in list(_procs):
        rec = _procs[cam]
        if rec["proc"].poll() is not None or now - rec["started"] < FLOOR_S:
            continue                              # dead (reap's job) or too young to judge
        geom = rec["cfg"].get("geometry") or {}
        if not (geom.get("zone_landing") and geom.get("zone_cabin")) and cam != "ch29":
            continue                              # counting OFF — this worker never posts transits
        try:
            cursor_age = now - os.path.getmtime(os.path.join(STATE_DIR, f"cursor_{cam}"))
        except OSError:
            continue                              # no cursor yet — nothing processed, nothing to judge
        if cursor_age > FLOOR_STREAM_FRESH_S:
            continue                              # stream not fresh — upstream problem, not a wedge
        try:
            transit_age = now - os.path.getmtime(os.path.join(STATE_DIR, f"transit_last_{cam}"))
        except OSError:
            transit_age = now - rec["started"]    # never posted since the file was introduced
        transit_age = min(transit_age, now - rec["started"])   # never blame a previous worker's silence
        if transit_age >= FLOOR_S:
            cfg = rec["cfg"]
            restarts = rec["restarts"]
            log(f"{cam}: OUTPUT FLOOR — segments flowing (cursor {cursor_age:.0f}s old) but no transit "
                f"posted in {transit_age:.0f}s (>= {FLOOR_S:.0f}s) during active hours "
                f"({hour_ist:02d}h IST). Restarting the worker; if this recurs, read its journal for "
                f"the wedge signature rather than trusting the restart.")
            stop(cam, "output floor: no transits while segments flow")
            start(cam, cfg)
            if cam in _procs:
                _procs[cam]["restarts"] = restarts + 1


def watchdog(main_pid):
    """The supervisor's own liveness — same reasoning as relay_soak.sh. If the loop stops ticking it
    cannot fix itself, so die and let systemd restart. Workers are in the same cgroup and go too."""
    while True:
        time.sleep(15)
        age = time.monotonic() - _hb[0]
        if age > LOOP_STALL_S:
            log(f"SUPERVISOR STALL: loop has not ticked in {age:.0f}s (limit {LOOP_STALL_S:.0f}s) — "
                f"exiting so systemd restarts the fleet")
            for cam in list(_procs):
                try:
                    _procs[cam]["proc"].kill()
                except Exception:
                    pass
            os._exit(1)


def main():
    global _last_good
    if not TOKEN:
        log("no ANALYSIS_TOKEN/GATEWAY_TOKEN in env — cannot read the registry; refusing to start")
        return 2
    global _SELF_MD5
    _SELF_MD5 = _self_md5()
    log(f"fleet up: {CLOUD}/api/gw/{GW}/cameras every {POLL_S:.0f}s; worker={WORKER_SCRIPT}")
    log(f"supervisor code {_SELF_MD5[:8]} ({os.path.abspath(__file__)}); worker code "
        f"{_worker_md5()[:8]}. The workers are cycled on a code change; THIS process is not — "
        f"a gpu_fleet.py deploy needs a unit restart and will be flagged if one is missed.")
    log("SAFETY: a failed or empty poll changes NOTHING — the running set is only ever altered by a "
        "registry that was read successfully and is non-empty.")
    floor_desc = ("OFF" if FLOOR_S <= 0 else
                  f"restart a zoned worker with fresh segments but no transit post for "
                  f"{FLOOR_S:.0f}s, {FLOOR_H0:02d}-{FLOOR_H1:02d}h IST")
    log(f"output floor: {floor_desc}")

    def _bye(signum, _f):
        log(f"signal {signum} — stopping {len(_procs)} worker(s)")
        for cam in list(_procs):
            stop(cam, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    threading.Thread(target=watchdog, args=(os.getpid(),), name="fleet-watchdog", daemon=True).start()

    last_hash = None
    last_srv_t = None
    unchanged_polls = 0
    while True:
        _hb[0] = time.monotonic()
        reg = fetch_registry()
        if reg is not None:
            _last_good = reg
            # ── REGISTRY FRESHNESS. Silence was the defect: only CHANGES were logged, so a
            # supervisor being served a stale body looked exactly like a quiet registry, and a
            # change made at 06:48 went unseen across four polls with nothing in the journal.
            # Two independent checks, both loud, because "no news" must never mean "no evidence".
            now_t = time.time()
            srv_t = reg.get("t")
            if srv_t is None:
                log("REGISTRY RESPONSE HAS NO SERVER TIMESTAMP — cannot prove it is fresh. The API "
                    "is older than the freshness check; treat hash stability as unverified.")
            elif last_srv_t is not None and srv_t <= last_srv_t:
                # The server stamps t on every response. Identical or going backwards means we were
                # handed a REPLAYED body — a cache between us and the app, not a quiet registry.
                log(f"STALE REGISTRY RESPONSE — server timestamp {srv_t:.0f} did not advance since "
                    f"the last poll ({last_srv_t:.0f}). Something is serving a CACHED body; the "
                    f"registry may have changed without us seeing it. hash={reg['hash']}")
            last_srv_t = srv_t if srv_t is not None else last_srv_t

            if reg["hash"] != last_hash:
                enabled = sorted(c for c, v in reg["cams"].items() if v["enabled"])
                log(f"registry hash {last_hash} -> {reg['hash']}; enabled: {enabled or 'NONE'}")
                last_hash = reg["hash"]
                unchanged_polls = 0
            else:
                unchanged_polls += 1
                # POSITIVE EVIDENCE ON A SCHEDULE. A periodic "still X, as of server time T" line is
                # what distinguishes "nothing changed" from "we stopped seeing changes".
                if unchanged_polls % HASH_HEARTBEAT_POLLS == 0:
                    age = (now_t - srv_t) if srv_t else float("nan")
                    log(f"registry unchanged for {unchanged_polls} polls: hash={reg['hash']} "
                        f"(server t={srv_t:.0f}, {age:.1f}s ago) — poll is LIVE, not stuck")
            converge(reg)
        reap()
        check_self_stale()
        cycle_stale_code()
        output_floor()
        alive = sorted(c for c, r in _procs.items() if r["proc"].poll() is None)
        if int(time.time()) % 300 < POLL_S:
            log(f"running: {alive or 'none'}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main() or 0)
