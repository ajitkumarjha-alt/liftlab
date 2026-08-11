# INCIDENT — the evidence gate refused every LIVE episode (2026-08-10 → 2026-08-11)

**A guard I added to stop fabricated data stopped almost all of it.**

## What happened

`aa39eb0` added an evidence gate to `post_episode`: refuse an episode claiming transits with no
detection evidence behind it. It was written against the ch29 churn case, where a transit latched in
a destroyed counter was flushed into a freshly-opened episode that had seen no frames.

The gate keyed on `det_counts` and `ids`. `gpu_analyze` collects those **only while validating** —
the accumulation sits inside `if val_state == "validating":`. In LIVE mode both are empty **by
design**, because a live episode is an audit record with no imagery, not a review item.

So the gate's condition was true for every live episode, and it refused all of them.

## Damage

`validation_item` rows stop dead across the whole fleet at the deploy:

    ch16  122   last 2026-08-10 12:00:24 IST
    ch27  131   last 2026-08-10 12:02:58
    ch29   80   last 2026-08-10 12:00:15
    ch30   77   last 2026-08-10 11:44:24
    ch32   95   last 2026-08-10 11:45:23
    ch34   89   last 2026-08-10 11:44:47
    ch37  120   last 2026-08-10 11:46:46

Seven of seven, within an 18-minute band, then nothing for ~27 hours. Not one camera degraded — all
of them, completely. The door-open audit trail for the entire fleet is missing for that window and
is **not recoverable**: the episodes were refused at the worker, so nothing was ever sent.

## Why the tests did not catch it

`tools/test_episode_gate.py` shipped WITH the gate and passed 4/4. Every one of its four cases
supplied a populated `det_counts` or asserted a refusal — i.e. it only ever exercised **validating
mode**. The mode in which the gate would actually spend almost all of its life was never tested,
so the tests confirmed the gate did what I intended and never asked where it would run.

## The lesson

**A gate must be tested in every mode it fires in.**

This is the same class as the smoke-harness gap that let the unbound-`t0` branch ship: a proof that
never exercised the production path. Both times the test was real, careful, and aimed at the wrong
execution: one tested the algorithm but not the startup path, this one tested the validating path
but not the live path. Passing tests measured coverage of my intent, not coverage of the code's
actual states.

Corollary worth keeping: **when a guard's inputs are populated conditionally, the condition is part
of the guard.** `det_counts` was not "the evidence" — it was "the evidence, if we happened to be
collecting evidence".

## The fix (`adb0034`)

The denominator is now `frames` — analysed frames, counted in BOTH modes, which is what "did we look
at anything" actually means:

* zero analysed frames → refused (the original 05:24:41 fabrication still caught);
* frames analysed but zero distinct track_ids **while a detection audit exists** → refused, with its
  own message (counted without a track);
* a live episode with real frames → **posted**.

`tools/test_episode_gate.py` now carries the live case explicitly, asserting a live episode with an
empty detection audit is posted, so this cannot return silently.

The same counter is the evidence denominator for peak car occupancy (`occupancy_frames`), which is
how the defect was found: building a feature that needed a mode-independent frame count exposed a
guard that had assumed a mode-dependent one.


---

# ADDENDUM — the occupancy proof brief named the wrong file (2026-08-11)

Not the same defect, recorded here because the lesson is the same shape.

## What happened

The brief for the peak-occupancy proof specified "ch30_peak.mp4 has a 6-person cabin at ~f5000-5200"
and set the expectation "the tracker should report >=4 there or the field is undercounting worse than
expected". The probe reported **peak = 1**.

The probe was right. `ch30_peak.mp4` f4900-5300 contains exactly one person, verified by sequential
decode. The six-person crowd is in **`ch30_full.mp4`** at f5400-5600 — a different file with a
similar name. The scene spec came from analyst memory spanning two similarly-named recordings.

## Why it nearly cost more than it did

The brief pre-committed a verdict to the number: "report >=4 or the field is undercounting". Had the
error not been caught, `peak = 1` would have been read as evidence that occupancy measurement was
broken — and the plausible next step is "fix" a field that was working, against a scene that was
never there.

## The lesson

**A scene spec is ground truth and needs the same provenance as any other ground truth.** A probe
brief that asserts what is in the frames must carry a verification frame, not a remembered one. This
project already voided a whole corpus over exactly this class (`9223bbb`, where hand-timed truth was
anchored to a spliced recording) and re-read frames by hand to settle a disputed row (`be80846`).
The rule established there — read the frames, do not recall them — applies to the input of a proof
as much as to its output.

Corollary, and the reason this sits beside the gate incident: **both failures were a correct
mechanism pointed at the wrong thing.** The gate's logic was right and ran in a mode it was never
tested in; the probe's logic was right and ran against a scene that was never checked. Neither was a
coding error. Both were verification aimed one level too shallow.

## What changed in the probe as a result

* **Decode-error counting.** The decoder's complaints are written by the C layer to fd 2, so they
  scrolled past uncounted. The probe now captures fd 2, counts the HEVC error lines, re-emits them
  all, and prints the count BEFORE any occupancy figure — a run over corrupt frames can no longer
  print a confident number and be quoted. Errors in the first ~50 frames are expected on any
  HLS-derived capture and are called out as such; errors inside the analysed range are not.
* **The sequential-decode invariant is documented at the loop it protects.** The probe reaches
  `from_frame` by decoding and discarding, never by `cap.set(POS_FRAMES)`: seeking HEVC lands
  mid-GOP and yields frames reconstructed against references that were never decoded — silently
  wrong rather than missing. The comment says so where someone would otherwise "optimise" it.
* **`peak_at` no longer implies information it does not carry.** It reported the FIRST frame
  achieving the maximum, so a constant occupancy of 1 always reported "peak at frame <first frame
  analysed>" — which read as a boundary artefact and was not. It now reports first..last, and says
  explicitly when occupancy was constant across every analysed frame.
