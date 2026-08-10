# ch29 capacity stall — 2026-08-10

Worker pid 865078, journal 05:20–05:47 UTC (10:50–11:17 IST). Not a wedge and not a resume defect:
the worker was running correctly and could not keep pace. Restarts cannot fix throughput, which is
why the output floor restarted it and the symptom returned.

## The loop

```
over-budget (2.3–2.7x)  ->  fall behind live  ->  skip-to-live drops segments
        ->  SEGMENT-CLOCK GAP (55–76s)  ->  rebuild tracker + counter, drop open episode
        ->  reload TensorRT engine (41 MiB)  ->  over-budget ...
```

Resets #2/#3/#4 at 05:20:37, 05:23:01, 05:25:27 — one every ~2.4 minutes, each reloading the engine
inside the segment loop. Transits are lost to churn, not to a stall.

## Measured, from the nine `seg timing` samples in the window

| | mean | range |
|---|---:|---:|
| throughput (`seg_ms`) | **5016 ms** | 4617–5358 |
| budget | 2000 ms | — |
| ratio | **2.51x over** | 2.31–2.68 |
| decode | 180 ms | 170–189 |
| track (YOLO, whole segment) | 598 ms | 587–608 |
| fetch | 532 ms | 512–560 |
| drop_frac | 26.9 % | 26.8–27.2 |

Throughput **degraded across the window**, 4617 → 5358 ms (+16 % in six minutes). It was not steady
state; it was getting worse.

## The finding that changes what mitigation is worth doing

`decode + track = 778 ms` of a measured `5016 ms`. Adding fetch in full — generous, since the comment
at :871 says the fetch overlaps the prior track — reaches `1310 ms`.

**~74–84 % of ch29's per-segment cost is not measured by the line being used to diagnose it.**

The `COMPUTE-bound (GPU)` verdict in that line is `"FETCH-bound" if fm > tm else "COMPUTE-bound (GPU)"`
— a two-way compare between fetch and track, which together are ~22 % of the segment. It announces
GPU-compute because 598 > 532, while ~4 s per segment sits unattributed. The label is not wrong about
`track > fetch`; it is not evidence about what dominates the segment.

This matters for every option below: **YOLO is 12 % of the overrun.** Eliminating tracking entirely
would leave ch29 at roughly 2.2x over budget.

### What is in the unmeasured 4 s — candidates from the code, not yet measured

* the **door/floor pass** (`gpu_analyze.py:942`): CPU Sobel + per-cell NCC with ±2px shift search,
  run every `DOOR_STRIDE` frames — 25 times per 50-frame segment on ch29 — and **not** included in
  `decode_ms` or `track_ms`;
* **`post_door_event`** on every state change plus a 60 s heartbeat, and **`post_floorcheck`** with a
  base64 JPEG crop 30x/hour — synchronous HTTP inside the frame loop;
* transit/validation POSTs and `capture_seq` image encoding;
* blocking `prefetched.pop(name).result()` wait not covered by the overlap.

Instrumentation for exactly this shipped with this commit: the seg-timing line now carries
`door=` and `other=`, and names the largest measured component instead of a two-way verdict —
saying explicitly when the unexplained residual is the biggest thing in the segment.

## Why ch29 specifically — NOT ANSWERED, and the measurement to answer it

I have timing for ch29 only. All seven workers start with `analyze_fps=0.0` (every frame tracked) and
`stride=2`; ch16/27/29/30 have `door=ON`, ch32/34/37 `door=off`. ch29 additionally carries
`door_levels={'close_th': 0.2}`, which moves its door era but is not obviously a throughput cost.

One command gives the per-camera table, because worker lines carry a pid and the fleet lines map pid
to camera:

```bash
journalctl -u liftlab-gpu-fleet --since '2026-08-10 05:20' --no-pager \
  | grep -E 'started pid=|seg timing'
```

**A caveat that the fleet-wide numbers will still not resolve:** ch29's `track=598 ms` was measured
*under seven-camera contention*. If the GPU is oversubscribed, that number is partly caused by the
other six, so "ch29 is intrinsically heavy" and "the fleet is oversubscribed" cannot be separated
from steady-state logs alone. Separating them needs a controlled run (e.g. ch29 alone for 5 minutes).

### Fleet headroom, with the same caveat

At 12.0 ms/frame (598 ms ÷ 50 frames) and 50 frames per 2 s segment, one camera's YOLO alone needs
**598 ms of a 2000 ms budget — 30 % of real time.** Seven cameras of *tracking alone* is ~2.1x the
L4's real-time capacity, which puts the ceiling nearer **3–4 cameras** at full frame rate than the
documented ~5 — before any door pass, POST or decode cost. Measured under contention, so treat as an
upper bound on badness, not a calibrated ceiling.

## Mitigation options — proposed, NOT applied. Aj decides.

Numbers are what today's logs support; the door/other split is unmeasured until the new instrumentation
runs, and **that split determines whether A or C helps at all**.

| option | mechanism | expected saving on ch29 | confidence |
|---|---|---:|---|
| **A. per-camera `ANALYZE_FPS`** | already supported (`gpu_analyze.py:906`); `analyze_fps=12.5` → stride 2 | ~299 ms of a ~3016 ms overrun (**10 %**) | high on the number, low on sufficiency |
| **B. priority tiers** | ch27/29/30 real-time; ch32/34/37 degraded or off. Frees GPU *and* CPU | unknown until the per-camera table exists | medium |
| **C. batch inference** | batch frames across cameras in one process | up to ~598 ms if GPU-bound | low — largest change, targets the 12 % |
| **D. move the door pass off the segment loop** | it is CPU work serialised with GPU work | unknown, possibly large | unknown until `door=` is measured |

**Recommendation: none of them yet.** Ship the instrumentation, take one 10-minute window of the new
seg-timing line across all seven, then choose. A, C and D each target a different quarter of the
segment, and today's logs cannot say which quarter is the problem. Option B is the only one that is
safe to reason about without it, because reducing camera count reduces every component at once.

## Standing items

1. **Watchdog cursor semantics.** `gpu_fleet.py:57` documents `cursor_{cam}` mtime as advancing "only
   when segments flow through the loop". It is written at `gpu_analyze.py:1138` whenever `new` was
   non-empty — including when every segment in it 404'd or timed out and was discarded
   (`seen.add(name); dropped += 1; continue`). So "segments flowing (cursor 1s old)" cannot
   distinguish *decoded* from *listed and thrown away*. Proposed: gate the cursor write on `segments`
   having advanced (frames actually decoded), so the floor's premise means what it says. **Not
   changed here** — it alters the meaning of a file the supervisor judges on, and deserves its own
   change with its own proof.
2. **Supervisor cold restart at 11:15.** Fleet pid `865073` → `3426561` one second after the floor
   restarted ch29, with `registry hash None -> d224b489940e` (a cold start, not a reload) re-spawning
   all seven. ch29 was restarted twice in three seconds. Cause not in the filtered journal; the
   unfiltered window would show a traceback or a systemd restart:

   ```bash
   journalctl -u liftlab-gpu-fleet --since '2026-08-10 05:47' --until '2026-08-10 05:49' --no-pager | head -80
   ```

## Data integrity — fixed in this commit

05:24:41 posted a transit with no evidence behind it:

```
episode opened at 1786339472 (live)
seg1810.ts: +1 transits (cum boarded=0 alighted=1, posted=36)
episode attempt (gap-between-segments): boarded=0 alighted=1 imgs=0 span=0s
episode dets: per-frame max=0 mean=0.0 over 0 frames; distinct track_ids=0; conf 0.00/0.00/0.00
episode POST -> HTTP 200
```

Zero frames, zero distinct tracks, zero detections, zero span — landed in `validation_item`
indistinguishable from a counted observation. Under churn a transit already latched in the counter is
flushed into a freshly-opened episode that never saw a frame: the count is a residue of destroyed
state, not a measurement.

`post_episode` now refuses any episode with no analysed frames or no distinct track_ids, logs the
refusal with the claimed counts, and returns. **A lost transit is honest; an invented one is not.**
The gate does not touch the churn — that is the capacity work above.
