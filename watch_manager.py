#!/usr/bin/env python3
"""Import-light manager for the continuous door-cycle scheduler.

The agent venv is deliberately minimal (no numpy/av/cv2/liftlab), so it CANNOT run
the scheduler in-process. This module — stdlib ONLY — is what the agent imports; it
launches continuous_scheduler.py as a SUBPROCESS under the full-deps python (the
same runtime analyze_local uses), tracks it by PID, and stops it with a signal.

NON-BLOCKING: run_watch(start) Popen()s and returns at once, so the agent poll loop
keeps heartbeating and a later {action:stop} is receivable. Stop = SIGTERM (graceful:
the child flushes a final status). Confirm = SIGUSR1 (operator eyeballed doors-shut).

Runtime config is read from watch_runtime.conf (written by apply_watch.sh after it
discovers which python has the deps):
    FULL_PY=/home/askjitk/liftlab-b4/.venv/bin/python
    EXTRA_PATH=/home/askjitk/liftlab-b3/pi-agent
    SCRIPT=/home/askjitk/liftlab-b3/pi-agent/continuous_scheduler.py
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = HERE / "watch_runtime.conf"
RUN_DIR = Path(os.environ.get("WATCH_RUN_DIR", "/tmp/liftlab-watch"))


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
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_state(ch):
    try:
        return json.loads(_state_path(ch).read_text())
    except Exception:
        return None


def _running(ch):
    st = _read_state(ch)
    if st and _alive(st.get("pid", -1)):
        return st
    return None


def run_watch(job, *, report=None, log=None, cloud=None, gw_id=None, headers=None,
              zones_path=None, nvr=None, **_):
    log = log or (lambda *a, **k: None)
    report = report or (lambda *a, **k: None)
    action = (job.get("action") or "start").lower()
    channel = int(job.get("channel", 29))
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    if action == "stop":
        st = _running(channel)
        if not st:
            res = {"status": "not_running", "channel": channel}
            report(job, res); return res
        try:
            os.kill(st["pid"], signal.SIGTERM)
        except OSError:
            pass
        for _ in range(80):                      # up to ~8s for graceful flush
            if not _alive(st["pid"]):
                break
            time.sleep(0.1)
        if _alive(st["pid"]):
            try:
                os.kill(st["pid"], signal.SIGKILL)
            except OSError:
                pass
        final = None
        try:
            final = json.loads(_status_path(channel).read_text())
        except Exception:
            pass
        _state_path(channel).unlink(missing_ok=True)
        log(f"[watch ch{channel}] stopped (pid {st['pid']})")
        res = {"status": "stopped", "channel": channel, "final": final}
        report(job, res); return res

    if action == "confirm":
        st = _running(channel)
        if not st:
            res = {"status": "not_running", "channel": channel}
            report(job, res); return res
        try:
            os.kill(st["pid"], signal.SIGUSR1)
            log(f"[watch ch{channel}] baseline confirm signal sent")
            res = {"status": "confirmed", "channel": channel}
        except OSError as e:
            res = {"status": "error", "channel": channel, "error": str(e)}
        report(job, res); return res

    if action == "status":
        st = _running(channel)
        detail = None
        try:
            detail = json.loads(_status_path(channel).read_text())
        except Exception:
            pass
        res = {"status": "running" if st else "not_running", "channel": channel, "detail": detail}
        report(job, res); return res

    # ---- action == start ----
    if _running(channel):
        res = {"status": "already_running", "channel": channel}
        report(job, res); return res

    cfg = _conf()
    full_py = cfg.get("FULL_PY")
    script = cfg.get("SCRIPT", str(HERE / "continuous_scheduler.py"))
    extra_path = cfg.get("EXTRA_PATH", str(HERE))
    if not full_py or not Path(full_py).exists() or not Path(script).exists():
        res = {"status": "misconfigured", "channel": channel,
               "error": f"watch_runtime.conf FULL_PY/SCRIPT invalid ({full_py}, {script})"}
        log(f"[watch ch{channel}] {res['error']}"); report(job, res); return res

    host, port, user, pw = (nvr or ("", "80", "", ""))
    token = ""
    if headers:
        token = (headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    env = {
        **os.environ,
        "PYTHONPATH": extra_path + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "NVR_HOST": host or "", "NVR_PORT": str(port or "80"),
        "NVR_USER": user or "", "NVR_PASS": pw or "",
        "CLOUD_URL": (cloud or "").rstrip("/"), "GATEWAY_ID": gw_id or "site-A",
        "GATEWAY_TOKEN": token, "ZONES_PATH": zones_path or "",
        "WATCH_STATUS_FILE": str(_status_path(channel)),
    }
    if job.get("url"):
        env["WATCH_URL"] = job["url"]
    logf = open(RUN_DIR / f"watch_ch{channel}.log", "ab", buffering=0)
    proc = subprocess.Popen([full_py, script, str(channel)], env=env,
                            stdout=logf, stderr=subprocess.STDOUT,
                            start_new_session=True, cwd=extra_path)
    _state_path(channel).write_text(json.dumps(
        {"pid": proc.pid, "channel": channel, "started": time.time(),
         "log": str(RUN_DIR / f"watch_ch{channel}.log")}))
    log(f"[watch ch{channel}] scheduler launched pid {proc.pid} "
        f"(seed during a DOORS-SHUT moment; then eyeball /validation/{gw_id})")
    res = {"status": "starting", "channel": channel, "pid": proc.pid,
           "note": "baseline_confirmed=False until you eyeball /validation and send action:confirm"}
    report(job, res)
    return res
