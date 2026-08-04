# INVESTIGATION — door cycle detection is missing most of the traffic

**Status: MEASURED, NOT FIXED.** No change proposed yet, as agreed. This gates per-floor demand,
C17/C18, transfer time, and — the part that matters most — it makes close-travel a measurement on a
**selected subset** of cycles rather than all of them.

**SUPERSEDED IN PART — read `INVESTIGATION_ch16_second_mechanism.md` first.** Two claims below are
now known to be wrong: the completion rates in §1 have a **contaminated denominator** (~31-46% of
what is counted as an "episode" is a sub-3s phantom excursion, not a door cycle), and the
"sampling holes" mechanism in §3 misreads emit-on-change gaps as gaps in observation. Corrected
figures and the real mechanism are in that document.

**Headline: `close_th` is not the story, and C27 is NOT biased — see section 4.** The engine's own note (*"close_th too low, doors never
read fully shut"*) is contradicted by the data. The loss is upstream and it is a **sampling**
problem, not a threshold one.

---

## 1. Where openings die

Per opening episode, current era, 7 days. **CAVEAT: these denominators are contaminated** — see
`INVESTIGATION_ch16_second_mechanism.md` §3. Excluding sub-3s phantom episodes that never reach
`closing`, completion is ch16 13.9%, ch27 20.1%, ch30 22.8%, not the figures below.

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

## 3. The mechanism — PARTLY WRONG, see the correction at the end of this section

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

(The 60 s values are the idle heartbeat re-emit.)

**CORRECTION.** The table above does NOT measure observation cadence. `gw_door_event` is
emit-on-change: `door_event_changed` keys on `(floor, direction, door_state)` and **openness is not
in the key**, so a row is written on a state/floor/direction change or the 60 s heartbeat — never
per observation. Inside a fetched segment the door is examined at 12.5 fps whether or not anything
is emitted. These gaps are therefore intervals between **state changes**, and a long one is
consistent with a stable state that was observed continuously. ch16's 60 s p90 in particular
reflects its 98% floor `no_read` (few key changes), not a blind detector. The real funnel loss is
`open -> closing`, and the discriminator is in
`INVESTIGATION_ch16_second_mechanism.md` §4-5.

### The upstream cause — NOT yet established

An earlier draft blamed the GPU fleet running **3.8–4.8× over budget at `drop_frac = 66.7%`**. That
snapshot was taken minutes after I restarted all seven workers to deploy `gpu_door.py`, so it
overstated the steady state — but the correction over-shot too. The accurate picture is **bimodal**:
mostly healthy, with a real and persistent bad mode (ch29 runs 2.38× over budget in steady state;
workers at 66.7% are still present in the journal).

The mechanism is now understood: **holes are whole dropped 2 s segments, not sparse sampling.** Inside
a fetched segment the door is sampled at 12.5 fps — 25× finer than needed. Every drop is a 2.0 s
blind window, the same order as the ~2.5 s close it would have to contain. That is why the p90 gaps
above sit at 2–3 segment multiples.

**But drops do not fully explain this document's completion deficit.** ch16 drops only 3.7 % of
segments and still completes just 24.2 % of cycles — so a second mechanism exists and on ch16 it
dominates. Full analysis, the ranked options, and the telemetry prerequisite that currently blocks
the causal test are in **`INVESTIGATION_gpu_segment_coverage.md`**.

## 4. C27 SAMPLING BIAS — TESTED, AND C27 SURVIVES

**Result: the bias hypothesis is NOT supported. C27 stands as reported.**

The hypothesis was that a slow close is more likely to be caught by a sparse sampler than a fast
one, so the completed-cycle pool would skew slow and the C27 median would be biased high.

### The first metric was circular — recording it so it is not repeated

Correlating `close_travel_s` against the largest sampling hole measured *inside* the cycle window
gave a strong-looking result: Spearman **rho = +0.352**, with stratified medians climbing
1.882 → 2.607 → 4.121 → 5.981 s from densest to sparsest quartile. It looked like a large bias.

It is an artefact. A longer close means a longer window, which mechanically gives a large hole more
opportunity to occur inside it:

```
Spearman rho(in-cycle max gap, cycle duration) = +0.887
```

The metric is nearly a restatement of the thing it was supposed to predict. **Do not quote the
in-cycle numbers, including the per-camera ones below** — they are all contaminated the same way.

### The controlled test

Sampling density measured in a **fixed 60 s window ending when the cycle opens** — the same length
for every cycle, and causally prior to the close it is being correlated with:

| ambient max hole before the cycle | n | median close_travel | 95% CI |
|---|---:|---:|---|
| 0.08–7.60 s (densest) | 1,121 | 2.488 s | [2.305, 2.721] |
| 7.61–10.39 s | 1,121 | 2.689 s | [2.484, 2.915] |
| 10.39–15.99 s | 1,121 | 2.686 s | [2.464, 2.987] |
| 15.99–59.50 s (sparsest) | 1,121 | 2.420 s | [2.264, 2.688] |

```
Spearman rho(ambient gap, close_travel_s) = -0.019
Spearman rho(ambient n,   close_travel_s) = -0.033   (would need to be clearly negative)
```

**Flat.** No monotone trend, deltas within ±0.2 s, CIs overlapping throughout, and both correlations
indistinguishable from zero. Cycles occurring during sparsely-sampled periods do **not** have longer
closes than cycles during densely-sampled ones.

### Why the mechanism did not bite

Because the failure mode is **omission, not distortion**. `close_travel_s` is stamped from the
tracker's own state crossings — `close_start` at the near_open crossing, `close_full` at `close_th`.
When samples are missing, the cycle usually fails to complete **at all** (which is exactly the
coverage loss in sections 1–3) rather than completing with an inflated duration. Dropped samples
remove cycles from the pool; they do not stretch the survivors. Among cycles that did complete, the
descent *was* observed, and the measurement is sound.

**C27 is a measurement on a smaller pool than it should be, but not a biased one.** Coverage and
comparability are separable here, and only coverage is damaged.

Residual caveat, stated honestly: this rules out the *proposed* mechanism — ambient sampling density
predicting close duration. It does not prove the completing cycles are representative in every other
respect. But the specific reason to doubt C27 has been tested and did not hold.

## 5. What this gates

* **Per-floor demand** — a crossing needs a detected cycle carrying a floor. At 17–40 % completion
  the ceiling is ~10 % of crossings. See the FLOOR ATTRIBUTION sheet.
* **C17/C18** — trip segmentation needs stops; most stops are not observed as complete cycles.
* **Transfer time (C26)** — needs dwell against passenger load, on the same missing cycles.
* **C27 close-travel** — measured, but on a selected subset. Section 4.

## 6. To investigate next, in order

1. ~~Test the sampling bias.~~ **DONE — section 4. C27 survives; the pool is smaller, not skewed.**
2. ~~The second loss mechanism on ch16.~~ **DONE — `INVESTIGATION_ch16_second_mechanism.md`.**
   The premise did not hold: ch16's signal is healthy, ~46% of its "episodes" were phantoms, and
   the corrected deficit (13.9% vs peers' 20-23%) is modest. The dominant loss is FLEET-WIDE —
   over half of all `closing` states revert to `open`, on every camera.
3. **Land append-only drop telemetry** before attempting the drop-vs-completion correlation.
   `analyzer_status` is upserted — one row per camera, no history — so that correlation cannot
   currently be computed at all. See §5 of the same document.
4. Only then consider engine thresholds. On this evidence `close_th` should not be touched: it is
   reachable, it is reached, and closings that are observed complete almost without exception.

## 7. What NOT to do

Do not lower `close_th`. The engine's own note points there and the data says it is the wrong
target — the transitions are not being *observed*, so no threshold change can classify them. Acting
on that note would move a number without fixing anything, and would make the next investigation
harder by changing the era.
