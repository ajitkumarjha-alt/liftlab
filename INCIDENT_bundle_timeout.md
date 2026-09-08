# INCIDENT — the study bundle timed out and handed a user a page of JSON

**When.** Monday 08:30, 7-day range.
**What the user saw.** The browser tab replaced by:

    {"error":"timeout", phase "sql", elapsed 60.26s}

**What was lost.** The whole download. Six datasets that would have produced fine were thrown away
because the seventh could not, and the dashboard the user was on was gone.

## Root cause

`rtt_trips.csv` walked each camera's door era **on the request path**.

This is the same failure mode, in the same system, for the third time — `/data`, then `/trends`,
now a download. Each time the shape is identical: a derivation that is affordable on a small
fixture and unaffordable on a real era, running inside a request. The precompute layer exists
precisely to stop it, and this file went around it.

**The row cap did not save it, and could not have.** The walk was bounded at
`RTT_MAX_ROWS` = 120,000 door rows per camera, and I shipped it believing that was the bound that
mattered. It is not: *a cap on rows is not a cap on time.* Seven cameras each **under** the cap
still spent 60.26 s in SQL. Uncapped cost is ~25 µs/row, so seven cameras at ~100k rows is ~17 s of
pure walk before any of the surrounding query cost, and a Monday-morning 7-day range is the widest
realistic case. The cap could only ever have refused a single enormous camera; it had nothing to
say about seven medium ones, which is the normal case.

I flagged this file as "the one real walk" when I shipped it and bounded the wrong dimension.

## Fix 1 — read the precomputed walk

`rtt_window` held only `rtt_core.summarise()`'s output, so there was nothing per-trip to read; that
is why the bundle walked. It now stores the **trips** as well:

* `_rtt_by_cam(..., want_trips=True)` returns the trip list built from `rtt_core.trips()` over the
  rows it has **already walked** — the same call `summarise()` makes internally. Not a second
  derivation and not a second read.
* `rtt_refresh` **pops** `trips` out of the summary before storing it, into its own `rtt_window.trips`
  column. The `payload` column stays byte-identical, which matters: it is embedded in every
  `trends_cache` entry and parsed on every trends request. Folding a few thousand trips into it
  would have inflated the hot path to serve one download.
* `_rtt_trips_read` serves it. Same era key, same never-across-a-boundary rule as `_rtt_read`.
* The bundle now reads one indexed row per camera and walks nothing.

**Proven by disabling the walk, not by timing.** `test_study_bundle.py` replaces `rtt_core.trips`
and `rtt_core.summarise` with a function that raises, then builds a bundle and asserts the file is
still produced with its rows. A timing assertion would pass on a fixture too small to be slow; this
cannot.

**A bug this surfaced.** `_rtt_by_cam` only attaches `trips` on the `ok` path, so every camera whose
measurement could not be made (`no_floor`, `no_era`, `no_rows`) stored NULL — and NULL is how the
column says *the sweep has not run*. ch27 reported "pending computation" for a camera that can never
have a round trip. **Walked-and-empty is not not-walked**: a completed walk now stores `[]`, and an
empty list is explained by the summary's measurement state, so ch27 reads `no_floor` with
rtt_core's own sentence.

## Fix 2 — one file failing withholds one file

The old rule was: any failure, no zip. The reasoning was that a bundle missing a file, with a README
that lists it, claims a completeness it does not have. That was right about the README and **wrong
about the remedy** — the fix is to say so in the README, not to discard six good datasets.

* Each file is produced by its own callable, with **its own slice of the clock**
  (`DASH_BUNDLE_FILE_BUDGET_S`, 15 s). One deadline across the whole build meant the first slow file
  ate the budget and every later one died of someone else's cost while reporting its own name.
* A failure becomes `<name>.UNAVAILABLE.txt` in the archive, carrying the reason, the range and the
  time spent. Unzipped into a folder, a *missing* file is indistinguishable from one the reader
  forgot to look at; a file called `rtt_trips.UNAVAILABLE.txt` cannot be mistaken for either data or
  an oversight.
* README gains a **`## Withheld from this bundle`** section, placed **above** "Read this first" — a
  reader who takes the folder at face value must learn what is not in it before they start counting
  what is. The manifest marks the row `**WITHHELD**`, and the per-file section keeps its caveats so
  a reader who obtains the file another way still has them.
* `X-Bundle-Withheld` names them on the response, so the page can tell the user the download is
  incomplete without opening the zip.

Only a failure of the **archive itself** is now an error response.

## Fix 3 — the button never opens a JSON tab

A raw JSON error page is a stack trace being used as a user interface: the reader cannot tell
whether the system is broken, whether their range was too wide, or whether to try again, and the
page they were on is gone.

* The button is a `<button>` driving `fetch()`, not an `<a href>`. States: **preparing bundle…** →
  saved file (object URL + synthetic click, no external anything) → or an inline panel.
* The inline panel says three things in this order: what happened, that **the data is not the
  problem**, and the actions — `retry`, `try 7 days instead`, `try today only`, `dismiss`.
* A **successful but incomplete** download reports itself too: "DOWNLOADED, WITH n FILE(S)
  WITHHELD", naming them. Saying nothing would let a reader open six files and count them as seven.
* `fmt=json` is set by the fetch and asks for a machine-readable error. **Without it — a browser
  reaching the URL directly — the endpoint answers with an HTML page** offering the same retry, the
  same narrower ranges, a link back to the dashboard, and the sentence "Nothing is wrong with your
  data."

## Logging — so the next one is visible first

`slowlog` prints when a handler is **already** over its threshold, which is how 60.26 s was
recorded: after the fact, in a journal, once a user had been handed an error page. The number that
predicts a regression is the per-file duration **before** anything crosses the line — `rtt_trips`
creeping 4 s → 11 s → 24 s over three weeks is the entire warning, and it is invisible to a log that
only fires at 60.

* **`bundle_run`** records **every** request: total, period, per-file `phases` JSON, slowest file and
  its seconds, bytes, and what was withheld. Same shape, same 14-day retention and same argument as
  `precompute_run`.
* A stdout line per request naming every phase, sorted slowest first.
* **On the daily health line**, because a table nobody queries is the journal problem one level up:
  `[BUNDLE: last study bundle took 41s against a 30s warning line (budget 60s), slowest file
  rtt_trips.csv 38.2s]`. Warning line is `HEALTH_BUNDLE_SLOW_S`, default 30 s — half the budget, on
  the same reasoning `PRECOMPUTE_SLOW_S` uses. It also fires when a file was withheld even if the
  bundle was fast, and stays **silent** when the bundle was fast and complete: a line that always
  fires is not a warning.

    sqlite3 /var/lib/liftlab/gateway.db \
      "SELECT datetime(started_at,'unixepoch','+5 hours','+30 minutes') t, period, total_s, \
              slowest, slowest_s, n_withheld FROM bundle_run ORDER BY started_at DESC LIMIT 20"

## Deploy

    systemctl restart liftlab-cloud
    systemctl start liftlab-precompute.service     # REQUIRED: fills rtt_window.trips

Until that sweep runs, `rtt_trips.csv` is **withheld with a stub explaining it is pending** — the
column is new and empty, and pending is not an absence of round trips. Everything else in the bundle
works immediately. `rtt_window.trips` / `n_trips_stored` and `bundle_run` are created by
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE` inside the job and the endpoint; no manual migration.

`DASH_RTT_STORE_MAX_TRIPS` (default 200,000) caps stored rows per camera per window. Order of
magnitude: ch29 ran 1,569 trips in a week.

## What I would check next

The three walks that escaped onto the request path all did so in code I believed was bounded. The
bound was on rows, or on a cache key, or on an era filter — never on **time**, which is the only
dimension a request actually has. `bundle_run.phases` is the first instrument in this system that
measures that per unit of work rather than per request, and it is worth pointing at `/trends` and
`/data` too.

---

## Addendum, 2026-09-07: the cap was also blocking analysis

Reported on the same review: the 7-day bundle **refused ch29's RTT entirely** — "199,855 rows >
120k cap". So the guard did two harmful things at once. It did **not** prevent the timeout (seven
cameras all *under* the cap still blew the budget, which is the whole point above), and where it
*did* fire it withheld the round trips the bundle exists to serve. A guard that misses the failure
it was written for and blocks the work it was protecting is not a guard.

`DASH_BUNDLE_RTT_MAX_ROWS` is deleted. There is no row cap on the bundle's RTT at any volume: the
trips come from `rtt_window`, walked once per era per window by the timer.

**A stored refusal from the retired build is now told apart from a live one.** `rtt_refresh` passes
`max_rows=None`, so the current build *cannot* produce `too_many_rows`; any stored one predates the
change. Repeating its note verbatim tells an operator that the system refuses their range at 120,000
rows — untrue, and it sends them to narrow a range that would have worked. Both the RTT fleet matrix
and `rtt_trips.csv` now class it `stale` and say: stored by a build that capped the walk, cannot be
reproduced, re-run `precompute_job.py`. One `_rtt_state_note()`, two surfaces, so the explanation
cannot drift.

On the live box the remedy is simply the sweep — `rtt_refresh` overwrites the row.
