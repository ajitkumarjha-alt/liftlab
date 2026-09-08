# INCIDENT — floor reading degraded fleet-wide at 2026-09-02 17:50 IST; ch27 is floor-blind, ch29 lost its lobby

**Status: mechanism CONFIRMED, source LOCALISED to the camera/NVR image profile, RESPONSE DECIDED.**
The GPU decode path is EXONERATED by measurement (below). The response is to recalibrate every
affected camera against the CURRENT image and start a new era on each — the site is NOT being asked
to restore the old profile. Why the change lands in the same minute as a GPU-box restart that
cannot have caused it is unexplained and is being left so.

**Severity: floor attribution on every floor-reading camera has been degraded since 2026-09-02
17:50 IST.** ch27 has produced effectively no floor data for five days. ch29 has produced almost no
'G'. All per-floor data and all RTT since that instant is suspect.

**Coincident event.** The fleet restart following the NVIDIA driver reinstall — 580.159.03 →
580.173.02, CUDA 13.0, kernel 6.8.0-1066. Coincident, but ruled out as the cause by measurement;
see "The decode is exonerated". The coincidence is unexplained and not being chased.

---

## What happened

Ten-minute buckets on ch29 put the change inside one bucket:

| bucket (IST) | rows | `floor='G'` | mean `read_conf` |
|---|---:|---:|---:|
| 17:30 | 823 | 15 | 0.848 |
| 17:40 | 748 | **84** | **0.835** |
| **17:50** | 644 | **2** | **0.785** |
| 18:00 | 632 | 1 | 0.780 |
| 18:10 | 564 | 0 | 0.784 |

It is not a decline. It is a step, and it does not recover.

### It is fleet-wide, and ch29's lobby is the mildest case

Floor-bearing reads per day, and the mean confidence they carried:

| cam | rows 08-18 | floor reads 08-18 | conf | rows 09-04 | floor reads 09-04 | conf |
|---|---:|---:|---:|---:|---:|---:|
| ch27 | 46,286 | **25,197** (54.4 %) | 0.652 | 7,279 | **18** (0.2 %) | 0.584 |
| ch29 | 53,694 | 35,494 (66.1 %) | 0.843 | 45,532 | 26,501 (58.2 %) | 0.772 |
| ch30 | 18,029 | **7,416** (41.1 %) | 0.896 | 11,060 | **1,820** (16.5 %) | 0.683 |
| ch16 | 8,031 | 0 | — | 14,103 | 0 | — |

ch16 was already floor-blind (see `INCIDENT_ch16_floor_blind.md`); ch32/ch34/ch37 are door-only and
have no floor panel. So of the three cameras that were reading floors on 09-02, **one is dead, one
lost 75 % of its reads, and one lost its ground floor.**

## The mechanism: every NCC score fell ~0.06, and three different gates sit inside that band

`gw_door_event.candidates` carries the per-cell top-2 plus the blank score on every read, so the
matcher's own numbers are on record either side of the break. ch29, per-day means:

| | Aug 5–19 | Sep 3–7 |
|---|---:|---:|
| units-cell top-1 NCC | 0.87 | 0.80 |
| tens-cell top-1 NCC | 0.855 | 0.795 |
| `G` own score | 0.89–0.90 | 0.79–0.83 |
| tens-cell **blank** NCC at the lobby | **0.95** | **0.86** |
| `ambiguous` share of rows | 29 % | 38 % |

A uniform loss across every cell and every glyph, with `shift` still `[0,0]` on every read. Three
gates in `gpu_door.FloorReader` sit within that 0.06 band, and each camera failed at whichever one
it was nearest:

**ch29 — the blank gate (`gpu_door.py:698-702`).** A lobby read is `blank` in the tens cell plus
`G` in the units cell. Blank may only win a **lit** cell (contrast ≥ `lit_range`=120) if
`b ≥ blank_strong` (0.90) **and** `b ≥ top1 + blank_lit_margin` (0.15):

    Aug 18 -> "G"    tens: blank 0.950 vs '6' 0.718   0.950 >= 0.90 OK, >= 0.868 OK  -> blank
    Sep  4 -> "6G"   tens: blank 0.894 vs '6' 0.768   0.894 <  0.90 --, <  0.918 --  -> '6' wins

`6G` is not a floor, so it lands in `reason='invalid_label:6G'` and is discarded.

**ch30 — the same gate, one cell over.** `invalid_label:666` ×366, `:366` ×54, `:333` ×51 on
Sep 4, none of them present in August. A phantom digit filling a cell that should read blank.

**ch27 — the `min_score` floor (0.55).** Its August confidence was already the lowest on the fleet
(0.652); a 0.06 loss puts the winning glyph under the floor. Sep 4 is **99.7 % `no_read`** — 7,257
of 7,279 rows. Not a misread; nothing named at all.

### ch29 lobby frames, per day

Frames where `G` won the units cell, split by whether the tens cell blanked:

| day | lobby frames | `G` emitted | blocked as `xG` | top junk glyph |
|---|---:|---:|---:|---|
| 2026-08-18 | 3,136 | 2,852 | 284 (9.1 %) | `7`×187 |
| 2026-08-19 | 1,796 | 1,386 | 410 (22.8 %) | `7`×367 |
| 2026-09-02 | 354 | 237 | 117 (33.1 %) | `7`×32, `6`×30 |
| 2026-09-03 | 1,003 | **32** | 971 (**96.8 %**) | `6`×383 |
| 2026-09-04 | 300 | **6** | 294 (**98.0 %**) | `6`×95 |

### The derived alphabet is now a casualty too, and it is inverted

The floor alphabet is derived FROM the reads, so the degradation propagated into it. ch29's stored
row (derived 2026-09-08) currently:

| floor | Aug 10–19 reads | since Sep 3 | stored verdict |
|---|---:|---:|---|
| 10 | 7,039 | 4,064 | `quarantine:glyph_shadow_of_18` (31 impossible-speed flips) |
| 21 | 6,250 | 3,600 | `quarantine:glyph_shadow_of_27` (59 flips) |
| 51 | 3,356 | 1,763 | `quarantine:glyph_shadow_of_58` (9 flips) |
| 57 | 1,760 | 684 | `quarantine:glyph_shadow_of_50` (9 flips) |
| 77 | 18,075 | 4,955 | `quarantine:glyph_shadow_of_17` (95 flips) |
| **67** | **1,293** | **7,742** | **ADMITTED (`labeled`)** |

Five real floors convicted, and the phantom admitted. The mechanism is symmetrical to everything
else here: scores sitting on a gate produce single-glyph flips at impossible speed, which is
exactly the evidence the shadow rule convicts on — so the degradation manufactures its own
corroboration. 67 escapes because it is in `labels.json` and labeled floors bypass every quarantine
rule by design.

**The dashboard is therefore filtering out real floor-10/21/51/57/77 reads right now while
accepting the phantom.** The retention guard added on 09-08 does not help: these are convictions,
not silence, and it deliberately leaves convictions standing. Recalibration is the fix — once the
reads are good the impossible-speed flips stop and the floors re-admit on the next derive, which is
the re-admission path the rule already carries.

It also has a direct consequence for the rebuild: **the stored alphabet must not be used to gate
recalibration exemplars.** `bootstrap_exemplars.py` gates on pre-incident evidence instead.

### The misreads are not confined to 'G'

The same spurious `6` that turns `G` into `6G` turns single-digit floors into two-digit ones.
Floor 67 on ch29: **66 reads on Aug 18, 1,457 on Sep 4, 1,535 on Sep 5.** Numeric floor *counts*
look stable, which is why this reads as "only G broke" from a summary — but the *identities* are
contaminated.

## What it is NOT

The first hypothesis was that the alphabet-refresh stage in the Sep 2–3 sweeps clobbered ch29's
`G` template. Three independent checks refute it:

1. **`templates_hash` never moved.** It is stamped per read from the live engine
   (`gpu_analyze.py:752`, hashed at construction from the loaded template dict). ch29 carries
   `260d4a0f67ff058743c9f8aeb6c25e9500c07236284cd33c1ffa0b507f21e28c` on **every row since
   2026-07-22 09:03 IST**, and `door_version 260d4a0fh3-stateLaa52T5471cb+495e8f48` on every row
   since 2026-08-11 12:00. One hash, one era, both sides of the break. `ch29_templates.npz` on
   liftlab-cloud hashes to the same value — the August templates and the live templates are the
   same bytes, so there is nothing to restore and nothing to pin. A rebuild with different
   exemplars would produce a different hash, so the exemplar count cannot have moved either.
2. **The sweep cannot write templates.** `precompute_job.py` stage 1 → `dash_api.alphabet_refresh`
   writes exactly one thing, the `floor_alphabet` table (a whitelist of floor *strings*). It never
   opens crops, `labels.json`, or `templates.npz`. Templates are only written by
   `door_calib.build_from_crops`, a manual CLI step.
3. **`G` is still admitted.** The stored `floor_alphabet` row for ch29 contains
   `G, P1, P2, P3, P5`. The whitelist is rejecting nothing.

Nor is it a camera or panel move: `shift` is `[0,0]` on every read either side of the step. A moved
panel translates; this softened in place.

## The decode is exonerated — the degradation is already in the delivered bytes

Two clips of ch29, both **stream copies** of the same HLS the worker consumes, both decoded on
**liftlab-cloud** with OpenCV/FFmpeg 62.28.101 — no NVIDIA driver, no `hevc_cuvid`, no CUDA
anywhere near either of them — scored with the **live** templates (hash-asserted `260d4a0f…`) and
the live cells `(5,2,8,20) (14,5,8,20) (21,2,8,20)`:

| | `ch29_tue.mp4` (Aug 11) | `ch29_mon.mp4` (Sep 7) | DB, Aug | DB, Sep |
|---|---:|---:|---:|---:|
| `G` score, mean | **0.908** | **0.810** | 0.89–0.90 | 0.79–0.83 |
| tens `blank` at the lobby, mean | **0.927** | **0.856** | ~0.95 | ~0.86 |
| tens `blank`, median | **0.922** (> `blank_strong` 0.90) | **0.857** (< 0.90) | | |
| lobby frames blocked as `xG` | 503 / 1,299 (38.7 %) | 1,083 / 1,084 (**99.9 %**) | 9.1 % | 97–98 % |
| `G` emitted as a floor | 796 | **1** | | |

Each clip reproduces its own era's DB scores on a machine that has nothing to do with the failure.
**The GPU decode path cannot be the cause, and neither can the driver.**

The relay is ruled out on inspection rather than measurement: `live_relay_8.sh:138` is
`-rtsp_transport tcp -i "$url" -an -c:v copy` — a pure remux. It never re-encodes, so the HEVC
reaching liftlab-gpu is the camera's own encoder output.

### What did change: the camera's image profile

Same two clips, same decoder, no template matching — just pixels:

| | Aug 11 | Sep 7 |
|---|---:|---:|
| resolution / fps | 704×576 / 24.78 | 704×576 / 24.97 |
| delivered bitrate | 0.216 Mbps | 0.239 Mbps |
| panel mean brightness | 123.1 | 123.5 |
| **panel Laplacian variance** | **4,168** | **5,031 (+21 %)** |
| **tens-cell contrast (max−min)** | **199** | **235** |
| **tens-cell mean brightness** | **111.9** | **81.0 (−28 %)** |
| units-cell contrast | 190 | 232 |

Both files are `hev1`, both muxed by `Lavf61.7.103`, neither carries a `Lavc` encoder tag — stream
copies by the same tool, directly comparable. **Same hour of day, too:** the August clip is
2026-08-11 08:37 IST and the September clip 2026-09-07 midday — both AM-peak, so ambient light and
the camera's day/night profile are held constant across the comparison. There is no remaining
confound; the difference is the image itself.

**It is not blur, and it is not bitrate.** Resolution, frame rate, bitrate and overall panel
brightness are unchanged. What moved is *local* contrast: more high-frequency energy, higher
per-cell contrast, and the space between the strokes driven 28 % darker. That is a **contrast /
gamma / edge-enhancement change on the camera or NVR** — a sharpening, not a softening.

Which is why it destroyed the reads while looking fine to the eye. A per-cell `blank_<i>` template
is mostly the *glow and inter-stroke gradient* of its neighbours (see `build_templates`); crush
those and the blank template stops matching the empty cell while a dim junk glyph starts to. NCC is
a shape correlation against the August rendering, so "crisper to a human" and "correlates worse
with the stored template" are not in tension — they are the same fact.

### The 17:50 coincidence is UNEXPLAINED, and is being left that way

The change lands in the same minute as a GPU-box restart that the measurements above rule out as
its cause. Either that maintenance window also touched the NVR or the cameras, or something
reapplied an image profile at 17:50 on 09-02 for an unrelated reason.

**No further investigation is planned.** The camera-side change is established, the response does
not depend on knowing why it happened, and nobody on site holds the prior settings to compare
against. This is recorded as an open coincidence rather than a pending action so that a future
reader does not mistake it for a thread someone is still pulling. If it recurs, signal 5 on the
health line (below) is now what will say so within a day.

### `probe_decode_ab.py` — written, DEFERRED

    cd /opt/liftlab-analysis
    sudo -E .venv/bin/python probe_decode_ab.py --save /tmp/ab

Decodes one live segment four ways (`live`, `nvdec`, `ffmpeg_sw`, `pyav`) and scores the same panel
crop from each. Its job would now be confirmation rather than discovery — all four should agree to
within a hair, closing the GPU box out completely. **Not being run:** the two-clip control above
already answered the question it was built for, and the recalibration does not wait on it. It stays
in the tree for the next time a decode is suspected. (`sudo -E` matters when it is run — the live
`USE_NVDEC` is in `/etc/liftlab-gpu.env`.)

## Response: recalibrate against the current image, new era on each camera

**The site is NOT being asked to restore the old profile.** Nobody there holds the prior settings,
so "put it back" would be a guess dressed as a remedy — and a guess that lands *near* the August
rendering is worse than one that misses entirely, because it produces reads that score just well
enough to be believed. The current image is sharp, correctly exposed and perfectly readable; what
is stale is the templates, and templates are the thing we control.

So: **recalibrate every affected camera against the CURRENT image, mark a new era on each, leave
all pre-09-02 data untouched, and never pool across the boundary.**

| # | camera | work |
|---|---|---|
| 1 | **PL2B / ch30** | Full rebuild — zones, door state template from the 23 windows, panel cells and alphabet. This camera also **moved**, so its geometry is rebuilt, not just its templates. |
| 2 | **PL1A / ch29**, **PL3B / ch27** | Floor templates + cells rebuilt against the September image. Exemplars bootstrapped from the current stream wherever the OLD templates still score ≥ 0.85 on numerics — those reads are correct, merely marginal — then the lobby added from `G` crops on confirmed PL1A windows. Held-out verification before any cut-over. **PL3B additionally gets a single-character cell**, without which its lobby cannot register at all. |
| 3 | **door templates, ch27/29/30/32/34/37** | Re-run held-out separation on current footage. Rebuild any whose closed/open split has narrowed; leave the rest alone. |
| 4 | **eras** | New `door_version` and floor era on every rebuilt camera, dated to cut-over, carrying a reason string naming the 09-02 image change. Surfaced in `eras.csv` and every dash panel. |
| 5 | **health line** | Per-camera daily mean `read_conf` against the camera's own baseline — signal 5, below. |

Bootstrapping from ≥ 0.85 numerics is the load-bearing choice in step 2: it means the new exemplars
are drawn from reads the *existing* instrument still gets right, so the rebuild is anchored to
verified truth rather than to whatever the degraded reader currently believes. The lobby is the one
glyph that cannot be bootstrapped that way — `G` no longer survives the blank gate at all — which
is why its crops come from confirmed windows instead.

### The era boundary is automatic, and that is by design

`gpu_analyze.build_door_engine` composes `door_version` as
`{templates_hash[:8]}{tracker_logic}{levels_tag}+{geometry_hash}`. Rebuilding floor templates moves
the first component; rebuilding cells moves the last; rebuilding an h3 state template moves the
`T…` tag inside `levels_tag`. **Every rebuild in the table above therefore starts a new era without
anyone having to remember to declare one** — the boundary is a property of the instrument, not a
note someone writes. What the version string does *not* carry is *why*, which is what step 4 adds.

### Why not simply widen the gates

`DOOR_BLANK_STRONG` / `DOOR_BLANK_LIT_MARGIN` / `DOOR_MIN_SCORE` (`gpu_analyze.py:110-112`) would
each make the symptom go away in an afternoon. They are the wrong instrument:

- They are **process-wide, not per-camera**. Loosening the blank gate for ch29 loosens it for
  ch16/ch27/ch30 too, and each of those has a different failure at a different margin.
- The gates are load-bearing. `blank_lit_margin` exists because a blank that merely edges a lit
  glyph (0.907 vs 0.83) *deletes a digit* — a confident misread, worse than the honest `no_read`
  ch27 is producing now.
- The scores are down because the templates no longer describe the image. Widening the gate admits
  the same stale-template reads under a confident label; it converts a visible outage into a silent
  one, and leaves no era boundary to mark where the data changed.

## Consequence for reporting

- **All per-floor data since 2026-09-02 17:50 IST is suspect**, on every camera. ch27 has none;
  ch30 has 16 % of its former volume; ch29 has no lobby and an inflated floor 67.
- **RTT on PL1A is unusable after the step.** `rtt_window` for ch29: the 1-day window is 2 trips at
  100 % anomalies (`long (>600s) — likely a missed G read`), the 7-day is 42 trips at 64.3 %. The
  2,266-trip / 17.5 % figure the deck shows is carried almost entirely by pre-09-02 data. **The
  last valid RTT sample is the 2026-09-02 window.**
- If templates are rebuilt, every camera gets a new era and no cross-boundary series is valid.

## What changed in the repo alongside this

### Signal 5 on the health line — READING WORSE, NOT MISSING

The gap this incident exposed is that **nothing watched the reader**. Signals 1-3 ask whether
anything is arriving; signal 4 asks whether the counter returns too little. All four were correct
and silent for five days while three cameras' floor reading fell apart. `read_conf` has been on
every row the whole time and was never looked at.

`health_check._readconf` compares each camera's daily mean `read_conf` and its confident-read
volume against **its own era-scoped baseline**, and fires on either arm:

- **confidence** — mean `read_conf` down more than 0.05 from baseline (`HEALTH_CONF_DROP`)
- **volume** — confident floor reads below 40 % of baseline (`HEALTH_CONF_VOL_FRAC`)

Both arms are needed, and neither alone suffices. On 09-03 ch27 and ch29 fire on volume only; on
09-05 ch27 fires on volume only (18 reads is too thin to judge a mean) while ch29 fires on
confidence only. A single-arm check misses a camera on one of those days either way.

**The baseline is scoped to the current `door_version`, and is N judged days rather than a calendar
window.** The era scoping is what stops this check firing on its own remedy: the recalibration in
the table above starts a new era on every camera, so the baseline resets and the check reports
"baseline building" for three days instead of alarming. The judged-days rule is what makes it work
at all — the gateway has a 13-day ingest gap between 08-19 and 09-02, so a calendar 14-day baseline
evaluated on 09-03 reaches back only to 08-20, finds nothing, and declines to speak on exactly the
morning it exists for.

Verified by replaying the live gateway at three instants:

    2026-08-19 09:00 (healthy)        -> degraded: []             LINE: None
    2026-09-03 09:00 (morning after)  -> degraded: [ch27,ch29,ch30]
    2026-09-05 09:00 (three days in)  -> degraded: [ch27,ch29,ch30]

    READS: READING WORSE, NOT MISSING — ch27 1455 confident floor reads against a baseline
    16396/day (9% of it); ch29 9278 against a baseline 35494/day (26% of it); ch30 mean read_conf
    0.7601 against its own era baseline 0.8718 (down 0.1117) and 250 reads against 6039/day (4%).

Silent on the healthy day, and it names all three cameras on the morning after. That is the check
that would have caught 09-02 on 09-03.

### The alphabet retention guard

Unrelated to the root cause — a guard against a *second* way the lobby can
disappear, found while ruling the alphabet out. Every non-numeric floor (G, P1, P2, P3) is admitted
solely via `labels.json`, and `dash_api._labels_evidence` swallows `OSError`/`ValueError` and
returns empty. One unreadable file and the sweep overwrites the stored alphabet in place, dropping
G fleet-wide with no trace.

`_derive_floor_alphabet` now takes `prior` (the stored admissions) and `_retain_prior` re-admits on
**absence of evidence** (`reject:non_numeric_unlabeled`, `reject:only_N_sightings`, no rows at all)
while leaving **convictions** (`quarantine:glyph_shadow_of_X`, `quarantine:unanchored_island`,
`reject:no_transition_support`) to stand. Every retention is recorded in `detail[f].via`, returned
in the refresh meta, and printed by both sweeps. `DASH_ALPHA_RETAIN=0` disables it for a deliberate
reset.

Verified against a copy of the live DB with `CALIB_DIR` pointed at nothing:

    DASH_ALPHA_RETAIN=0: prior=64 -> after=50   G in alphabet? False   P1/P2/P3/P5 kept? []
    DASH_ALPHA_RETAIN=1: prior=64 -> after=55   G in alphabet? True    P1/P2/P3/P5 kept? [P1,P2,P3,P5]
       via[G] = retained:previously_admitted (would have been reject:non_numeric_unlabeled ...)

Worth noting separately: ch29's flip-kill evidence has grown sharply under the degraded reads
(33→23 now carries 113 impossible-speed flips), and several real floors — 11, 12, 27, 28, 30, 32,
33, 45, 47, 48, 53, 56, 62 — currently survive the shadow rules **only** because they are labeled
and labeled floors are exempt. `labels.json` is carrying more weight on ch29 than it looks.

---

## Timeline

| when (IST) | what |
|---|---|
| 2026-07-22 09:03 | `templates_hash 260d4a0f…` — unchanged from here on |
| 2026-08-11 12:00 | `door_version …h3-stateLaa52T5471cb+495e8f48` — unchanged from here on |
| 2026-08-19 | last full day of healthy reads on all three cameras |
| 2026-09-02 ~17:45 | NVIDIA driver reinstall; fleet restart |
| **2026-09-02 17:50** | **step: ch29 conf 0.835 → 0.785, `G` 84 → 2 in one 10-min bucket** |
| 2026-09-03 | ch27 floor reads 54 % → 0.2 %; ch29 lobby 96.8 % blocked |
| 2026-09-08 | decode exonerated by A/B on two same-hour stream-copied clips; source localised to the camera image profile; **response decided: recalibrate onto new eras, no site revert**; health signal 5 and the alphabet retention guard landed |
