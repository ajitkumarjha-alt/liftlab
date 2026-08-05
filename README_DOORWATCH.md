# h2 BASELINE — door-cycle engine vs hand-timed video (2026-08-05)

Produced by `tools/doorwatch_replay.py` on dev-box, corpus `stopwatch-2026-08-05`,
tracker h2 UNMODIFIED. Both videos 704x576 @ ~25fps, ROI scale 1.0 (matches calib
`frame_wh`). Replayed at `--frame-stride 2` = 12.5fps, which is what production
actually runs (`DOOR_STRIDE=2`), not 25fps.

## The table (this is h3's acceptance test)

| | ch30 | ch27 |
|---|---:|---:|
| gradable hand-timed closes | 11 | 9 |
| DETECTED | **9** | **4** |
| MISSED | 2 | **5** |
| PHANTOM (inside verified-closed windows) | 0 | **4** |
| UNMATCHED emissions | 29 | 22 |
| total events emitted | 38 | 30 |

```
=== ch30  tracker=h2  ch30_full.mp4 ===
  video 704x576 @ 25.0fps  roi=(2, 2, 335, 446)  openness span=1.0 (min 0.0 max 1.0)
  emitted 38 close events; hand-timed closes gradable: 11

  DETECTED   9/11
  MISSED     2
  PHANTOM    0   (events inside verified door-CLOSED windows)
  UNMATCHED  29  (emitted, no truth within 5s, not in a phantom window)

   osd(truth)   osd(emit)    d_s  hand_s  engine_s   err_s  status
     12:04:57    12:04:56    0.2    2.60         -       -  clean
     12:05:49    12:05:44    4.2       -         -       -  occluded
     12:07:39    12:07:41    2.7       -         -       -  occluded
     12:08:55    12:08:50    5.0    2.10      2.08   -0.02  clean
     12:11:32    12:11:27    4.5       -      0.32       -  complex
     12:12:16    12:12:13    2.8    2.20         -       -  clean
     12:12:47    12:12:44    2.1       -         -       -  clean_untimed
     12:13:28    12:13:24    3.9       -         -       -  clean_untimed
     12:14:53    12:14:51    1.7    2.50         -       -  clean
    travel error: n=1 mean=-0.02s min=-0.02s max=-0.02s

  MISSED: 12:08:16(boundary), 12:10:48(clean_untimed)

  UNMATCHED emissions: 12:05:45(-s), 12:05:46(-s), 12:05:47(-s), 12:05:54(-s), 12:06:25(-s), 12:06:34(1.60s), 12:07:46(1.92s), 12:07:53(-s), 12:08:02(-s), 12:08:07(0.56s), 12:08:09(0.72s), 12:09:03(-s) ... +17 more

=== ch27  tracker=h2  ch27_full.mp4 ===
  video 704x576 @ 24.96fps  roi=(125, 3, 238, 397)  openness span=1.0 (min 0.0 max 1.0)
  emitted 30 close events; hand-timed closes gradable: 9

  DETECTED   4/9
  MISSED     5
  PHANTOM    4   (events inside verified door-CLOSED windows)
  UNMATCHED  22  (emitted, no truth within 5s, not in a phantom window)

   osd(truth)   osd(emit)    d_s  hand_s  engine_s   err_s  status
     12:08:52    12:08:52    0.8       -      3.77       -  clean_untimed
     12:10:03    12:10:00    2.6    2.80         -       -  clean
     12:12:08    12:12:06    1.9       -      0.56       -  clean_untimed
     12:14:07    12:14:04    2.4       -      0.32       -  clean_untimed

  MISSED: 12:08:00(clean_untimed), 12:11:01(clean_untimed), 12:11:39(clean), 12:13:13(clean_untimed), 12:13:50(clean_untimed)

  PHANTOMS inside verified-closed windows:
    12:05:26  travel=9.54s   [closed and empty; h2 emitted 5 events 1.8-16.6s]
    12:05:36  travel=4.01s   [closed and empty; h2 emitted 5 events 1.8-16.6s]
    12:06:04  travel=2.00s   [closed and empty; h2 emitted 5 events 1.8-16.6s]
    12:06:42  travel=20.03s   [closed and empty; h2 emitted 5 events 1.8-16.6s]

  UNMATCHED emissions: 12:07:35(1.76s), 12:09:48(-s), 12:09:50(0.64s), 12:09:57(-s), 12:10:21(-s), 12:10:50(2.96s), 12:11:13(13.86s), 12:11:58(0.72s), 12:12:50(0.40s), 12:12:51(-s), 12:12:54(-s), 12:12:57(-s) ... +10 more
```

## What the replay reproduces

**Phantoms — CONFIRMED.** ch27 emitted 4 events inside the verified closed-and-empty window
12:05:06–12:06:45, with travels 9.54s, 4.01s, 2.00s and 20.03s. Two of those (4.01, 2.00) sit
squarely inside the [0.5, 30] filter and are indistinguishable from real closes downstream. The
hand-timed note said "5 events 1.8–16.6s"; the replay finds 4 in that window at stride 2.

**Missed closes — CONFIRMED, and worse on ch27 than the note suggested.** ch27 detected only 4 of 9
(12:08:00, 12:11:01, 12:11:39, 12:13:13, 12:13:50 all missed — including 12:11:39, one of the two
hand-timed clean cycles). ch30 detected 9 of 11.

**Emission volume — NEW, and not in the hand-timed table.** h2 emitted **68 close events against 20
gradable real closes** — 3.4x more events than there are doors closing. 51 of them match no real
close at all and fall outside the three verified-closed windows, so they are neither scored as
detections nor as phantoms. The three hand-audited windows were sampling a much larger population:
the phantom rate is not 4 events, it is most of what the engine emits.

## What the replay does NOT confirm

**The low-travel bias is UNPROVEN by this run.** Only one matched pair had both a hand-timed travel
and an engine travel: ch30 12:08:50, engine 2.08s vs hand 2.10s, error **-0.02s** — not the ~0.3s
low bias reported. Every other matched cycle had `close_travel_s` absent (the engine matched the
cycle but emitted no travel) or no hand-timed value to compare against. **n=1 supports nothing.**

Do not treat "h3 fixes the low bias" as testable against this corpus until more matched pairs exist.
That needs either more hand-timed travels on cycles h2 actually detects, or the reason `close_travel_s`
is None on most matched cycles investigated first — the latter is likely the more informative question,
because an engine that detects a close but cannot time it is its own defect.

## Caveats on the run itself

* The mp4s are concatenated `.ts` segments and the decoder logs `Could not find ref with POC` /
  `Error constructing the frame RPS` at segment joins. Frames around those joins may be dropped or
  corrupt, which plausibly contributes to both misses and spurious edges. A frame-accurate rerun
  would need clean re-encodes.
* Match tolerance is +/-5s per spec. ch30's matches run 0.2-5.0s off, several near the limit; a
  tighter tolerance would reclassify some detections as misses. The +/-4s segment-overlap drift
  called out for ch30 is visible in that spread.
* `truncated` rows are excluded from gradable; `boundary`, `occluded`, `complex` and `clean_untimed`
  are included, so "detected" counts cycles that were visible but not hand-timed.

---

## CORRECTION (same run, re-read with the ch30 concat inversion in hand)

The ch30 concat has a segment-ordering inversion around **video t=210-212**, which with
`--osd-base 12:04:48` maps to **OSD 12:08:18-12:08:20**. Two of ch30's scores are artefacts of it,
and both were reported above as if they were engine behaviour.

**1. ch30's `PHANTOM 0` is wrong — the true count is 2.**
The hand-timed window (12:08:20-12:08:40, "closed and crowded") records h2 emitting **0.56 and
0.72**. The replay emitted travels of exactly **0.56s and 0.72s** — at **12:08:07 and 12:08:09**,
13-31s *before* the window. Identical values to two decimal places: these are the same two events,
displaced out of the scoring window by the inversion. They were counted as UNMATCHED rather than
PHANTOM purely because of a timestamp shift in the corpus.

**2. One of ch30's 2 misses is the inversion, not a miss.**
The missed close at **12:08:16** is the row already flagged `boundary` in the ground truth, and it
sits inside the inversion zone. It should not be charged to h2.

Corrected ch30 line, and what h3 is actually measured against:

| | ch30 as first reported | ch30 corrected |
|---|---:|---:|
| DETECTED | 9/11 | **9/10** |
| MISSED | 2 | **1** |
| PHANTOM | 0 | **2** |

ch27 is unaffected — its mapping is continuous and verified at 60s intervals, with only the stray
11:36:58 chunk excluded by `--skip-before 30`.

**The general point, worth more than the two numbers:** a scoring window that depends on wall-clock
alignment silently mis-scores when the corpus timeline is not monotonic. ch30's phantoms did not
disappear, they moved — and the harness reported the reassuring number. Any future corpus needs its
timeline verified as monotonic before its scores mean anything, or the phantom windows need to be
anchored to something other than OSD.

---

## DECISION 2 diagnosis: why matched closes emit no `close_travel_s`

One run, h2 instrumented to capture the raw `ct = close_full - close_start` that `_emit()` rejects.

```
reject band: ct < 0.3 or ct > 30.0   (close_th=0.1, near_open=0.9)

ch30, 9 matched cycles          ch27, 4 matched cycles
  truth     raw ct   hand        truth     raw ct   hand
12:04:57      0.08    2.6      12:08:52     3.766      -
12:05:49      0.0       -      12:10:03      0.08    2.8
12:07:39      0.0       -      12:12:08     0.561      -
12:08:55      2.08    2.1      12:14:07      0.32      -
12:11:32      0.32      -
12:12:16      0.0     2.2      -> kept 3, rejected-low 1, rejected-high 0
12:12:47      0.16      -
12:13:28      0.0       -
12:14:53      0.08    2.5
-> kept 2, rejected-low 7, rejected-high 0
```

**Every rejection is sub-minimum. Not one is over-max.** The raw values are 0.0, 0.08, 0.16 s — that
is **0 to 2 frames** at the 12.5 fps the door pass actually runs. Against those, the hand-timed truth
for the same cycles is **2.1-2.6 s**.

So `close_start` and `close_full` are being stamped on the same frame, or one frame apart, for closes
that physically took two and a half seconds. The tracker is not mis-measuring the descent — **it is
not observing the descent at all.** It sees `open`, then `closed`, and stamps both ends of a
2.4-second motion inside 80 ms. `_emit()` then correctly refuses to call 0.08 s a close travel, and
the cycle surfaces with `close_travel_s = None`. The plausibility filter is working exactly as
designed; what it is protecting against is an openness signal that jumps rather than traverses.

This is the tracker-level view of the fleet-wide finding already recorded in
`INVESTIGATION_door_cycle_coverage.md` — openness is bimodal with almost no mass between 0.10 and
0.30 — and it reframes the "clipped travel, ~0.3 s low" description from the video session. On these
cycles the measured interval does not shrink by 0.3 s, it **collapses to near zero**: 7 of ch30's 9
matched cycles came in at <=0.16 s.

**What this means for h3.** The traversal gate cannot be validated on a signal that only ever shows
endpoints. At 12.5 fps a 2.4 s close should present ~30 intermediate leaf positions; these cycles
present 0-2. So h3's first obligation is not gating but *sampling* — establishing whether the leaf's
intermediate positions are recoverable from the ROI at all (edge column mid-descent), because if they
are not, monotonic-traversal gating has nothing to be monotonic over and would reject real closes as
readily as reflections. That question is answerable from the same corpus by dumping the per-frame
openness trace across a known real close, and it should be settled before the gate is written.

---

## OPENNESS TRACE: the signal is not a measurement of door position

The trace DECISION 2 asked for, run on ch30 12:08:55 (the correctly-timed cycle) and ch30 12:12:16
(the collapsed one), same camera, same ROI, same run, stride 2 = the production 12.5 fps. Emission
counts reproduce the h2 baseline exactly (ch30 38, ch27 30), so the tracker is fed identically —
only the instrumentation is new.

Tools, all replayable: `tools/openness_trace.py` (per-frame dump of all four stages),
`tools/openness_validity.py` (the three tests below), `tools/openness_labels_20260805.csv`
(hand labels). `tools/descent_audit.py` and `tools/anchor_audit.py` are the intermediate
measurements that failed to discriminate; they are kept because their failure is the reason the
question had to be re-posed.

### The branch does not resolve to A, B or C

It resolves to a prior question that the h3 design assumed away.

**The good cycle's openness does ramp.** ch30 12:08:50 descends 0.98 → 0.74 → 0.63 → 0.49 → 0.32 →
0.13 → 0.07 over 26 analyzed frames / 2.16 s, against a hand-timed 2.10 s. It is a clean traversal,
exactly what a gate would want.

**It is not a close.** Frames sampled every 0.25 s across that cycle show the door *opening* between
video t=238.50 and 239.00 and standing open through 242.75, with passengers walking out through it.
The tracker stamps `close_start` at 240.00 and `close_full` at 242.00 — both inside a continuously
open doorway. The 2.08 s "close travel" is a two-second slice of an open door, and it matched a real
close 5.0 s away, at the exact edge of the ±5 s tolerance.

So the corpus contains **zero validated engine travels**. The one number that made h2 look
occasionally right is a coincidence, and 3013116's corrected ch30 line is the more accurate summary
of the engine than the travel-error row ever was.

### Three tests, none of which the engine produced

**1. openness disagrees with the door at its own thresholds.** 48 frames sampled at `openness>=0.95`
("fully open") and `<=0.05` ("fully closed"), spread across both runs, hand-labelled from the ROI
crop:

| | agrees with the door | states the OPPOSITE | uncertain |
|---|---:|---:|---:|
| both cameras, 48 frames | 31 (65%) | **16 (33%)** | 1 (2%) |

ch30 `openness=1.00` was a physically closed door in 5 of 12 samples; ch27 in 5 of 12. A coin flip
scores 50%. `near_open` and `close_th` are exactly where the state machine commits.

**2. The raw edge column is not separable.** Column values for physically-open and physically-closed
frames overlap over 65% (ch27) and 71% (ch30) of the labelled set. ch27 is the clearest case: at
12:05:11 col=130 reads `openness=0.00` and at 12:05:14 col=137 reads `openness=1.00` — 3 seconds
apart, visually identical frames, a 7-pixel difference spanning the tracker's entire range. No
renormalisation of a column recovers a door position that the column does not encode.

**3. The reference collapses and clips, and the abstain path is dead.**

| | ch30 | ch27 |
|---|---:|---:|
| geometric range of col (p2..p98) | 152.0 px | 90.0 px |
| rolling reference span, median | 43.1 px (28%) | 34.0 px (38%) |
| frames with span < half geometric | 69% | 81% |
| frames pinned at exactly 0.0 or 1.0 | 26% | 29% |
| frames rejected by `min_strength=0.30` | **0 / 7954** | **0 / 7944** |

`strength` is 1.000 on every frame of both runs. `door_edge_column`'s honest-None contract —
"it either locates the edge or says it couldn't" — never fires, because "row peak beats 3× the row
mean" is satisfied by any textured row. The function always answers, and what it answers is *where
the strongest vertical gradient in the ROI is*, which is the leaf only sometimes.

### Why, physically

The cameras are inside the car looking at the doors, and the ROI covers the doorway. Full-frame
renders show passengers standing in it for long stretches — a person is a far stronger vertical
gradient than a door leaf. The rolling p10/p90 then normalises against a distribution dominated by
whoever is standing there, which is why the reference span sits at 28-38% of the geometric range and
why a 7 px wobble can span the full scale.

### Consequences

**h3 is not implemented, and the traversal gate as designed is not viable on this signal.** A gate
keyed on monotonic traversal of `openness` would be gating on a quantity that contradicts the door a
third of the time at its decision points. It would reject real closes and admit reflections on the
same evidence. This is not a threshold that needs tuning.

**The discretization point DECISION 2 asked for is identified** — the rolling p10/p90 clip in
`DoorTracker.openness`, 26-29% of frames pinned — but fixing it is not sufficient, because test 2
shows the underlying column does not carry the state.

**The proposed replacement is a candidate, not a validated signal.** A whole-ROI bright-pixel
fraction does ramp smoothly (96.8% of ch30 frames land strictly between the thresholds, vs 42.4% for
openness), which is consistent with the independent frame analysis. But anchoring each close on its
10→90% crossing did **not** recover hand-timed travel: ch30 mean error +1.85 s (range -0.18 to
+4.94), ch27 mean -1.60 s. A crude global threshold over an ROI full of passengers is not yet a door
sensor. Calibrated polarity/threshold per camera may fix it; that has to be *shown*, on this corpus,
before anything keys on it.

**What is actually blocked.** Every door number downstream rests on a signal that has never been
validated against the door. The next step is not a tracker revision — it is establishing a door-state
signal that clears test 1, and the ROI placement (passengers occupy the measured region) looks like
the first thing to change. That likely needs the site visit already flagged in
`SITE_VISIT_REQUIRED.md`, or at minimum a re-marked ROI validated against hand labels.

### Caveats on this run

* The 48 labels are single-observer, from ROI crops, by one pass of the eye. They are enough to
  reject "openness measures the door" at 33% opposite, and not enough to grade a replacement.
* Windows are ±8 s around hand-timed closes and can include adjacent events; ch30's ~4 s
  segment-overlap drift and the 12:08:18-20 concat inversion (3013116) both sit inside this corpus.
* 27% (ch30) / 46% (ch27) of analyzed frames are pixel-identical to the previous analyzed frame — a
  static scene in an H.264 stream, not necessarily corruption, but it means "frames" and
  "observations" are not the same count.

---

## DOOR SIGNAL v2 — validation result: STATE passes, TRAVEL fails, h3 NOT built

Per the validation-first order: signal, then validate, then (only on a pass) a tracker. The signal
is built by `tools/door_signal_v2.py`, scored by `tools/door_signal_validate.py`. openness is not
rehabilitated anywhere — it is replaced.

### The signal

Closed-door template NCC, per camera, over a TOP BAND (y 15-85, x spanning the doorway). The
template is the pixel-wise median of 14 hand-labelled closed frames per camera — different floors,
different passengers — so it is the leaf, not any one scene. Leave-one-out NCC on held-out closed
frames: ch27 median 0.996, ch30 median 0.791.

Candidates were computed side by side (full-ROI NCC, top-band NCC, top-band motion, bright fraction
at two thresholds, ROI mean) so validation could choose rather than the author assuming.

### TEST A — STATE: PASS on both cameras

48 hand labels, scored at each signal's own best threshold, with leave-one-out so the number is not
just the fit to its own labels. 0 labels fell inside ch30's concat-inversion zone (video t=209-213),
so none needed excluding; 1 ch27 label is `uncertain` and is excluded.

| signal | ch30 AUC | ch30 LOO | ch27 AUC | ch27 LOO |
|---|---:|---:|---:|---:|
| **ncc_top** | **1.000** | **95.8%** | **1.000** | **95.7%** |
| ncc_roi | 0.950 | 87.5% | 0.960 | 91.3% |
| bright_160 | 1.000 | 95.8% | 0.778 | 87.0% |
| top_motion | 0.943 | 79.2% | 0.782 | 52.2% |
| *openness (h2, from 5e58211)* | *—* | *65%* | *—* | *65%* |

`ncc_top` is the choice: AUC 1.000 on both, LOO ~96%, and it is the only candidate that is strong on
both cameras. The constraint's prediction holds — a global bright-fraction rule is fine on ch30
(bright_160 LOO 95.8%) and collapses on ch27 (87.0% at AUC 0.778, i.e. it separates only because the
threshold lands well, not because the distributions are apart). Top-band *motion* fails both: the
ROI has passengers moving in it constantly, so ch30's motion baseline sits at 2-4 rather than 0.

### TEST B — TRAVEL: FAIL on both cameras

Closes are sustained rising transitions of `ncc_top`; travel is the near_open->close_th crossing
span of DECISION 1, transplanted onto the validated signal.

| hand osd | hand_s | matched | travel_s | err_s | |
|---|---:|---|---:|---:|---|
| ch30 12:04:57 | 2.6 | 12:04:57 | 0.24 | -2.36 | FAIL |
| ch30 12:08:55 | 2.1 | 12:08:59 | 1.84 | -0.26 | pass |
| ch30 12:12:16 | 2.2 | 12:12:19 | 2.00 | -0.20 | pass |
| ch30 12:14:53 | 2.5 | 12:14:57 | 1.92 | -0.58 | FAIL |
| ch27 12:10:03 | 2.8 | 12:10:04 | 2.32 | -0.48 | FAIL |
| ch27 12:11:39 | 3.2 | 12:11:39 | 2.16 | -1.04 | FAIL |

**ch30 2/4, ch27 0/2. Neither camera passes, so h3 is not built for either.**

Four principled estimator families were tried before stopping — 10->90 crossing as specified,
full-ramp extent, derivative-based ramp foot, level-crossing, plus top-band motion duration. Their
answers for the same six closes differ by more than a second, and no family reached better than 2/4
and 1/2. **That spread is the finding.** With n=6, a corpus that splices single frames (ch30 video
t=9.8 jumps 0.48->0.96->0.48 in one frame, which is what breaks 12:04:57), and an NCC-to-position
mapping that is S-shaped rather than linear, this corpus cannot validate a travel measurement to
+/-0.4s. Continuing to tune would be fitting an estimator to six points, not validating one.

Two structural notes, since they bound any retry:
* **The 10->90 spec is definitionally narrower than the hand timing.** Hand travel is first motion to
  fully closed, i.e. 0->100%; a 10->90 crossing of a linear ramp is 0.8x that by construction. A
  perfect signal fails the literal spec by ~20%. The table above therefore uses the near_open->close_th
  form, which is what DECISION 1 actually defines and what the hand timing measures.
* **NCC saturates.** Once the leaf covers the band, further motion barely moves NCC, so NCC-time is
  not proportional to position-time. Timing travel off NCC needs an NCC->position calibration that
  this corpus has no way to fit.

### What the validated STATE signal alone does for detection

Not h3, not a tracker, no travel — just `ncc_top` transitions scored the way `doorwatch_replay.py`
scores h2, for evidence toward the next decision:

| | ch30 h2 (corrected, 3013116) | ch30 v2 state | ch27 h2 | ch27 v2 state |
|---|---:|---:|---:|---:|
| DETECTED | 9/10 | **10/11** | 4/9 | **8/9** |
| MISSED | 1 | 1 | 5 | **1** |
| PHANTOM | 2 | **0** | 4 | **0** |
| UNMATCHED | 29 | **9** | 22 | **4** |
| total emissions | 38 | **19** | 30 | **12** |

ch27 needs no alignment correction and its numbers stand as they are: 8 of 9 real closes found with
match offsets 0.2-3.8s, zero phantoms in the verified-closed windows, and 12 emissions against 9 real
closes instead of h2's 30.

**ch30's row carries a caveat that ch27's does not.** It uses a -4.1s correction for the documented
segment-overlap drift. Uncorrected, ch30 scores 8/11 detected, 1 phantom, mean offset 3.63s. The ~4s
drift is independently recorded in this file from the video session, but the exact -4.1s was measured
from these same detections, so the corrected ch30 row is partly circular and should not be quoted
without this sentence. ch27's is not.

### Verdict and what is actually needed

**h3 does not ship, on either camera.** TEST A passing is not sufficient — a tracker that knows
open from closed but cannot time the descent reproduces exactly the DECISION 2 defect on a better
signal, and the acceptance bar at 3013116 includes travel.

What is blocked and on what:
* **ch27** — state is validated (LOO 95.7%, AUC 1.000, template LOO NCC 0.996) and detection is
  strong. Travel needs either more hand-timed travels on this camera (n=2 supports nothing, per the
  same standard applied to h2's n=1) or an NCC->position calibration. This is the cheaper of the two
  gaps and does not obviously need a site visit.
* **ch30** — state is validated, but its template is visibly weaker (LOO 0.791 vs ch27's 0.996)
  because passengers stand in the ROI, and its corpus has both the ~4s drift and single-frame
  splices. This is the camera the ROI re-mark in `SITE_VISIT_REQUIRED.md` would help most: a top band
  that clears the passengers, and a clean re-encode without splices.

Nothing deployed. No acceptance criterion weakened; the bar remains 3013116 with DECISION 1 travel
rules, and h3 has not been measured against it because it has not been written.

---

## CLEAN-CORPUS REVALIDATION — TEST A passes harder, TEST B fails for a new reason

Everything below is on the clean corpus (`ch27_clean.mp4` 70260f @24.8910fps, `ch30_peak.mp4`
27257f @24.9945fps), frame-anchored truth, templates rebuilt from clean-corpus closed frames on a
train/test split, and TEST A labels derived from the truth rather than from my eye.

### TEST A — STATE: PASS on both, more strongly than before

Labels are now truth-derived: **closed** = door-closed windows (test split; the template is built
from the disjoint train split), **open** = the frames just before each clean close's `start_f`,
where the door is open and not yet moving. No eyeballing.

| | ch30 63064cb | ch30 clean | ch27 63064cb | ch27 clean |
|---|---:|---:|---:|---:|
| `ncc_top` AUC | 1.000 | 0.958 | 1.000 | **1.000** |
| `ncc_top` LOO acc | 95.8% | 89.6% | 95.7% | **99.1%** |
| template LOO NCC (median) | 0.791 | **0.992** | 0.996 | **0.997** |
| labels | 24 eyeball | 48 truth-derived | 24 eyeball | 108 truth-derived |

**TEST A: PASS, ch27 and ch30.** ch30's clean template is transformed (0.791 -> 0.992): the dirty
template was diluted by passengers standing in the ROI. Its LOO accuracy reads lower than 63064cb's
only because the label set changed — truth-derived open labels sit at the moment of first motion,
which is genuinely the hardest instant to call, where the old eyeball labels were sampled at a
broken signal's confident extremes. The two numbers are not comparable and the clean one is the
honest one.

Two candidate reversals worth recording: `top_motion` now PASSES on ch30 (AUC 0.979, LOO 95.8%)
where it failed at 79.2% on the dirty corpus, because clean door-closed windows have genuinely zero
motion; and `roi_mean`/`bright_128` collapse to chance (AUC 0.50-0.55) on both cameras, so the
brightness family is dead on ch27 *and* ch30 once the labels are honest.

### TEST B — TRAVEL: FAIL on both, and the reason is not tuning

| | ch27 | ch30 |
|---|---:|---:|
| timed truth closes | 11 | 4 |
| detected / travels produced | 10 | 3 |
| within +/-0.4s | **1** | **2** |
| DECISION 1 (>=4 in-band AND all produced in-band) | **FAIL** | **FAIL** |

Four estimator families, run side by side by `tools/travel_estimators.py`:

| family | ch27 in-band | mean abs err | max abs err | bias | ch30 in-band |
|---|---:|---:|---:|---:|---:|
| level (near_open->close_th) | 1/10 | 1.85 | 5.83 | +0.33 | 2/3 |
| r1090 (the literal spec) | 1/10 | 1.97 | 5.43 | -0.08 | 2/3 |
| **foot (derivative ramp foot)** | **2/10** | **0.68** | **0.93** | **-0.12** | 2/3 |
| motion (top-band motion duration) | 0/10 | 1.84 | 2.24 | -0.76 | 0/3 |

**Closest family: `foot`** — lowest spread by a wide margin (max error 0.93s vs level's 5.83s) and
near-zero bias. It still fails.

**The reason is not calibration.** On ch27's n=10, every family's estimate is *anti*-correlated with
hand travel — Pearson r = -0.83 (level), -0.82 (r1090), -0.57 (foot), -0.62 (motion); Spearman
-0.29 to -0.47. The closes the signal calls long are the ones the hand called short. A signal that
ranks its own measurements backwards is not carrying travel information, and no affine correction
recovers it.

**And the acceptance criterion cannot see this.** Fitting the best affine correction to `level`
yields "10/10 within +/-0.4s" — but with slope **-0.104**, i.e. a disguised constant. The hand
travels span only 1.56-2.40s with sd 0.28s, so the +/-0.4s tolerance is **1.4x the truth's own
standard deviation**:

> **A constant predictor that ignores the video entirely and always answers 2.03s scores 11/13
> (85%) within +/-0.4s.**

So `>=4 of 15 in tolerance` is passed by a stopped clock. The binding clause in DECISION 1 is
"all produced values in tolerance", and the only reason the engine fails it is that its estimates
are noisy, not that they are uninformative — a constant would pass more easily than a real
measurement. This is a defect in the test, not only in the signal, and it should be fixed before
the next attempt: either tighten the tolerance to well under 0.28s, or hand-time closes with real
travel spread (the current set is nearly constant), or score correlation rather than a per-row band.

### Why the signal cannot time travel: the top band saturates

Concrete instance, ch30 truth close 20459->20511. `ncc_top` crosses into closed at frame **20420**,
39 frames *before* the truth's `start_f`, holds ~0.96-0.98 across the entire hand-timed close, and
crosses again at 20508 — 3 frames from `close_full`. The state machine, already closed since 20420,
never re-arms, so the close is scored as not detected.

The top band was chosen precisely because passengers do not occlude it (5e58211). But the leaf
covers a band above head height **before** it finishes covering the doorway below. So `ncc_top`
reaches its final value early and its remaining motion is asymptotic — excellent for *state*, which
is why TEST A passes at AUC 1.000, and structurally unable to time the *end* of a close. That is one
mechanism producing both the anti-correlation and ch27's systematic +10..+58 frame lag in
`close_full`.

### Verdict

**h3 is not built. TEST B fails on both cameras with n=15 on clean, continuous, frame-anchored
data — this is a real signal limitation, as anticipated, and the run stops here as instructed.**

What the next attempt needs, in order:
1. **Fix the acceptance test.** A criterion a constant passes cannot validate a measurement.
2. **A position proxy that does not saturate.** The top band is the wrong place to time a close even
   though it is the right place to detect one. A band at or below mid-door height, or the NCC
   *residual* against both an open and a closed template, would keep moving through the whole
   descent. State and travel may simply need different regions — nothing requires one signal to do
   both, and TEST A's result says the state half is solved.
3. **Hand travels with real spread.** Ten closes between 1.56s and 2.40s cannot discriminate
   estimators; deliberately include slow/obstructed closes.

Not deployed. No acceptance criterion weakened — the criterion is reported as too weak, which is the
opposite, and the h3 travel path stays blocked on both cameras.
