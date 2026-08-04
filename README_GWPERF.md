# Gateway performance — /dash and /ops on liftlab-cloud

**Status (2026-08-04): two changes are DEPLOYED and verified on branch `gw-timeout`.**

| # | Change | State |
|---|---|---|
| 1 | Server-side request budget on `/dash/{gw}/data` | **DEPLOYED, verified** |
| 1b | Client in-flight guard + failure cap + backoff | **DEPLOYED, verified** |
| 2 | Bounded SQL rewrite of `/dash/{gw}/data` | not started |
| 3 | `/ops` indexes (`apply_gwindex.sh`) | written, unrun, **re-measure first** |

`/dash/{gw}/data` still returns 503 by design until step 2 lands — it cannot complete, so a prompt
error is the correct behaviour, not a regression.

The unverified islice / `_transit_by_cam` / single-flight-cache work is parked on branch
`gwperf-wip` and is NOT deployed.

---

## THE HEADLINE FACT: /dash/{gw}/data DOES NOT TERMINATE

Measured from browser DevTools plus direct timing (2026-08-03):

```
/dash              (page shell)   200, 6.5 s, 48.1 kB     <- the shell is FINE
/dash/site-A/data                 000, 600 s, 0.0 kB      <- curl --max-time 600
                                  DevTools: Pending, 0.0 kB
```

**This is not a slow endpoint. It is a non-terminating one.** Caddy is not involved — it logged zero
dash errors and proxies the shell fine.

The consequence is the important part: **every `/dash` page load leaves a query running on the box
that never finishes.** The dashboard auto-refreshes, so hours of an open tab pile up non-terminating
work. That is very likely the major contributor to the sustained load and to the OOM kills below.

### Do not quote 42.98 s as the live cost

An earlier note in this file cited `_tier2("ch29") = 42.98 s`. That figure was measured **under
`cProfile` instrumentation**, which inflates it, and it is **not** the live cost. It is retained only
as a relative signal that `_tier2` dominates. The real live behaviour is "does not complete".

What is still trustworthy from that run is the row volume, because those are counts, not timings:

```
ch29   era rows                77,905
       all-era rows           170,839
       all-era rows w/ floor  140,793
       transits joined          4,815
```

Seven cameras, three near-full reads each, per request.

## The fix — required shape, not a suggestion

1. **`/dash/{gw}/data` must aggregate in SQL with a bounded time window. Default: last 7 days.**
2. **No materialising history in Python.** Reducing allocation was already tried (`rows[idx+1:]` →
   `itertools.islice`) and it moves the wrong quantity. The problem is the volume pulled into the
   process, not how it is handled once there.
3. **An all-history view, if kept at all, must be explicitly requested and labelled expensive.** It
   must never be what a default page load runs.
4. **The endpoint needs a server-side timeout.** A query that overruns must return an error, not run
   forever. A request that hangs while holding resources is precisely how a slow page becomes an OOM
   — see the two kills below. This is a hard requirement, not a nicety.

---

## Corrected premise: the database was never unindexed

The check that reported zero indexes is unreliable:

```sql
-- WRONG: returns 0 for every table. The correlated `name` does not reach the
-- table-valued pragma, so the subquery yields no rows instead of erroring.
SELECT name, (SELECT COUNT(*) FROM pragma_index_list(name))
  FROM sqlite_master WHERE type='table';

-- RIGHT:
SELECT tbl_name, COUNT(*) FROM sqlite_master WHERE type='index' GROUP BY tbl_name;
```

The live DB has 11 indexes, including the two that matter — both already the shape you would
propose:

```sql
CREATE INDEX ix_door_event ON gw_door_event (gateway_id, cam, ts)
CREATE UNIQUE INDEX ux_transit ON transit_event (gateway_id, cam, track_id, direction, ts_bucket)
```

## Query plans — the hot paths are already SEARCH

Captured against the live schema, 2026-08-03. There is no SCAN-that-should-be-a-SEARCH driving the
latency:

| Query | Plan |
|---|---|
| `_door_gpu_by_cam` (×7) | `SEARCH gw_door_event USING INDEX ix_door_event (gateway_id=? AND cam=?)` |
| `_tier2` main read (×7) | `SEARCH gw_door_event USING INDEX ix_door_event (gateway_id=? AND cam=?)` |
| `_tier2` era census (×7) | `SEARCH ... ix_door_event (gateway_id=? AND cam=?)` + TEMP B-TREE for GROUP BY |
| `_transit_by_cam` | `SEARCH transit_event USING INDEX ux_transit (gateway_id=?)` |
| `_transits_for_join` | `SEARCH transit_event USING INDEX ux_transit (gateway_id=?)` + TEMP B-TREE |
| `_floor_coverage` | `SCAN e` — gw_event, 4,242 rows, joined to gw_source by rowid. Cheap. |
| `_latest` (ops) | `SCAN watch_status` — `ORDER BY id DESC LIMIT 1` stops at row 1. O(1). |
| ops floor-collapse (×2) | `SEARCH gw_door_event USING INDEX ix_door_event (gateway_id=?)` ← **gateway_id only** |

The last row is the one genuine index gap, and it is an **/ops** problem, not the `/dash` problem.
`ix_door_event` is `(gateway_id, cam, ts)`; with `cam` unconstrained the `ts` range cannot be used, so
the query walks all 428k index entries and fetches every row to test `ts` and read `floor`. See
`apply_gwindex.sh` — written, per-query justified, **not yet applied**.

## Per-helper profile of /dash/{gw}/data

Against the live DB, deployed (unfixed) code:

```
_cameras              0.002 s   +0.2 MB
_door_by_cam          0.226 s   +2.5 MB
_door_gpu_by_cam     10.487 s  +16.1 MB
_transit_by_cam       0.537 s
_transfer_by_cam      0.198 s
_floor_coverage       0.004 s
_registry             0.000 s
_transits_for_join    0.537 s
_tier2  (7 cameras)  > 20 min, did not complete
```

Note `_door_gpu_by_cam` at 10.5 s is on its own already above any sub-second target, so `_tier2` is
necessary but not sufficient to fix.

`_tier2` performs **three** near-full reads per camera: the era rows, a `GROUP BY door_version` era
census, and an all-era floor-alphabet read that deliberately spans every era (see the comment at
`dash_api.py` `_derive_floor_alphabet` — admission evidence is all-era *by design*, so narrowing it
is a semantic change, not a free optimisation).

## Measured endpoint baselines (2026-08-03, DB at 210 MB / 428,615 gw_door_event rows)

```
/ops/site-A/data    20.2, 15.0, 12.9, 13.5, 8.8 s   (mean 14.1 s)   <- slow, but TERMINATES
/dash               200, 6.5 s, 48.1 kB                             <- page shell, fine
/dash/site-A/data   000 at 600 s, 0.0 kB                            <- DOES NOT TERMINATE
```

`/ops` is worse than the 4.8–9.3 s originally reported (the DB has grown) but it does finish, and it
already aggregates in SQL with time windows — which is exactly the shape `/dash` needs and lacks.
`/dash/{gw}/data` is a different class of problem: it never returns.

## The OOM is RECURRING, on unmodified code

Two kills in ~13 hours, the second one **after** everything of mine had been rolled back and with
the deployed hashes verified original:

```
Aug 03 04:44:16 kernel: Out of memory: Killed process 2550731 (uvicorn)
                        total-vm:2721820kB, anon-rss:1481176kB
Aug 03 18:00:36 kernel: Out of memory: Killed process 3054399 (uvicorn)
                        total-vm:2761292kB, anon-rss:1492008kB      <- ORIGINAL code
```

This is standing production behaviour, not an artefact of the perf work. `systemd` restarts the
gateway each time, so it self-heals, but every kill takes the segment ingest and the heartbeat
endpoints down with it. Treat it as an open incident independent of the `/dash` latency work.

Sampled every 10 s after the restart:

```
04:49  rss=  61 MB  avail=1303 MB  load=0.87   idle
06:01  rss=  75 MB  avail=1257 MB  load=0.79
06:11  rss= 489 MB  avail= 850 MB  load=1.82   /dash loaded
06:21  rss= 667 MB  avail= 690 MB  load=2.07   peak
06:42  rss= 367 MB  avail= 903 MB  load=1.47   released
09:56  rss= 296 MB  avail=1042 MB  load=0.52
14:13  rss= 294 MB  (flat ~4.5 h)
14:23  rss= 192 MB                             arenas trimmed
```

**Memory is returned.** This is not an unbounded leak — it is a large transient per request plus slow
arena decay. One `/dash` survives on a 1.97 GB box; the fatal window was *concurrent* requests. The
page auto-refreshes, each request takes minutes, so refreshes overlap and stack until OOM. That is
also why RSS looked "flat at 126 MB" whenever nobody had the dashboard open.

Consequence for acceptance: **a fast /dash is not sufficient. RSS must hold flat under a sustained
refresh load before /dash is considered fixed.**

## What is in this branch (NOT DEPLOYED)

- `dash_api.py`
  - `_tier2`: `rows[idx+1:]` → `itertools.islice`. Removes a full tail-list copy per door cycle.
    Real, but **insufficient** — `_tier2(ch29)` is still 42.98 s with it in.
  - `_transit_by_cam`: Python tally → SQL `GROUP BY`. Equivalence-tested 400/400 over random
    schemas including NULL/unknown direction and a `ts=0` case the naive `MAX(ts)` got wrong
    (hence `MAX(NULLIF(ts,0))`).
  - `_dash_heavy`: single-flight + TTL cache over the historical half, live strip left uncached.
    Threaded test: 20 concurrent callers → 1 computation, max 1 at once; TTL expiry recomputes;
    key space bounded at 8. **This closes the OOM stacking mechanism but does not make the cold
    path fast**, which is why deploy verification still timed out.
- `ops_api.py`: `relay_status.gw_rss_mb` — the gateway's own RSS sampled on each relay heartbeat, so
  the next memory event arrives as a curve. Rides inside the existing 720-row cap.
- `apply_dashperf.sh`: compile → smoke-import → install → restart → verify → auto-restore. It did
  correctly roll back twice; the safety works.
- `apply_gwindex.sh`: the two justified /ops indexes. Never run.

## Next session

**Do not deploy without supervising the run to its verification step.** Two attempts have already
failed verification and auto-rolled back; a third under a dropping tunnel risks dying between
`install` and `restore`, which is the one window `apply_dashperf.sh` cannot recover from.

1. Rewrite `/dash/{gw}/data` per the four required points above: SQL-bounded window defaulting to
   7 days, no Python materialisation, all-history only on explicit request and labelled expensive,
   and a server-side timeout that errors instead of hanging.
2. The era census (`GROUP BY door_version`) and the all-era floor-alphabet read are the two that
   resist windowing on **semantic** grounds — the alphabet is deliberately all-era (see
   `_derive_floor_alphabet`). Decide those explicitly; narrowing them changes reported numbers, so
   it is a product call, not an optimisation.
3. Deploy, then soak `/dash` under sustained refresh. **Acceptance is not latency alone: RSS must
   hold flat under sustained refresh load.** A fast `/dash` that re-inflates memory is not a fix.
4. `apply_gwindex.sh` (the two /ops indexes) is independent of all this and still unrun.

### Operational cautions

- **SSH: use the direct internal route, not the IAP tunnel.** `gcloud compute ssh
  --tunnel-through-iap` was unavailable for 20+ minute stretches all session while Caddy stayed
  responsive (401 in 0.43 s) and the instance read `RUNNING`. This works and is far more reliable
  from dev-box (same VPC, 10.160.0.4 -> 10.160.0.2):

  ```bash
  ssh -i ~/.ssh/google_compute_engine -o StrictHostKeyChecking=no 10.160.0.2 'uptime'
  ```

  The IAP flakiness dominated elapsed time. Do not start a deploy you cannot supervise through to
  its verification step — `apply_dashperf.sh` can only auto-restore if the session survives to run
  the restore.
- **Careful with `pkill -f` over ssh**: a pattern that appears in your own `bash -c` command line
  matches the shell running it and kills the session. Use a bracket guard (`[r]ss.sh`).
- `gateway.db-wal` has sat at ~60 MB while holding ~668 live pages (~2.7 MB). This is an inflated
  container from the outage that SQLite never shrinks — wasted disk, not a correctness problem.
  **Do not run `wal_checkpoint(TRUNCATE)` while Litestream is replicating**: Litestream does its own
  checkpointing and an external WAL reset can force a new generation. If it must be reclaimed, the
  safe order is stop litestream → truncate → start litestream (which takes a fresh snapshot).
  `journal_size_limit` is per-connection, not persisted in the file, so it would have to be set in
  the app's `_db()` to have a lasting effect.


---

# 2026-08-04 session results

## THE ACTUAL ROOT CAUSE: the client, not the query

From browser DevTools: **one tab left open produced 46 requests over 11.7 hours with ~25
simultaneously Pending.** The page ran `load(); setInterval(load, 15000);` — a fixed timer with no
in-flight guard and no failure cap — against an endpoint that never finishes. Every tick stranded
another query.

Closing the tab alone took `/ops` from **17.9s to 0.005s** and load from **5.38 to 0.74**.

This reframes everything measured on 2026-08-03: those numbers were taken on a box saturated by
stranded requests from an open dashboard. In particular the "`/ops` mean 14.1s" baseline was
contaminated. Re-measured on a quiet gateway: **5.5s mean** (5.27, 5.27, 5.28, 5.48, 5.99).

## Step 1 — server-side budget (DEPLOYED)

`DASH_DATA_BUDGET_S`, default 25s. Two mechanisms, because either alone leaves a hole:
`set_progress_handler` aborts SQL already executing (Python cannot interrupt a running query), and
`_budget_check()` between phases and once per camera catches the Python row-walking that SQLite
cannot see. Returns 503 + `Retry-After` naming the phase that ran out.

**The trap it closes:** `_q` swallowed `OperationalError` and returned `[]`. A budget abort arrives
as `OperationalError('interrupted')`, so without care a TIMEOUT would have rendered as an empty
panel — a camera with 77k rows reporting "no data". `_q` now re-raises as `DashTimeout` when the
deadline has passed; only the genuine missing-table case still returns the honest empty.

Verified live: `/dash/site-A/data` -> **503 in 25.97s** (phase `sql`), `/dash` shell 200, `/ops` 200.

Note a ~1-8s overshoot past the deadline: the progress handler fires between SQLite VM steps, and
time spent materialising rows into Python objects is not VM steps, so the abort lands late.

## Step 1b — client guard (DEPLOYED)

One request in flight (`inflight` guard; the timer is only re-armed from a settled request, so
overlap is structurally impossible), stop after 3 consecutive failures with an explicit
"data unavailable — retry" control, and backoff 15s -> 30s -> 60s. A non-2xx is now an error —
the old code piped the response straight to `.json()` and would have rendered the 503 body as
dashboard data.

## Soak: the OOM mechanism is closed

3 concurrent refresh streams, 18 requests, the exact pattern that stacked into the kills:

```
all 18 requests   -> 503 in 25.5-28.2s   (none hung)
RSS               -> 91 MB start, 199 MB peak, settled 178 MB, FLAT for 8 min after
MemAvailable      -> held ~1.1 GB throughout (was falling to ~5 MB)
load              -> peaked 5.7 (was 43)
```

Peak RSS under the OOM-causing pattern is now ~200 MB instead of 1.4 GB.

## Decisions taken (operator, 2026-08-04)

- **Era census** (`GROUP BY door_version`, builds the era selector): cache per `(gw, cam)` with a
  long TTL. Exact same numbers; a newly-appearing era shows one TTL late. No schema change.
  Rejected: windowing it (defeats its purpose — it exists to surface eras auto-newest would hide).
- **All-era floor alphabet** (140,793 rows for ch29): scheduled recompute persisted to a table,
  with `derived_at` and evidence-row count exposed. All-era semantics preserved exactly and it
  survives restarts. Rejected: bounding evidence to N days — that is the only option that CHANGES
  REPORTED NUMBERS (a rarely-visited floor drops out of the alphabet and its reads start being
  rejected as off-alphabet).

## Remaining acceptance

| Criterion | State |
|---|---|
| `/dash/{gw}/data` under 2s | **not met** — needs step 2 |
| RSS flat under sustained refresh | **met** (soak above) |
| Zero OOM kills over 24h | in progress — none since 2026-08-03 18:00 |

## Deploy machinery — two bugs found and fixed

Both would have produced false verdicts, and one had already fired:

1. **The smoke test imported the wrong module — read this before trusting any past "verified".**
   `cd $APP && PYTHONPATH=$TMPD:$APP python -c 'import dash_api'` prepends the CWD for `-c`, and the
   CWD wins over `PYTHONPATH`. So it imported the ALREADY-INSTALLED module and printed "smoke ok"
   for code it had never loaded. `apply_dashperf.sh` has the same shape, so **both of
   2026-08-03's "smoke ok" lines were vacuous — that gate was testing the file already on disk, not
   the candidate being deployed.**

   Fixed with `sys.path.insert(0, TMPD)` inside the interpreter (the only ordering that actually
   binds) plus an assertion on `dash_api.__file__`, so a wrong-module import now fails loudly
   instead of passing silently.

   **Precisely how much this invalidated, because the distinction matters:** the smoke gate was
   worthless, but it was not the only gate. Both 2026-08-03 deploys still installed the real file,
   restarted the service, and ran a genuine HTTP check against the running process — and that check
   is what failed and triggered the rollback. So those deploys were *not* "verified against
   nothing": the end-to-end verification was real and did its job. What was lost was the early
   warning. A file that imported cleanly on disk but broke on import would have sailed past the
   smoke gate and been caught only after the ingest was already down, by the restart check. The
   consequence was a weaker safety net, not a false PASS.
2. **Connection-refused read as a hang.** `systemctl is-active` goes green when the process forks,
   but uvicorn needs 8-35s to bind :9090. Probing early returns HTTP 000 in ~0.2ms, which the script
   scored as "still hanging" and rolled back a healthy deploy. Added a readiness gate on
   `/openapi.json`; the 000 branch now splits on elapsed time.
