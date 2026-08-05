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
| 3 | `/ops` indexes (`apply_gwindex.sh`) | **DROPPED — not deferred** (see Decisions closed) |

`/dash/{gw}/data` went from **never terminating** (000 at 600s) to a median of **0.77s**.

## Decisions closed (2026-08-04) — do not relitigate these

**`liftlab-alphabet.timer` is RETIRED. Do not recreate it.** The floor-alphabet derivation is
**stage 1 of the single ordered `liftlab-precompute` job**, not its own timer. This was a deliberate
deviation from an instruction to re-enable the alphabet timer, and it was **reviewed and accepted**:
stage 2 (`aggregate_refresh`) depends on stage 1, because `_tier2` reads the stored alphabet to
decide which floor reads are admissible. Two independent timers could race and bake a
missing-or-stale-alphabet aggregate into a cached result that then serves reads. One job enforces the
ordering; two timers only hope for it. If you find yourself about to add a second timer, don't.

**Step 3 (`apply_gwindex.sh`) is DROPPED, not deferred.** `/ops` is 5.5s on a quiet gateway, and the
windowed `gw_door_event` queries already plan onto all three columns of the EXISTING `ix_door_event`.
That does not justify write cost at ~3.5 writes/second. The script is left in the tree for its
reasoning; if the case ever changes, **re-derive it from fresh measurement rather than resurrecting
this proposal** — the numbers it was originally argued from (the 14.1s `/ops` baseline) were
contaminated by stranded `/dash` requests and are not a valid starting point.

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

## Step 3 — DROPPED (superseded; kept for the reasoning only)

`/ops` re-measured on a quiet gateway is **5.5s**, not the contaminated 14.1s. More importantly, the
windowed `gw_door_event` queries now plan as `ix_door_event (gateway_id=? AND cam=? AND ts>?)` using
the EXISTING index. Before running `apply_gwindex.sh`, re-measure `/ops` against 5.5s and check
whether its floor-collapse queries still enter the index on `gateway_id` alone. Do not add write
cost at ~3.5 writes/s for an index the query no longer needs.

## Leftovers on the box

`floor_alphabet` (7 rows) and `alphabet_job.py` remain installed but unused after the rollback.
`liftlab-alphabet.timer` was **disabled** — my rollback path did not remove the timer it installed,
so it would otherwise have kept running a 35s job every 30min for a table nothing reads. Re-enable
**DO NOT re-enable `liftlab-alphabet.timer` — it no longer exists and must not be recreated.**
See "Decisions closed" at the top of this file: the alphabet is now stage 1 of the single
ordered `liftlab-precompute` job.


---

# Occupancy — computed, measured, and deliberately NOT shipped (2026-08-04)

Recorded so it is not re-attempted from scratch.

`model.occupancy_periods()` exists and is correct: cumulative boarded-minus-alighted, **reset at
every idle gap** (default 10 min) and at every `counting_version` change, reporting per period the
camera's precision, the crossings accumulated, and an implied error bound. Negative values are
recorded via `went_negative` / `min_raw` and clamped only for display.

**It is not rendered anywhere, because of what it measures:**

```
569 periods over 7 days.  242 went NEGATIVE (42.5%).  Worst raw value: -24 (ch27)

ch29 116 periods / 65 negative      ch27  81 / 47
ch16  90 periods / 35 negative      ch30  75 / 28
```

A negative occupancy is proof the derivation broke — more people left the car than entered it. At
**42.5 %** that is not an edge case to footnote, it is the typical outcome. The clamp would be
hiding a broken result more often than a rounding artefact.

The root cause is not a bug in the derivation: the system counts **door crossings, not occupancy**,
at 83–95 % per-camera precision, and the estimate is a *difference* of two error-prone counts, so
the errors add rather than cancel. Resetting at idle gaps bounds the drift; it does not fix the
arithmetic.

**Do not ship an occupancy figure until crossings are materially more accurate.** The FLOOR
ATTRIBUTION sheet carries the finding (the 42.5 % rate) so the rejection is visible in the
workbook without any occupancy number appearing in it.


---

# Verification gates — the pattern that cost four incidents

Four failures in one week, all the same shape: **the gate proved something adjacent to what
mattered, and passing it meant less than it appeared to.**

| # | The gate | What it actually proved | What shipped anyway |
|---|---|---|---|
| 1 | `smoke-import` in `apply_dashperf.sh` | that the **installed** module imports | it never loaded the staged file at all — `cd $APP` puts the CWD ahead of `PYTHONPATH` for `-c`, so it validated code it had never read |
| 2 | the same smoke-import | that *a* router attribute exists | `door_router` vs `door_event_router` — caught by hand before deploy, not by the gate |
| 3 | `bash -n relay_soak.sh` | that the file **parses** | `NIC_MODULE_LOADABLE` read at line 376, assigned at 518 — under `set -u` a crash 3s after launch, crash-looped by systemd |
| 4 | `smoke-import` in `apply_gwingest.sh` | that `ops_api` **imports** and routes register | `_f()` undefined inside the handler body — Python resolves names at call time, so every relay heartbeat 500'd for six minutes |

Two sentences carry all four:

> **import != executes.  parse != starts.**

A module that imports proves nothing about whether its handlers run. A script that parses proves
nothing about whether it starts. In every case the gate exercised a *cheaper* property than the one
being claimed, and the gap between them is exactly where the defect lived.

## The rule

**Every gate must exercise the thing it claims to verify**, and the test for a gate is:

> *Would it have caught the last failure of this kind?*

Not "is it green" — green was never the problem. All four gates were green. The question is whether
the gate is *sensitive* to the failure it is standing in front of, and the only way to know is to
break the code deliberately and watch the gate fail.

**Every gate in this repo has now been demonstrated against a deliberately broken copy**, and the
demonstration is part of the commit rather than a claim in a comment:

* `smoke_relay_start.sh` — runs the real `relay_soak.sh` to its first loop turns with the outside
  world stubbed. On a copy with the unbound variable reintroduced: `bash -n` **passes**, all 56 unit
  tests **pass**, and the smoke **fails with 9 errors** naming the variable and the line.
* `smoke_ingest.sh` — starts a scratch uvicorn from the **staged** directory against a scratch DB
  and POSTs a realistic payload to every producer-facing ingest endpoint. On a copy with the `_f()`
  NameError reintroduced: the import gate still prints **"smoke ok"**, while the ingest gate returns
  **HTTP 500** and quotes `NameError: name '_f' is not defined`.

## Two assertions, not one

`smoke_ingest.sh` asserts **2xx AND that the row landed**. A handler returning 200 while silently
swallowing the write is the same defect class as `_q()` returning `[]` on `OperationalError`: a
success signal over missing data. That one has already bitten this codebase once — it is why a
timeout used to render as an empty dashboard panel instead of an error — so the gate does not accept
a status code as evidence that anything was stored.

## Gates run PRE-INSTALL, against the staged file

Both smoke tests run before anything is written to the live path, so a failure costs nothing: the
live file is untouched and the running service never restarts. A post-install check fires *after*
the damage and is not a gate, it is a post-mortem. `smoke_ingest.sh` proves it is testing the right
code by asserting `module.__file__` starts with the staged directory — failure #1 above is precisely
what happens when nothing checks that.

The one architectural constraint worth recording: FastAPI's `TestClient` would be tidier than
spawning uvicorn, but it needs `httpx`, which is not installed on the gateway. **A deploy gate must
not install packages on a production box in order to run itself**, so the gate spawns a scratch
uvicorn on a high port instead, using only what is already there.


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

## Step 3 — DROPPED (2026-08-04)

`/ops` was 5.5s on a quiet gateway, and the earlier 14.1s was contaminated by stranded `/dash`
requests. The windowed `gw_door_event` queries now plan onto all three columns of the EXISTING
`ix_door_event`. Re-measure `/ops` against 5.5s before running `apply_gwindex.sh`; do not add write
cost at ~3.5 writes/s for an index the query no longer needs.

---

# Litestream — the backup was never restorable, and what was done about it (2026-08-04)

## The finding

While verifying that the newly-deployed `worker_telemetry` table replicated, a restore was attempted
for the first time in a while. **It failed, at every timestamp tried:**

```
cannot find max wal index for restore: missing initial wal segment:
generation=1950dae6522690d2 index=00005d8e offset=337872
```

Litestream was `active`, shipping WAL segments to GCS continuously, and `litestream snapshots` listed
a recent 68 MB snapshot. **None of that meant the backup worked.** The WAL chain had a gap, so
`litestream restore` could not reconstruct the database at any point in time.

**Shipping is not restorability. The only evidence that a backup works is a restore.** The deploy
scripts now run one rather than inferring it from replication activity.

## When the gap dates from, and why

The generation in use (`1950dae6522690d2`) began **2026-08-04T02:29:37Z**; the surviving snapshot was
written at 10:29:14Z at index 24568. The missing segment is index `00005d8e` (23950) — *older* than
the snapshot that needed it. The journal shows the mechanism:

```
"retainer error" ... error="delete wal segments before index: ...
   oauth2: cannot fetch token: dial tcp 192.178.211.95:443: i/o timeout"
"monitor error"  ... error="... TLS handshake timeout"      (x8 in 24h)
"sync error"     ... error="checkpoint: mode=PASSIVE err=database is locked"   (x7 in 24h)
```

GCS API flakiness from this box interrupted the **retainer** partway through deleting superseded WAL
segments, leaving the chain inconsistent — some segments removed, later ones kept. The `database is
locked` checkpoint failures (clustered 08-03 15:43 → 08-04 03:53, none since) prevented clean WAL
truncation and widened the window in which that could happen.

**How far back the loss goes is not knowable from the replica** — the generation itself only dates to
02:29 on 08-04, and no earlier generation survives. Treat everything before 2026-08-04 12:19 UTC as
having no point-in-time backup.

## The pre-generation fallback snapshot

Taken before touching anything, with `sqlite3 .backup` (online backup API, safe against the live WAL
database), `PRAGMA integrity_check` = `ok` on the copy, then uploaded and size-verified:

```
gs://liftlab-backup-lodha/liftlab/manual-snapshots/gateway-pre-generation-20260804.db
  234,713,088 bytes   2026-08-04T12:03:47Z
  gw_door_event=481,382   transit_event=26,093   worker_telemetry=203
```

It is deliberately stored **outside** the `liftlab/gateway.db/` prefix Litestream owns, so no
generation cleanup can reach it. This is a one-off flat file, not part of any chain — restore it by
downloading and opening it directly.

## Outcome — VERIFIED RESTORABLE (2026-08-04 14:07 UTC)

Despite the livelock described below, the regeneration completed and the result was **proven by an
actual restore**, not inferred:

```
generation 56539de99e6d45cb   snapshot 00000000.snapshot.lz4  70,521,124 B  12:19:25Z
WAL chain  0x00000000 .. 0x00000056   87 indices, ZERO gaps
old generation 1950dae6522690d2 : 0 objects (fully removed)
```

```
litestream restore -> applied wal through index 87 -> 238,874,624 bytes
PRAGMA integrity_check       : ok
gw_door_event    490,156      (+8,774 vs the 12:03 fallback baseline)
transit_event     26,659      (+566)
worker_telemetry   1,010      (+807, all 7 cameras present)
newest row       2026-08-04 14:06:55 UTC  — within ~1 min of the restore
```

The old failure mode was `missing initial wal segment`. The new chain starts at index 0 — the same
index as the snapshot — and is contiguous, which is precisely what was broken before.

**This restore was run from a workstation, not from the gateway**, using the released
`litestream v0.3.13` binary against a two-line config pointing at the GCS replica, with local ADC for
credentials. That is worth knowing: **restorability can be verified without touching the box**, and
should be, because a restore run on the machine you are trying to protect is not a real drill.

```yaml
# ls.yml — enough to verify a restore from anywhere with read access to the bucket
dbs:
  - path: /var/lib/liftlab/gateway.db     # the ORIGINAL path, used only as a key
    replicas:
      - type: gcs
        bucket: liftlab-backup-lodha
        path: liftlab/gateway.db
```
```
litestream restore -config ls.yml -o /tmp/restored.db /var/lib/liftlab/gateway.db
```

## Two things to know before running the regeneration again

**1. The VM cannot write to GCS.** Its scopes are `devstorage.read_only`. Litestream writes using a
service-account key (`GOOGLE_APPLICATION_CREDENTIALS=/etc/liftlab/backup-key.json` on its unit).
`apply_litestream_regen.sh` reuses that key inside a throwaway `CLOUDSDK_CONFIG` so the box's global
`gcloud` auth is left alone. The first run aborted here with a 403 — correctly, **before** deleting
anything, because the upload gate precedes every destructive step.

**2. Never delete the parent replica prefix — delete the specific generation.** The first working run
issued `gcloud storage rm -r gs://.../liftlab/gateway.db`. That prefix is also where the *new*
generation is written.

*What actually happened, measured rather than assumed:* the delete enumerated the prefix **once**, at
a moment when only the old generation existed, then worked through that fixed list. The new
generation was created afterwards and so was never in the list. Sampling the new chain from GCS
during the delete confirmed it: 87 → 90 WAL indices, **zero** indices lost, **zero** gaps, still
anchored at index 0. My initial reading — that the delete was chasing Litestream's new writes in a
livelock — was **wrong**. It was simply a slow recursive delete of ~14k objects over this box's
flaky GCS link, with retries.

It was still slow enough to matter: ~100 minutes, and enough CPU on a 2-vCPU box to make `sshd`
refuse connections for roughly 40 minutes, which is why the run could not be supervised or cleanly
aborted. And the design hazard is real even though it did not fire this time — had Litestream come
back up *before* the enumeration finished, the new generation's objects would have been on the list.

The script now takes the generation id *before* stopping Litestream, asserts Litestream actually
stopped, deletes only `.../generations/<OLD_GEN_ID>/`, and bounds that delete with `timeout 600`.
Leftover objects from an old generation are harmless junk. A 100-minute delete that locks you out of
the box is not — bounding it matters more than completing it.

---

# /reports on the e2-small — MEASURED, AND IT DOES NOT FIT (2026-08-04)

**Verdict: the gateway cannot carry an export alongside the seven live streams. The button was
built, deployed, measured, and then UNMOUNTED. Exports belong on dev-box.**

The design did everything it was supposed to — niced child process, one at a time, never on the
request path — and it still is not enough, because the constraint is CPU on a 2-vCPU box, not
architecture.

## The measurement

Baseline, box quiet (a first attempt was thrown away: its baseline was taken while the box was still
at load 7 from gate runs, which would have handed /ops a 36s allowance and produced a meaningless
pass):

```
/dash/site-A/data : 0.75  0.98  0.73  1.05  1.47      worst 1.47s
/ops/site-A/data  : 6.75  5.53  7.24  5.96  6.24      worst 7.24s
```

During a 30-day export (job niced at 10, in its own process):

```
/dash/site-A/data : 0.95  1.28  0.98  1.26  3.50      worst 3.50s   (2.4x baseline)
/ops/site-A/data  : 18.01 8.75  6.99  26.29 8.49      worst 26.29s  (3.6x baseline)
```

**/ops degraded to 26.3 seconds.** That is a page an operator uses, made unusable for the duration.

**/dash "passed" only against the lenient 3x rule in the harness.** Measured against this project's
own stated /dash bar — under 2 seconds — its 3.50s spike is a breach too. Two different thresholds,
same conclusion; the 3x rule was mine and it was too generous.

## Throughput, for scale

| range | wall clock on the e2-small | locally, for comparison | rows |
|---|---:|---:|---:|
| 7 days | **426 s** (7:06) | 41 s | 18,188 transits |
| 30 days (all history) | **579 s** (9:39) | 55 s | 22,630 transits |

Roughly **10x slower** than a workstation. Peak worker RSS 254-394 MB against 898 MB available, so
**memory was never the problem** — the box has the RAM and lacks the CPU.

## What was proven before the bar failed

Not wasted: the mechanism is sound and all of it is reusable on dev-box.

* submit -> poll -> download -> **workbook opens** (13 sheets, 2.7 MB for 7 days)
* one job at a time held under load: `running=1, queued=3` at the cap, 6th submission -> HTTP 429
* worker observed at `nice 10`, in its own session, RSS its own
* static-copy db md5 **unchanged** by a full export; `query_only=1` and sqlite refused a write
* broken DB -> job `failed` carrying its real error text, never a hang
* output dir 0750, workbook 0640

Not reached before the run was stopped: era-straddling build, empty range, kill-mid-run, retention
sweep, unauthenticated download. The run was halted deliberately — each remaining queued export
would have degraded /ops for another seven minutes to test something unrelated to the bar.

## Current state

`/reports` is **unmounted** (404). `reports_api.py`, `report_runner.py`, the `liftlab_report`
package and openpyxl remain installed, and `main.py` is backed up either side of the change. Bringing
it back is two lines in `main.py`; the reason not to is above, not in the code.

**The fallback is dev-box**, which is where this should go: same code, same gate, no live streams to
starve. Nothing about the module is gateway-specific except the `GATEWAY_DB` path it reads.

## A cleanup that silently did nothing (2026-08-04)

Removing the proof-run workbooks with

```bash
sudo rm -f /var/lib/liftlab/reports/*.xlsx
```

reported success and deleted nothing. The glob is expanded by the **invoking** shell, before sudo —
and that user cannot read a `0750 liftlab:liftlab` directory, so it matched nothing, `rm -f` got a
literal unmatched path, and exited 0. Two workbooks of resident movement data stayed on disk while
the command said it had removed them.

```bash
sudo sh -c "rm -f /var/lib/liftlab/reports/*.xlsx"   # glob expands UNDER sudo
```

Same shape as the week's other failures: a command that reports success without doing the thing.
`rm -f` in particular cannot fail, which is exactly why it cannot be trusted as evidence — verify by
listing afterwards, under the same privilege that could see the files in the first place.

---

# /reports on dev-box — DEPLOYED 2026-08-04

Moved here after the gateway measurement (above). `https://dev.gargi.online/reports`.

## Gate on the target box: 25 passed, 0 failed

Including the five checks the gateway run never reached, and the retention assertion added because a
cleanup had reported success while deleting nothing:

```
retention sweep actually deletes the workbook
  PASS  swept job marked expired (was done)
  PASS  workbook is GONE from disk (checked by existence, not exit code)
  PASS  second check: path does not exist
  PASS  download of an expired job -> 410
  PASS  410 explains WHY (retention)
```

## Verification at deploy time

```
app healthz     : 200
app /reports    : 200
public NO auth  : 401        <- Caddy basic_auth, resident data is not public
existing :8080  : 302        <- the pre-existing dev app, untouched
refresh timer   : active
reports service : active

restore_state.json:
  ok             true
  restored_at_h  2026-08-04 22:02   (IST)
  data_max_ts_h  2026-08-04 22:00   (IST)
  rows           498,163
```

## Two decisions, stated because they are security choices

**Auth: Caddy `basic_auth` on `/reports*`, reusing the gateway's operator credential.** code-server is
NOT running on dev-box, so "the existing code-server auth" does not exist. The host served
`127.0.0.1:8080` with **no authentication at all**, and resident movement data does not go behind
nothing. The `:8080` route is deliberately left exactly as it was — the reports block is inserted
ahead of it, not merged with it.

**GCS credential: the user ADC already present on dev-box.** The gateway's
`/etc/liftlab/backup-key.json` was deliberately NOT copied: that key can **write** to the backup
bucket, and a dev box must never be able to damage backups. The cost is that the restore is tied to a
personal account — which is precisely why a failing refresh renders as a red banner rather than a log
line. **Follow-up worth doing: a dedicated read-only service account.**

## The refresh is also a backup test

Hourly `litestream restore` into `/var/lib/liftlab/gateway.db`. It refuses to publish a restore that
fails `integrity_check`, has zero rows, or carries data OLDER than what is already present (a
regressed chain would silently rewind every figure). On any failure the previous database stays and
the page shows a **backup alarm** with the last good restore point.

On 2026-08-04 the chain was found unrestorable at every timestamp, and nothing noticed because nobody
had attempted a restore in weeks. Now something attempts one every hour and says so when it fails.

## Locking

`refresh_reports_db.sh` and `report_runner.py` take the same `flock`. The refresh publishes by atomic
rename; a build straddling the swap would keep reading the old inode and silently report a different
restore point than the page claims. The runner holds it for the whole build, so a refresh waits.

## Known cosmetic gap

`generation` reads `null` in the state file — the `litestream generations` lookup in the refresh
script returns nothing on this box (`database path or replica URL required`). It is informational
only; the restore itself, its verification and the timestamps are all unaffected.

## Acceptance proof on dev-box: 33 passed, 0 failed (2026-08-05)

All seven items, including the five the gateway run never reached and the two that exist only
because the data source is a restore.

```
1. ERA-STRADDLING RANGE       4/4  warned pre-submission; COVERAGE & ERAS sheet present
2. EMPTY RANGE                2/2  no crash; 15 explicit zero-rows statements
3. REFRESH-vs-EXPORT LOCKING  4/4  refresh blocked; export +240s, refresh +496s
4. KILL THE WORKER MID-RUN    5/5  failed with a reason; queue still works afterwards
5. RETENTION SWEEP            6/6  workbook GONE (sudo test -e); 410 explains why
6. UNAUTHENTICATED ACCESS     5/5  /reports /jobs /download /data-as-of all 401
7. REFRESH FAILURE SURFACES   7/7  ok=false, real error, previous DB intact, alarm clears
```

Item 3 was additionally exercised unplanned: the scheduled hourly timer fired mid-run and serialized
against a running export on its own, which is better evidence than the deliberate race.

Item 7 points the restore at a bucket that does not exist and asserts the box degrades HONESTLY —
`ok=false` carrying `storage: bucket doesn't exist`, the last good restore point preserved, and the
previous database intact at 525,473 rows. A failed refresh must become "old but honest data, loudly
flagged", never a missing file or a silently empty page.

## The bug item 4 found, which the gateway run would never have caught

First run FAILED it: `kill -9` the worker and the job stayed `running` past 400s while the next
submission queued behind it forever.

`os.kill(pid, 0)` is not a liveness test for your own children. A killed child that nobody
`wait()`ed on becomes a **zombie** — it keeps its pid and answers signal 0 — so the reaper called a
dead worker alive. Because the supervisor only spawns when nothing is running, **every queued export
waited behind a process that no longer existed**, until the 30-minute timeout.

Fixed two ways, both needed:
* the supervisor keeps each `Popen` and `poll()`s it, which reaps the child and clears the zombie;
* liveness reads `/proc/<pid>/stat` and treats state `Z` as dead, for handles lost across a restart.

The `/proc` parse splits on the **last** `)` because `comm` can contain spaces and parentheses.

**This bug was equally present on the gateway.** It never surfaced there because that acceptance run
was stopped after `/ops` degraded — before the kill test. Running the items you did not reach is what
caught it; the ones that passed did not imply it.

## `pkill -f` self-matching, the sharper version

Third recurrence this week, and the earlier lesson was incomplete. `pkill -f "[p]rove_reports_devbox"`
still killed its own shell, because the same command line later referenced
`/tmp/prove_reports_devbox.sh` — the `[p]` bracket only helps when the plain string appears **nowhere
else in the command**. Split the kill into its own invocation, or match by pid.
