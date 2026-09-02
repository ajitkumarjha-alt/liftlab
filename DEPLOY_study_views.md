# DEPLOY — the two study views: riders per lift per day, RTT per hour per lift

Both are FIRST-CLASS VIEWS of data that already exists. Neither is a new measurement, neither adds
request-path cost, and neither derives anything a read can trigger.

    Riders / day    rows = IST date, cols = lift, cells = riders.  A re-shape of transit_event.
    RTT / lift      rows = lift, cols = hour-of-day, cells = median RTT with n.
                    A re-shape of what rtt_refresh ALREADY stored in rtt_window.

## THIS DEPLOY IS NOT OPTIONAL, EVEN IF YOU DO NOT WANT THE NEW TABS

It carries an ATTRIBUTE-ESCAPING FIX to `esc()`, and `esc()` is the escaper every panel on `/dash`
already uses. `esc()` escaped only `&<>` — safe in a text node, NOT safe inside an HTML attribute —
and camera labels are operator-supplied and stored verbatim by `survey_api` (`.strip()[:64]`, no
sanitising). A label containing a double quote closes the attribute early and everything after it
is parsed as markup, which is script injection through the camera-naming form.

The new matrices are what EXPOSED it (they are the first code in `_PAGE` to put a database string
inside `title=` / `data-tip=`), but the weakness is in the shared helper and it predates them. So
this is a fix to the existing page that happens to arrive alongside two new tabs, not a feature
deploy you can defer because you have no use for the tabs. Ship it, then decide about the tabs.

Nothing that already used `esc()` renders any differently: in a text node `&quot;` and `&#39;` are
displayed as `"` and `'`.

## What landed

| | |
|---|---|
| table | `study_matrix (gateway_id, kind, period, counting_version, door_version, payload, computed_at, compute_ms)` |
| era key | `_trends_key(gw, '')` — the FLEET key: every camera's counting and door version, joined |
| precompute | stage 4 in `precompute_job.py`, after 2b, recorded as `precompute_run.study_s` |
| endpoints | `GET /dash/{gw}/riders_per_day`, `GET /dash/{gw}/rtt_fleet` |
| CSV | `dataset=riders_per_day` (wide, `split=riders\|boarded\|alighted`), `riders_per_day_long`, `rtt_per_hour` |
| page | two new tabs, `?view=riders` and `?view=rttfleet`, plus a **Download study bundle** button in every download bar |
| bundle | `GET /dash/{gw}/bundle` — one ZIP, seven CSVs and a README, filtered to the selected range |
| test | `tools/test_study_views.py`, `tools/test_study_bundle.py` |

## Three changes that touch the EXISTING views too

Found in review of this change, fixed here because the new views are what exposed them:

* **`esc()` now escapes `"` and `'`** — see the NOT OPTIONAL section at the top. This is the one
  that makes the deploy mandatory rather than elective: the helper is shared by every panel on the
  page, and the hole it left is reachable from the camera-naming form.
* **The date inputs in `periodBar()` are now scoped per view** (`dfrom-<mode>`). The views are
  hidden with `display:none`, not removed, so a bare `id=dfrom` existed three times and
  `getElementById` returned Trends' — picking a date on another tab read the wrong box and the date
  reverted on redraw.
* **`health_check._precompute_slow` and `precompute_run_latest` read the stage columns from the
  schema** instead of naming them. Only reason: between this deploy and the first sweep a box has
  rows but no `study_s`, and naming it would have raised, been swallowed, and reported "the job has
  never run" on a gateway with two weeks of sweep history.

## Deploy

    # 1. ship the code. dash_api.py is REQUIRED even if the new tabs are unwanted — it
    #    carries the esc() attribute-escaping fix for every existing panel.
    #    dash_api.py, precompute_job.py, health_check.py, tools/test_study_views.py
    systemctl restart liftlab-cloud

    # 2. FILL THE TABLE. Until this runs both tabs say NOT COMPUTED YET, by design —
    #    a read never triggers a derivation, so nothing fills it on first view.
    systemctl start liftlab-precompute.service     # or: precompute_job.py site-A

    # 3. confirm
    sqlite3 /var/lib/liftlab/gateway.db \
      "SELECT kind, period, length(payload), datetime(computed_at,'unixepoch','+5 hours 30 minutes') \
       FROM study_matrix ORDER BY kind, period"
    curl -s localhost:8000/dash/site-A/rtt_fleet?period=week | head -c 400

`study_matrix` and the `precompute_run.study_s` column are created by `CREATE TABLE IF NOT EXISTS`
and `ALTER TABLE` inside the job, so there is no manual migration. `precompute_run_latest` and
`health_check._precompute_slow` both read the column list from the schema rather than naming it, so
a box between this deploy and its first sweep keeps reporting its sweep history instead of going
silent — that regression window is the only reason those two read PRAGMA at all.

Rollback is `git revert` plus a restart. `study_matrix` is written only by the job and read only by
the two endpoints; leaving the table behind costs a few KB per gateway and breaks nothing.

## Cost, measured

On the fixture sweep (4 cameras, 3 days) stage 4 is **0.12 s of a 0.54 s sweep** — against 0.20 s
per-camera trends, 0.10 s RTT, 0.03 s aggregate, 0.02 s alphabet. That fixture is far too small to
rank the stages against each other (aggregate and alphabet scale with in-era rows and there are
almost none here); what it does establish is the SHAPE of stage 4's cost, which is why it is
expected to stay small on the live box:


* `riders_per_day` never materialises a transit row in Python. IST has no DST, so the day bucketing
  is `strftime('%Y-%m-%d', ts+19800, 'unixepoch')` and SQLite does the GROUP BY — seven aggregate
  queries in, cams x days cells out, whatever the tables hold. This is what makes a fleet-wide
  daily table affordable at all: the same table built by fetching rows and tallying them in Python
  is the shape that made `/dash/{gw}/data` never terminate.
* `rtt_per_hour` **does not walk a single door row.** It reads seven stored `rtt_window` payloads
  and re-shapes them. `test_study_views.py` asserts `_rtt_by_cam` is not called even during the
  FILL: a second RTT derivation is exactly what `rtt_core` exists to prevent, and RTT is the most
  expensive number in the system (~25 µs/row; 10.9 s for ch29's 436,486-row era).

If the sweep ever approaches the timer interval, stage 4 is not where to look. See
DEPLOY_trends_perf.md §3c — the shared read between the fleet and per-camera trends payloads is
still the first lever, and aggregate is still the largest stage.

## What these views refuse to do, and why

**RTT refuses a calendar range.** `/dash/{gw}/rtt_fleet?from_d=…` returns
`state="not served for this range"` rather than an answer. RTT is stored per rolling window on each
camera's current era; deriving one for arbitrary dates means walking seven cameras' door history on
the request path, which is the 152.8 s `/trends` this whole precompute layer was built to kill.
`dash_rtt` has refused the same way since RTT moved off the request path. The period buttons name
the windows that exist: Today / 7 days / 30 days / All → 1, 7, 30, 0 days.

**Riders DOES derive a custom date range**, on `dash_trends`'s rule: that is a deliberate question
someone has asked, it is not what the page loads, and every input is bounded in SQL. The response
says `cache.state="derived live"` so it is never mistaken for the precomputed one.

**An unrecognised `period` is refused with a 400**, by the endpoint and by the CSV. `_range_bounds`
maps anything it does not know to `(None, None, "all data")`, so `?period=ALL` or `?period=1` would
otherwise fall past the cache and run a fleet-wide all-history aggregation on the request path —
while labelling itself a custom date range, which it is not. A typo must not be able to buy the
most expensive answer in the system under a label that hides it.

## The absences, which are the whole point

Every defect in this dashboard's history has been an absence rendered as a number. Both views were
built against that list, and `tools/test_study_views.py` locks each one down.

**DARK IS NOT ZERO.** A cell reading `—` is a day the camera produced no rows in *any* stream —
transits, door reads, validated episodes. A cell reading `0` is a day it was watched and carried
nobody. They are drawn differently, the legend says which is which, and the `—` survives into the
CSV as a literal `—` rather than an empty field, because Excel reads a blank and a zero the same
way. This is `demand_log`'s rule and its glyph, deliberately: the study workbook and the dashboard
must not be able to disagree about the same day. A range where NO lift was observed on ANY day
shows a panel saying so instead of a grid of zeros.

**RIDERS ARE NOT PEOPLE.** A rider is one counted crossing of the door line. A round trip counts
twice; someone who rides four times counts four times. The caveat sits under the table — the table
is what gets screenshotted — and rides *inside* the CSV as `#` comment lines, because a note beside
a download link does not travel with the attachment.

**AN OUTAGE IS NOT OBSERVATION.** Transits inside a `DATA_GAPS` window are excluded from the count
AND the day is not credited as observed, so a gap reads dark rather than as a quiet day.

**EVERY LIFT GETS AN RTT ROW, AND EVERY EMPTY ROW CARRIES ITS REASON — in the table cell, spanning
the hour columns, not in a tooltip and not in a footnote.** Four kinds, told apart, because the one
a reader draws decides whether they go and look at a lift, a camera, or a timer:

| kind | means | what it must never be read as |
|---|---|---|
| `uncalibrated` | no door engine has ever posted for this camera | a lift that made no journeys |
| `designed` | the engine posts, but not the input RTT needs — `no_floor` / `no_era` / `no_rows`. RTT is defined by the home floor; without a floor read there is no trip to measure | a lift that made no journeys |
| `pending` | nobody has computed it yet | a measurement of zero |
| `refused` | the server declined — a row cap, an era ambiguity | an answer of none |

The row is guarded on **the data it is about to walk**, never on a whitelist of known-bad states. A
whitelist drifts out of date the moment the server gains a state — that is precisely how
`too_many_rows` took the entire trends view down on 2026-08-19 — so a state from a future build
still renders, with its own note reported verbatim rather than another absence's explanation
borrowed. `test_study_views.py` plants a synthetic `a_state_from_a_future_build` row to prove it.

The `rtt_per_hour` CSV repeats the reason on all 24 rows of an unmeasured lift rather than emitting
one summary row. That looks redundant until the file is pivoted to lift × hour — which is the shape
the dataset exists for — where a single summary row vanishes and the pivot shows 24 blanks with no
explanation anywhere in it.

**PENDING IS NOT ABSENCE, IN THE FILE TOO.** A cache miss serves a structurally complete skeleton
with every value NULL and `state="not_computed"` (the `_trends_skeleton` rule: a payload that omits
keys is not a smaller answer, it is a crash). The CSVs emit `# NOT COMPUTED YET` header lines rather
than a zero-row file, because an empty spreadsheet reads as "nothing was counted".

## The study bundle

`GET /dash/{gw}/bundle?period=…` returns `liftlab_<gw>_<from>_<to>.zip`. The button sits in all
three download bars — only one view is visible at a time, so the reader sees exactly one — and it
carries the same range as every other download on the page.

| file | grain | source |
|---|---|---|
| `riders_per_day.csv` | date x lift, all three splits as COLUMNS (`<cam>_riders`, `<cam>_boarded`, `<cam>_alighted`) + fleet totals | `study_matrix` |
| `riders_per_hour.csv` | lift x hour-of-day | `trends_cache` |
| `door_cycles_per_hour.csv` | lift x hour-of-day, with close-travel where it is measured | `trends_cache` |
| `occupancy_per_episode.csv` | one row per door-open episode | derived, bounded in SQL to the range |
| `rtt_trips.csv` | one row per round trip, plausible AND anomalous, reason in a column | derived, capped per camera |
| `eras.csv` | lift x era: door_version and counting_version boundaries with dates, precision, floor whitelist | derived, two GROUP BYs |
| `outages.csv` | one row per `DATA_GAPS` window | a constant, no query |
| `README.md` | every caveat the dash surfaces, **verbatim** | generated from the caveat registry |

**WHY A BUNDLE.** Each CSV link was already honest on its own. A study is not assembled one link at
a time, though: seven files arrive in seven downloads, in an order nobody records, and the caveats
stay behind on the page they came from. What gets mailed onward is a folder of numbers with no
provenance — and every misreading this system guards against becomes available again the moment the
sentence is separated from the number. So the README is INSIDE the zip, it is written FIRST into the
archive, and it names every file with at least one caveat under its own heading.

**THE CAVEATS ARE VERBATIM, AND THAT IS TESTED.** Each one is the same module constant the dashboard
prints beside the same number — `RIDERS_CAVEAT`, `RIDERS_DARK_NOTE`, `DIRECTION_RULE`,
`PRECISION_PER_ERA_NOTE`, `H3_CYCLE_CAVEAT`, `rtt_core.RTT_CAVEAT`, `OCC_LABEL` +
`OCC_CALIBRATION` + `OCC_ANCHOR_NOTE`, `FLOOR_WHITELIST_NONE_NOTE`. `test_study_bundle.py` asserts
each as an exact substring of the constant, not against a phrase retyped in the test — a paraphrase
in the test would let a paraphrase ship, and the first thing to drift is always the qualifier. The
README is generated from a registry keyed by filename, so a file cannot be added to the bundle
without its caveats arriving with it; the test walks that registry rather than a list of its own.

**BOUNDS.** Precomputed where precomputed exists, and each file's manifest line says which.

* The two hour-of-day files have **no live fallback**. On a custom date range they carry a state and
  a reason per camera instead of numbers, because `_trends_compute` is the 152.8 s derivation this
  whole layer exists to keep off the request path and running it for seven cameras inside one
  download would be that request seven times over. The period buttons are fully precomputed.
* `rtt_trips.csv` is the one expensive walk. It is capped at `DASH_BUNDLE_RTT_MAX_ROWS` (default
  `RTT_MAX_ROWS`, 120,000) door rows **per camera and it REFUSES rather than truncating** — a
  partial walk drops whole round trips and reports a median from part of the window. The refusal
  lands in the file as a row with the row count and the reason, never as an absent camera.
* One shared text budget (`DASH_BUNDLE_TEXT_BUDGET`, 64 MB) across the whole zip, not seven
  independent caps: the 10 MB target is a question about the download, not about any one member.
  CSV deflates ~8-12x. A file that stops short says so in its last line AND in the README — a short
  file that looks complete is the worst of the three outcomes.
* A wall-clock budget (`DASH_BUNDLE_BUDGET_S`, 60 s), armed exactly as `/data` arms its own. A
  timeout is a 503 naming the phase, **never a partial zip**: a bundle missing a file, with a README
  that lists it, claims a completeness it does not have.

**ABSENCES ARE ROWS.** A lift with no floor attribution appears in `rtt_trips.csv` with
`class=no_floor` and "UNAVAILABLE, not zero" in its reason, exactly as it appears in the table on
screen. A camera never simply fails to be in the file.

## Era discipline

`study_matrix` is keyed on the fleet era key, so ANY camera rolling its door or counting version
invalidates both matrices rather than leaving a fleet row half-current.

Within the riders matrix the counting build is carried **per cell**, from direct evidence
(validated episodes that day) where there is any and from the counting-version span otherwise; the
payload says which. A cell counted by a build that is not the one now running on that lift is
outlined and flagged `off_era`, and a range spanning more than one build gets a banner naming them.

Off-era days are **labelled, not dropped**. The day happened and its count is real; what is not true
is that it is comparable with today's. Dropping the rows would hide history — the 2026-07-30 "where
did my 41,789 pre-h2 rows go", which were era-hidden and not lost — and pooling them unlabelled
would compare two instruments. So they are shown, and marked, and the CSV carries
`counting_version` and `off_current_era` on every row.
