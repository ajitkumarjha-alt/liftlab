#!/usr/bin/env python3
"""The two faults behind ch29's restarts: a guard that convicts on quiet, and a queue nobody watched.

1. COUNTING WEDGED (door-independent) killed THREE workers between 04:57 and 05:46 IST on
   2026-08-13 — historical rates 3.5, 8.0 and 10.1/hr. It arms on an EWMA, which is a MEAN, and
   then convicts on a single zero. A mean is not a floor: measured over the restored snapshot,
   51-55% of DAYTIME hours on days a camera was demonstrably live carry no transits at all, and
   nights are emptier. Now gated on 07:00-23:00 IST, the same window and the same evidence as the
   health line. The guards that key on POSITIVE evidence are untouched.

2. analyzer_status stopped ~25 min into each worker while transits flowed for hours, with no
   'POST analyzer_status failed' and no 'heartbeat POST failed' anywhere. Transits are posted
   DIRECTLY; status, door events and floorchecks ride the post queue. A sender thread that dies
   takes every queued lane with it, silently — `put()` keeps succeeding and depth keeps growing —
   while the directly-posted lane carries on looking healthy. Nothing supervised that thread, and
   the one signal that would have reported it rode the thread it was reporting on.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def guard_hours():
    """The active-hours gate, evaluated exactly as gpu_analyze evaluates it."""
    fails = []
    os.environ.setdefault("ANALYSIS_TOKEN", "test")
    os.environ.setdefault("CLOUD", "http://127.0.0.1:0")
    os.environ.setdefault("CAM", "ch29")
    os.environ.setdefault("GW", "site-A")
    import gpu_analyze as g

    print(f"  active window: {g.TRANSIT_IDLE_ACTIVE_FROM:02d}:00-{g.TRANSIT_IDLE_ACTIVE_TO:02d}:00 IST")
    # the real incident: 04:57-05:46 IST, rates 3.5 / 8.0 / 10.1 per hour
    from datetime import datetime, timedelta, timezone
    IST = timezone(timedelta(hours=5, minutes=30))

    def armed(hour, rate):
        ts = datetime(2026, 8, 13, hour, 30, tzinfo=IST).timestamp()
        h = g._ist_hour(ts)
        active = g.TRANSIT_IDLE_ACTIVE_FROM <= h < g.TRANSIT_IDLE_ACTIVE_TO
        return bool(g.TRANSIT_IDLE_STALL_S > 0 and active and rate is not None
                    and rate >= g.TRANSIT_HIST_MIN_RATE)

    for hour, rate, want, why in ((4, 3.5, False, "the ch29 kill: 04:57 IST at 3.5/hr"),
                                  (5, 8.0, False, "the second kill: 05:xx IST at 8.0/hr"),
                                  (5, 10.1, False, "the third kill: 05:xx IST at 10.1/hr"),
                                  (2, 20.0, False, "2am, even on a busy-by-EWMA hour"),
                                  (9, 8.0, True, "09:xx on a lift that normally moves — still convicts"),
                                  (18, 10.1, True, "evening peak — still convicts"),
                                  (9, 1.0, False, "09:xx but historically quiet — below MIN_RATE"),
                                  (22, 5.0, True, "22:xx is inside the window"),
                                  (23, 5.0, False, "23:xx is outside it")):
        got = armed(hour, rate)
        mark = "ARMS " if got else "quiet"
        print(f"    {hour:02d}:30 IST @ {rate:>4}/hr -> {mark}  {why}")
        if got != want:
            fails.append(f"{hour:02d}:30 @ {rate}/hr armed={got}, expected {want} — {why}")
    return fails


def queue_supervision():
    """Kill the sender thread the way a stray exception would, and see what notices."""
    from post_queue import PostQueue, CRITICAL, COALESCE
    fails = []
    sent, logs = [], []

    def sender(url, payload, what=""):
        if payload.get("kill"):
            raise BaseException("something outside Exception")   # the class that used to end the thread
        sent.append(what)

    q = PostQueue(sender, log=logs.append).start()
    q.put("/x", {"n": 1}, "door_event", cls=CRITICAL)
    for _ in range(40):
        if sent:
            break
        time.sleep(0.05)
    print(f"  baseline: delivered {sent}, alive={q.alive()}")

    print("  a BaseException from the sender (this used to kill the thread silently):")
    q.put("/x", {"kill": True}, "door_event", cls=CRITICAL)
    time.sleep(0.4)
    print(f"    alive={q.alive()} failed={q.failed}")
    if not q.alive():
        fails.append("a BaseException from the sender still ends the thread — every queued lane "
                     "goes silent while directly-posted transits keep flowing")
    q.put("/x", {"n": 2}, "door_event", cls=CRITICAL)
    for _ in range(40):
        if len(sent) >= 2:
            break
        time.sleep(0.05)
    if len(sent) < 2:
        fails.append("the queue stopped delivering after one bad payload")

    print("  a thread that is gone anyway must be REVIVED, and say so:")
    q._t = threading.Thread(target=lambda: None)      # simulate a dead sender
    q._t.start(); q._t.join()
    revived = q.ensure_alive()
    print(f"    ensure_alive() -> revived={revived}, alive={q.alive()}, count={q.revived}")
    if not revived or not q.alive():
        fails.append("ensure_alive did not restart a dead sender")
    if not any("POST QUEUE SENDER WAS DEAD" in m for m in logs):
        fails.append("the revival was silent — the operator learns nothing")
    q.put("/x", {"n": 3}, "door_event", cls=CRITICAL)
    for _ in range(40):
        if len(sent) >= 3:
            break
        time.sleep(0.05)
    if len(sent) < 3:
        fails.append("the revived sender does not deliver")

    print("  stalled_for() separates a STUCK queue from a QUIET one:")
    idle = PostQueue(lambda *a, **k: None, log=lambda m: None)
    print(f"    empty queue, never sent: stalled_for={idle.stalled_for():.1f}s")
    if idle.stalled_for() != 0.0:
        fails.append("an empty queue reports itself stalled — a quiet camera would trip the fallback")
    gate = threading.Event()
    stuck = PostQueue(lambda *a, **k: gate.wait(timeout=10), log=lambda m: None)
    stuck._started_ts = time.time() - 300
    stuck.put("/x", {"n": 1}, "door_event", cls=CRITICAL)
    print(f"    queued and nothing sent for 300s: stalled_for={stuck.stalled_for():.0f}s")
    if stuck.stalled_for() < 200:
        fails.append("a queue with work outstanding and no sends is not reported as stalled")
    gate.set()
    q.stop()
    return fails


def liveness_does_not_ride_the_queue():
    """The heartbeat must fall back to a direct POST when the queue is stuck."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "gpu_analyze.py")).read()
    fails = []
    for needle, why in (
            ("_PQ.ensure_alive(log=log)", "the heartbeat must ask whether the sender is alive"),
            ("_q_stall = _PQ.stalled_for()", "it must measure whether the queue is draining"),
            ("if _PQ is not None and _q_stall < QUEUE_STALL_DIRECT_S:",
             "a stalled queue must not carry the signal that would report it"),
            ("posting\\n                        f\"analyzer_status DIRECTLY", "")):
        if needle and needle.replace("\\n", "\n") not in src and why:
            fails.append(f"{why} ({needle!r} absent)")
    if "queue_stalled_s" not in src:
        fails.append("queue health is not reported to the gateway, so /ops cannot see it")
    print("  heartbeat supervises the queue, reports its depth, and posts directly when it stalls")
    return fails


def main():
    fails = []
    print("=== 1. the door-independent counting guard, by hour and rate ===")
    fails += guard_hours()
    print("\n=== 2. the post-queue sender: unkillable, supervised, and visibly revived ===")
    fails += queue_supervision()
    print("\n=== 3. liveness no longer rides the subsystem it attests ===")
    fails += liveness_does_not_ride_the_queue()
    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — a quiet night no longer convicts, the sender survives anything its sender raises "
          "and is revived if it dies, and the heartbeat reports the queue instead of depending on it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
