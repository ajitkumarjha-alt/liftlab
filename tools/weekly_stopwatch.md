# Weekly stopwatch — the standing check on the door engine

One page. Run it every week. The harness that graded h2 and h3 does not get retired when h3 ships —
it stays live as the instrument's conscience, because the database is the engine's own opinion and
cannot grade the engine. Video the engine never saw is the only ground truth available.

---

## STATUS: NOT ARMED

**Gate: h3 acceptance.** As of `668cf46` (2026-08-05) h3 is **not built** — TEST B (travel) FAILS on
both cameras on the clean corpus, TEST A (state) passes. See README_DOORWATCH.md § "TEST B RETRY".

This page is written and ready, but the weekly cycle does not start until README_DOORWATCH.md
§ "THE ACCEPTANCE TABLE — h2 on the CLEAN corpus" / "What h3 must reach" is met by a shipped
tracker: ch30 >= 7/8 detected, ch27 >= 11/13, phantoms 0 on both. Arm this page by deleting this
section — nothing else here changes.

What has and has not been executed, so nothing here is taken on trust: **LEG 1's replay commands
were run on dev-box as written** and the baseline table below is their output. **LEG 2's capture
command has not been run** — dev-box has no ffmpeg and this page carries no operator password. What
was checked for it: the playlist URL answers `401` unauthenticated (so basicauth is required and the
path shape is right), and both corpus files decode as HEVC 704x576 through the same cv2 the harness
uses. First LEG 2 run should expect to correct a detail in Step 1.

Until then, LEG 1 below is still worth running whenever `gpu_door.py` is touched: it is a
regression check on the engine, and it does not care whether the engine is h2 or h3.

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

python3 tools/doorwatch_replay.py --cam ch30 \
    --video /home/ajit_kumarjha_lodhagroup_com/projects/liftlab/ch30_peak.mp4 \
    --roi 2,2,335,446 --frame-stride 2

python3 tools/doorwatch_replay.py --cam ch27 \
    --video /home/ajit_kumarjha_lodhagroup_com/dwrec/rec/ch27_clean.mp4 \
    --roi 125,3,238,397 --frame-stride 2
```

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

Run on dev-box 2026-08-05, `TRACKER_LOGIC = "h2"` (`gpu_door.py:80`), commit `668cf46`, with the two
commands exactly as written above. This is the **failing** engine — it is the floor h3 has to clear,
and it is here to show what the alerts look like when they fire, not as a passing reference.

| | ch30 | ch27 |
|---|---:|---:|
| gradable truth closes | 8 | 13 |
| DETECTED | **5** (62.5 %) | **8** (61.5 %) |
| MISSED | 3 | 5 |
| PHANTOM | **1** | **41** |
| UNMATCHED emissions | 25 | 117 |
| total emitted | 31 | 166 |
| travel: n produced / timed truth | **1 / 4** | **1 / 11** |
| travel bias (mean err) | **+1.12 s** | **-1.52 s** |

All four alerts fire on both cameras. That is the expected result for h2 and the reason h3 exists.

**This is now the acceptance table in README_DOORWATCH.md**, where it replaced the dirty-corpus
baseline (ch30 9/11 detected / 0 phantom, ch27 4/9 / 4 phantom). That older table's phantom counts
and both its travel sets had already been voided by `9223bbb` / `1719155` when the corpus was
replaced, but no clean-corpus h2 *detection* replay had been run until this one, so its detection
row stood unchallenged as the target. On clean, continuous, frame-anchored data h2 detects **~62 %
on both cameras**, not 82 % and 44 %, and ch30's `PHANTOM 0` becomes 1. The dirty table is kept
below the new one in that file, not deleted: the delta between them is the evidence for why corpus
integrity gates every number downstream of it.

Two readings worth carrying into h3's acceptance:

* **ch27 emits 166 events for 13 real closes, 41 of them inside verified door-closed windows.** The
  phantom problem is an order of magnitude larger than the dirty-corpus count of 4 suggested.
* **Travel bias at n=1 is not a bias.** Each camera produced exactly one `close_travel_s` against a
  timed close, and the two disagree in *sign* (+1.12 s, -1.52 s). Alert 3 is unreadable at this
  coverage, which is exactly why alert 4 exists and why it is checked first.

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

| # | Alert | Where to read it | What it means |
|---|---|---|---|
| 1 | **detection below bar** — frozen corpus: ch30 < 7/8, ch27 < 11/13. Fresh 1 h capture: < 90 % | `DETECTED n/m` | Real closes the engine never emitted. Riders' journeys are missing from C27, and the loss is silent — nothing downstream can tell an unmade trip from an unmeasured one. |
| 2 | **PHANTOM > 0** | `PHANTOM n` | An emission inside a verified door-**closed** window. The engine invented a door cycle. Any nonzero count fails; phantoms inside the `[0.5, 30]s` filter reach C27 as fabricated trips. |
| 3 | **\|travel bias\| > 0.2 s** | `travel error: n=... mean=...` | Systematic clip in `close_travel_s`. The sign matters: the h2 failure mode was a *low* bias, the tracker stamping both ends of a descent it never observed. |
| 4 | **travel coverage short** | `n=` on the same line, vs timed truth rows | Missing entirely, or `n` below the camera's timed-close count, means the engine declined to measure. Alert 3 cannot fire on values that were never produced, so a silent engine passes it. Check this one first. |

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
* **Detection is graded against hand truth; travel is graded against hand *timing*.** Frame-anchored
  endpoints carry ~±0.16 s, so a bias threshold of 0.2 s sits close to the instrument's own floor.
  A pass at 0.19 s is not a measurement of quality, it is a tie with the ruler.
* **LEG 1 cannot fail for anything but an engine change.** Same video, same truth, every week. A
  green LEG 1 says the engine did not regress; it says nothing about whether the engine still
  matches the building.

---

## Log

Append every run — both blocks verbatim, the tracker string printed in the header, the commit SHA of
`gpu_door.py`, and one line of verdict — to `tools/stopwatch_log.md`. A week that is not written down
did not happen, and the value of this check is entirely in the series.
