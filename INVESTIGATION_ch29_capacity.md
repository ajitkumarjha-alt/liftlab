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

## Why ch29 specifically — ANSWERED by the per-camera data (2026-08-10, percam.txt)

Means over the same window, pid mapped to camera from the fleet's start lines:

| cam | pid | throughput | ratio | decode | track | **residual** | door |
|---|---|---:|---:|---:|---:|---:|---|
| ch16 | 865076 | 1545 ms | 0.77x | **320** | 460 | 765 | ON |
| ch27 | 865077 | 2024 ms | 1.01x | 145 | 485 | **1394** | ON |
| **ch29** | 865078 | **5311 ms** | **2.66x** | 179 | 597 | **4535** | ON |
| ch30 | 865079 | 1150 ms | 0.58x | 120 | 430 | 600 | ON |
| ch32 | 865080 | 1130 ms | 0.57x | 140 | 480 | 510 | off |
| ch34 | 2963946 | 1100 ms | 0.55x | 140 | 500 | 460 | off |
| ch37 | 865082 | 1180 ms | 0.59x | 150 | 525 | 505 | off |

**ch29's measured components are normal. Its residual is not.**

* `track` 597 ms vs a fleet range of 430–525 — only ~24 % above typical, nowhere near the 4.4x
  throughput gap. ch29 is *not* doing more YOLO work than the others.
* `decode` 179 ms is mid-pack; **ch16's decode is nearly double ch29's** (320 ms) and ch16 is fine.
* `fetch` 477–560 ms is mid-pack; ch16's is higher (579–766 ms) and ch16 is fine.
* The residual — throughput minus decode minus track — is **4535 ms against a healthy 460–765 ms**,
  roughly **9x the fleet median**.

**This kills the contention hypothesis.** Six workers share the same L4 and sit at 0.55–1.01x budget.
If the GPU were saturated, they would all be slow together. They are not; only ch29 is.

**The residual tracks `door=ON`.** Cameras with the door pass off run 460–510 ms of residual; the four
with it on run 600, 765, 1394 and 4535. ch27 — the second-worst camera and the only other one to
cross budget — is also the second-highest residual. That is consistent with the door/floor pass and
its synchronous POSTs being the cost centre, and `coldrestart.txt` shows one directly:

```
05:47:45  SLOW POST floorcheck: 5237ms (timeout=10s)      <- ch29
05:47:45  SLOW POST analyzer_status: 5204ms (timeout=10s) <- ch32
```

Five-second POSTs are happening, inside the segment loop, on the camera that is 2.7x over budget.
This is **strong narrowing, not proof** — the `door=`/`other=` split shipped here is what turns it
into a measurement. But the direction is now settled: the problem is not GPU compute, and the fleet
is not oversubscribed.

## CORRECTION to an earlier claim in this document

The first version of this document estimated the fleet ceiling at **3–4 cameras**, by taking ch29's
`track=598 ms` as representative and multiplying by seven against a 2000 ms budget. **The per-camera
data contradicts that and it should not be quoted.**

Six of seven cameras run at **0.55–1.01x budget** with `track` 430–525 ms each. Seven workers of
tracking summing to ~3.5 s of wall time per 2 s segment while all seven keep pace means `track_ms` is
wall time that is largely *not* GPU-busy — Python overhead, transfer, CPU-side ByteTrack — so it
cannot be summed across processes to derive a GPU ceiling. The arithmetic was wrong in kind, not just
in value.

What the data supports instead: **the fleet has headroom at seven cameras today.** ch16/30/32/34/37
sit near half their budget; ch27 is marginal at 1.01x mean and drifts over during busy stretches; ch29
alone is broken. A ceiling number needs a controlled ramp, not an extrapolation from one sick worker.

## Mitigation options — proposed, NOT applied. Aj decides.

Numbers are what today's logs support; the door/other split is unmeasured until the new instrumentation
runs, and **that split determines whether A or C helps at all**.

Re-ranked against the per-camera table. The options that tune YOLO are now the weak ones: ch29's
`track` is 597 ms of a 5311 ms segment, and six cameras prove the GPU is not the constraint.

| option | mechanism | expected saving on ch29 | confidence |
|---|---|---:|---|
| **D. take the synchronous POSTs off the segment loop** | `post_door_event` / `post_floorcheck` / transit POSTs run inline; a 5237 ms floorcheck POST is in the journal. Queue them to a worker thread | potentially most of the 4535 ms residual | **highest** — but confirm with `door=`/`other=` first |
| **B. priority tiers** | ch32/34/37 degraded or off | frees CPU and GPU, but the fleet is NOT saturated, so this treats a symptom ch29 does not have | low — the premise it was proposed under has gone |
| **A. per-camera `ANALYZE_FPS`** | `analyze_fps=12.5` → stride 2 | ~299 ms of a ~3311 ms overrun (**9 %**) | high on the number, **low on relevance** |
| **C. batch inference** | batch frames across cameras | up to ~597 ms if GPU-bound | **lowest** — largest change, targets 11 % of the problem, and the GPU is not the bottleneck |

**Recommendation: still measure first, but the target has moved.** One 10-minute window of the new
`door=`/`other=` split on ch29 and ch27 decides between "the door pass is expensive" and "the POSTs
are blocking". Both point at D, which is a change to *where* work happens rather than *how much* is
done — no data is lost, unlike A and B.

**ch27 needs watching independently.** At 1.01x mean it is already at the edge with a 1394 ms
residual, and it crossed budget repeatedly during the window (1.10–1.32x). It is the same defect
earlier in its progression, not a separate one.

## Standing items

1. **Watchdog cursor semantics.** `gpu_fleet.py:57` documents `cursor_{cam}` mtime as advancing "only
   when segments flow through the loop". It is written at `gpu_analyze.py:1138` whenever `new` was
   non-empty — including when every segment in it 404'd or timed out and was discarded
   (`seen.add(name); dropped += 1; continue`). So "segments flowing (cursor 1s old)" cannot
   distinguish *decoded* from *listed and thrown away*. Proposed: gate the cursor write on `segments`
   having advanced (frames actually decoded), so the floor's premise means what it says. **Not
   changed here** — it alters the meaning of a file the supervisor judges on, and deserves its own
   change with its own proof.
2. **Supervisor cold restart at 11:15 — ANSWERED, and it was not a fault.** `coldrestart.txt`:

   ```
   05:47:56  [gpu-fleet] signal 15 — stopping 7 worker(s)
   05:47:56  systemd[1]: Stopping liftlab GPU fleet ...
   05:47:57  systemd[1]: liftlab-gpu-fleet.service: Deactivated successfully.
   05:47:57  systemd[1]: Consumed 2h 33min 20.012s CPU time.
   05:47:57  systemd[1]: Started liftlab GPU fleet ...
   ```

   A deliberate `systemctl restart` (SIGTERM, clean shutdown, `Deactivated successfully`), not a
   crash and not a systemd `Restart=` recovery. `registry hash None` is simply a fresh process with
   no previous hash to compare against. It landed one second after the output floor restarted ch29,
   which is why ch29 appears to restart twice — the floor's restart, then the unit's.

3. **The supervisor does NOT adopt pre-existing workers.** `_procs` is populated only at
   `gpu_fleet.py:177`, inside `start()`, from the `Popen` handle; there is no `pgrep`, no `/proc`
   scan and no adoption path anywhere in the file. ch34's out-of-band pid (2963946 against a
   865076–865082 batch) is a worker the *same* fleet restarted mid-life — the fleet stopped it
   cleanly by pid on shutdown, which an unadopted orphan could not have been.

   The deploy hazard is real regardless of mechanism, and it is worse than adoption: if a supervisor
   ever dies WITHOUT running its shutdown, its `Popen` children are orphaned, a new supervisor starts
   a second worker per camera, and the orphans keep running the old code with no supervisor tracking
   them. Same symptom, different cause, and neither is detectable from outside the process. Hence the
   code-version check below rather than a documented ritual: it is indifferent to why the running
   code differs from disk.

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
