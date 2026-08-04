# INVESTIGATION — GPU segment coverage: what constrains it, and what would it take?

**Status: SCOPED, NOT FIXED.** Opened as its own item because it is the suspected upstream cause of
the door-cycle loss in `INVESTIGATION_door_cycle_coverage.md`. This is an infrastructure question,
not an engine change. **No change proposed yet — and one prerequisite must land before the causal
question can even be answered honestly (section 5).**

---

## 1. The mechanism: holes are DROPPED SEGMENTS, not sparse sampling

This is the single most important fact, and it reframes the problem.

| | value | source |
|---|---|---|
| segment duration | **2.0 s** | `SEG_DUR_S`, `gpu_analyze.py:46` |
| frames per segment | 50 (25 fps) | observed in `seg timing` logs |
| door pass cadence | every 2nd frame → **12.5 fps** | `DOOR_STRIDE=2`, `gpu_analyze.py:110` |
| → potential door observation every | **0.08 s** | within a fetched segment |

**Inside a segment the door is sampled 12.5 times a second.** That is roughly 25× finer than needed
to time a ~2.5 s close. Sampling density is not the constraint.

When a segment is dropped it is dropped **whole**: the door pass never runs on any of its 50 frames.
So every drop is a **2.0 second blind window** — the same order of magnitude as the event being
measured. C27's least-biased median close is **2.49 s**. **A single dropped segment can straddle an
entire door close.**

That prediction is confirmed by the observed gap distribution in the door investigation: p90 gaps of
4.96 s (ch27), 6.26 s (ch29), 6.33 s (ch30) are 2–3 consecutive dropped segments, and the median hole
on ch29's `open → closed` direct jumps is 9.26 s ≈ 4–5 segments.

**Consequence for the stride question in the brief: `DOOR_STRIDE` is NOT the lever.** Setting it to 1
doubles door-pass compute and does nothing to a 2 s hole. Do not touch it.

## 2. Where the drops come from — two paths, both segment-granular

Both are explicit and deliberate in `gpu_analyze.py`:

1. **Skip-to-live** (`:777-782`) — if more than `MAX_BEHIND=3` new segments are queued, the backlog is
   discarded to stay near the live edge: `dropped += lag - MAX_BEHIND`.
2. **404 / pruned** (`:806-809`) — the segment aged off the origin's rolling window before it was
   fetched. Not retried, by design ("move toward live").

Measured over 6 hours on the fleet:

```
prefetch WAIT EXCEEDED / fetch failed :  0        (the wedge path is not firing)
segment-lag log lines                 : 1097

lag distribution:   lag=1 → 777    lag=2 → 113    lag=3 → 63
                    lag=4 →  40    lag=5 → 104   ← a distinct second mode
```

Mostly healthy (lag=1, nothing dropped), **but a clear bimodal tail at lag=5**. At lag=5 with
`MAX_BEHIND=3`, two of every five segments are discarded *by policy* — a 40 % drop rate that is the
code working as written, not a failure.

## 3. Is the L4 the constraint? Partly — and not in the way the headline number suggests

Device-level, at 11:10 on 2026-08-04:

```
NVIDIA L4   15 % utilisation   2687 / 23034 MiB   7 workers
throughput=1868ms vs budget=2000ms -> 0.93x  FETCH-bound   drop_frac=4.49%
throughput=1598ms vs budget=2000ms -> 0.80x  COMPUTE-bound drop_frac=17.83%
throughput=1359ms vs budget=2000ms -> 0.68x  COMPUTE-bound drop_frac=0.96%
```

The device is **not** saturated: 15 % util, `track` ~550 ms of a 2000 ms budget.

But per-camera steady-state processing time tells a different story:

| cam | proc_ms | budget | ratio | drop_frac | door-cycle completion |
|---|---:|---:|---:|---:|---:|
| ch29 | 4769 | 2000 | **2.38× OVER** | 14.0 % | **17.5 %** (worst) |
| ch27 | 2547 | 2000 | **1.27× OVER** | 16.5 % | 39.9 % |
| ch30 | 1814 | 2000 | 0.91× | 4.5 % | 39.4 % |
| ch16 | 1846 | 2000 | 0.92× | 3.7 % | **24.2 %** |
| ch37 | 1058 | 2000 | 0.53× | 1.6 % | (no cycles) |
| ch32 | 1310 | 2000 | 0.66× | 1.4 % | (no cycles) |
| ch34 | 1336 | 2000 | 0.67× | 0.6 % | (no cycles) |

**Individual workers run over budget while the device sits at 15 %.** That points at per-worker
serialisation — one camera's fetch/decode/track chain not keeping pace — rather than L4 throughput.
Adding GPU capacity would not obviously help; the device is idle 85 % of the time.

Journal snapshots also show workers at 23–26 % and **four at exactly 66.7 %**, so drop rate varies
widely by worker and over time. It is not a single fleet-wide number.

### Correcting an earlier claim of mine

An earlier draft of the door investigation attributed the sampling holes to the fleet running
"3.8–4.8× over budget at drop_frac 66.7 %". That reading was taken minutes after I restarted all
seven workers to deploy `gpu_door.py`, so it overstated the steady state. **But my correction then
over-shot in the other direction**: 66.7 % workers are still present in the current journal, and ch29
is genuinely 2.4× over budget in steady state. The accurate statement is the bimodal one above —
mostly healthy, with a real and persistent bad mode — not "healthy" and not "uniformly broken".

## 4. Drops do NOT fully explain the door-cycle loss

This is the finding that most constrains what a fix can promise.

Since any drop during a close loses that close, `drop_frac` is roughly a per-close loss rate. If drops
were the whole story, completion would track `1 - drop_frac`. It does not:

* **ch16: 3.7 % drop, yet only 24.2 % completion.** Over 96 % of its segments arrive and it still
  misses three quarters of its closes.
* ch30: 4.5 % drop, 39.4 % completion — same drop rate, very different completion.
* ch29: 14.0 % drop, 17.5 % completion — far worse than 14 % would predict.

**So there is a second loss mechanism, and on ch16 it dominates.** Candidates, none tested:
per-camera ROI/geometry quality, the openness signal not crossing `near_open` on some cameras,
`door_event_changed` suppressing emissions, or the ch16 hardware fault already recorded in
`INCIDENT_ch16_floor_blind.md`. Driving `drop_frac` to zero would **not** by itself get completion
above ~40 %.

## 5. PREREQUISITE — the drop telemetry has no history

`analyzer_status` is **upserted, one row per camera**. Over a 7-day snapshot every camera has
`n_hb = 1`. There is no time series.

That means the obvious causal test — *does completion track `drop_frac` per camera over time?* —
**cannot currently be run at all.** Every number in section 3 is a single instantaneous sample, and
the 66.7 % figures exist only in journald, which rotates.

**This is the first thing to fix, and it is small.** Append drop/throughput telemetry to a history
table (or keep the upsert row and add an append-only companion) so that a week later the correlation
is answerable from data instead of from log scraping. Until then, any claim about the cause of the
door-cycle loss — including mine in section 3 — rests on snapshots.

## 6. What it would take, ranked

| # | change | attacks | cost | expected gain |
|---|---|---|---|---|
| 1 | **Append-only drop/throughput history** | nothing directly | very low | unblocks every question below; without it we are guessing |
| 2 | Raise `MAX_BEHIND` and/or deepen `PREFETCH_N` | skip-to-live (the lag=5 mode) | low, config | recovers the 40 %-drop mode if the backlog is transient |
| 3 | Investigate why ch29 is 2.4× over budget at 15 % device util | per-worker serialisation | medium | the single worst camera; likely fetch-path, not GPU |
| 4 | Deeper origin retention | 404 pruning | medium, cost $ | only if pruning is shown to be material — currently unmeasured |
| 5 | Shorten `SEG_DUR_S` below 2 s | blind-window *size* | medium, more requests | halves the hole per drop; does not reduce drop rate |
| 6 | **The second mechanism in section 4** | the ch16-shaped loss | unknown | probably the largest single gain, and least understood |

Note that **1 and 6 are the two that matter most, and neither is a throughput change.** The framing
in the brief — "L4 throughput, fetch path, or stride config" — resolves as: **not stride** (25× finer
than needed), **not L4 throughput** (device 85 % idle), **partly fetch path** (the lag=5 mode and
per-worker over-budget), **and substantially something else that is not yet identified.**

## 7. What NOT to do

* Do not lower `DOOR_STRIDE`. It doubles compute and cannot shrink a 2 s hole.
* Do not add GPU capacity on the strength of the 66.7 % number. The device is 85 % idle; the
  bottleneck is per-worker, and buying more L4 would leave it exactly where it is.
* Do not promise that fixing drops fixes cycle detection. Section 4 shows it does not.
