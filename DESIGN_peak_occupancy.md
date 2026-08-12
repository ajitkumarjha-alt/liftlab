# PEAK CAR OCCUPANCY — what it measures, what it does not, and the anchor decision

The figure shipped on the dashboard and in the workbook's CAR LOADING sheet is the largest number of
**distinct tracks simultaneously inside the cabin zone** during one door-open episode.

It never accumulates. That is the whole reason it exists: the workbook already carries a
crossing-derived occupancy (cumulative boarded − alighted) which went **negative in most periods**
and is shipped as a rejected method, not as a number. A per-frame count of simultaneous presence
cannot drift and cannot go negative. The two are different instruments and must never be added,
averaged, or presented as versions of one figure.

It is a **floor**. Everyone the detector missed, everyone occluded behind another body, and everyone
whose foot point fell outside the polygon is absent from it.

    measured minimum; ~0.5x at heavy crowding (n=1 scene, ch30); undercount grows with crowding

That sentence is `dash_api.OCC_CALIBRATION`, mirrored into `liftlab_report/eras.py` with a test
asserting the two have not drifted. It travels in the `/data` and `/trends` payloads, on the camera
panel, under the trend chart, and at the top of the CAR LOADING sheet — the number is not quotable
without it.

---

## The pair test — FOOT vs CENTRE, run live on ch30

The obvious hypothesis for the undercount was the anchor: occupancy uses the **foot** point, the same
membership point the transit counter uses, and a foot leaves the cabin polygon before the person
does. `tools/occupancy_probe.py --anchor both` computes both memberships from the **same detections
on the same frame**, so the anchor is the only variable.

| scene | frames | result |
|---|---|---|
| `ch30_full.mp4` f5350-5650 — the crowd | 151 | centre < foot on **117/151** |
| `ch30_full.mp4` f5125-5250 — door open, boarding | 63 | centre < foot on **63/63**; foot constant at 2, i.e. **zero lobby bleed** |

**Centre measured LOWER, not higher.** `zone_cabin` is a **floor polygon**: it describes where feet
stand, and a body's middle sits above it. A box whose top is truncated at the frame edge loses its
centre above the polygon while its foot stays inside. This was predicted from synthetic zones before
the live run and the live run confirmed it — outcome three of the three the probe was written to
distinguish.

So the gap is **not** an anchor bug, and switching anchors would have made the number worse while
appearing to be a fix.

### Decision (2026-08-11)

Ship on the **FOOT anchor**, with the calibration stated everywhere the number appears. The foot
anchor is also the one that showed no lobby bleed during boarding, which is the failure mode that
would have made the figure actively misleading rather than merely conservative.

---

## BACKLOG — not now

**Occupancy-specific body-volume zones, plus a centre anchor.** The real fix is a second polygon
describing the cabin *volume* as projected into the image, rather than reusing the counting path's
floor polygon. With that, a centre anchor becomes the correct membership point and the undercount
should shrink. This is a calibration artefact per camera, in the same family as the door templates,
and it needs its own validation — a zone that overreads is worse than a floor that underreads,
because it stops being a bound.

Not started. Recorded here beside the evidence that motivates it so the reasoning is not
re-derived from scratch, and so that "why not centre?" has an answer that is not "we did not try".

**Constraint on any future fix:** `counting.cabin_ids(dets, anchor=...)` deliberately does **not**
route the centre anchor through `_zone_of`. That helper is the counting path's definition of "where
is this person", and transit direction semantics depend on a point that crosses the threshold
cleanly. Occupancy may use a different membership point; counting may not. An unknown anchor raises
rather than silently falling back.

---

## What would turn the floor into a count

Paired hand counts. `tools/weekly_stopwatch.md` LEG 2 Step 4b collects them: while hand-timing a
close, count the people in the car at the fullest moment and write it in the truth CSV's
`peak_occupancy` column. They land in `validation_item.human_occupancy`, and CAR LOADING pairs each
against the machine peak for the same episode and prints the ratio **per pair** — deliberately not
averaged into one factor until the pairs span several lifts and several crowding levels.

The `~0.5x` is one scene on one camera. It calibrates the **direction** of the error and the rough
size. It is not a correction factor and must not be applied as one.

---

## Related

* `INCIDENT_live_episode_gate.md` — the evidence gate that refused every live episode, found while
  building this feature (occupancy needed a mode-independent frame count and the gate had assumed a
  mode-dependent one), plus the addendum on the probe brief that named the wrong file.
* `tools/occupancy_probe.py` — the offline probe, including the sequential-decode invariant and the
  decode-error counting that make a run over corrupt frames unable to print a confident number.
* `counting.py::cabin_ids` — the membership function, both anchors.
