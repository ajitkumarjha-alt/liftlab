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


if __name__ == "__main__":
    sys.exit(main())
