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
