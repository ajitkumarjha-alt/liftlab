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
| **SUMMARY** | Range, generated-at, eras present, gaps excluded; headline close-travel median/p85/n vs the 2.00 s assumption and % over the 2.31 s Bank C cliff, **per instrument era**; the stopping-rule chart (running mean ± 95 % CI vs 2.31 — the study ends when the band clears the line). |
| **VS THE SHEET** | One row per MEP-02 v28 coefficient per era: C19, C26, C27, C17/C18, B24. Columns: assumption, observed, n, 95 % CI, decision threshold, verdict. Verdicts are mechanical (CI clears / straddles); unmeasurable coefficients say why. |
| **PER-LIFT** | One block per channel: bank, counting_version, **precision beside every count**, per-era door-cycle stats (median/p85/min-max/histogram), dwell distribution, transits with per-hour rate (gap-excluded), coverage %. |
| **FLEET** | **Unweighted sums**, labelled as such, with the per-lift precision range shown (never a silently-averaged precision). Grouped by bank; warns when the bank column is unpopulated. Fleet close-travel is `n/a — spans eras`. |
| **PEAK ANALYSIS** | Per day: worst 5-min boarding window (or the fixed window), peak:average ratio, peak-demand % when `--population` given; coefficients inside peak windows vs all-day, per era. |
| **RAW** | Row-level era-tagged export — the audit trail. Every row carries instrument, counting_version, era, precision-at-time, and an in-declared-gap flag. |
| **COVERAGE & ERAS** | Boundaries crossed with row counts each side, gap windows excluded, per-channel coverage % and row counts by era. A channel with zero rows is listed, not omitted. |
| **TIER-2 BLOCKED** | Floor-attribution status per channel (confident / no_read / ambiguous / invalid, glyphs seen) — documents why C21/C22 remain unmeasurable. |

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
  — CI wider than the value itself, or n < 30. The sheet says "not enough data
  to state a value (n=…; the range spans …, too wide to be useful)".
* **Load balance needs three lifts.** Across two points the coefficient of
  variation is degenerate (√2 whenever one is zero). Hours below the minimum get
  no row; if most hours fall below it the table is withheld with one line saying
  why. The coverage-spread warning stays regardless.
* **Lift naming** comes from `channel_map.label` — the building calls it "lift
  1", the gateway calls it ch16. The channel is appended on each lift's first
  mention per sheet ("lift 1 (ch16)"), and an unnamed lift is marked as such
  rather than given an invented name from its channel number.

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
