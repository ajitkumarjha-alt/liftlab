# INVESTIGATION — door cycle detection is missing most of the traffic

**Status: MEASURED, NOT FIXED.** No change proposed yet, as agreed. This gates per-floor demand,
C17/C18, transfer time, and — the part that matters most — it makes close-travel a measurement on a
**selected subset** of cycles rather than all of them.

**Headline: `close_th` is not the story.** The engine's own note (*"close_th too low, doors never
read fully shut"*) is contradicted by the data. The loss is upstream and it is a **sampling**
problem, not a threshold one.

---

## 1. Where openings die

Per opening episode, current era, 7 days:

| cam | openings | reached `open` | reached `closing` | **completed** |
|---|---:|---:|---:|---:|
| ch16 | 7,984 | 68.7 % | 24.3 % | **24.2 %** |
| ch27 | 17,349 | 65.8 % | 40.2 % | **39.9 %** |
| ch29 | 2,068 | 76.2 % | 17.7 % | **17.5 %** |
| ch30 | 9,307 | 65.7 % | 39.6 % | **39.4 %** |

**Read the last two columns together.** `reached closing` and `completed` are within 0.3 points of
each other on every camera. So **once a door reaches `closing`, it essentially always completes to
`closed`.** If `close_th` were too low we would see the opposite — many `closing` states stranded,
never reaching `closed`. We see none of that.

The loss is **`open` → `closing`**: two-thirds of openings on ch29, and a third on ch27/ch30, never
register a closing transition at all.

## 2. `close_th = 0.10` is reachable, and is reached

If the doors never read fully shut, openness would floor above `close_th`. It does not:

| cam | n | min | mean | < 0.10 | < 0.20 | < 0.30 |
|---|---:|---:|---:|---:|---:|---:|
| ch16 | 26,418 | 0.000 | 0.577 | 30.1 % | 30.9 % | 31.7 % |
| ch27 | 137,702 | 0.000 | 0.524 | 26.3 % | 30.5 % | 34.6 % |
| ch29 | 51,279 | 0.000 | 0.559 | 32.2 % | 35.2 % | 37.5 % |
| ch30 | 53,464 | 0.000 | 0.487 | 32.3 % | 37.0 % | 40.3 % |

Openness reaches 0.000, and sits below `close_th` a quarter to a third of the time. Note also how
little mass lies **between** 0.10 and 0.30 (ch16: 30.1 % → 31.7 %, i.e. 1.6 points across the whole
band). The signal is bimodal — mostly shut or mostly open — with very little time observed in
transit. That is the shape of a **sparsely sampled** fast transition, not a threshold set too low.

## 3. The mechanism: the closing motion is never observed

What follows an `open` state:

| cam | → `closing` | → **`closed` directly** | → `opening` | median gap on the direct jump |
|---|---:|---:|---:|---:|
| ch29 | 458 (25 %) | **1,346 (74 %)** | 22 (1 %) | **9.26 s** |
| ch27 | 11,712 (65 %) | **6,212 (34 %)** | 138 (1 %) | **2.28 s** |

**74 % of ch29's open states jump straight to `closed`, with a median 9.26 s hole between the last
`open` observation and the first `closed` one.** The entire closing motion — the thing
`close_travel_s` exists to measure — happened while the engine was not looking.

Observation cadence confirms it:

| cam | p50 | p90 | p99 | gaps > 2 s | gaps > 5 s |
|---|---:|---:|---:|---:|---:|
| ch16 | 1.32 s | 60.00 s | 60.78 s | 44.3 % | 27.2 % |
| ch27 | 0.40 s | 4.96 s | 36.06 s | 28.5 % | 9.9 % |
| ch29 | 0.16 s | 6.26 s | 60.07 s | 14.0 % | 11.1 % |
| ch30 | 1.52 s | 6.33 s | 60.03 s | 39.4 % | 13.5 % |

(The 60 s values are the idle heartbeat re-emit. The operative figure is the **> 2 s** column: a
door closes in roughly 2–3 s, so a gap of that order routinely straddles the entire descent.)

### The likely upstream cause, already measured elsewhere

The GPU fleet journal on 2026-08-04 shows every worker running **3.8×–4.8× over budget with
`drop_frac = 66.667 %`**:

```
seg timing: throughput=7946ms ... vs budget=2000ms -> 3.97x OVER-BUDGET
            COMPUTE-bound (GPU); drop_frac=66.667% dropped_total=2
```

Two-thirds of segments dropped produces exactly these holes. The chain is:

```
workers over budget -> 66.7% of segments dropped -> openness sampled with 2-9s holes
  -> the closing descent falls between samples -> 'closing' never observed
  -> no complete cycle -> cycle count is a fraction of real stops
```

This is a **throughput** problem surfacing as a door-detection problem. `close_th` is downstream of
it and blameless.

## 4. Why this matters more than coverage — the comparability question

Cycles complete only when the closing motion was sampled. **A slow close is more likely to be
caught by a sparse sampler than a fast one**, purely because it occupies more sampling opportunities.

So the completed-cycle pool is plausibly **biased toward slower closes**, and `close_travel_s` — the
study's headline C27 measurement, currently reported at a median of ~2.1–3.3 s against a 2.00 s
assumption — may be **biased high** by a mechanism that has nothing to do with the doors.

**This is a hypothesis with a stated mechanism, not an established result.** It has not been tested,
and it should be before the C27 finding is quoted further. A direct test: compare `close_travel_s`
against the observation density around each cycle. If well-sampled cycles show systematically faster
closes than sparsely-sampled ones, the bias is real and quantifiable.

## 5. What this gates

* **Per-floor demand** — a crossing needs a detected cycle carrying a floor. At 17–40 % completion
  the ceiling is ~10 % of crossings. See the FLOOR ATTRIBUTION sheet.
* **C17/C18** — trip segmentation needs stops; most stops are not observed as complete cycles.
* **Transfer time (C26)** — needs dwell against passenger load, on the same missing cycles.
* **C27 close-travel** — measured, but on a selected subset. Section 4.

## 6. To investigate next, in order

1. **Test the bias in section 4** before anything else. It is the only item that touches a figure
   already being reported.
2. Establish whether the GPU over-budget condition is the cause: does completion rate track
   `drop_frac` per camera and over time? ch29 has the worst completion (17.5 %) — check its
   throughput against the others.
3. Only then consider engine thresholds. On this evidence `close_th` should not be touched: it is
   reachable, it is reached, and closings that are observed complete almost without exception.

## 7. What NOT to do

Do not lower `close_th`. The engine's own note points there and the data says it is the wrong
target — the transitions are not being *observed*, so no threshold change can classify them. Acting
on that note would move a number without fixing anything, and would make the next investigation
harder by changing the era.
