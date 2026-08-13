#!/usr/bin/env python3
"""PostQueue: critical rows survive pressure, droppables absorb it, order and timestamps hold."""
import os, sys, threading, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from post_queue import PostQueue, CRITICAL, DROPPABLE, COALESCE


def main():
    fails = []
    sent = []
    gate = threading.Event()

    def slow_sender(url, payload, what=""):
        gate.wait(timeout=5)                      # simulate a wedged gateway
        sent.append((url, payload, what))

    q = PostQueue(slow_sender, max_critical=8, max_droppable=4, log=lambda m: None)
    q.start()

    # Pressure: 20 critical + 40 droppable while the gateway is wedged.
    for i in range(20):
        q.put("/door", {"ts": 100.0 + i, "seq": i}, "door_event", cls=CRITICAL)
    for i in range(40):
        q.put("/fc", {"ts": 200.0 + i}, "floorcheck", cls=DROPPABLE)
    for i in range(10):
        q.put("/st", {"ts": 300.0 + i, "n": i}, "analyzer_status", cls=COALESCE)

    nc, nd, nq = q.depth()
    print(f"under pressure: critical={nc} droppable={nd} coalesced={nq}")
    if nc > 8: fails.append(f"critical queue exceeded its bound ({nc})")
    if nd > 4: fails.append(f"droppable queue exceeded its bound ({nd})")
    if nq != 1: fails.append(f"analyzer_status not coalesced to 1 ({nq})")

    c = q.drain_counters()
    print(f"dropped: {c['dropped']}")
    print("\n=== analyzer_status starvation (ch29 18:39-08:00) ===")
    fails += status_is_never_starved()
    if c["dropped"]["floorcheck"] == 0: fails.append("droppable overflow was not counted")
    if c["dropped"]["status"] != 9: fails.append(f"coalesce should count 9 replaced, got {c['dropped']['status']}")
    if c["dropped"]["critical"] != 12:
        fails.append(f"expected 12 critical drops at bound 8 with 20 offered, got {c['dropped']['critical']}")

    gate.set()                                     # gateway recovers
    for _ in range(100):
        if q.depth() == (0, 0, 0):
            break
        time.sleep(0.05)
    q.stop()

    crit = [p for _u, p, w in sent if w == "door_event"]
    print(f"delivered: {len(sent)} total, {len(crit)} critical")
    # ORDER: whatever survived must be in the order it was produced
    seqs = [p["seq"] for p in crit]
    if seqs != sorted(seqs): fails.append(f"critical rows delivered out of order: {seqs}")
    # The SURVIVORS must be the NEWEST (drop-oldest), so the seq gap is at the start
    if seqs and seqs[-1] != 19: fails.append(f"newest critical row was not delivered (last={seqs[-1]})")
    # TIMESTAMPS: never rewritten by the queue
    if any(p["ts"] != 100.0 + p["seq"] for p in crit):
        fails.append("queue altered a payload timestamp")
    # the coalesced status delivered must be the NEWEST one
    st = [p for _u, p, w in sent if w == "analyzer_status"]
    if len(st) != 1 or st[0]["n"] != 9:
        fails.append(f"coalesce delivered {[p.get('n') for p in st]}, expected only the newest (9)")

    # A sender that raises must not kill the thread
    boom = []
    def bad_sender(url, payload, what=""):
        boom.append(1)
        raise RuntimeError("gateway 500")
    q2 = PostQueue(bad_sender, log=lambda m: None).start()
    for i in range(3):
        q2.put("/door", {"ts": i}, "door_event", cls=CRITICAL)
    for _ in range(60):
        if q2.depth() == (0, 0, 0): break
        time.sleep(0.05)
    c2 = q2.drain_counters()
    q2.stop()
    print(f"failing sender: {len(boom)} attempts, failed counter={c2['failed']}")
    if len(boom) != 3: fails.append("sender thread died on an exception")
    if c2["failed"] != 3: fails.append("failures not counted")

    print()
    print("FAIL: " + "; ".join(fails) if fails else "POST QUEUE: ALL ASSERTIONS PASS")
    return 1 if fails else 0


def status_is_never_starved():
    """ch29's analyzer_status stopped 18:39-08:00 while its door and transit lanes kept flowing.
    The coalesce slot is the obvious suspect; this eliminates it.

    Three properties, each of which would have to fail for the queue to be the cause:
      * COALESCE is served ABOVE droppable, so a flood of floorchecks cannot starve it;
      * a put() into the coalesce slot notifies the sender, so it never waits out the 1 s timeout;
      * a send that RAISES is counted and the loop continues — one failing status cannot wedge the
        thread that also carries door events.
    """
    import threading
    import time as _t
    fails, sent, gate = [], [], threading.Event()

    def sender(url, payload, what=""):
        gate.wait(timeout=5)
        if payload.get("boom"):
            raise RuntimeError("gateway said no")
        sent.append(what)

    q = PostQueue(sender, max_critical=8, max_droppable=4, log=lambda m: None)
    # PRESSURE FIRST, sender blocked: 200 droppables against one status.
    for i in range(200):
        q.put("/fc", {"ts": i}, "floorcheck", cls=DROPPABLE)
    q.put("/st", {"ts": 1}, "analyzer_status", cls=COALESCE, drop_key="analyzer_status")
    for i in range(3):
        q.put("/door", {"ts": i, "seq": i}, "door_event", cls=CRITICAL)
    q.start(); gate.set()
    for _ in range(50):
        if "analyzer_status" in sent:
            break
        _t.sleep(0.05)
    order = [w for w in sent]
    print(f"  first three delivered under 200-deep droppable pressure: {order[:3]}")
    if "analyzer_status" not in order:
        fails.append("analyzer_status was never sent under droppable pressure — the coalesce slot "
                     "CAN be starved, which would explain the 18:39-08:00 gap")
    elif order.index("analyzer_status") > 3:
        fails.append(f"analyzer_status waited behind {order.index('analyzer_status')} droppables")

    # A FAILING STATUS MUST NOT WEDGE THE LANE THAT CARRIES DOOR EVENTS.
    q.put("/st", {"ts": 2, "boom": True}, "analyzer_status", cls=COALESCE, drop_key="analyzer_status")
    q.put("/door", {"ts": 99, "seq": 99}, "door_event", cls=CRITICAL)
    for _ in range(50):
        if sent.count("door_event") >= 4:
            break
        _t.sleep(0.05)
    print(f"  after a status POST raised: door_event still delivered "
          f"({sent.count('door_event')} total), queue failed={q.failed}")
    if sent.count("door_event") < 4:
        fails.append("a raising analyzer_status stopped the sender thread — door events would stop "
                     "too, which is NOT what was observed, so this is not the mechanism either")
    q.stop()
    return fails


if __name__ == "__main__":
    sys.exit(main())
