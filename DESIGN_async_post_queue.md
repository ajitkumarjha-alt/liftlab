# Design note — async POST queue (Stage 2)

Written before building because one instruction in the brief, applied literally, would silently
corrupt numbers we already publish.

## The verdict this rests on

`cpu=53-104 ms` against `post=274-1323 ms` on every camera: **90-96 % of the door cost is blocking
HTTP to the gateway.** Compute was never the constraint. `cv2.setNumThreads(1)` and cheaper-NCC are
dead; the async queue is data-mandated.

## THE AMBIGUITY — "drop-oldest" is not uniformly safe

The brief says drop-oldest under pressure with a visible counter. For `floorcheck` and
`analyzer_status` that is exactly right. For `door_event` it is not, and the reason is not
"we lose a row" — it is that **derived counts change**.

`gw_door_event` is not a set of independent observations. It is a STATE SEQUENCE, and three
consumers walk it:

* `dash_api._door_transition_census` — the cycle funnel, counting transitions;
* `_h3_cycle_ts` — the h3 cycle count IS a transition into `closed` from `closing` or `open`
  (there is no cycle marker column for h3 by design);
* tomorrow's 24 h attribution SQL.

So a dropped row does not thin the data, it rewrites it:

| dropped row | consequence |
|---|---|
| the `closed` row of a cycle | **the cycle disappears** from the h3 count and from the funnel |
| the `closing` row | `closing->closed` becomes `open->closed`; funnel stage counts shift |
| a `cycle`-carrying row (h2) | `close_travel_s` for that cycle is lost outright |

A silent uniform drop-oldest would therefore make the fleet look like it is doing less work under
load — precisely when it is doing more — and the counter in the seg-timing line would not tell a
consumer of the DATABASE anything, because they never see the journal.

## Proposed: two classes, one queue, differential policy

The emit gate already distinguishes them at enqueue time, so this needs no new inference:

```python
should, door_prev_key = door_gd.door_event_changed(door_prev_key, drec)
if should or (d_off - door_last_emit) >= DOOR_HB_S:
```

`should` is True on a state change **or** a completed cycle. The other branch is a liveness
heartbeat re-emitting an unchanged key.

| class | members | policy |
|---|---|---|
| **CRITICAL** | `door_event` where `should` is True (state change or cycle) | **never dropped** |
| **DROPPABLE** | `door_event` heartbeat re-emits, `floorcheck`, `analyzer_status` | drop-oldest, counted |

Dropping a heartbeat is safe by construction: it carries no information the previous row did not,
which is why the emit gate exists. Dropping a state change is not.

## What happens when the CRITICAL queue fills

This is the decision I most want confirmed, because every option costs something:

* **(a) block the frame loop** — restores the exact stall we are removing. Rejected.
* **(b) drop and mark the gap** — post a `seq` counter on every door_event; a consumer seeing a
  gap in `seq` knows the sequence is incomplete for that camera and can refuse to compute a funnel
  over it. Data stays honest; derived counts become *knowably* wrong rather than quietly wrong.
* **(c) spill to disk** — durable, ordered, survives a worker restart, and the gateway is the thing
  that is sick, not the disk. Costs a file format and a replay path.

**Recommendation: (b) now, (c) if the gateway stays sick.** (b) is small and makes the failure
visible in the database rather than only in a journal nobody greps. A `seq` gap is the DB-side
equivalent of the dropped-posts counter the brief asks for, and it is what makes tomorrow's
attribution SQL trustworthy — it can exclude cameras with gaps instead of averaging over them.

## Ordering

One sender thread per worker, FIFO. Order is preserved by construction, so no reordering can occur
between `ts` and insertion `id` — which matters because `dash_api` walks `ORDER BY ts, id`.

## Timestamps — already correct, must stay correct

`post_door_event` builds `{"ts": rec["t"], ...}` where `rec["t"]` is `d_off`, derived from the
segment clock, not from wall time at post. **The queue must carry the payload built at capture and
never restamp on send.** Verified in the current code; calling it out because a queue is exactly
where a well-meaning `ts=time.time()` gets added later.

## Visibility

Seg-timing line gains, per class:

```
posts=q:3/64 dropped=heartbeat:12 floorcheck:2 status:1 critical:0
```

`critical:0` is the line to watch. Any non-zero value there means derived counts are affected and
the `seq` gap will show it in the DB too.

## What this does NOT do

It decouples the worker from the gateway. **It does not cure the gateway.** After this ships, ch29
stops being over budget and the 0.3-10 s POST latency is still there, now invisible from the worker
side. That is work item 2, and it must not be dropped because the symptom stopped hurting us.

## Questions before I build

1. **Confirm the CRITICAL/DROPPABLE split** — specifically that heartbeat `door_event` re-emits are
   droppable. They are the largest droppable volume and the whole reason the queue can stay small.
2. **Confirm (b) `seq` gap marker** over (a) block or (c) disk spill.
3. `analyzer_status` — droppable is proposed, but the dash GPU card reads it for liveness, so a
   sustained drop makes a healthy worker look down. Acceptable, or should it be CRITICAL?
