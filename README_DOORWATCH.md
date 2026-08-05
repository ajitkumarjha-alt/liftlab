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
