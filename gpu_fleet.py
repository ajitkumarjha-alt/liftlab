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


def log(m):
    print(f"[gpu-fleet] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}", flush=True)


_procs = {}          # cam -> {proc, started, cfg, restarts, last_exit}
_last_good = None    # the last SUCCESSFULLY read registry — the only thing we ever act on
_hb = [time.monotonic()]


def fetch_registry():
    """The registry, or None. None means 'do not act' — every failure mode returns it."""
    req = urllib.request.Request(f"{CLOUD}/api/gw/{GW}/cameras",
                                 headers={"Authorization": "Bearer " + TOKEN})
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
            out[cam] = {"enabled": bool(c.get("enabled")), "stride": int(c.get("stride") or 2),
                        "analyze_fps": float(c.get("analyze_fps") or 0),
                        # Door geometry AND counting zones travel with the camera, from roi.json via
                        # the registry, so a fleet-spawned worker needs no unit-file edits for either.
                        "geometry": {k: str(v) for k, v in sorted(g.items()) if k in
                                     ("door_roi_frame", "panel_rois", "digit_cells", "arrow_cell",
                                      "zone_landing", "zone_cabin", "zone_frame")}}
    return {"cams": out, "hash": d.get("hash")}


def start(cam, cfg):
    env = dict(os.environ, CAM=cam, GW=GW,
               DOOR_STRIDE=str(cfg["stride"]), PYTHONUNBUFFERED="1")
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
                         ("zone_frame", "ZONE_FRAME")):
        if geom.get(key):
            env[envname] = geom[key]
        else:
            env.pop(envname, None)
    env["GPU_DOOR"] = "1" if (geom.get("door_roi_frame") and geom.get("panel_rois")
                              and geom.get("digit_cells") and geom.get("arrow_cell")) else "0"
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
                   "restarts": _procs.get(cam, {}).get("restarts", 0), "last_exit": None}
    zones = ("registry" if (geom.get("zone_landing") and geom.get("zone_cabin"))
             else ("builtin-ch29" if cam == "ch29" else "NONE — counting OFF (save zones into roi.json)"))
    log(f"{cam}: started pid={p.pid} stride={cfg['stride']} analyze_fps={cfg['analyze_fps']} "
        f"door={'ON' if env.get('GPU_DOOR') == '1' else 'off (geometry incomplete — draw it at /calib-roi)'} "
        f"zones={zones}")


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
    log(f"fleet up: {CLOUD}/api/gw/{GW}/cameras every {POLL_S:.0f}s; worker={WORKER_SCRIPT}")
    log("SAFETY: a failed or empty poll changes NOTHING — the running set is only ever altered by a "
        "registry that was read successfully and is non-empty.")

    def _bye(signum, _f):
        log(f"signal {signum} — stopping {len(_procs)} worker(s)")
        for cam in list(_procs):
            stop(cam, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    threading.Thread(target=watchdog, args=(os.getpid(),), name="fleet-watchdog", daemon=True).start()

    last_hash = None
    while True:
        _hb[0] = time.monotonic()
        reg = fetch_registry()
        if reg is not None:
            _last_good = reg
            if reg["hash"] != last_hash:
                enabled = sorted(c for c, v in reg["cams"].items() if v["enabled"])
                log(f"registry hash {last_hash} -> {reg['hash']}; enabled: {enabled or 'NONE'}")
                last_hash = reg["hash"]
            converge(reg)
        reap()
        alive = sorted(c for c, r in _procs.items() if r["proc"].poll() is None)
        if int(time.time()) % 300 < POLL_S:
            log(f"running: {alive or 'none'}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main() or 0)
