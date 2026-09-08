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

### Litestream is running, it owns the checkpoint cycle, and it is losing the same race

An earlier revision of this section claimed litestream was not running and that autocheckpoint was
therefore at SQLite's default. **Both halves were wrong.** The check had been run on `dev-box`,
where litestream is an unused binary with no config and no unit — not on `liftlab-cloud`, which is
where `gateway.db` lives. Measured on the gateway (2026-09-07):

    $ hostname                          liftlab-cloud
    $ systemctl is-active litestream    active        (enabled; v0.3.13)
    $ ls -l /etc/litestream.yml         145 B, Jul 16 16:29
      -> gs://liftlab-backup-lodha/liftlab/gateway.db
    generation 56539de99e6d45cb  index 00007688  replica lag -26s

And the `PRAGMA wal_autocheckpoint` reading was not evidence about the database *even on the right
box*. `wal_autocheckpoint` is **per-connection** state — it installs a WAL hook on the connection
that asks. A fresh `sqlite3` CLI connection answers `1000` on any build anywhere, because 1000 is
that new connection's own default. It says nothing about what litestream's connection or the ingest
app's connections have set. The question was not answerable by the command used to answer it.

**Litestream owns the checkpoint cycle here, and the shadow-WAL index proves it.** Index `0x7688` is
30,344 rotations across the generation's 34.02 days — **one per ~97 s**, which is litestream's
default `checkpoint-interval` of 1 minute plus the time it takes to get the lock. It is shipping a
WAL segment every ~1 s (220–460 ms per write), so replication itself is healthy.

**The WAL file is 103.7 MB, and that number does not mean what it looks like.** Sampled every 10 s
for a minute on the gateway:

    wal 108,714,472 B  constant to the byte for 60s
    db  1,155,551,232 -> 1,155,637,248 B   (+86,016 B = 21 pages copied out)

108,714,472 = 32 + **26,387** frames x (4096 + 24) — 25.7x the 4,231,272 B (1,027 frames) that the
August sampler recorded in `smoke_reports.sh`. But litestream's live position is offset **3,061,192**
inside the current index: **2.8 % of the file.** SQLite never shrinks a WAL except on a TRUNCATE
checkpoint, so 103.7 MB is a **high-water mark left by a past episode**, not live backlog. The WAL is
cycling in the first ~3 MB of a 103.7 MB file. What it proves is narrow and still worth knowing: *no
TRUNCATE checkpoint has succeeded since whatever grew it.*

### So what held the lock — the answer is unchanged, and litestream is the other victim

    $ journalctl -u litestream --since '24 hours ago' | grep -c 'database is locked'
    93
    $ journalctl -u litestream --since 2026-09-01 | grep -c 'database is locked'
    184         -> 91 over the prior 5.5 d (16.5/day), then 93 in the last 24 h
    ... error="checkpoint: mode=PASSIVE err=database is locked"   clustered, ongoing

**Read those two numbers together.** The steady rate is ~16.5/day; today is **5.7x** that. Today is
the day of this incident — the fleet restart and the seven workers' backlog. So the refusals are not
a constant background hum, they *track the contention this incident is about*, which is what makes
them a usable signal. For scale, August's chain-breaking episode ran at 7 in 24 h.

Litestream cannot be what the sweep collided with, for two reasons that are both in that line:

* **The mode is PASSIVE.** A passive checkpoint never blocks a writer and never waits — it copies
  what it can and returns. There is nothing there for a writer to block on.
* **Litestream is losing this race, 93 times a day.** It is a fellow casualty of the contention, not
  its source. Whatever beats litestream 93 times a day is what beat the sweep.

**The direct cause is another writer — the ingest path holding SQLite's single WAL write lock** while
it commits the restart backlog. That is what the section above already concluded, and litestream's
presence does not move it. The failure was instant (`0.3 ms`, section 7) — the signature of
`busy_timeout=0` meeting a held lock, not of a long checkpoint pause.

What litestream *does* change is the amplifier, and it makes it permanent rather than occasional.
Section 7's measurement — WAL 4,032 KB -> 20,608 KB with one reader pinned, passive checkpoint
reclaiming 528 of 5,122 pages — is not a scenario on this box. **Litestream's replication connection
means there is always a pinned reader.** A study-bundle build stacks a *second* long read on top of
it, which is why bundles make this worse rather than causing it.

And the stakes are not only the dashboard's. In August these same refused checkpoints "prevented
clean WAL truncation and widened the window" in which the retainer left the replica chain
inconsistent — `README_GWPERF.md`, the incident that cost every point-in-time backup before
2026-08-04. **Today's 93 is 13x the rate that preceded that loss**, and even the quiet-day 16.5 is
over twice it.

### What to actually watch — `wal_mb` is a watermark, not a gauge

`lock_diagnostics` takes `os.stat` of the `-wal` file, so `wal_mb` reports the **high-water mark**.
On the gateway it reads 103.7 MB today and will keep reading 103.7 MB whether checkpointing is
healthy or not, until some TRUNCATE checkpoint shrinks the file. It is a *worst-ever* number, useful
for spotting that a bad episode happened, useless for telling you one is happening now.

The live signal is litestream's own sync loop, and it is one command:

    journalctl -u litestream --since '24 hours ago' | grep -c 'database is locked'

Rising means writers are holding the lock long enough to refuse a passive checkpoint — which is the
*backup* alarm before it is a dashboard one.

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

**That sample is from the fixture box.** On `liftlab-cloud` the same line reads `litestream active`
and `wal 103.7MB` — and per the section above, that `wal_mb` is a high-water mark, so do not read a
large one as evidence of contention during *this* sweep. `n_locked` and `lock_wait_s` are the fields
that describe the sweep that printed them.

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
      "SELECT datetime(started_at,'unixepoch','+5 hours','+30 minutes') t, total_s, rss_peak_mb, \
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
