# INVESTIGATION — floors with stops and no riders (ch29: 51, 42, 56, 7, 52)

**Status: MEASURED. Answer is "neither phantom nor misattributed" — a third mechanism.** No change
proposed. Opened alongside `INVESTIGATION_ch16_second_mechanism.md`; the two turn out NOT to be the
same family, which is itself the finding.

---

## The question

Five ch29 floors show door cycles but zero attributed riders at counts where chance cannot explain it
(binomial p<0.01 against the camera's 14.5% base join rate):

| floor | stops | riders | expected at base rate | P(0) by chance |
|---|---:|---:|---:|---:|
| 51 | 73 | 0 | 10.6 | 0.0000 |
| 42 | 72 | 0 | 10.4 | 0.0000 |
| 56 | 51 | 0 | 7.4 | 0.0003 |
| 7 | 45 | 0 | 6.5 | 0.0010 |
| 52 | 30 | 0 | 4.3 | 0.0097 |

Six other zero-rider floors (40, 19, 15, 3, P5, 60) have too few stops to expect a rider at all and
are unremarkable. The five above are not.

Two hypotheses were proposed: **phantom** (the door never really opened, inflating any future
C17/C18 stop count) or **misattributed** (it opened at a different floor, which would corrupt
per-floor demand). The distinction matters, so it was measured rather than argued.

## Not phantom — the doors really opened

Peak `openness` reached during each cycle, from the raw trace:

| floor | cycles | median peak openness | min | cycles below 0.5 |
|---|---:|---:|---:|---:|
| 51 | 73 | **1.000** | 0.503 | **0 / 73** |
| 42 | 72 | 0.987 | 0.504 | **0 / 72** |
| 56 | 51 | 0.988 | 0.503 | **0 / 51** |
| *G (control, 220 riders)* | 344 | 1.000 | — | 1 / 344 |
| *68 (control, 61 riders)* | 103 | 0.999 | — | 0 / 103 |

Every one of these cycles reached a fully open door, and the distribution is indistinguishable from
the control floors that do attribute riders. **There are no phantoms here.** (Note this differs from
ch16, where ~46% of "episodes" are sub-3s excursions that never reach `closing` — hence the two
investigations are not the same family after all.)

## Not misattributed — the floor came from inside the cycle

| floor | floor borrowed from an EARLIER read | median age of the read used |
|---|---:|---:|
| 51 | 2 / 73 | **0.0 s** |
| 42 | 7 / 72 | 0.0 s |
| 56 | 0 / 51 | 0.0 s |
| *G (control)* | 8 / 344 | — |
| *68 (control)* | 22 / 103 | — |

The 10-second `DOOR_ATTR_S` borrow window is the obvious misattribution risk — a stop taking its
floor from a stale read of a floor the car was passing. It is not what is happening: essentially
every one of these cycles took its floor from a confident read **inside the cycle itself**, at a
median age of 0.0 s, and the controls actually borrow *more* (68: 21% vs 51: 3%).

## What it actually is: riders outside the join window

Widening the join from the door-open window to ±60 s around it:

| floor | riders in the door window | riders within ±60 s |
|---|---:|---:|
| 51 | 0 | **7** |
| 42 | 0 | **9** |
| 56 | 0 | **5** |
| 52 | 0 | 3 |
| 7 | 0 | 0 |

**The transits exist.** They fall outside `[open_ts, close_ts]`, so the join misses them. The stop is
real, the floor is right, the rider is real — the two are simply not overlapping in time.

That is a third possibility the phantom/misattributed dichotomy did not include, and it has a
different consequence from either:

* it does **not** inflate stop counts (the stops are genuine), so C17/C18 are unaffected;
* it does **not** corrupt per-floor demand by putting riders on the wrong floor;
* it **does** understate per-floor demand, floor-by-floor, by an amount that varies with how long
  each floor's doors stay open.

Floor 7 remains at zero even at ±60 s — the one case here that is not explained by window width and
is worth its own look.

## What has NOT been established

The exact offset distribution (are these transits systematically *after* the close, and by how much?)
is **still open**. An attempt was made and its output is **discarded as untrustworthy**, recorded here
so it is not repeated or half-remembered as a result:

```
        this attempt      the reconstruction that matches the dashboard
floor 51   986 stops                    73 stops
floor G  16363 stops                   344 stops
floor 7   4800 stops, 4607 of them with NO 'closed' row inside 60s (96%)
```

A 13-47x disagreement in stop counts means the second reconstruction was generating spurious cycles,
not that the first was wrong — floor 7 producing 96% unterminated windows is the tell. Its offset
medians (+10.6s, -11.3s, -16.8s, +71.3s) are therefore **not evidence of anything** and must not be
quoted. The lesson is the ordinary one: a derived measurement whose denominator does not reconcile
with a known-good one is a bug in the new code until proven otherwise.

Redoing it properly is the next step, because it decides the remedy: a symmetric tolerance
around the window is right if offsets straddle zero, whereas a systematic lag would point at clock
skew or a counting-line offset between the two engines and should be fixed at source instead.

**Do not widen the join window on the strength of this document.** ±60 s was a diagnostic probe, not
a proposal; at 3016 stops and 985 transits a wide window will happily attribute the same rider to
several stops.

## Consequence for the dashboard, already applied

The heatmap caption now states the attribution rate and says the two charts are not comparable
cell-for-cell, so a reader does not conclude from dense green against empty blue that the data is
broken. The corrected reading of those cells is: *the stop happened, at that floor, and the rider
the counter saw fell outside the window we joined on.*
