"""Run buttons — /calib-run/{gw}/{cam}/{job}. The FOURTH web wizard piece.

Every per-camera calibration command is argument-free now (roi.json feeds the ROIs and cells,
labels.json feeds the build), so they become buttons: Collect, Top-up, Build, Fitcells, Refresh
frame. The operator never opens a terminal for a camera again.

door_calib runs as a SUBPROCESS, not an import. That is the whole point: door_calib needs cv2 and
numpy, the web app deliberately has neither, and importing it here would drag the CV stack into the
ingest process — the boundary every other wizard piece has respected. A subprocess keeps the two
apart, so a segfault in OpenCV cannot take the door-event ingest down with it.

NO USER STRINGS REACH THE COMMAND LINE. Jobs are a fixed allowlist mapping a name to a literal argv;
the camera and gateway travel in the ENVIRONMENT (door_calib reads GW/CAM from env), where they
cannot be argv injection, and they are pattern-checked before that. There is no path by which a
request supplies a flag.

Output streams to a per-camera log file and the page polls for new lines by index. Deliberately not
SSE: a poll survives a page reload, a flaky site connection, and an operator who wandered off — all
of which happen in a lift lobby, which is where this gets used.

Routes:
  POST /calib-run/{gw}/{cam}/{job}   start a job (409 if one is already running for this camera)
  GET  /calib-run/{gw}/{cam}/status  runner health + live output (?since=N for the tail)
  POST /calib-run/{gw}/{cam}/stop    terminate the running job
"""
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
# The --build output lands here. Resolved the SAME way door_calib resolves it, so the runner and
# the job cannot disagree about which tree needs to be writable.
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", "/var/lib/liftlab/templates"))
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
JOB_TIMEOUT_S = float(os.environ.get("CALIB_JOB_TIMEOUT_S", "1800"))   # --collect 60 is ~2s/frame
MAX_LINES = int(os.environ.get("CALIB_JOB_MAX_LINES", "4000"))

# The interpreter that HAS cv2 (not this app's). Set DOOR_CALIB_PY in the unit if it is elsewhere.
DOOR_CALIB_PY = os.environ.get("DOOR_CALIB_PY", "")
# THE code-identity authority (2026-07-30): DOOR_CALIB_SCRIPT is REQUIRED. The old fallback list
# didn't even contain /opt/liftlab-analysis — a deploy there was a silent no-op for the Build
# button unless the env happened to be pinned. Guessing was only harmless while the pin existed;
# an unset pin now ABORTS instead of quietly running whichever historical copy still exists.
DOOR_CALIB_SCRIPT = os.environ.get("DOOR_CALIB_SCRIPT", "")
_PY_GUESSES = ["/opt/liftlab-analysis/.venv/bin/python", "/opt/liftlab-b3/calib/.venv/bin/python",
               "/opt/liftlab-b3/cloud/.venv/bin/python", "/usr/bin/python3"]
_SCRIPT_HISTORY = ["/opt/liftlab-analysis/door_calib.py", "/opt/liftlab-b3/cloud/door_calib.py",
                   "/opt/liftlab-b3/calib/door_calib.py", "/opt/liftlab-gpu/door_calib.py"]


def _md5(path):
    import hashlib
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def _resolve_script():
    """(path, err). No fallback, no picking between copies: DOOR_CALIB_SCRIPT set and
    existing, or refuse — listing every historical location that DOES hold a copy (with
    md5s) so 'which one is real' is answered out loud, never by accident."""
    if not DOOR_CALIB_SCRIPT:
        present = [f"{p} (md5 {_md5(p)})" for p in _SCRIPT_HISTORY if os.path.exists(p)]
        return None, ("DOOR_CALIB_SCRIPT is not set — refusing to guess which door_calib.py "
                      f"is real. Copies found: {present or 'none'}. Pin the unit env to the "
                      "one true copy.")
    if not os.path.exists(DOOR_CALIB_SCRIPT):
        return None, (f"DOOR_CALIB_SCRIPT={DOOR_CALIB_SCRIPT} does not exist — fix the unit "
                      f"env (nothing else is searched: the pin is the authority)")
    return DOOR_CALIB_SCRIPT, None

# THE ALLOWLIST. A job name maps to a literal argv tail — nothing from the request is interpolated.
JOBS = {
    "frame":    (["--frames", "1"],   "Refresh frame",
                 "decode one live frame at native resolution for the ROI page"),
    "collect":  (["--collect", "60"], "Collect 60",
                 "collect 60 panel crops (appends to what is already there)"),
    "topup":    (["--collect", "30"], "Top-up collect",
                 "collect 30 more crops — for glyphs the label pass showed are thin"),
    "build":    (["--build"],         "Build templates",
                 "build templates.npz from the collected crops + labels.json"),
    "fitcells": (["--fitcells"],      "Fitcells",
                 "auto-fit per-cell geometry against the labelled crops"),
}

calib_run_router = APIRouter()

_jobs = {}                       # (gw,cam) -> job dict
_lock = threading.Lock()


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _unwritable():
    """Directories the job will write that this process's user cannot. Checked BEFORE launching, so
    a permissions problem shows on the button as a reason instead of surfacing as a PermissionError
    traceback three minutes into a Build. The CLI era ran door_calib under sudo, so these trees are
    routinely root-owned while the buttons run as the service user."""
    bad = []
    for d in (CALIB_DIR, TEMPLATES_DIR):
        probe = d
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent          # a dir the job will CREATE needs a writable parent
        if not os.access(probe, os.W_OK | os.X_OK):
            bad.append(str(d))
    return bad


def _runner():
    """(python, script, reason). Resolved once per call so a fixed unit takes effect on restart
    without a code change."""
    py = DOOR_CALIB_PY or next((p for p in _PY_GUESSES if os.path.exists(p)), "")
    if not py:
        return None, None, ("no python with cv2 found — set DOOR_CALIB_PY to the interpreter that "
                            "runs door_calib (this app's venv deliberately has no cv2)")
    sc, err = _resolve_script()
    if err:
        return None, None, err
    bad = _unwritable()
    if bad:
        import getpass
        try:
            who = getpass.getuser()
        except Exception:
            who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "this user"
        return py, sc, (f"not writable by {who}: {', '.join(bad)} — the CLI built these under sudo, "
                        f"so they are root-owned. Fix: chown -R {who} {' '.join(bad)}")
    return py, sc, None


def _logpath(gw, cam):
    return CALIB_DIR / gw / cam / "_job.log"


def _pump(key, proc, job):
    """Read the child's merged output into the ring + the log file, and enforce the timeout."""
    lp = job["log"]
    deadline = time.time() + JOB_TIMEOUT_S
    try:
        with open(lp, "a", encoding="utf-8", errors="replace") as fh:
            for line in proc.stdout:
                line = line.rstrip("\n")
                with _lock:
                    job["lines"].append(line)
                    job["n"] += 1
                fh.write(line + "\n")
                fh.flush()
                if time.time() > deadline:
                    proc.kill()
                    with _lock:
                        job["lines"].append(f"[runner] TIMEOUT after {JOB_TIMEOUT_S:.0f}s — killed")
                        job["n"] += 1
                    break
    except Exception as e:                       # never let the reader thread die silently
        with _lock:
            job["lines"].append(f"[runner] output reader failed: {type(e).__name__}: {e}")
            job["n"] += 1
    rc = proc.wait()
    with _lock:
        job["rc"] = rc
        job["running"] = False
        job["ended"] = time.time()
        job["lines"].append(f"[runner] {job['job']} finished rc={rc} in {job['ended'] - job['started']:.0f}s")
        job["n"] += 1


# ROUTE ORDER MATTERS. FastAPI matches in registration order, so the literal /stop must be
# declared BEFORE the /{job} catch-all — otherwise POST .../stop binds job="stop" and answers
# 400 "unknown job", i.e. the Stop button silently never works.
@calib_run_router.post("/calib-run/{gw}/{cam}/stop")
def stop_job(gw: str, cam: str):
    _safe(gw, cam)
    with _lock:
        rec = _jobs.get((gw, cam))
        if not rec or not rec["running"]:
            raise HTTPException(404, "nothing running for this camera")
        proc = rec["proc"]
    proc.terminate()
    return {"ok": True, "stopped": rec["job"]}


@calib_run_router.get("/calib-run/{gw}/{cam}/status")
def job_status(gw: str, cam: str, since: int = 0):
    _safe(gw, cam)
    py, script, reason = _runner()
    with _lock:
        rec = _jobs.get((gw, cam))
        if not rec:
            out = {"running": False, "job": None, "rc": None, "lines": [], "next_since": 0}
        else:
            all_lines = list(rec["lines"])
            # `n` counts every line ever produced; the ring holds the last MAX_LINES. first = the
            # index of the oldest line still held, so a client that fell behind resyncs instead of
            # silently receiving the wrong slice.
            first = rec["n"] - len(all_lines)
            start = max(0, min(len(all_lines), since - first))
            out = {"running": rec["running"], "job": rec["job"], "rc": rec["rc"],
                   "started": rec["started"], "ended": rec["ended"],
                   "elapsed_s": round((rec["ended"] or time.time()) - rec["started"], 1),
                   "lines": all_lines[start:], "next_since": rec["n"], "pid": rec.get("pid")}
    out["runner"] = {"ok": reason is None, "python": py, "script": script, "reason": reason}
    out["jobs"] = [{"id": k, "label": v[1], "help": v[2]} for k, v in JOBS.items()]
    return JSONResponse(out)


@calib_run_router.post("/calib-run/{gw}/{cam}/{job}")
def run_job(gw: str, cam: str, job: str):
    _safe(gw, cam, job)
    if job not in JOBS:
        raise HTTPException(400, f"unknown job {job!r} — one of {sorted(JOBS)}")
    py, script, reason = _runner()
    if reason:
        raise HTTPException(503, reason)
    key = (gw, cam)
    with _lock:
        cur = _jobs.get(key)
        if cur and cur["running"]:
            raise HTTPException(409, f"{cur['job']} is already running for {cam} "
                                     f"({int(time.time() - cur['started'])}s) — wait or stop it")
    d = CALIB_DIR / gw / cam
    d.mkdir(parents=True, exist_ok=True)
    argv = [py, script] + list(JOBS[job][0])
    # The camera identity travels in the ENVIRONMENT, never in argv — door_calib reads GW/CAM from
    # env, and this is what keeps a path parameter from ever becoming a command-line flag.
    env = dict(os.environ, GW=gw, CAM=cam, PYTHONUNBUFFERED="1")
    rec = {"job": job, "argv": argv, "started": time.time(), "ended": None, "running": True,
           "rc": None, "lines": deque(maxlen=MAX_LINES), "n": 0, "log": str(_logpath(gw, cam))}
    rec["lines"].append(f"[runner] $ GW={gw} CAM={cam} {' '.join(argv[1:])}")
    # NON-NEGOTIABLE provenance line (three silent-authority bugs in one week — geometry
    # space, label keys, file path): every run states WHICH file executed, by content.
    rec["script_md5"] = _md5(script)
    rec["lines"].append(f"[runner] executing {os.path.abspath(script)} md5={rec['script_md5']} "
                        f"via {py}")
    rec["n"] += 2
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                cwd=str(Path(script).parent), env=env)
    except OSError as e:
        raise HTTPException(500, f"could not start door_calib: {e}")
    rec["pid"] = proc.pid
    rec["proc"] = proc
    with _lock:
        _jobs[key] = rec
    threading.Thread(target=_pump, args=(key, proc, rec), name=f"calib-{cam}-{job}", daemon=True).start()
    return JSONResponse({"ok": True, "job": job, "pid": proc.pid, "label": JOBS[job][1]}, status_code=202)
