# DEPLOY — STARVED, NOT SILENT: the fourth health signal

**The gap.** On the Monday review of 2026-09-07, PL2B (ch30) had been counting 47-115 boardings/day
since the Sep 2 restart against ~500 before, while its door cycles ran a normal 601/day. 0.11
boardings per door cycle against 0.67-1.0 on every other lift. **The health line said nothing for
five days and was right by its own rules** — the camera was not silent. Fresh heartbeat, advancing
segments, transits every few minutes. All three existing signals ask *is anything arriving*, and the
answer was yes. PL4A did the same thing after a restart and recovered on its own two days later,
also unreported.

**Why no absolute threshold works.** `health_check.py`'s own docstring measured it: 51-55% of
daytime hours on a demonstrably live camera contain zero transits, and ch29's busiest day ever had
three empty hours. A count-based alarm fires on healthy cameras more than half the time. The
quantity that does *not* vary that way is the **ratio of what was counted to what the doors did** —
people board when the doors open — and it is a property of the counter, not of the traffic.

## The check

| | |
|---|---|
| metric | boardings (`transit_event.direction='in'`) per **door open**, per camera, per IST hour |
| door open | a transition INTO `door_state='open'`, `rtt_core.opens_with_floor`'s rule rendered in SQL with `LAG`, NULL states kept in the sequence |
| baseline | that camera's **own** median hourly ratio over `HEALTH_DEGRADED_BASELINE_D` (7) days |
| trigger | `HEALTH_DEGRADED_MIN_HOURS` (6) judged active hours below `HEALTH_DEGRADED_FRAC` (0.40) x baseline, within `HEALTH_DEGRADED_LOOKBACK_H` (24) |
| output | `[DEGRADED: STARVED, NOT SILENT — …]` on the health line; `degraded` + `starvation` on the payload |
| table | `starvation_check` |
| test | `tools/test_health_degraded.py` |

**Every camera is compared only with itself.** Fleet ratios span 0.67-1.0 in normal operation, so a
fleet-wide floor would either miss a starved busy camera or convict a healthy quiet one.

**Not door CYCLES.** A cycle is a transition into `closed` and lives in `dash_api._h3_cycle_ts`;
reimplementing it here would be a second copy of the number the whole MEP-02 sheet resolves to. An
*open* is the event a boarding belongs to, it is exactly expressible in SQL, and both sides of the
comparison use the one derivation — so the ratio cannot drift because two definitions disagreed.

**Thin hours are not judged.** Below `HEALTH_DEGRADED_MIN_OPENS` (10) an hour's ratio is arithmetic
on noise, and firing on it is exactly the wolf-crying the module rejects. Off-hours are skipped for
the same reason silence is not alarmed outside `ACTIVE_FROM`..`ACTIVE_TO`.

**No baseline is an UNKNOWN, not a clean bill.** Under `HEALTH_DEGRADED_MIN_BASE_HOURS` (24)
judgeable hours of history the camera reports `no baseline` and is never convicted.

**It is REPORTED, not a BREACH.** `ok` is untouched. Nothing is down — the lift runs, the camera
works, and the number is wrong. It is stated on the line every time until it is addressed, the rule
the config gaps follow.

**An era crossing in the window is named.** A counting build that changed inside the baseline can
move this ratio on its own, and "the counter was rebuilt" is a different finding from "the camera is
starved" — they lead to different boxes. The phrase says so when it applies.

**It does not run on the tick.** `evaluate()` is called every ~10 minutes and this walks days of
door rows through a window function. The signal is 6+ hours wide, so it recomputes at most every
`HEALTH_DEGRADED_INTERVAL_S` (1800 s) and serves the stored verdict with its age in between — the
read-never-derives rule this system runs on, applied to a check. A threshold change invalidates the
cache, so a change cannot appear applied when it is not.

## What the line looks like

    LiftLab health 10:30: all 4 cameras posted within the hour — OK [DEGRADED: STARVED, NOT
    SILENT — ch30 counting 0.1 boardings per door open (median) against its own baseline 0.8
    (12% of it) for 14 of 18 judged active hours, first at 2026-09-06 14:00. These cameras ARE
    posting and their doors ARE working; the counter is returning too little. Check the counting
    worker, not the stream.]

The last sentence is load-bearing. Every other named camera on this line means *nothing is
arriving*; this one means *too little is*, and without it the reader goes and checks a stream that
is fine.

Both numbers in the sentence are medians, deliberately. An earlier version reported the **mean** of
the recent window, which with a collapse 20 hours into a 24-hour lookback read "32% of baseline"
beside "14 of 18 hours below 40%" — two numbers about one camera that the reader has to reconcile.

There is no "consecutively" count. The obvious version counted adjacent entries in the *judged-hour
list*, which is not the same as adjacent hours — thin hours and the overnight gap are skipped, so a
run reported as consecutive could span a night. The count plus the hour it started says the same
thing without the claim that can be wrong.

## Deploy

    systemctl restart liftlab-cloud     # health_check.py runs from the same tree

`starvation_check` is created by `CREATE TABLE IF NOT EXISTS` on first run; no migration. The first
run after deploy does the full scan (~1 s per gateway on the fixture shape) and every run within the
next 30 minutes serves it.

**Tuning.** `HEALTH_DEGRADED_FRAC=0.40` is the number this was specified with, not a measured one —
it is the only threshold here that is a judgement rather than a derivation, and it is worth
revisiting once the fleet's normal spread of hourly ratios has been observed for a few weeks. The
payload carries `baseline`, `floor`, `recent_median`, `n_under` and `n_recent_hours` per camera every
run, which is exactly the evidence needed to set it from data instead.
