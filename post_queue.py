"""Async POST queue — takes blocking HTTP off the segment loop without losing state transitions.

MEASURED JUSTIFICATION. Split instrumentation on 2026-08-10: cpu=53-104ms against
post=274-1323ms on every camera. 90-96% of the door pass was blocking HTTP to the gateway, not
compute. This decouples the worker from the gateway; it does NOT cure the gateway, and the
gateway's own slow-request log (slowlog.py) ships first so that problem stays visible.

THE THING THIS GETS RIGHT, AND WHY IT IS NOT A PLAIN RING BUFFER.

`gw_door_event` is not a set of independent observations. It is a STATE SEQUENCE, and three
consumers walk it: dash's cycle funnel, `_h3_cycle_ts` (the h3 cycle count IS a transition into
'closed' — there is no marker column, by design), and the attribution SQL. So dropping a row does
not thin the data, it REWRITES it:

    dropped 'closed' row   -> the cycle disappears from the h3 count and the funnel
    dropped 'closing' row  -> closing->closed becomes open->closed; stage counts shift
    dropped cycle row (h2) -> that cycle's close_travel_s is lost outright

A uniform drop-oldest would therefore make the fleet look like it is doing LESS work under load,
exactly when it is doing more. Hence two classes:

    CRITICAL   door_event carrying a STATE CHANGE or a completed cycle   -> never dropped
    DROPPABLE  door_event heartbeats, floorcheck, analyzer_status        -> dropped, counted

Dropping a heartbeat is safe BY CONSTRUCTION: the emit gate re-sends an unchanged key purely for
liveness, so it carries nothing the previous row did not. That is the same statement the gate
already makes; a drop here just makes it again.

analyzer_status is COALESCED rather than queued: only the newest is kept, replace-in-place. Liveness
then lags only while the worker genuinely cannot reach the gateway — honest "down-ish" — instead of
draining a backlog of stale statuses that each claim to describe now.

WHEN THE CRITICAL QUEUE OVERFLOWS, the row is dropped and `seq` makes it VISIBLE IN THE DATABASE.
Every door_event carries a per-camera monotonic counter, so a consumer seeing 41, 42, 45 knows two
are missing and can refuse to compute a funnel over that window rather than averaging across a hole
it cannot see. Blocking the frame loop instead would restore the exact stall being removed.

ORDER IS PRESERVED: one sender thread, FIFO. `dash_api` walks `ORDER BY ts, id`, so ts order and
insert order must not diverge.

TIMESTAMPS ARE STAMPED AT CAPTURE, NEVER AT SEND. The payload is built by the caller and enqueued
whole; this module never writes a `ts`. That is load-bearing for era and join integrity, and a queue
is exactly where a well-meaning `ts=time.time()` gets added later.
"""
from __future__ import annotations

import threading
import time
from collections import deque

CRITICAL = "critical"
DROPPABLE = "droppable"
COALESCE = "coalesce"


class PostQueue:
    """Bounded, single-sender, class-aware POST queue.

    `sender(url, payload, what)` is injected so this module owns no HTTP and can be tested without
    a network: the worker passes its existing http_post_json.
    """

    def __init__(self, sender, send_cap_s=120.0, coalesce_max_wait_s=60.0, max_critical=512, max_droppable=128, log=print):
        self._sender = sender
        self._log = log
        self._crit = deque()
        self._drop = deque()
        self._coalesced = {}                 # what -> (url, payload); newest only
        self._coalesced_at = {}              # what -> when this lane was first filled and unserved
        # The coalesce lane may not go unserved longer than this even under sustained CRITICAL
        # pressure. 60s is two heartbeats: long enough that normal operation is unchanged, short
        # enough that the fleet never loses sight of a busy worker.
        self.coalesce_max_wait_s = float(coalesce_max_wait_s)
        self.last_sent_ts = None             # for stalled_for(): a stuck sender vs a quiet one
        self._started_ts = time.time()
        self.revived = 0                     # times the sender thread had to be restarted
        self._send_started_ts = None         # when the CURRENT send began; None between sends
        self._send_what = None
        # A single send may not hold the queue longer than this before it is abandoned. 120s is well
        # past any healthy POST (the slow-log fires at 2s) and well short of a shift.
        self.send_cap_s = float(send_cap_s)
        self._max_crit = max_critical
        self._max_drop = max_droppable
        self._cv = threading.Condition()
        self._stop = False
        self._t = None
        # Counters. Reported in the seg-timing line and reset when read, so each line describes the
        # interval it covers rather than all of history.
        self.dropped = {"critical": 0, "heartbeat": 0, "floorcheck": 0, "status": 0, "other": 0}
        self.sent = 0
        self.failed = 0

    # ---- producer side (called from the frame loop; must never block) ----
    def put(self, url, payload, what, cls=DROPPABLE, drop_key=None):
        with self._cv:
            if cls == COALESCE:
                key = drop_key or what
                if key in self._coalesced:
                    self.dropped["status" if "status" in what else "other"] += 1
                else:
                    self._coalesced_at[key] = time.time()   # first unserved fill; see slot_waiting_s
                self._coalesced[key] = (url, payload, what)
            elif cls == CRITICAL:
                if len(self._crit) >= self._max_crit:
                    # The only place a state transition is ever lost. Loud, counted, and visible in
                    # the DB through the seq gap the caller stamped on the payload.
                    self._crit.popleft()
                    self.dropped["critical"] += 1
                    self._log(f"POST QUEUE OVERFLOW: dropped a CRITICAL {what} — the door state "
                              f"sequence now has a gap and seq will show it. Gateway is not keeping "
                              f"up ({len(self._crit)} queued).")
                self._crit.append((url, payload, what))
            else:
                if len(self._drop) >= self._max_drop:
                    _u, _p, w = self._drop.popleft()
                    k = ("floorcheck" if "floorcheck" in w else
                         "heartbeat" if "door_event" in w else "other")
                    self.dropped[k] += 1
                self._drop.append((url, payload, what))
            self._cv.notify()

    # ---- consumer side ----
    def _next(self):
        """CRITICAL first, then coalesced, then droppable — EXCEPT that the coalesce slot has a
        guaranteed maximum wait.

        THE ch29 INCIDENT, 2026-08-12 18:39 -> 05:00. Strict priority means the slot is reached only
        when `self._crit` is EMPTY at the instant _next() runs. ch29 emitted 1196-5505 door events an
        hour all night — every one of them CRITICAL — and once the arrival rate crossed the drain
        rate, `_crit` stopped emptying. From that moment the slot was never served again: analyzer_status
        stopped dead while door events flowed perfectly, one healthy thread, no exception, no log
        line, nothing to find. It could not recover on its own and it did not.
        
        Starving DROPPABLE under sustained pressure is still correct — those rows are expendable by
        definition. Starving the COALESCE lane is not, because that lane carries the liveness signal,
        and a liveness signal that yields to load is silent exactly when the box is busiest.

        So the slot keeps its low priority until it has waited `coalesce_max_wait_s`, and then it
        goes first. Under normal load nothing changes: the slot is served in the gaps, as before.
        """
        if self._coalesced and self.slot_waiting_s() >= self.coalesce_max_wait_s:
            _k = next(iter(self._coalesced))
            self._coalesced_at.pop(_k, None)
            return self._coalesced.pop(_k)
        if self._crit:
            return self._crit.popleft()
        if self._coalesced:
            _k = next(iter(self._coalesced))
            self._coalesced_at.pop(_k, None)
            return self._coalesced.pop(_k)
        if self._drop:
            return self._drop.popleft()
        return None

    def slot_waiting_s(self):
        """How long the coalesce lane has gone UNSERVED. 0.0 when the slot is empty.

        Timed from the FIRST fill that has not yet been served, not from the newest payload: the
        question is how long this lane has been silent, and replacing the payload every 30s does not
        make the silence any shorter."""
        if not self._coalesced_at:
            return 0.0
        return time.time() - min(self._coalesced_at.values())

    def _run(self):
        while True:
            with self._cv:
                while not self._stop and not (self._crit or self._drop or self._coalesced):
                    self._cv.wait(timeout=1.0)
                if self._stop and not (self._crit or self._drop or self._coalesced):
                    return
                item = self._next()
            if item is None:
                continue
            url, payload, what = item
            # SEND IN FLIGHT, timestamped. urllib's timeout is per socket OPERATION, not per call: a
            # gateway that trickles bytes, or a half-open connection a NAT dropped, can hold one POST
            # open indefinitely without ever tripping it. A blocked sender and a dead sender look
            # IDENTICAL from outside — queue accepts, depth grows, nothing is delivered, nothing is
            # logged — and neither can recover on its own, which is why ch29 stayed silent for ten
            # hours rather than for ten seconds.
            self._send_started_ts = time.time()
            self._send_what = what
            try:
                self._sender(url, payload, what=what)
                self.sent += 1
                self.last_sent_ts = time.time()
            except BaseException as e:                # a dead gateway must not kill the sender
                # BaseException, not Exception. This caught Exception only, so anything outside that
                # hierarchy — or a raise from inside self._log — unwound _run() and the thread died
                # SILENTLY. Nothing supervised it, so every lane that rides this queue (door events,
                # floorchecks, analyzer_status) went quiet forever while the main loop carried on:
                # transits kept flowing because they are posted DIRECTLY, not queued. That is exactly
                # the ch29 signature — status stops, transits continue, worker lives for hours.
                self.failed += 1
                try:
                    self._log(f"POST {what} failed: {type(e).__name__}: {str(e)[:120]}")
                except BaseException:
                    pass                              # logging must never be the thing that kills it
            finally:
                self._send_started_ts = None

    def start(self):
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._run, name="post-queue", daemon=True)
            self._t.start()
        return self

    def alive(self):
        return bool(self._t and self._t.is_alive())

    def stuck_in_send_s(self):
        """Seconds the CURRENT send has been in flight, 0.0 if none. A thread can be alive and
        useless — is_alive() is not liveness, it is only existence."""
        t0 = self._send_started_ts
        return (time.time() - t0) if t0 else 0.0

    def ensure_alive(self, log=None):
        """Restart the sender if it died. -> True if it had to be revived.

        A queue whose thread is gone accepts puts forever and delivers nothing: `put()` still
        succeeds, depth still grows, and every caller believes it posted. The only way to notice is
        to ask, so somebody has to ask — the worker's heartbeat does, once per beat.
        """
        stuck = self.stuck_in_send_s()
        if self.alive() and stuck < self.send_cap_s:
            return False
        if self.alive():
            # ALIVE BUT WEDGED. Abandon it: the thread is blocked in a socket read that no timeout is
            # going to end, and it holds no lock while it waits, so a fresh sender can take over the
            # queue immediately. The abandoned thread is a daemon and dies with the process — one
            # leaked thread per wedge is a far better outcome than a queue that never delivers again.
            self._log(f"POST QUEUE SENDER WEDGED IN A SEND for {stuck:.0f}s "
                      f"({self._send_what!r}) — abandoning that thread and starting a fresh sender. "
                      f"urllib's timeout is per socket operation, so a trickling or half-open "
                      f"connection can hold one POST open indefinitely.")
            self._send_started_ts = None
        self.revived += 1
        (log or self._log)(f"POST QUEUE SENDER WAS DEAD — restarting it (revival #{self.revived}, "
                           f"depth={self.depth()}, sent={self.sent} failed={self.failed}). Every "
                           f"queued lane was silent until now; directly-posted lanes were not, which "
                           f"is why this can hide behind healthy-looking transit counts.")
        self._t = None
        self.start()
        return True

    def stalled_for(self):
        """Seconds since the last successful send WHILE something is queued; 0.0 when idle/current.

        Distinguishes a stuck sender from a quiet one — an empty queue that has sent nothing for an
        hour is a quiet camera, not a fault."""
        nc, nd, nq = self.depth()
        if not (nc or nd or nq):
            return 0.0
        return time.time() - (self.last_sent_ts or self._started_ts)

    def stop(self, drain_s=5.0):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._t:
            self._t.join(timeout=drain_s)

    # ---- reporting ----
    def depth(self):
        with self._cv:
            return len(self._crit), len(self._drop), len(self._coalesced)

    def drain_counters(self):
        """Counts since the last call, and the current depths. Reset-on-read so a seg-timing line
        describes its own interval."""
        with self._cv:
            d = dict(self.dropped)
            for k in self.dropped:
                self.dropped[k] = 0
            sent, failed = self.sent, self.failed
            self.sent = self.failed = 0
            return {"dropped": d, "sent": sent, "failed": failed,
                    "depth": (len(self._crit), len(self._drop), len(self._coalesced))}

    def counters_str(self):
        c = self.drain_counters()
        nc, nd, nq = c["depth"]
        d = c["dropped"]
        return (f"posts=q:{nc}c/{nd}d/{nq}s sent={c['sent']} failed={c['failed']} "
                f"dropped=critical:{d['critical']} heartbeat:{d['heartbeat']} "
                f"floorcheck:{d['floorcheck']} status:{d['status']}")
