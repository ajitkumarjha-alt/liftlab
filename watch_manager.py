#!/usr/bin/env python3
"""Import-light manager for the continuous door-cycle scheduler (STDLIB ONLY).

The agent venv (B3) is minimal — no numpy/av/liftlab — so it cannot run the CV
scheduler in-process. This module, which the agent imports, launches
continuous_scheduler.py as a SUBPROCESS under the B4 python, EXACTLY as
analyze_local does: PYTHONPATH=B4_DIR, cwd=B4_DIR, job as JSON on the child's
STDIN, and the GATEWAY TOKEN STRIPPED from the child env.

TOKEN BOUNDARY (the invariant): the child never holds the token and never posts.
It streams NDJSON on stdout; a reader thread HERE (in the agent, which owns the
Bearer credential) does every POST — events -> /api/gw/events, the OSD seed frame
-> /api/gw/{gw}/validation/{ch}, status -> file (+ best-effort). Same privacy
boundary as analyze_local, adapted to a streaming child instead of a one-shot.

NON-BLOCKING: run_watch(start) Popen()s + spawns the reader daemon and returns at
once, so the agent poll loop keeps heartbeating and {action:stop} is receivable.
Stop=SIGTERM (graceful). Confirm=SIGUSR1. Parent death closes the child's stdout
pipe, so the child auto-reaps.

Runtime config (watch_runtime.conf, written by apply_watch.sh; defaults match
agent.py's B4_DIR/B4_PY):
    FULL_PY=/home/askjitk/liftlab-b4/.venv/bin/python
    B4_DIR=/home/askjitk/liftlab-b4
    SCRIPT=/home/askjitk/liftlab-b4/continuous_scheduler.py
"""
import base64
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = HERE / "watch_runtime.conf"
RUN_DIR = Path(os.environ.get("WATCH_RUN_DIR", "/home/askjitk/liftlab-watch"))  # durable, NOT /tmp
_READERS = {}                       # channel -> {"proc":Popen, "thread":Thread}
_LOCK = threading.Lock()


def _conf():
    cfg = {}
    try:
        for line in CONF.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def _state_path(ch):
    return RUN_DIR / f"watch_ch{int(ch)}.json"


def _status_path(ch):
    return RUN_DIR / f"watch_ch{int(ch)}.status.json"


def _alive(pid):
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def _running(ch):
    with _LOCK:
        ent = _READERS.get(ch)
    if ent and ent["proc"].poll() is None:
        return ent["proc"].pid
    st = None
    try:
        st = json.loads(_state_path(ch).read_text())
    except Exception:
        pass
    if st and _alive(st.get("pid", -1)):
        return st["pid"]
    return None


# ---- POSTs (parent owns the Bearer credential) ----
def _post(url, payload, headers, timeout=10):
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _post_bytes(url, body, headers, timeout=30):
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "image/jpeg", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception:
        return None


def _reader(ch, proc, cloud, gw_id, headers, log):
    """Pump the child's NDJSON stdout -> cloud POSTs. Runs in the agent process,
    which holds the token. The child never posts."""
    cloud = (cloud or "").rstrip("/")
    bad = 0
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                # The child isolates fd 1 so this should never happen; if a stray
                # non-JSON line ever appears, SKIP it (never misparse as an event)
                # and LOG it (bounded) rather than drop it silently.
                bad += 1
                if bad <= 10 or bad % 500 == 0:
                    log(f"[watch ch{ch}] non-JSON stdout line #{bad} SKIPPED: {line[:100]}")
                continue
            kind = obj.get("kind")
            if kind == "events" and cloud:
                _post(f"{cloud}/api/gw/events", obj["payload"], headers)
            elif kind == "status":
                st = obj.get("status", {})
                try:
                    _status_path(ch).write_text(json.dumps(st))
                except Exception:
                    pass
                if cloud:
                    _post(f"{cloud}/api/gw/{gw_id}/watch_status", st, headers)  # best-effort
            elif kind == "validation" and cloud:
                try:
                    jpg = base64.b64decode(obj["jpg_b64"])
                    q = f"requested_start={obj.get('requested_start','')}&mode={obj.get('mode','watch-seed')}"
                    code = _post_bytes(f"{cloud}/api/gw/{gw_id}/validation/{obj['channel']}?{q}", jpg, headers)
                    log(f"[watch ch{ch}] seed validation frame posted ({code}) -> /validation/{gw_id}")
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        proc.wait()
        _state_path(ch).unlink(missing_ok=True)
        with _LOCK:
            if _READERS.get(ch, {}).get("proc") is proc:
                _READERS.pop(ch, None)
        log(f"[watch ch{ch}] scheduler exited (rc={proc.returncode})")


def run_watch(job, *, report=None, log=None, cloud=None, gw_id=None, headers=None,
              zones_path=None, nvr=None, **_):
    log = log or (lambda *a, **k: None)
    report = report or (lambda *a, **k: None)
    # The cloud stores job params in a JSON column; the agent passes them nested as
    # job["params"] (same as analyze_local's `p = job.get("params", {})`). Read from
    # there FIRST — reading top-level would collapse every stop/confirm into start.
    p = job.get("params") if isinstance(job.get("params"), dict) else {}
    action = (p.get("action") or job.get("action") or "start").lower()
    channel = int(p.get("channel", job.get("channel", 29)))
    job_url = p.get("url") or job.get("url")
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    if action == "stop":
        pid = _running(channel)
        if not pid:
            res = {"status": "not_running", "channel": channel}; report(job, res); return res
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(80):
            if not _alive(pid):
                break
            time.sleep(0.1)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        final = None
        try:
            final = json.loads(_status_path(channel).read_text())
        except Exception:
            pass
        _state_path(channel).unlink(missing_ok=True)
        log(f"[watch ch{channel}] stopped (pid {pid})")
        res = {"status": "stopped", "channel": channel, "final": final}; report(job, res); return res

    if action == "confirm":
        pid = _running(channel)
        if not pid:
            res = {"status": "not_running", "channel": channel}; report(job, res); return res
        try:
            os.kill(pid, signal.SIGUSR1)
            res = {"status": "confirmed", "channel": channel}
        except OSError as e:
            res = {"status": "error", "channel": channel, "error": str(e)}
        log(f"[watch ch{channel}] baseline confirm signal sent")
        report(job, res); return res

    if action == "reseed":
        # genuinely fresh seed (camera moved / lighting changed permanently): drop the
        # persisted baseline, then stop the child so the next start re-seeds live.
        try:
            (RUN_DIR / f"baseline_ch{channel}.npz").unlink(missing_ok=True)
        except Exception:
            pass
        pid = _running(channel)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        log(f"[watch ch{channel}] persisted baseline dropped; child stopped — restart to re-seed live")
        res = {"status": "reseed", "channel": channel}
        report(job, res); return res

    if action == "status":
        pid = _running(channel)
        detail = None
        try:
            detail = json.loads(_status_path(channel).read_text())
        except Exception:
            pass
        res = {"status": "running" if pid else "not_running", "channel": channel, "detail": detail}
        report(job, res); return res

    # ---- action == start ----
    if _running(channel):
        res = {"status": "already_running", "channel": channel}; report(job, res); return res

    cfg = _conf()
    b4_dir = cfg.get("B4_DIR", os.environ.get("B4_DIR", "/home/askjitk/liftlab-b4"))
    full_py = cfg.get("FULL_PY", os.environ.get("B4_PY", f"{b4_dir}/.venv/bin/python"))
    script = cfg.get("SCRIPT", f"{b4_dir}/continuous_scheduler.py")
    if not Path(full_py).exists() or not Path(script).exists():
        res = {"status": "misconfigured", "channel": channel,
               "error": f"FULL_PY/SCRIPT invalid ({full_py}, {script}) — check watch_runtime.conf"}
        log(f"[watch ch{channel}] {res['error']}"); report(job, res); return res

    host, port, user, pw = (nvr or ("", "80", "", ""))
    runner_job = {
        "channel": channel, "gateway_id": gw_id or "site-A",
        "zones_path": zones_path or f"{b4_dir}/camera_zones.json",
        "work_dir": str(RUN_DIR / f"analyze_ch{channel}"),
        "nvr_settings": {"host": host, "port": int(port or 80), "user": user, "password": pw},
    }
    if job_url:
        runner_job["url"] = job_url

    # match analyze_local EXACTLY: strip the token, PYTHONPATH=B4_DIR, cwd=B4_DIR
    child_env = {k: v for k, v in os.environ.items() if k != "GATEWAY_TOKEN"}
    child_env["PYTHONPATH"] = b4_dir
    logf = open(RUN_DIR / f"watch_ch{channel}.log", "a", buffering=1)
    proc = subprocess.Popen([full_py, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=logf, cwd=b4_dir, env=child_env, text=True)
    try:
        proc.stdin.write(json.dumps(runner_job)); proc.stdin.close()
    except Exception as e:
        proc.kill()
        res = {"status": "error", "channel": channel, "error": f"stdin write: {e}"}; report(job, res); return res

    th = threading.Thread(target=_reader, args=(channel, proc, cloud, gw_id or "site-A", headers, log),
                          name=f"watch-reader-ch{channel}", daemon=True)
    th.start()
    with _LOCK:
        _READERS[channel] = {"proc": proc, "thread": th}
    _state_path(channel).write_text(json.dumps(
        {"pid": proc.pid, "channel": channel, "started": time.time()}))
    log(f"[watch ch{channel}] scheduler launched pid {proc.pid} (token stripped; parent posts). "
        f"Seed during a DOORS-SHUT moment, then eyeball /validation/{gw_id} and send action:confirm.")
    res = {"status": "starting", "channel": channel, "pid": proc.pid,
           "note": "baseline_confirmed=False until you eyeball /validation and send action:confirm"}
    report(job, res)
    return res
