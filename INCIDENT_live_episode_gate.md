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
