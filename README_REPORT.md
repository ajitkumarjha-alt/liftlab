# liftlab-report — repeatable, date-range-parameterised workbook export

Exports an Excel workbook of lift analytics from the gateway SQLite DB
(**read-only** — sqlite `mode=ro` + `PRAGMA query_only`; this tool cannot
write a gateway table). Runs on liftlab-cloud.

## Invocation

```
liftlab-report --from <ISO ts> --to <ISO ts> [--out <path>]
               [--peak-window auto|HH:MM-HH:MM] [--population N]
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

## The sheets

| Sheet | What it is |
|---|---|
| **SUMMARY** | Range, generated-at, eras present, gaps excluded; headline close-travel median/p85/n vs the 2.00 s assumption and % over the 2.31 s Bank C cliff, **per instrument era**; the stopping-rule chart (running mean ± 95 % CI vs 2.31 — the study ends when the band clears the line). |
| **VS THE SHEET** | One row per MEP-02 v28 coefficient per era: C19, C26, C27, C17/C18, B24. Columns: assumption, observed, n, 95 % CI, decision threshold, verdict. Verdicts are mechanical (CI clears / straddles); unmeasurable coefficients say why. |
| **PER-LIFT** | One block per channel: bank, counting_version, **precision beside every count**, per-era door-cycle stats (median/p85/min-max/histogram), dwell distribution, transits with per-hour rate (gap-excluded), coverage %. |
| **FLEET** | **Unweighted sums**, labelled as such, with the per-lift precision range shown (never a silently-averaged precision). Grouped by bank; warns when the bank column is unpopulated. Fleet close-travel is `n/a — spans eras`. |
| **PEAK ANALYSIS** | Per day: worst 5-min boarding window (or the fixed window), peak:average ratio, peak-demand % when `--population` given; coefficients inside peak windows vs all-day, per era. |
| **RAW** | Row-level era-tagged export — the audit trail. Every row carries instrument, counting_version, era, precision-at-time, and an in-declared-gap flag. |
| **COVERAGE & ERAS** | Boundaries crossed with row counts each side, gap windows excluded, per-channel coverage % and row counts by era. A channel with zero rows is listed, not omitted. |
| **TIER-2 BLOCKED** | Floor-attribution status per channel (confident / no_read / ambiguous / invalid, glyphs seen) — documents why C21/C22 remain unmeasurable. |

Charts are native Excel charts: close-travel histograms (2.00 and 2.31 are
bin edges), the stopping-rule running-mean chart, hourly boarding/alighting
profiles, dwell distributions, and the coverage timeline.

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
run and the read-only connection refuses INSERTs.
