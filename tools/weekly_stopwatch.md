# Weekly stopwatch — the standing check on the door engine

One page. Run it every week. The harness that graded h2 and h3 does not get retired when h3 ships —
it stays live as the instrument's conscience, because the database is the engine's own opinion and
cannot grade the engine. Video the engine never saw is the only ground truth available.

---

## STATUS: ARMED — 2026-08-05, on h3 state-only

Armed at `9f79051`, which met the acceptance table in README_DOORWATCH.md § "What h3 must reach":
ch30 8/8 detected (target >= 7/8), ch27 13/13 (target >= 11/13), phantoms 0 on both (target 0).

**The engine this page now grades is `h3-state`, and it does not measure travel.** Three passes of
TEST B failed to validate a travel estimator — most recently `0680e57`, with both mandatory
implementation fixes in, where the constant-2.03s predictor's MAE (0.23s) beat every real estimator
by 5-16x. h3 therefore emits `close_travel_s = None` on every cycle with a reason string, by design.
Alerts 3 and 4 below are written for that: they check that the null is honest and that travel is
being sampled by hand, NOT that a travel number is accurate. Reinstating a travel-accuracy alert is
a change to make when a travel estimator passes TEST B, and not before.

**h3 is NOT DEPLOYED.** `gpu_door.TRACKER_LOGIC` still reads `h2`; nothing in the worker selects
h3. Until it is deployed, LEG 1 grades the engine in the repo, not the engine in production — run it
with `--tracker h2` as well when you want to know what production is doing.

What has and has not been executed here, so nothing is taken on trust: **LEG 1's replay commands
were run on dev-box exactly as written** and the reference table below is their output. **LEG 2's
capture command has never been run** — see the KNOWN GAP under LEG 2 Step 1.

---

## Cadence

| | |
|---|---|
| when | Monday, before the weekly report run |
| who | whoever owns the door engine that week |
| cost | LEG 1 ~15 min wall clock, no human attention. LEG 2 ~1 h capture + ~45 min hand-timing |
| where | LEG 1 on dev-box. LEG 2 capture anywhere with ffmpeg and cloud access; scoring on dev-box |
| output | append both printed blocks to `tools/stopwatch_log.md`, commit |

Run LEG 1 every week. Run LEG 2 when the corpus is stale (see "When to re-capture"), and always
after any change to camera position, lens, lighting, NVR firmware, or relay encoding settings —
those change the world the frozen corpus no longer represents.

---

## LEG 1 — frozen-corpus regression (every week)

Replays the **repo's current tracker** over the two frozen corpus files and scores it against the
frame-anchored hand truth. No capture, no hand-timing, no judgement. This is the leg that catches
*the engine changing*.

```bash
cd /home/ajit_kumarjha_lodhagroup_com/projects/liftlab

python3 tools/doorwatch_replay.py --cam ch30 --tracker h3 \
    --video /home/ajit_kumarjha_lodhagroup_com/projects/liftlab/ch30_peak.mp4 \
    --roi 2,2,335,446 --frame-stride 2

python3 tools/doorwatch_replay.py --cam ch27 --tracker h3 \
    --video /home/ajit_kumarjha_lodhagroup_com/dwrec/rec/ch27_clean.mp4 \
    --roi 125,3,238,397 --frame-stride 2
```

`--tracker h3` is the engine under test. Drop the flag (or pass `--tracker h2`) to grade the
incumbent instead — worth doing in the same sitting until h3 is actually deployed, because until
then h2 is what production runs and h3 is what the repo contains.

`--frame-stride 2` is not a speed knob — it is what production runs (`DOOR_STRIDE` defaults to 2 in
`gpu_analyze.py:110`, ~12.5 fps of the 25 fps decode). If a camera's registry `stride` has been
changed, pass that value instead, or the replay grades an engine cadence that does not exist.

The ROIs are the calibrated ones and both calib `frame_wh` are 704x576, matching the recordings, so
no `--roi-scale`. Truth and phantom windows default to `tools/groundtruth_20260805.csv` and
`tools/phantom_periods_20260805.csv` and are frame-anchored to these two named files — there is no
`--osd-base` to get wrong.

Corpus facts, verified: `ch30_peak.mp4` 27257 f @ 24.9945 fps (18 min), `ch27_clean.mp4` 70260 f
@ 24.8910 fps (47 min), both HEVC 704x576 (the sub stream the relay carries). 21 truth closes total:
ch30 8 (4 with hand travel), ch27 13 (11 with hand travel).

### Reference numbers

Run on dev-box 2026-08-05 at commit `9f79051`, `--tracker h3` (`TRACKER_LOGIC_H3 = "h3-state"`), with
the two commands exactly as written above. **This is the passing reference.** A week that does not
reproduce it is the week to investigate.

| | ch30 h2 | ch30 **h3** | ch27 h2 | ch27 **h3** |
|---|---:|---:|---:|---:|
| gradable truth closes | 8 | 8 | 13 | 13 |
| DETECTED | 5 (62.5 %) | **8 (8/8)** | 8 (61.5 %) | **13 (13/13)** |
| MISSED | 3 | **0** | 5 | **0** |
| PHANTOM | 1 | **0** | 41 | **0** |
| UNMATCHED emissions | 25 | 9 | 117 | 41 |
| total emitted | 31 | 17 | 166 | 54 |
| refractory suppressed | — | **0** | — | **0** |
| cycles flagged occluded | — | 0 | — | 5 |
| travel produced | 1 / 4 | **0, by design** | 1 / 11 | **0, by design** |

The h2 column is kept alongside because it is what production still runs, and because the delta is
the argument for deploying h3 at all.

**Two lines in that table are not achievements and must not be read as any.** `refractory
suppressed = 0` means the refractory window never fired on this corpus — the phantoms died on h3's
open-plateau precondition instead, so the refractory is untested, not proven. `occluded = 5` on ch27
is a *negative* result: none of the 5 is a matched cycle, and both hand-labelled extended closes
(f21317 at 4.76 s, f46263 at 6.28 s) came back unflagged. The occlusion flag misses every event it
exists to catch. **Do not gate anything on `occluded`,** and do not treat a change in that count as
signal until there is a hand-labelled occlusion set to validate it against.

**This is now the acceptance table in README_DOORWATCH.md**, where it replaced the dirty-corpus
baseline (ch30 9/11 detected / 0 phantom, ch27 4/9 / 4 phantom). That older table's phantom counts
and both its travel sets had already been voided by `9223bbb` / `1719155` when the corpus was
replaced, but no clean-corpus h2 *detection* replay had been run until this one, so its detection
row stood unchallenged as the target. On clean, continuous, frame-anchored data h2 detects **~62 %
on both cameras**, not 82 % and 44 %, and ch30's `PHANTOM 0` becomes 1. The dirty table is kept
below the new one in that file, not deleted: the delta between them is the evidence for why corpus
integrity gates every number downstream of it.

Two readings from that h2 run, both of which h3 answered:

* **ch27 emitted 166 events for 13 real closes, 41 of them inside verified door-closed windows.** The
  phantom problem was an order of magnitude larger than the dirty-corpus count of 4 suggested. h3
  emits 54 and 0 respectively.
* **Travel bias at n=1 was never a bias.** Each camera produced exactly one `close_travel_s` against
  a timed close and the two disagreed in *sign* (+1.12 s, -1.52 s). h2 was not measuring travel; it
  was occasionally emitting a number. h3 stops pretending, which is why alerts 3 and 4 are now about
  the honesty of the null rather than the accuracy of a value.

---

## LEG 2 — fresh capture (when the corpus goes stale)

This is the leg that catches *the world changing*: drifted camera, new lighting, a re-crimped lens,
a firmware update that alters the sub stream. LEG 1 cannot see any of it, because the frozen corpus
is frozen.

### Step 1 — capture 1 h per camera

ffmpeg pulls the live HLS the Pi is already PUTting; nothing new is deployed on the Pi to do this.
The playlist is behind Caddy basicauth (verified: an unauthenticated GET returns **401**), so the
operator credentials from CADDY_USERS.md go in the URL.

```bash
# dev-box has no ffmpeg — install once, or run this step on the Pi/gateway, which does:
sudo apt-get install -y ffmpeg     # 7:6.1.1-3ubuntu5 on this box

OP=ajit; PW='<operator-password>'; DAY=$(date +%Y%m%d)
mkdir -p /home/ajit_kumarjha_lodhagroup_com/dwrec/rec

for CAM in ch27 ch30; do
  ffmpeg -nostdin -hide_banner -loglevel warning \
    -i "https://${OP}:${PW}@lift.gargi.online/live/site-A/${CAM}/index.m3u8" \
    -t 3600 -an -c:v copy -movflags +faststart \
    "/home/ajit_kumarjha_lodhagroup_com/dwrec/rec/${CAM}_${DAY}.mp4" \
    2>&1 | tee "/tmp/cap_${CAM}_${DAY}.log"
done
```

`-c:v copy` — never transcode. Re-encoding changes the pixels the door engine reads, and the point
of the capture is to feed the engine the same bytes production feeds it. `-an` because the sub
stream carries no audio worth muxing.

> **KNOWN GAP — TODO, and the reason LEG 2 has never run.** This command has never been executed.
> Two things block it, both mechanical:
> 1. **dev-box has no ffmpeg** (`apt-cache policy ffmpeg` -> candidate `7:6.1.1-3ubuntu5`, installed:
>    none). Either install it or run this step on the Pi/gateway, which has it.
> 2. **No operator credential is available to the harness.** An unauthenticated GET of the playlist
>    returns **401**, so Caddy basicauth is required and is working; what is missing is a credential
>    this runbook is allowed to carry. Do NOT paste an operator password into this file or into
>    shell history — put it in a root-readable env file on the capture host and reference it, the
>    way `/etc/liftlab-agent.env` already holds `GATEWAY_TOKEN` for the relay.
>
> Until both are resolved, LEG 2 cannot run, which means **travel has no source at all** now that h3
> emits none. That makes this gap the single highest-priority item on this page — it is not a
> convenience, it is the only remaining path to a compliance travel figure. Alert 4 fires every week
> until it is closed.

Run the two cameras **sequentially, not in parallel**, unless you have checked the uplink headroom
first: two 1 h pulls compete with the seven live relay streams the Pi is already sustaining, and a
starved relay drops segments — which is precisely how you manufacture a spliced corpus.

### Step 2 — verify continuity BEFORE anything else

The relay keeps a 12-segment (~24 s) rolling window on tmpfs. If ffmpeg falls behind or the relay
restarts, segments are missed and the mp4 is *spliced* — and a spliced corpus is not merely noisy,
it silently mis-scores. A concat inversion in the old `ch30_full.mp4` faked a `PHANTOM 0` (`3013116`)
and forced a drift correction that made a detection score partly circular. That corpus, and both
hand-timed travel sets taken on it, had to be thrown away (`9223bbb`).

```bash
grep -iE "discontinu|non-monotonous|corrupt|drop" /tmp/cap_ch27_${DAY}.log /tmp/cap_ch30_${DAY}.log

python3 - <<'PY'
import cv2, glob, os
for p in sorted(glob.glob(os.path.expanduser("~/dwrec/rec/ch*_2*.mp4"))):
    c = cv2.VideoCapture(p)
    fps = c.get(cv2.CAP_PROP_FPS); n = int(c.get(cv2.CAP_PROP_FRAME_COUNT))
    w, h = int(c.get(3)), int(c.get(4)); c.release()
    print(f"{os.path.basename(p):28s} {n:7d} f @ {fps:.4f} fps  {w}x{h}  "
          f"{n/fps/60:5.1f} min  {'OK' if n >= 0.98*3600*fps else 'SHORT — RE-CAPTURE'}")
PY
```

**Expect decode noise at the head of the file and do not re-capture for it.** A capture that starts
mid-GOP — which every HLS pull does — makes the decoder print `Could not find ref with POC N` /
`Error constructing the frame RPS` until the first keyframe. `ch30_peak.mp4` does this for roughly
its first 50 frames, and the replay above is unaffected because the earliest truth close is at frame
1538. If your first close lands inside the damaged head, pass `--skip-before 30` and start the truth
after it. What is *not* acceptable is the same message appearing mid-file: that is a lost segment.

Reject and re-capture if: any discontinuity line in the ffmpeg log, frame count under 98 % of
`3600 x fps`, resolution not 704x576, or fps not within 0.5 % of 25. Do **not** repair a short
capture by concatenating a second pull — that is how the last corpus was ruined.

Then read the OSD by eye at the start, the middle and the end of each file and confirm the clock
advances monotonically. Record the OSD at frame 0; it becomes `osd_base`.

### Step 3 — register the new corpus

`tools/truth_io.py` holds `CORPUS`, keyed by **camera**, one entry each:

```python
CORPUS = {
    "ch27": {"file": "ch27_clean.mp4", "path": "~/dwrec/rec/ch27_clean.mp4",
             "fps": 24.8910, "frames": 70260, "osd_base": "16:05:33",
             "roi": (125, 3, 238, 397)},
    ...
}
```

Point the entry at the new file and paste in the **measured** fps and frame count from Step 2 —
never the nominal 25. ch27 runs at 24.8910; using 25 biases every derived time by ~0.4 %.

**Two traps, both structural, both worth knowing before you edit anything:**

1. `truth_io.load_truth()` and `load_phantoms()` filter on the `cam` column and **ignore the `file`
   column**. Truth rows for two different files of the same camera will be pooled, and last week's
   frame numbers will be scored against this week's video. So put each week's truth in its **own
   pair of CSVs** and pass them explicitly — never append to `groundtruth_20260805.csv`:

   ```bash
   --truth tools/groundtruth_YYYYMMDD.csv --phantoms tools/phantom_periods_YYYYMMDD.csv
   ```

2. `CORPUS` holds exactly one file per camera, so registering this week's capture **de-registers the
   frozen corpus**. Run LEG 1 first, or on a separate checkout, and revert `truth_io.py` afterwards.
   If the weekly cycle survives, the fix is to key `CORPUS` by file rather than by camera — one small
   change that turns this whole step into a flag.

### Step 4 — hand-time the closes

**A fresh video with no hand truth scores nothing, and it scores nothing that looks like a pass.**
With zero truth rows for the camera, `doorwatch_replay.py` prints `DETECTED 0/0`, `MISSED 0`,
`PHANTOM 0` and files every emission under `UNMATCHED`. That reads clean and means nothing. There is
no way around the hand-timing; it is what makes the check a check.

Write `tools/groundtruth_YYYYMMDD.csv`, schema `cam,file,start_f,end_f,travel_s,status`, one row per
real close seen on video:

* `start_f` — first frame of leaf motion; `end_f` — first frame fully closed. Frame numbers in the
  new file, not wall clock. Endpoints carry ~±2 frames per end (`TRAVEL_ERR_S = 0.16`).
* `travel_s` — `(end_f - start_f) / fps`, using the **measured** fps.
* `status` — `clean` for a close whose endpoints you are willing to defend; `*_excluded`
  (`complex_excluded`, `partial_start_excluded`, ...) for a real close you will not time. Excluded
  rows still count for detection, which is why they belong in the file.
* Deliberately include **slow and obstructed closes**. The frozen corpus's eleven ch27 travels span
  1.56–2.40 s, and that near-constant spread is the binding limitation on every travel result to
  date — it makes the criterion demand an MAE near the hand-timing noise floor. A corpus of only
  ordinary closes cannot discriminate a real measurement from a stopped clock.

Write `tools/phantom_periods_YYYYMMDD.csv`, schema `cam,file,start_f,end_f,note`: frame ranges with
**no door motion**. These are door-state windows, not occupancy — an emission inside one is a
phantom whoever is standing in the cabin. Verify no window overlaps a truth close; a "no motion"
window containing a close invalidates both files.

### Step 5 — score

Same commands as LEG 1, with the new video and the new CSVs:

```bash
python3 tools/doorwatch_replay.py --cam ch27 \
    --video /home/ajit_kumarjha_lodhagroup_com/dwrec/rec/ch27_${DAY}.mp4 \
    --roi 125,3,238,397 --frame-stride 2 \
    --truth tools/groundtruth_${DAY}.csv --phantoms tools/phantom_periods_${DAY}.csv
```

---

## The alerts

Read them off the printed block. All four are failures; any one of them means the engine does not
ship this week's numbers unexamined.

Alerts 3 and 4 changed shape when h3 was armed. h2's versions checked whether a travel number was
*accurate*; h3's check that there is no travel number at all and that a human took the measurement
instead. That is not a relaxation — a state-only engine that starts emitting travels is a defect,
and travel with no source is a worse failure than travel with a known bias. Reinstate an accuracy
alert only when a travel estimator passes TEST B.

| # | Alert | Where to read it | What it means |
|---|---|---|---|
| 1 | **detection below bar** — frozen corpus: ch30 < 7/8, ch27 < 11/13. Fresh 1 h capture: < 90 % | `DETECTED n/m` | Real closes the engine never emitted. Riders' journeys are missing from C27, and the loss is silent — nothing downstream can tell an unmade trip from an unmeasured one. |
| 2 | **PHANTOM > 0** | `PHANTOM n` | An emission inside a verified door-**closed** window. The engine invented a door cycle. Any nonzero count fails; phantoms inside the `[0.5, 30]s` filter reach C27 as fabricated trips. |
| 3 | **any `close_travel_s` is non-null** | `engine_s` column — every row must read `-` | h3 does not measure travel and must not appear to. A number here means either the null discipline broke or the engine in the replay is not h3. This alert is INVERTED from a normal accuracy check on purpose: for a state-only engine, an unexpected measurement is the defect. |
| 4 | **the weekly hand-timed travel sample was not taken** | your own LEG 2 log, not the replay output | Travel now comes from hand-timed video, not from the engine. If nobody sampled it, compliance travel has no source at all this week — the failure is silent and lands outside this harness, which is exactly why it is an alert. |

**Why alert 1 has two forms.** A flat 90 % was the original instruction, and on the frozen corpus's
small denominators it does not divide sensibly: 90 % of ch30's 8 gradable closes rounds to **8/8**,
so a single miss on an 18-minute video would fail an engine that is working. The bar for the frozen
corpus is therefore h3's own ship criterion in counts — **ch30 >= 7/8, ch27 >= 11/13** (README_DOORWATCH.md
§ "What h3 must reach") — and the 90 % applies to LEG 2's 1 h captures, where 25–30 closes per camera
give it room. Both numbers come from the same intent; only the denominator differs.

Note the consequence: an h3 that ships at exactly 7/8 and 11/13 sits at **87.5 % and 84.6 %**, below
the fresh-capture 90 %. That is deliberate — shipping once and staying good are different bars — but
it means the first LEG 2 after h3 ships may alert on a compliant engine. If it does, the question is
whether 90 % is the right standing bar, not whether the engine regressed. Do not soften either
number to make a small denominator behave; capture more video instead.

Two non-alerts that still need a human glance:

* **exit code 2 / `REFUSING TO SCORE: openness never moved`** — the ROI is wrong for this video's
  resolution. Not a detector failure; the run did not happen. A wrong ROI does not error, it yields
  a flat edge column and would otherwise score a confident 0/0.
* **`UNMATCHED` count** — emissions with no truth within 5 s and not inside a phantom window. Not
  scored, because an unmatched emission may be a real close the hand-timing missed. A big jump in it
  is still the loudest early signal that something moved, and h2 ran 22–29 of them.

---

## When to re-capture (LEG 2 triggers)

* the frozen corpus is more than a quarter old;
* any camera moved, was re-focused, or had its ROI re-calibrated;
* lighting changed in the lobby or cabin (new fittings, film on the doors, a mirror replaced);
* NVR firmware or relay encode settings changed — the sub stream's codec, resolution or GOP
  structure is an input to the door engine;
* LEG 1 passes but a real phantom or miss is reported from the field. That combination means the
  frozen corpus no longer contains the failure, which is the corpus's problem, not the field's.

---

## What this check does not cover

Stated so nobody reads a green week as more than it is.

* **Two cameras of seven.** ch27 and ch30 only. ch16, ch29, ch32, ch34, ch37 are ungraded — no
  hand-timed video exists for them.
* **One hour of one day.** Peak-hour behaviour, night behaviour and rare mechanical faults are
  outside any corpus this runbook produces.
* **Travel is not covered by this engine at all.** h3 measures door STATE. Every compliance figure
  that depends on how long a door took to close now rests on LEG 2's hand-timed sample and on
  nothing else. A green LEG 1 says nothing whatever about travel.
* **`UNMATCHED` is unexplained, not benign.** h3 emits 41 unmatched on ch27 and 9 on ch30. These are
  probably real closes nobody hand-timed — the truth holds 13 closes across 47 minutes, far fewer
  than a working lift performs — but probably is not measured, and a phantom that happens to fall
  outside a verified-closed window would land in this same bucket. LEG 2 is what would settle it.
* **The phantom score has no unseen portion.** The closed template is built from the first 40 % of
  each door-closed window and phantoms are scored inside those same windows. The harness reports
  train-split and test-split counts separately, but with 0 phantoms both read 0 — the split found
  nothing because there was nothing to find, which is weaker than a clean test-split number.
* **The occlusion flag is unvalidated and currently wrong.** See the note under the reference table.
* **LEG 1 cannot fail for anything but an engine change.** Same video, same truth, every week. A
  green LEG 1 says the engine did not regress; it says nothing about whether the engine still
  matches the building.

---

## Log

Append every run — both blocks verbatim, the tracker string printed in the header, the commit SHA of
`gpu_door.py`, and one line of verdict — to `tools/stopwatch_log.md`. A week that is not written down
did not happen, and the value of this check is entirely in the series.
