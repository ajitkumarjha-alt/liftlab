# INVESTIGATION — the "second mechanism" on ch16

**Status: ANSWERED, NO CHANGE PROPOSED.** Read-only analysis. No engine code touched, no threshold
moved, **no fork of the close-travel era** — C27's provenance is unchanged by this document.

**Headline: the premise does not survive contact with the data.** ch16 is not dominated by a distinct
second mechanism. Roughly **half of what was being counted as a "door episode" on ch16 is not a door
cycle at all**, and the completion rate was a ratio with a contaminated denominator. The loss that
*is* real is **fleet-wide, not ch16-specific**.

---

## 1. What was claimed, and what is actually true

The claim was: *ch16 drops only 3.7 % of segments yet completes only 24.2 % of cycles, so something
else dominates there.* Both halves need correcting.

**First: `gw_door_event` is emit-on-change.** `door_event_changed` (`gpu_door.py:778`) keys on
`(floor, direction, door_state)` — **openness is not in the key**. A row is written on a state
transition, a floor/direction change, or the 60 s liveness heartbeat. So the gaps between rows that
the previous investigation read as *sampling holes* are largely gaps between **state changes**, not
gaps in observation. Inside a fetched segment the door is examined at 12.5 fps regardless of whether
anything is emitted.

That also explains ch16's alarming "p90 gap = 60.00 s": ch16 is **98.0 % `no_read`** on the floor
panel (the documented fault in `INCIDENT_ch16_floor_blind.md`), so floor/direction almost never
changes, so almost nothing but door-state transitions and heartbeats is emitted. Its low row count is
a *reporting* artifact of a known fault, not evidence of a blind detector.

## 2. ch16's door signal is healthy

Every quality measure is normal or near-normal:

| measure | ch16 | ch27 | ch29 | ch30 |
|---|---:|---:|---:|---:|
| distinct openness values (7 d) | 977 | 1001 | 1001 | 1001 |
| strictly interior (0 < x < 1) | 52.3 % | 57.9 % | 55.8 % | 68.0 % |
| median dwell in `open` | 2.12 s | 2.50 s | 7.93 s | 2.12 s |
| `open → closed` direct-jump median gap | **2.30 s** | **2.29 s** | 10.16 s | **2.01 s** |
| `open → closing` (the transition that matters) | 53 % | 67 % | 38 % | 64 % |

The openness signal is not quantised, not stuck, reaches both 0.0 and 1.0, and spends half its time
in transit. ch16's direct-jump gap is **indistinguishable from ch27's and ch30's**. On these measures
**ch29 is the outlier, not ch16.**

## 3. The denominator was contaminated: ~46 % of ch16's "episodes" are phantoms

Non-completing episodes, by duration:

| cam | 0–1 s | 1–2 s | 2–3 s | 3–5 s | 5–8 s | 8–15 s | 15–30 s | 30 s+ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **ch16** | **42.0 %** | 7.6 % | 5.3 % | 10.6 % | 7.3 % | 4.9 % | 7.8 % | 14.5 % |
| ch27 | 17.5 % | 9.9 % | 10.4 % | 14.4 % | 11.5 % | 13.0 % | 12.2 % | 11.1 % |
| ch30 | 19.2 % | 14.6 % | 16.5 % | 18.1 % | 11.8 % | 9.0 % | 5.1 % | 5.8 % |

**42 % of ch16's non-completing episodes last under one second** — against 17–19 % elsewhere. A whole
opening-to-closed sequence in under a second is not a passenger lift door; a single close alone takes
~2.5 s (C27). These are brief openness excursions that never reach `closing`.

Classifying an episode as a **phantom** when it lasts < 3 s *and* never reaches `closing`:

| cam | episodes | phantoms | % phantom | real-ish episodes | clean closes | **completion on real episodes** |
|---|---:|---:|---:|---:|---:|---:|
| ch16 | 8,997 | 4,188 | **46.5 %** | 4,809 | 670 | **13.9 %** |
| ch27 | 17,763 | 5,559 | 31.3 % | 12,204 | 2,457 | 20.1 % |
| ch30 | 9,211 | 3,812 | 41.4 % | 5,399 | 1,232 | 22.8 % |

Correcting the denominator moves ch16 from **7.4 %** to **13.9 %**, against peers at 20–23 %. A real
deficit remains, but it is a modest gap — **not a dominant second mechanism**.

## 4. What actually separates completing from non-completing episodes

One thing, overwhelmingly: **whether the episode ever reached `closing`.**

| | ch16 completes | ch16 does not | ch27 completes | ch27 does not |
|---|---:|---:|---:|---:|
| n | 667 | 8,330 | 2,450 | 15,313 |
| median duration | 14.53 s | 2.06 s | 8.15 s | 4.58 s |
| **reached `closing`** | **98.8 %** | **23.7 %** | **99.2 %** | **31.8 %** |
| median state changes | 4 | 3 | 4 | 3 |

Once an episode reaches `closing` it completes ~99 % of the time, on every camera. The funnel has a
single dominant stage, and it is `open → closing`.

## 5. The loss that IS real is fleet-wide

Two findings apply to every camera, not to ch16:

**Oscillation.** `closing → open` reversion rates: ch16 58.5 %, ch27 56.3 %, ch29 53.4 %, ch30 50.4 %.
**Over half of all closings revert to open rather than completing.** This is the largest single
funnel loss anywhere in the system.

Mostly it is real door behaviour rather than threshold jitter — the median reversion first descends
**0.216 (ch16) to 0.332 (ch30)** below `near_open = 0.90` before reverting, which is genuine travel,
not noise around the boundary. Only the shallow tail looks like jitter (ch16 16.7 % of reversions dip
< 0.05, ch29 10.1 %, ch27 3.8 %, ch30 0.0 %).

**Yield.** Even the best camera converts only ~23 % of real episodes into a clean close. The cycle
definition requires a monotone `closing → closed` run; real doors oscillate, so most episodes never
present one.

## 6. Detector, or input?

On the evidence: **input, and specifically ch16's field of view — but this cannot be settled from
stored data.**

Pointing at input: signal statistics are normal (§2), so the edge detector is functioning; the excess
is concentrated in *sub-second phantom excursions* (§3), which is what transient scene content — a
person crossing, a reflection, a lighting shift — produces; ch16 also has the highest jitter-shaped
reversion tail and the shallowest dips; and ch16 already has a documented framing/geometry fault
severe enough to make floor OCR 98 % `no_read`. A mis-framed door ROI would produce exactly this
signature.

Pointing away from detector: no threshold change can help here. The episodes that fail never reach
`closing`, and `close_th`/`near_open` govern transitions the signal never approaches on those
episodes.

**What cannot be determined from stored data, and what would settle it.** Emit-on-change discards the
openness trace between state changes, so per-frame signal quality is not recoverable from
`gw_door_event` — for ch16 only **35 usable same-state consecutive pairs** exist in 7 days, far too
few to characterise volatility. This is the same class of gap as the analyzer telemetry: *the data
needed was never persisted.* Settling it requires either (a) the 12.5 fps openness trace from the GPU
box for a sample window, or (b) a look at ch16's actual door-ROI crops against its geometry — a site
/ calibration question already on `SITE_VISIT_REQUIRED.md`.

## 7. Consequences for what has already been reported

* **The completion rates in `INVESTIGATION_door_cycle_coverage.md` §1 have a contaminated
  denominator** and overstate the loss. Corrected figures are in §3 above.
* **The "sampling holes" framing in that document's §3 is wrong in mechanism**: those are gaps
  between emitted state changes, not gaps in observation.
* **C27 is unaffected.** `close_travel_s` is computed inside the tracker at 12.5 fps and carried on
  the cycle; it does not depend on the emit gate. The bias test result stands.

## 8. What NOT to do

* Do not touch `MAX_BEHIND`, `close_th`, or `near_open`. The dominant loss is episodes that never
  reach `closing`; no threshold governs that, and moving one would fork the close-travel era for no
  measured gain.
* Do not treat ch16 as the priority. Its corrected deficit (13.9 % vs 20–23 %) is real but small
  against a fleet-wide yield of ~23 %.
* Do not quote "completion rate" without saying whether phantoms are excluded. The two numbers differ
  by roughly 2× on ch16.
