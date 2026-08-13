"""Progress-based liveness for the GPU worker.

systemd "active (running)" has now lied twice — the relay stall, and the
07:50 UTC worker hang (13s of normal startup, then 5h44m of silence with
the unit active and the process resident). A loop blocked in recv(), stuck
on a lock, or grinding inside one segment is indistinguishable from a
healthy one at the process level. Liveness therefore has to be measured as
WORK COMPLETED, not as process-exists.

Two independent things live here:

  arm()             — a daemon thread that watches segment progress; on a
                      stall it dumps EVERY thread's Python stack to stderr
                      (journald keeps it) and hard-exits for systemd.
  harden_sockets()  — a floor under every socket, including ones inside
                      libraries we don't call directly.

The stack dump is the point. Static reading of gpu_analyze/gpu_door found
no unbounded call — every HTTP path already carries an explicit timeout —
so the next hang has to name its own line rather than be guessed at.

Wiring: see gpu_analyze.py (harden_sockets before the Session is built,
arm() before the loop, progress() per segment, phase() breadcrumbs around
the calls that can block).
"""

from __future__ import annotations

import faulthandler
import os
import socket
import sys
import threading
import time

_last_progress = time.monotonic()
_last_label = "<none yet>"
_phase = "<start>"
_phase_t = time.monotonic()
_lock = threading.Lock()
_armed = False


def progress(label: str = "") -> None:
    """Mark forward progress. Call once per segment ACTUALLY processed —
    not per loop turn, or an idle spin would look like health."""
    global _last_progress, _last_label
    with _lock:
        _last_progress = time.monotonic()
        if label:
            _last_label = label


def phase(name: str) -> None:
    """Breadcrumb: what the loop is doing right now. Does NOT reset the
    stall timer — it only makes the stall report say where we died, so the
    journal names the phase even if the traceback is ambiguous."""
    global _phase, _phase_t
    with _lock:
        _phase = name
        _phase_t = time.monotonic()


def age() -> float:
    """Seconds since the last progress() call."""
    with _lock:
        return time.monotonic() - _last_progress


def _log(msg: str) -> None:
    sys.stderr.write("[gpu-watchdog] %sZ %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), msg))
    sys.stderr.flush()


def _watch(stall_secs: float, grace: float, poll: float) -> None:
    started = time.monotonic()
    while True:
        time.sleep(poll)
        if time.monotonic() - started < grace:
            continue                      # cold start: model load + CUDA init, no segment yet
        stalled = age()
        if stalled < stall_secs:
            continue

        with _lock:
            label, ph, ph_age = _last_label, _phase, time.monotonic() - _phase_t
        # os._exit (inside self_restart), NOT sys.exit: SystemExit raised in a non-main thread
        # unwinds only THAT thread. sys.exit here would kill the watchdog and leave the wedged main
        # loop running — a silently disarmed watchdog on top of a hung worker, which is strictly
        # worse than no watchdog. This exit must be unconditional.
        self_restart("WATCHDOG STALL: no segment processed for %.0fs (limit %.0fs). last_seg=%s "
                     "phase=%r (in this phase %.0fs)" % (stalled, stall_secs, label, ph, ph_age))


def self_restart(reason: str, log=None) -> None:
    """The ONE way this worker ever exits on purpose. Logs a greppable banner, dumps every thread,
    exits rc=1 for the supervisor.

    WHY A BANNER. Every deliberate exit in this worker dumps stacks with faulthandler, whose output
    contains the words "Thread 0x..." and "  File ..." — and NOT "Traceback", "Error" or
    "Exception". So a self-restart looks in the journal exactly like an unexplained crash: rc=1 and
    nothing that any of the words an operator greps for will match. ch29 restarted five times across
    three days before that was established, and the reason line was sitting in the journal the whole
    time under a phrase nobody thought to search.

    The banner is deliberately ugly and unique: WORKER SELF-RESTART, plus the literal word TRACEBACK
    so the natural grep finds the dump that follows it.
    """
    line = f"WORKER SELF-RESTART rc=1 — {reason}"
    (log or _log)(line)
    (log or _log)("WORKER SELF-RESTART rc=1 — TRACEBACK (faulthandler, all threads) follows; this is "
                  "a DELIBERATE exit, not an unhandled exception")
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(1)


def arm(stall_secs: float = 120.0, grace: float = 180.0, poll: float = 5.0) -> None:
    """Start the watchdog thread. Idempotent."""
    global _armed
    if _armed:
        return
    _armed = True

    # On-demand dump of a LIVE process, no py-spy install needed:
    #   kill -USR1 $(systemctl show -p MainPID --value liftlab-gpu)
    # then read it in `journalctl -u liftlab-gpu`. Use this the instant it goes quiet,
    # BEFORE restarting — a restart destroys the only evidence that matters.
    try:
        import signal

        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True, chain=False)
        _log("SIGUSR1 armed: kill -USR1 <pid> dumps all stacks to the journal")
    except (AttributeError, ValueError):
        pass                              # no SIGUSR1 (non-POSIX), or not the main thread

    progress("<armed>")
    threading.Thread(target=_watch, args=(stall_secs, grace, poll),
                     name="gpu-watchdog", daemon=True).start()
    _log("armed: stall_secs=%.0f grace=%.0f poll=%.1f" % (stall_secs, grace, poll))


def harden_sockets(timeout: float = 30.0) -> None:
    """Default timeout for every socket created from here on.

    gpu_analyze/gpu_door already pass explicit timeouts on every HTTP call, so this
    is not the fix for a known bug — it is the floor under the ones we don't own
    (urllib3 internals, ultralytics' downloader, anything a dependency opens).

    Per-recv, not per-transfer: a large .ts body is safe as long as bytes keep
    arriving. Does NOT cover getaddrinfo() — DNS resolution blocks in libc and no
    Python timeout reaches it. That gap is precisely why the stack dump exists.

    Must run BEFORE any Session/connection pool is constructed.
    """
    socket.setdefaulttimeout(timeout)
    _log("socket default timeout = %.0fs (sockets created from here on; NOT DNS)" % timeout)
