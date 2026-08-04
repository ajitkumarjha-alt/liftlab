# Gateway performance — /dash and /ops on liftlab-cloud

**Status (2026-08-04): /dash is OFF the request path and returning 200 in under a second warm.**
Live `dash_api.py` = `1f79edb6`, branch `gw-timeout`.

| # | Change | State |
|---|---|---|
| 1 | Server-side request budget on `/dash/{gw}/data` | **DEPLOYED** |
| 1b | Client in-flight guard + failure cap + backoff | **DEPLOYED** |
| 2a | Window the era reads in SQL | **DEPLOYED** |
| 2b | Era census cached per (gw,cam), bounded key space | **DEPLOYED** |
| 2c | Floor alphabet derived on a schedule, persisted | **DEPLOYED** (stage 1 of the precompute job) |
| 2d | Per-camera aggregates precomputed off the request path | **DEPLOYED** |
| 3 | `/ops` indexes (`apply_gwindex.sh`) | held, **re-measure first — probably unnecessary** |

`/dash/{gw}/data` went from **never terminating** (000 at 600s) to a median of **0.77s**.

---

# Step 2 results (2026-08-04) — all three pieces built, acceptance NOT met

**Deployed and live: (a) + (b). Rolled back: (c).** Live `dash_api.py` = `1fc1a9c5`.

## The measurement that reframes step 2

Windowing was expected to be the main lever. It is not, and the reason is the data shape:

```
gw_door_event spans 2026-07-20 .. 08-04 — about 15 days total
  last 1d:   84,890      last 7d:  399,939
  last 3d:  200,553      all:      459,436
```

**A 7-day window is 87% of the table.** And the sweep, run with (a) live, is unambiguous:

```
days=7    503 @ 35.3s      days=0.5   503 @ 25.5s
days=3    503 @ 25.5s      days=0.25  503 @ 32.3s
days=1    503 @ 36.7s      days=0.1   503 @ 25.5s
```

Shrinking the window **70x changed nothing**. So at that point the windowed reads were a negligible
share of the cost, and the two unwindowed reads were essentially all of it.

## Per piece

**(a) window the era reads — DEPLOYED.** Pushed a `ts` floor into `_door_gpu_by_cam`, the `_tier2`
main read, and `_door_transition_census`. `_tier2` previously took `t0/t1` and applied them by
rebuilding the list *after* fetching everything — the exact materialise-then-slice removed here.
Plans confirmed: `SEARCH gw_door_event USING INDEX ix_door_event (gateway_id=? AND cam=? AND ts>?)`
— **the existing index already serves this shape; no new index needed.**
Result: no measurable change on its own, for the reason above. It still earns its place: all-history
grows without bound, a window does not.

**(b) era census cached per (gw,cam) — DEPLOYED.** TTL 15m, single-flight, key space bounded at 32
because `gw` is a path parameter (verified: 200 caller-supplied `gw` values -> 8 entries at a test
bound of 8). Result: warm-census consecutive requests were still 503 @ 39.4 / 26.0 / 35.5 / 25.8s.

**(c) alphabet on a schedule, persisted — BUILT, VERIFIED, ROLLED BACK.** The job itself works:

```
ch16: 49 floors from  19,425 rows  era=e79e50d3h2       13.26s
ch27: 11 floors from  68,513 rows  era=425f92e1h2        9.02s
ch29: 69 floors from 141,439 rows  era=260d4a0fh2Laa52  11.49s
ch30:  6 floors from  17,884 rows  era=661a1fa0h2        1.76s
                                          7 ok, 0 failed, 35.5s
```

A read never derives — `apply_gwalphabet.sh` refuses to install if `_tier2` still references
`_derive_floor_alphabet`. Ordering is enforced: an empty table means `alphabet=None` = "accept all
floors", a behaviour change, so the job runs and the table is verified populated *before* the new
module is allowed to serve. Stored with `derived_at`, `evidence_rows`, `era`, `door_version`, and
`/dash` surfaces `era_mismatch` when the alphabet's era differs from the metrics' era.

Rolled back because the gate required at least one 200 in five and got **503 @ 25.3 / 30.5 / 30.0 /
33.8 / 36.0s**.

## Why the bar is still not met

With the census and alphabet costs removed, the **windowed era reads became the bottleneck** — and
the window barely bounds them, because 7 days is 87% of the data. Roughly 400k rows per request are
still walked in Python for the flap/reopen state machine and the stops walk, which are sequential
and do not reduce to SQL aggregates cleanly.

## Options for the remaining gap — needs a decision, none taken

1. **Precompute the per-camera aggregates on a schedule**, exactly the pattern (c) just proved:
   `alphabet_job` did 7 cameras in 35.5s off the request path. Extending it to `door_gpu`/`tier2`
   would take the whole walk off the request path and preserve every number. Largest change; most
   likely to actually reach 2s.
2. **Shorten the default window** to ~1-2 hours. Cheap, but it CHANGES WHAT THE NUMBERS MEAN — a
   close-travel median over 2 hours is a different statistic from one over 7 days. A product call.
3. **Raise the budget** and accept a slow-but-terminating page. Does not meet the 2s bar.

## Acceptance

| Criterion | State |
|---|---|
| `/dash/{gw}/data` under 2s | **NOT met** — 503 at the 25s budget |
| RSS flat under sustained refresh | **met** (199 MB peak / 178 MB settled, 3 concurrent streams) |
| Zero OOM kills over 24h | **on track** — none since 2026-08-03 18:00 (~10.5h at time of writing) |

## Step 3 — held, and probably unnecessary

`/ops` re-measured on a quiet gateway is **5.5s**, not the contaminated 14.1s. More importantly, the
windowed `gw_door_event` queries now plan as `ix_door_event (gateway_id=? AND cam=? AND ts>?)` using
the EXISTING index. Before running `apply_gwindex.sh`, re-measure `/ops` against 5.5s and check
whether its floor-collapse queries still enter the index on `gateway_id` alone. Do not add write
cost at ~3.5 writes/s for an index the query no longer needs.

## Leftovers on the box

`floor_alphabet` (7 rows) and `alphabet_job.py` remain installed but unused after the rollback.
`liftlab-alphabet.timer` was **disabled** — my rollback path did not remove the timer it installed,
so it would otherwise have kept running a 35s job every 30min for a table nothing reads. Re-enable
with `systemctl enable --now liftlab-alphabet.timer` when (c) goes back in.


---

# Final acceptance (2026-08-04)

## The architecture that worked

`door_gpu` and `tier2` are precomputed into `door_aggregate` by `precompute_job.py` on a 1h timer
(niced, idle I/O). `_dash_data_inner` performs **no live walk** — `apply_gwprecompute.sh` statically
refuses to install if it still calls `_tier2` / `_door_gpu_by_cam` / `_transits_for_join`.

Keyed `(gateway_id, cam, counting_version, door_version, window_days)`; a read must match the current
values exactly, so an aggregate from another era or window reads as "not computed for the current
era/window" and never as data. Pending is explicitly distinguished from a genuine zero — the Tier-2
ceiling panel is only claimed when every camera has a COMPUTED aggregate that had nothing to report.

One ordered job, not two timers: stage 2 depends on stage 1, because `_tier2` reads the stored
alphabet to decide admissible floors. The half-installed `liftlab-alphabet` units were retired.

## Measured

```
A. sequential x5 (warm)     2.23, 0.73, 0.78, 0.52, 0.77 s   all 200   median 0.77s
B. 3 concurrent streams x6  all 18 -> 200, 0.74 - 2.75 s
   RSS  77.9 MB -> 85.8 MB  (+7.9 MB across 18 concurrent requests)
   MemAvailable held 1.21 GB
```

| Criterion | Verdict |
|---|---|
| `/dash/{gw}/data` under 2s | **Met warm** (median 0.77s). NOT met for the cold first request after idle (2.23s) or under 3-way concurrency (up to 2.75s). |
| RSS flat under sustained refresh | **Met** — +7.9 MB, against 1.48 GB and an OOM before. |
| Zero OOM kills over 24h | **On track, not yet proven** — none since 2026-08-03 18:00 (~12h at time of writing). Re-check after 24h. |

## A prediction of mine that was WRONG — recorded so it isn't repeated

I expected the `itertools.islice` fix to cut ch27's outlier precompute time. **It did not.**

```
before islice:  ch27 145,828 rows -> 303.5s
after  islice:  ch27 145,906 rows -> 370.0s
comparison:     ch29  78,600 rows ->  26.7s
```

ch27 has 1.9x ch29's rows and ~14x its time, and removing the quadratic ALLOCATION did not move it.
So the tail-slice was never ch27's problem; something else in `_tier2` is superlinear on ch27's data
specifically — the per-floor join re-scans `transits` from `ti` for each stop, so heavily OVERLAPPING
door windows would make it O(stops x transits-in-window). That is the next thing to profile, and it
should be profiled rather than predicted — this is the third hypothesis in this file that measurement
overturned.

This is a background-job cost, not a request-path cost, so it does not affect the acceptance bar.
It does set the job's runtime (~533s) and therefore its duty cycle: at 1h that is ~15%.

## Step 3 — still held, and the case for it has weakened further

`/ops` was 5.5s on a quiet gateway, and the earlier 14.1s was contaminated by stranded `/dash`
requests. The windowed `gw_door_event` queries now plan onto all three columns of the EXISTING
`ix_door_event`. Re-measure `/ops` against 5.5s before running `apply_gwindex.sh`; do not add write
cost at ~3.5 writes/s for an index the query no longer needs.
