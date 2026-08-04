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

**That prediction is NOT confirmed by the gap distribution**, contrary to an earlier draft of this
document. Those gaps were measured between `gw_door_event` rows, which are emit-on-change — a long
gap means the state was stable, not that nothing was observed. See
`INVESTIGATION_ch16_second_mechanism.md` §1. The 2 s blind-window arithmetic above stands on its own
(it follows from `SEG_DUR_S` and `DOOR_STRIDE`), but it currently has **no supporting measurement**,
and the drop-vs-completion evidence in §4 runs against it.

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

Per-camera figures were reported here as steady state. **They were not** — see the correction below.
Current values (all workers with 1,200+ segments of uptime):

| cam | proc_ms | budget | ratio | drop_frac (cumulative) |
|---|---:|---:|---:|---:|
| ch27 | 2444 | 2000 | 1.22× over | 18.1 % |
| ch29 | 1573 | 2000 | 0.79× | 5.8 % |
| ch16 | 1832 | 2000 | 0.92× | 5.2 % |
| ch30 | 1832 | 2000 | 0.92× | 4.2 % |
| ch37 | 1384 | 2000 | 0.69× | 0.8 % |
| ch32 | 1183 | 2000 | 0.59× | 0.7 % |
| ch34 | 1373 | 2000 | 0.69× | 0.7 % |

Only ch27 is over budget, and modestly. The device is idle 85 % of the time.

### Correcting this section — twice over

**`drop_frac` is CUMULATIVE since process start** (`dropped / total`, `gpu_analyze.py:726`). It is not
an instantaneous rate, so a small denominator makes any value possible. Two claims I made from
single samples of it were wrong:

1. **"Four workers at exactly 66.7 %."** 66.667 % = **2 dropped of 3 segments** — a worker that had
   just restarted. Once every worker has non-trivial uptime, no worker is anywhere near it (table
   above, max 18.1 %). It was never a chronic condition.
2. **"ch29 runs 2.38× over budget in steady state."** A transient. ch29 now reads 0.79×.

Both readings, and the "restart storm" correction before them, came from instantaneous samples of a
cumulative counter with no history. That is exactly what §5 is about — and it is now fixed.

## 4. Drops do NOT fully explain the door-cycle loss

Still true, but the numbers have been corrected — see `INVESTIGATION_ch16_second_mechanism.md`, which
showed the original completion rates had a contaminated denominator (~31-46% of "episodes" are
sub-3s phantom excursions, not door cycles).

On corrected figures, excluding phantoms:

| cam | drop_frac | completion on real episodes |
|---|---:|---:|
| ch16 | 3.7 % | 13.9 % |
| ch30 | 4.5 % | 22.8 % |
| ch27 | 16.5 % | 20.1 % |

The argument survives the correction and in one respect gets stronger: **ch27 drops 4.5x more
segments than ch30 and still completes at a comparable rate (20.1% vs 22.8%)**, while ch16 drops the
least of the three and completes the worst. Segment loss and cycle completion are close to
uncorrelated across these three cameras.

The dominant loss is elsewhere and is fleet-wide: **over half of all `closing` states revert to
`open`** (50.4-58.5% across cameras) rather than completing to `closed`. Driving `drop_frac` to zero
would not move that.

## 5. PREREQUISITE — RESOLVED 2026-08-04: `worker_telemetry` is live

`analyzer_status` is an upsert, one row per camera, no history — which is why every figure in §3 was
an instantaneous sample and why two of them were wrong.

**Deployed 2026-08-04.** `worker_telemetry` appends the same heartbeat, throttled to one row per
camera per 60 s and pruned at 14 days. The upsert path the dashboard reads is untouched (asserted by
the deploy gate). All 7 cameras began populating within 5 s.

* growth: ~10,080 rows/day fleet-wide, ~114 B/row, plateauing at **~16 MB** at 14-day retention
* Litestream: replicates automatically (whole-file WAL shipping, no config change) — **but see the
  caveat below**
* the prune is per-cam so it matches the `ix_wtele` prefix: an indexed SEARCH at 0.005 ms rather than
  a 28–58 ms full scan on every append inside the request handler

**How to use it — the counters are cumulative.** Do not regress `drop_frac` directly. Difference
consecutive rows per camera and reset on an `uptime_s` decrease (that is the restart marker):

```sql
-- instantaneous drop rate between consecutive heartbeats
(dropped[t] - dropped[t-1]) / NULLIF(segments[t] - segments[t-1], 0)   -- skip rows where uptime_s dropped
```

**CAVEAT — the backup is currently not restorable.** While verifying that the new table replicates, a
restore failed at every timestamp tried:

```
cannot find max wal index for restore: missing initial wal segment:
generation=1950dae6522690d2 index=00005d8e offset=337872
```

Litestream is running and shipping WAL segments continuously, but the chain has a gap, so `litestream
restore` cannot complete. Pre-existing and unrelated to this deploy (the gap predates it). Underlying
causes visible in the journal: GCS API flakiness from this box (`oauth2: cannot fetch token`, TLS
handshake and i/o timeouts) hitting the retainer mid-delete, plus 7 `checkpoint: mode=PASSIVE err=
database is locked` failures. **gateway.db has no usable backup until a fresh generation is started.**
Tracked separately; not fixed here.

## 6. What it would take, ranked

| # | change | attacks | cost | expected gain |
|---|---|---|---|---|
| 1 | ~~Append-only drop/throughput history~~ **DONE 2026-08-04** | nothing directly | very low | shipped; §5 |
| 2 | Raise `MAX_BEHIND` and/or deepen `PREFETCH_N` | skip-to-live (the lag=5 mode) | low, config | recovers the 40 %-drop mode if the backlog is transient |
| 3 | ~~Why is ch29 2.4× over budget~~ **WITHDRAWN** — a transient; ch29 reads 0.79×. ch27 (1.22×) is the only one over budget | per-worker serialisation | medium | re-ask from `worker_telemetry` once a week of history exists |
| 4 | Deeper origin retention | 404 pruning | medium, cost $ | only if pruning is shown to be material — currently unmeasured |
| 5 | Shorten `SEG_DUR_S` below 2 s | blind-window *size* | medium, more requests | halves the hole per drop; does not reduce drop rate |
| 6 | ~~The second mechanism in section 4~~ **ANSWERED** — not a ch16 anomaly; the fleet-wide `closing -> open` reversion is the real loss | cycle yield | unknown | see `INVESTIGATION_ch16_second_mechanism.md` |

Note that **1 and 6 are the two that matter most, and neither is a throughput change.** The framing
in the brief — "L4 throughput, fetch path, or stride config" — resolves as: **not stride** (25× finer
than needed), **not L4 throughput** (device 85 % idle), **partly fetch path** (the lag=5 mode and
per-worker over-budget), **and substantially something else that is not yet identified.**

## 7. What NOT to do

* Do not lower `DOOR_STRIDE`. It doubles compute and cannot shrink a 2 s hole.
* Do not add GPU capacity on the strength of the 66.7 % number. The device is 85 % idle; the
  bottleneck is per-worker, and buying more L4 would leave it exactly where it is.
* Do not promise that fixing drops fixes cycle detection. Section 4 shows it does not.
