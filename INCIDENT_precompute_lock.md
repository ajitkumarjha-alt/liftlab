# INCIDENT — the precompute sweep died of a lock it never waited for

**When.** 2026-09-07, 5m41s into the sweep. Fleet restarted 30 minutes earlier; seven workers
posting their backlog.

**What happened.** `sqlite3.OperationalError: database is locked` — first in every study stage
(`riders_per_day` / `rtt_per_hour`, every period), then **fatally in `precompute_run_record`**,
which took the whole job down after most of its work had already succeeded. Memory peak 943 MB.

## Root cause: the connection had no busy timeout at all

`dash_api._db()` was a bare `sqlite3.connect()`. **SQLite's default `busy_timeout` is 0**: a writer
that meets a held lock gives up on contact, without waiting. Measured on a scratch WAL database in
`tools/test_precompute_lock.py`:

    a second WRITER with busy_timeout=0 gave up after 0.3 ms

Under WAL there is exactly one writer at a time, so a burst of ingest is enough. `door_event_api`
sets `PRAGMA busy_timeout=30000` and carries this exact reasoning beside it — *"Under WAL with a
long-running dashboard read or a checkpoint in flight that is the difference between a slow write
and a failed one"* — so **the ingest path waited and the sweep did not.** With the restart backlog
flowing, the sweep lost every race it entered.

## What actually held the lock — measured, not assumed

The question raised was whether litestream checkpointing or a long-running read (a study-bundle
build) could be starving the writer. Section 7 of `tools/test_precompute_lock.py` reproduces the
mechanics rather than reasoning about them:

    WAL after 1500 writes, no reader open :     4032 KB
    WAL after 1500 more, ONE reader pinned:    20608 KB   (writes succeeded: True)
    checkpoint while pinned: (0, 5122, 528)  -> reclaimed 528 of 5122 pages
    once the reader lets go: (0, 0, 0) -> wal 0 KB
    a second WRITER with busy_timeout=0 gave up after 0.3 ms
    a READER while that writer holds the lock: not blocked

**A long-running read cannot be the direct cause.** Readers do not block writers under WAL, and the
writes above succeeded with a reader pinned open. What a long read *does* is stop the checkpoint
reclaiming: the WAL grew **5x** and a passive checkpoint recovered 528 of 5122 pages. A bigger WAL
means the checkpoint that eventually runs is longer, **and a checkpoint does pause writers while it
runs**. So a bundle build is an *amplifier* of contention, never its source — and the same is true
of litestream, which holds a long-lived read connection for replication.

**The direct cause is another writer**, and the failure was instant rather than a timeout, which is
the signature of `busy_timeout=0` and not of a genuinely long-held lock.

Whether litestream also disables SQLite's autocheckpoint on this box is a one-line check that is
worth doing, because it decides how big the WAL is allowed to get between litestream's own
checkpoints:

    sqlite3 /var/lib/liftlab/gateway.db 'PRAGMA wal_autocheckpoint; PRAGMA journal_mode;'
    ls -l /var/lib/liftlab/gateway.db-wal

`wal_mb` is now recorded on every sweep, so this stops being a question anyone has to remember to
ask.

## The fixes

**1. The connection waits.** `_db(busy_ms=None)` sets `PRAGMA busy_timeout`. Two values, and the
difference is deliberate: **requests get 5 s**, the timer gets **30 s**. A busy wait sleeps *inside*
SQLite, where the request budget's progress handler does not fire — a 30 s wait on a request path
would silently outlast the 25 s budget that is supposed to bound it. Nobody is waiting on the timer.

**2. Each stage retries a lock, and only a lock.** `busy_timeout` covers the wait inside one
statement; a stage that loses for the whole timeout still raises. `run_stage()` retries at 2 s, 6 s,
18 s. Every refresh is an upsert keyed on the era, so a retry is exactly as correct as the first
attempt. **Only a lock is retried** — a missing table or a bug reproduces exactly, and retrying it
burns the timer and buries the real message under three copies.

**3. The bookkeeping cannot kill the run.** `precompute_run_record` catches everything, logs it, and
returns a `record_error` field. *A record of what happened must never be able to destroy the thing
it is recording.* Asserted against both a closed connection and a genuinely held lock.

**4. One write lock per unit of work.** Each refresh did `INSERT`+commit then `DELETE`+commit —
taking the WAL's single write lock **twice** per camera per window, for a cleanup DELETE that almost
never removes a row. They are one transaction now, halving the contention points.

**5. The sweep reports its own contention.** `precompute_run` gains `n_locked`, `lock_wait_s`,
`wal_mb`, `rss_peak_mb`, `rss_peak_stage`, and the job prints them:

    [precompute] site-A contention: 0 lock retries, 0s waited · rss now 29.6MB,
    peak 29.6MB at [rtt site-A/ch27 w=1d] · wal 0.0MB · litestream inactive

A sweep that took an hour because it waited 40 minutes for a lock is a different problem from one
that spent 40 minutes computing, and the line could not previously tell them apart.

## Memory: 943 MB is not explained yet, and now it will be

Profiled at fixture scale — **one camera, 200,000 door rows** (ch29's real 7-day volume), full
sweep of every stage:

    baseline rss 15 MB
      alphabet             1.72s  rss  15 ->  23 MB
      aggregate            5.08s  rss  23 ->  32 MB
      rtt w=1/7/30/all     7.50s  rss  32 ->  42 MB
      trends cam / fleet   3.30s  rss  42 ->  38 MB
      study riders / rtt   0.89s  rss  38 ->  38 MB
    PEAK 42 MB

**27 MB above baseline for the busiest camera's whole sweep.** That does not add up to 943 MB, and
rather than invent an explanation the peak is now *recorded with the stage that set it*, so the next
sweep answers it:

    sqlite3 /var/lib/liftlab/gateway.db \
      "SELECT datetime(started_at,'unixepoch','+5 hours 30 minutes') t, total_s, rss_peak_mb, \
              rss_peak_stage, n_locked, lock_wait_s, wal_mb FROM precompute_run \
       ORDER BY started_at DESC LIMIT 10"

The candidates worth holding in mind while that fills:

* **`alphabet_refresh` is the one genuinely unbounded read in the sweep** — deliberately ALL-ERA
  (floors are physical; an era rollover does not move the building) and therefore not windowable,
  so it walks the camera's entire history and grows with ingest forever. It was 8 MB per camera at
  200k rows here.
* **The `w=0` RTT window is the full era**, which on ch29 was 436,486 rows when last measured —
  ~2x this fixture.
* **Python does not return every freed arena to the OS**, so a sweep's RSS is a ratchet across
  7 cameras x 4 windows x 4 periods rather than the max of any one of them.

## And a quadratic walk, found while profiling

`rtt_core.trips()` counted `n_stops` with `sum(1 for c in cyc if t0 < c <= t1)` — **a full pass over
every cycle in the era, for every trip**. That is O(trips x cycles): on a 200,000-row era, roughly
11k trips x 33k cycles = **360 million comparisons** for a number each trip needs once, and it is
why the RTT stage grows faster than the row count. It is a `bisect` now — exact on a non-decreasing
list, so the answer cannot change, which is asserted against the scan it replaced on 200 random
eras **with ties**, plus a complexity check that 4x the rows is not ~16x the time (measured 6.0x).

Separately, `_rtt_by_cam(want_trips=True)` was calling `rtt_core.trips()` a **second** time on rows
`summarise()` had already walked — a duplicate pass, and a second call site of the derivation
`rtt_core` exists to keep singular. `summarise(..., with_trips=True)` carries them out now.

## Deploy

    systemctl restart liftlab-cloud
    systemctl start liftlab-precompute.service

New `precompute_run` columns are added by `ALTER TABLE` inside the job; no migration. Tunables:
`DASH_BUSY_TIMEOUT_MS` (5000), `DASH_PRECOMPUTE_BUSY_MS` (30000), `PRECOMPUTE_RETRY_S` (`2,6,18`).
