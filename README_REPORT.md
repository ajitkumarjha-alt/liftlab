# liftlab-report — repeatable, date-range-parameterised workbook export

Exports an Excel workbook of lift analytics from the gateway SQLite DB
(**read-only** — sqlite `mode=ro` + `PRAGMA query_only`; this tool cannot
write a gateway table). Runs on liftlab-cloud.

## What the study is

Empirical calibration of lift **design factors**, measured from cabin CCTV in
handed-over, occupied residential towers. Traffic analysis for new towers is
driven by assumed coefficients from the MEP-02 v28 standards table, which has
never been validated against a real occupied building. This measures what
actually happens and feeds corrected factors back into the sheet.

Two questions: (1) was the lift selection correct for this project — count,
speed, capacity — given how the building is actually used? (2) can the same
coefficients be reused for future projects, or do they need revising and by how
much? Scope is 5 projects, one at a time, ~1 week of footage each. It is a
**benchmarking study, not a compliance audit**, and any one export is one site's
measurements toward that.

Eight coefficients are under test (C17, C18, C19, C21, C22, C26, C27, B24).
READ THIS FIRST carries a scope table showing each one's status **computed from
the data in that export**, so the reader sees what the measured part is a
fraction of.

The 2.31 s figure is a **sensitivity result, not the purpose**: banks differ in
round-trip slack, and on the tightest-margin bank a door-close time above the
assumed 2.00 s is enough to flip its design case. Door-close matters because the
sheet multiplies it by ~15 probable down-stops, making it the most leveraged
term — not because 2.31 is a target.

The highest-value output is **not in the RTT calibration at all**: the 8 %
handling-capacity figure is a *demand* assumption, measurable directly from the
cameras with no floor attribution. Well below it means over-design; well above
means an undiagnosed service problem.

**The machine emits facts; humans draw conclusions.** The workbook reports
observations beside assumptions with n and interval, and mechanical verdicts
(clears / straddles / not measurable). It never recommends a lift count,
declares a bank compliant, or advises a design change.

## Invocation

```
liftlab-report --from <ISO ts> --to <ISO ts> [--out <path>]
               [--format csv|xlsx|both]
               [--peak-window auto|HH:MM-HH:MM] [--population N]
               [--min-close 0.5]
               [--db <path>] [--gateway site-A] [--banks lift_banks.json]
```

* `--from` / `--to` — range (end exclusive). Naive timestamps are read as
  **IST (+05:30)**, the building's clock.
* `--out` — output path. Default: `/mnt/user-data/outputs` if present, else
  `./reports`, with a timestamped filename.
* `--peak-window` — `auto` (default) finds the worst 5-minute boarding window
  per day; `HH:MM-HH:MM` fixes the window.
* `--population` — tower population; enables peak-demand % vs the 8 % HC
  design assumption. Without it the boarding count is reported and the % left
  blank.
* `--min-close` — the one-frame quantization floor in seconds (default 0.5, or
  `$LIFTLAB_MIN_CLOSE_S`). See below.
* `--db` — defaults to `$GATEWAY_DB`, then `/var/lib/liftlab/gateway.db`.
* `--banks` — bank sidecar (see below).
* `--format` — which outputs to write (default `both`).
  * `csv` — writes the DEMAND LOG only, as two CSVs beside `--out`:
    `<name>_demand-log-hourly.csv` and `<name>_demand-log-daily.csv`. No workbook.
  * `xlsx` — the workbook only. It contains the same rows as a **DEMAND LOG** sheet.
  * `both` — both of the above.

Setup on liftlab-cloud (one-off):

```
pip install openpyxl        # into the venv that runs the analysis stack
sudo ln -s /opt/liftlab/pi-scripts/liftlab-report /usr/local/bin/liftlab-report
```

## ⚠ pi_watch and gpu_engine are NOT comparable

The Pi door-watch (`gw_event`) and the GPU DoorFloorEngine (`gw_door_event`)
are **different instruments** — different edge detector, clock and sampling —
split at **2026-07-21T00:00:00Z**. Their close-travel figures are never
comparable and this workbook **never pools them**: every figure is computed
per `(channel, instrument, era)`. Where a cell would need cross-era pooling it
says `n/a — spans eras` instead of printing a number. A range that straddles
any boundary still produces a workbook, with the boundary declared on
COVERAGE & ERAS and a warning banner on every affected sheet.

Other declared boundaries: `CLOSE_TRAVEL_MAX 10→30` (2026-07-16, pi_watch
sub-era), per-camera `door_version` template rebuilds (each is a new GPU era),
counting_version epochs (transit eras), and the optional DoorTracker guard cut
(`DASH_DOOR_GUARD_TS`, same env the dashboard uses).

Known data gaps (the Jul-31 → Aug-01 outage etc.) are a **declared table** in
`liftlab_report/eras.py::DATA_GAPS` — append new gaps there; they are excluded
from every rate/duration statistic and listed on COVERAGE & ERAS.

The workbook also **auto-detects suspected gaps**: a channel going silent for a
whole day, or for ≥6 h, while producing rows on both sides. These are **flagged
on COVERAGE & ERAS and never auto-excluded** — declaring a gap stays a human
decision. Confirm the cause, append to `DATA_GAPS`, re-export.

## ⚠ The one-frame quantization floor

Door travel is measured by frame sampling, so every value is a multiple of the
frame interval. That interval is **detected empirically on every run**
(`stats.detect_quantum` — modal spacing, jitter-clustered, least-squares
refined) and never hardcoded, so it follows `analyze_fps` if that changes. On
the live gateway it detects as 0.08 s (12.5 fps).

Two guards follow from it, both reported rather than applied silently:

* **`--min-close` (default 0.5 s).** A lift door does not close in a few frames;
  values that short are the door state flickering between frames. They are
  rejected from *every* close-travel statistic. PER-LIFT shows median and p85
  **both pre- and post-filter** so the effect is auditable, COVERAGE & ERAS
  gives the rejected count and share per channel/era, and a pool losing more
  than 5 % to the floor raises a banner on SUMMARY.
* **Resolution-bound suppression.** When more than 20 % of a pool's values sit
  *at* the one-frame quantum, the measurement is bound by the instrument, not
  by the door. No verdict is emitted; the reason is printed in its place
  (`not measurable — measurement at sampling-resolution floor (X % of values at
  the N s quantum); needs higher analyze_fps or …`). This currently suppresses
  **door-open travel on every lift** — the v1 workbook's claim that doors clear
  the 3 s open assumption was an artifact of this floor, not a result.

## The sheets

| Sheet | What it is |
|---|---|
| **READ THIS FIRST** | Sheet 1. Opens with WHY THIS STUDY EXISTS (the programme, the two questions, the benchmarking scope) and WHICH COEFFICIENTS ARE UNDER TEST (all eight, with per-export status). Written for a reader new to the project (MEP reviewer, manager). What this measures; one line per sheet; numbered findings each with **Evidence** (sheet + cell range + chart) and **Confidence** (HIGH / MEDIUM / TOO EARLY TO SAY, with the reason and the n that would move it); what we cannot say yet and what it would take; how much data this really is; a glossary; the colour legend. Every claim is **generated from the computed data** — no hardcoded prose about specific numbers — and a finding whose metric was suppressed says so. |
| **SUMMARY** | Range, generated-at, eras present, gaps excluded; **C27 door operating time** — observed close travel median / 95 % CI / p85 / n against the 2.00 s assumption, and % beyond the 2.31 s flip point, **per instrument era**; the widest disagreement between two lifts; the fleet comparison chart and the stopping-rule chart (running mean ± 95 % CI — the study ends when the band clears the line). Notes that C27 is one of eight coefficients, pointing at the scope table. |
| **VS THE SHEET** | One row per MEP-02 v28 coefficient per era: C19, C26, C27, C17/C18, B24. Columns: assumption, observed, n, 95 % CI, decision threshold, verdict. Verdicts are mechanical (CI clears / straddles); unmeasurable coefficients say why. |
| **PER-LIFT** | One block per channel: bank, counting_version, **precision beside every count**, per-era door-cycle stats (median/p85/min-max/histogram), dwell distribution, transits with per-hour rate (gap-excluded), coverage %. |
| **FLEET** | **Unweighted sums**, labelled as such, with the per-lift precision range shown (never a silently-averaged precision). Grouped by bank; warns when the bank column is unpopulated. Fleet close-travel is `n/a — spans eras`. |
| **DEMAND BY LIFT AND HOUR** | Per counting era (never pooled): **mean boardings/alightings per observed day, by hour** — the coverage-comparable figure — with the **total observed** counts beneath each as whole people. Totals are *not* comparable between lifts, because coverage ranges widely and a lift watched longer shows a bigger total regardless; the caption says so and points back at the means. Dark hours read `—` in every matrix, never 0. Then the observed-days matrix behind every cell, busiest hour per lift, peak 5-min demand vs the 8 % assumption (or an explicit BLOCKED without `--population`), and load balance. Carries the **canonical busiest hour** with its definition. |
| **DEMAND LOG** | The flat table: **one row per hour per lift, plus a FLEET row per hour**, and a daily rollup (date, lift, totals, hours observed, coverage %). The least interpreted sheet in the workbook — no means, no modelling, no charts — so any headline figure elsewhere can be checked against the counts it came from. Identical rows to the CSVs. Dark hours read `—`, never 0. |
| **PEAK ANALYSIS** | Per day: worst 5-min boarding window (or the fixed window), peak:average ratio, peak-demand % when `--population` given; coefficients inside peak windows vs all-day, per era. |
| **RAW** | Row-level era-tagged export — the audit trail. Every row carries instrument, counting_version, era, precision-at-time, and an in-declared-gap flag. |
| **COVERAGE & ERAS** | Boundaries crossed with row counts each side, gap windows excluded, per-channel coverage % and row counts by era. A channel with zero rows is listed, not omitted. |
| **TIER-2 EVIDENCE** | Floor attribution as it really is. Read quality per camera **per era** (a rebuild is a different instrument); floors-per-second between consecutive confident reads with n; the arrow-direction distribution; and a verdict per coefficient naming **its own** blocker. Was titled TIER-2 BLOCKED and reported zero confident reads fleet-wide — see the correction below. |

Charts are native Excel charts. Every one gets visible tick labels on **both**
axes with an explicit number format, horizontal gridlines (vertical off), a
y axis anchored at 0, a legend only where there is more than one series, a
deliberate size so charts never overlap, and a one-line plain-English
**caption** above it saying what to take away. The chart set:

* **close-travel histograms** — bins coloured and legended by band (at/below the
  sheet assumption · over it but within the cliff · over the cliff), so the
  reader can tell which edge is which without counting bars;
* **fleet comparison** — one bar per lift, median with 95 % whiskers, cliff line
  across, bars red where the median is over the cliff. The chart to read first;
* **stopping rule** — generated for *every* lift/era with n ≥ 30, best-sampled
  pool repeated on SUMMARY;
* **hourly boarding profile**, **dwell distributions**;
* **coverage timeline** — declared outages shaded as grey bars behind the
  per-channel lines, so an explained dip reads differently from an unexplained one.

Formatting: Arial throughout, column widths sized to content, freeze panes below
every header, autofilter on RAW and PER-LIFT, explicit number formats everywhere
(seconds `0.00"s"`, percentages stored as fractions, counts `#,##0`, timestamps
`yyyy-mm-dd hh:mm`), amber-filled warning banners, verdict cells coloured to the
convention documented on READ THIS FIRST, and landscape fit-to-width print setup
with repeating header rows.

## One figure, one computation

Any figure quoted on more than one sheet comes from a single computation
(`model.canonical_figures`). v3 stated three different fleet busiest-hours
because three places each computed their own. The canonical fleet busiest hour
is stated once on DEMAND BY LIFT AND HOUR with its definition in words; a sheet
needing a different definition (FLEET's raw pooled totals, a per-era block) must
name it and say how it differs. Tests assert every unqualified busiest-hour
statement resolves to the canonical value, and that any other stated peak is
explicitly scoped.

## Reporting rules the narrative enforces

* **Three-way threshold comparison** at the precision the reader sees: a median
  displaying as 2.31 against a 2.31 line reads *sits exactly ON the line*, never
  above or below.
* **No point estimate in result language** where the interval cannot support one
  — CI wider than **half** the value (`stats.MAX_CI_WIDTH_RATIO`, default 0.5),
  or n < 30. A median of 2.75 s with an interval of [1.52, 4.25] is consistent
  both with a lift under the assumed value and one far over it, so no figure is
  quoted; the sheet states the spread and why instead.
* **Findings lead on the assumption gap**, not on a compliance count. Finding 1
  opens with what the sheet assumes against the range actually observed (with
  n); the compliance count follows as a consequence of that gap. Chart
  take-aways follow the same rule.
* **Load balance needs three lifts.** Across two points the coefficient of
  variation is degenerate (√2 whenever one is zero). Hours below the minimum get
  no row; if most hours fall below it the table is withheld with one line saying
  why. The coverage-spread warning stays regardless.
* **Lift naming** comes from `channel_map.label` — the building calls it "lift
  1", the gateway calls it ch16. The channel is appended on each lift's first
  mention per sheet ("lift 1 (ch16)"), and an unnamed lift is marked as such
  rather than given an invented name from its channel number.

## ⚠ Correction (2026-08-04): the Tier-2 sheet was reporting zero confident reads

**The headline verdict did not change — C17, C18, C21 and C22 remain unmeasurable — but almost
everything the workbook said *about why* was wrong.**

`fetch_floor_read_status()` counted a read as confident only when `reason` was `''` or `'ok'`. The
door engine emits `ok` when **two** panels agree, and `single_panel` when only one panel is
calibrated and that panel read successfully. A single-panel camera therefore emits `single_panel`
for **every good read it will ever produce** and never `ok`. Scoring those as non-confident reported
the entire fleet as floor-blind while ch27/ch29/ch30 held roughly 150,000 confident reads between
them. `dash_api.DOOR_OK_REASONS` and `door_event_api.py` already used the wider set; this module was
the outlier. The definition now lives once, in `eras.FLOOR_OK_REASONS`.

`single_panel` is a read that passed the same per-panel bar as each half of an `ok`; what it lacks is
the cross-check, not quality. The residual risk is real and is stated on the sheet: a single panel
cannot catch a **systematic** misread, which is what the derived floor alphabet defends against.

### The verdicts were hardcoded, and one comment said the opposite

`model.py` emitted `"not measurable — needs floor attribution"` and `narrative.py` set `BLOCKED`
for all four coefficients unconditionally, regardless of the data. Meanwhile `eras.py` claimed the
status was *"computed per run from the data, never declared, so the table cannot drift"* — true of
the other four coefficients, false of these. Both are fixed: blockers are now derived by
`model.coefficient_blockers()` and each coefficient names what actually stops it.

### What actually blocks each one

* **C21 / C22 (speed factors).** Floor attribution is **not** the blocker. Floors-per-second is
  measured now, from consecutive confident reads, with thousands of segments per camera. But C21/C22
  are a share of **rated speed**, and converting floors/s needs the inter-floor distance and the
  car's rated speed — neither is held in this database. Same class of gap as C19's rated capacity.
  The sheet reports floors/s and says plainly that it is not a speed factor.
* **C17 / C18 (probable stops).** Floor attribution is **not** the blocker either. The blockers are
  (a) **degenerate arrow direction** — ch27 reads 100 % `down` and ch30 83 % `up` / 0 % `down`, which
  is physically impossible and means the ROI or reader is miscalibrated, so any up/down split built
  on it is unsound; (b) a large share of door cycles carry no attributable floor, so stops-per-trip
  would undercount; (c) trip segmentation is not implemented in this report.
* **ch16 alone is genuinely floor-blind** — 1 confident read in its current era. That turned out to
  be its own fault: see `INCIDENT_ch16_floor_blind.md`.

### Resolved — `gpu_era_id()` no longer truncates the era

`gpu_era_id()` took `door_version.split('+')[0][:8]`, assuming the templates half is exactly 8
characters. Live rebuilds carry a hand-added tag (`e79e50d3h2`, `260d4a0fh2Laa52`), so truncation
merged a rebuild into the era it replaced — and because that function also groups door **cycles**,
close-travel was being pooled across rebuild boundaries.

**Measured before fixing: 7,539 of 10,571 cycles (71 %) sat in a merged group.**

| cam | pooled (before) | split (after) |
|---|---|---|
| ch16 | n=680, median 2.185, CI [1.965, 2.443] | `e79e50d3` n=84 median 3.046 · **`e79e50d3h2` n=596 median 2.122 CI [1.901, 2.380]** |
| ch27 | n=2417, median 3.258, CI [3.111, 3.449] | `425f92e1` n=33 median 2.319 · **`425f92e1h2` n=2384 median 3.278 CI [3.130, 3.457]** |
| ch29 | n=171, median 2.231, CI [1.442, 3.225] | `260d4a0f` n=28 · `260d4a0fh2` n=42 · **`260d4a0fh2Laa52` n=101 median 2.129 CI [1.176, 3.383]** |

**No verdict flipped** — every CI that straddled the 2.00 s assumption still straddles it, and
ch27's still clears it. But every reported n and CI moved, and the direction of the error is the
dangerous one: **pooling made the study look more converged than it is.** ch29's true current-era CI
is 0.42 s *wider* than the pooled one. Since the stopping rule ends the study when the CI band
clears the line, an artificially narrow CI is the one error this report must not make. Fixed, with
two regression tests.


## The DEMAND LOG export

`boarded` = `transit_event` rows with `direction='in'`; `alighted` = `direction='out'`. These are
**door crossings, not people** — one person crossing twice counts twice.

Three rules the export exists to enforce, each with a test that fails if it regresses:

1. **Dark is never zero.** A camera not observed in an hour is written `—`. `0` means the camera
   *was* watched and counted nobody. Reading the first as the second understates demand exactly
   where coverage is worst. Observation comes from coverage buckets (any row in any stream), not
   from transit rows, so a lift that was up and genuinely idle still reads `0`.
2. **Counting versions are never pooled.** The grouping key includes the counting version in
   effect at that timestamp, so a camera spanning two builds inside one hour produces two rows.
   FLEET rows are per version for the same reason. The daily rollup carries a `counting_version`
   column — an addition to the requested columns, because splitting is the only way to avoid
   blending two builds into one daily total.
3. **Gap rows are excluded** and those hours are not credited as observed, matching
   `aggregate_transits()`.

Coverage % uses **hour slots judged by the hour's own midpoint** on both sides of the ratio. An
earlier version counted observed hours by 15-minute bucket midpoint while subtracting gap
*seconds* from the denominator; real data then produced `190.5%` coverage, because a bucket can
land just outside an outage and credit an hour the denominator has already written off. Observed
hours are now a subset of usable hours, so the ratio is ≤ 100 % by construction, not by clamping.

### What the export deliberately does not contain

* **Persons currently in the lift.** The system counts crossings, not occupancy. A running
  boarded-minus-alighted figure would accumulate each camera's counting error across every cycle
  and drift without bound. Per-camera precision is printed in the CSV header so the size of what
  would compound is visible.
* **Any per-floor breakdown.** The note is **measured, not asserted**: it prints confident floor
  reads per camera over the requested range (non-null floor with `reason` in `ok`/`single_panel`,
  the door engine's own definition) and names the cameras that are effectively floor-blind.
  Attribution is not uniform — on 2026-08-02 ch29 read 32682/39046 while ch16 read 0/6114 — so a
  per-floor table would be solid for some lifts and fabricated for others.

> ⚠ `reader.fetch_floor_read_status()` counts a confident read as `reason` in `''`/`'ok'` only.
> The engine actually emits `single_panel`, so that function currently scores **every camera at
> zero** confident reads. `fetch_floor_confidence()` (used by this export) uses the engine's
> definition. Both are kept — they answer different questions — but do not read one as the other.

The caveats are written into the CSV as leading `#` comment lines rather than shipped separately:
a demand table forwarded without its "dark is not zero" line will be misread. `pandas` skips them
with `comment='#'`; a spreadsheet shows them as text rows above the header.

## Banks

`lift_banks.json` maps channels to banks. All channels default to blank /
UNKNOWN and **the tool never guesses** — populate the file and re-run.

## Tests

```
python -m pytest test_liftlab_report.py -q
```

Covers: era-straddling ranges produce per-era aggregates and no pooled
number; gap windows are excluded from pools and rate denominators; an empty
range produces a valid workbook; the DB file is byte-identical after a full
run and the read-only connection refuses INSERTs; the frame quantum is detected
rather than assumed (and detects *nothing* on ungridded data); sub-floor closes
reach no close statistic by any route and the floor is configurable; suppressed
open travel never emits a verdict; Jul-24/27 are declared while an undeclared
silence is flagged-not-excluded; every finding on READ THIS FIRST names a cell
range that really holds the number it quotes; every chart has readable axes and
a caption; no number lands in a `General` cell.
