# RUNBOOK — recalibrating ch27 / ch29 / ch30 onto the post-2026-09-02 image

**Why:** `INCIDENT_decode_regression_0902.md`. A camera-side image-profile change at 2026-09-02
17:50 IST moved every NCC score down ~0.06. The site is **not** being asked to revert it; the
templates are being rebuilt against the current image instead, onto a new era per camera.

**Rule that governs everything below:** pre-09-02 data stays valid under its own era and is **never
pooled across the boundary**. Every rebuild moves `door_version` by construction
(`gpu_analyze.build_door_engine` hashes templates + logic + levels + geometry), so the boundary
appears without anyone declaring it. What must be declared is *why* — step 6.

**Where each step runs.** Steps 1 and 6 run on **liftlab-cloud** (the gateway DB is there).
Steps 2–5 run on **liftlab-gpu** in `/opt/liftlab-analysis`. Nothing here has been executed yet:
liftlab-gpu is currently unreachable from this session (`gcloud auth login` needed).

---

## 0. Before anything — freeze what you are replacing

> **CORRECTED 2026-09-08.** This step previously said `calib/site-A/<cam>/templates.npz`. **There
> is no templates.npz in the calib dir on any camera** — the engine fetches templates over HTTP and
> they live in a different directory that nothing was backing up. Following the old text produces
> an archive with no templates in it, which is what happened to `gpu-calib-20260908.tgz`
> (md5 `871f962477494896ea9213247b1c44cf`, 124 KB): it holds `door_state_templates/*.json` and the
> `gpu_*.py` sources, and none of the calibration.

**Two directories, and both are on liftlab-cloud, not liftlab-gpu:**

    /var/lib/liftlab/templates/site-A/<cam>.npz     the templates themselves
    /var/lib/liftlab/calib/site-A/<cam>/            crops, labels.json, labels_bind.json, roi.json

Take them together, streamed so nothing is written on the gateway:

    gcloud compute ssh liftlab-cloud --zone asia-south1-c --quiet \
      --command 'sudo tar czf - -C /var/lib/liftlab templates calib' > step0-calib-<date>.tgz

Verify every pulled npz against the hash its camera is actually stamping in `gw_door_event`, before
it is used for anything — a calib dir that is not what the worker is running would recalibrate the
wrong instrument:

| camera | expected `templates_hash[:16]` |
|---|---|
| ch27 | `425f92e1c3a235b3` |
| ch29 | `260d4a0f67ff0587` |
| ch30 | `661a1fa01dcd6909` |

    python3 -c "import numpy as np,gpu_door as gd;z=np.load('<cam>.npz');print(gd.templates_hash({k:z[k] for k in z.files})[:16])"

**Done 2026-09-08:** `step0-calib-20260908.tgz`, 35,819,598 B,
md5 `5decdcaaa2734f64cde18bc3d6f3e2c1`, all three hashes verified MATCH. `ch27_templates.npz`,
`ch30_templates.npz` and `ch27_roi.json` are now committed (`ff5795f`), so all three cameras have
their templates and geometry in git rather than only on one box.

## 1. Bootstrap exemplars from the current image — **ch29 ONLY** (liftlab-cloud)

> **REVISED 2026-09-08 after measuring the yield. ch27 and ch30 cannot be bootstrapped at all**, at
> any threshold. 400 post-cutover crops per camera, read with that camera's own live templates:
>
> | cam | built glyphs | 400 crops | exemplars, any threshold ≥0.65 |
> |---|---|---|---|
> | ch29 | `0-9 G P up down` | **183 ok**, 173 no_read, 44 ambiguous | usable |
> | ch27 | `2 3 4 5 6 down` | **1 ok**, 399 no_read | **zero** |
> | ch30 | `2 3 6 up` | 10 ok, 310 no_read, 80 ambiguous | **zero** — all 10 are the phantom `3` |
>
> The tool can only label glyphs the OLD templates can read, and those two sets are too incomplete
> to name their own panels. ch27's shift search also wanders — (-1,1) (1,-2) (-1,0) (0,2), no peak
> at (0,0) — which is what a rigid shift search does when nothing matches anywhere.
>
> **Do not run step 1 for ch27 or ch30.** It writes an empty corpus and the build then fails on
> `min_examples=3`, which looks like a tooling fault and is not one. Both go to step 3's full
> manual calibration instead: collect, label by hand at `/calib-label`, build. ch27 additionally
> needs a `G` template built from scratch — it has never had one — on top of the single-character
> cell its lobby needs.

`bootstrap_exemplars.py` selects crops from `floor_sample` (which carries the real panel JPEGs,
post-09-02, on the cloud box) and labels them using the **old** templates, keeping only reads where
every glyph still clears `--min-score` — the upper tail the degradation did not reach.

    python3 bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --dry-run     # census first
    python3 bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --min-score 0.80 \
            --out /tmp/boot_ch29

**Use `--min-score 0.80`, measured.** The yield curve on ch29:

    thresh  crops  missing   per-digit
      0.85    258  0,9       1:6 2:90 3:188 4:26 5:106 6:43 7:25 8:2    [thin: 8]
      0.82    431  none      0:4 1:39 2:163 3:256 4:51 5:158 6:68 7:58 8:5 9:6   [thin: 0]
      0.80    567  none      0:5 1:97 2:226 3:286 4:88 5:186 6:82 7:74 8:10 9:14
      0.78    662  none      0:13 1:140 2:270 3:308 4:92 5:201 6:88 7:98 8:15 9:24

`build_templates` has `min_examples=3`, so 0.82 is the lowest that builds and it leaves digit 0 on
four exemplars. Below ~0.78 a different hazard opens: floor 77 is a known shadow of 17 and the
label gate ADMITS it — 77 is a genuine pre-incident floor whose rate FELL rather than rose — so a
17-read-as-77 starts getting in. Supply 0/8/9 and `G` from confirmed windows regardless.

It writes `_calib_crop_NNNNN.png`, `labels.json` **and `labels_bind.json`** — the content binding
is mandatory, or `build_from_crops` excludes every label (the 2026-07-30 ch16 label-inheritance
rule). Check the tail of the output for two things:

- **`NO EXEMPLAR for digit(s) …`** — a digit with no exemplar is a digit the rebuild will *lose*.
  Widen `--since`, lower `--min-score`, or supply it from a confirmed window. Do not build past it.
- **The lobby is absent by design.** `G` scores ~0.79–0.83 now and cannot clear the bar; admitting
  it on a lower threshold is the circularity the tool exists to avoid.

**ch27 needs `--cells`/`--arrow` explicitly** — there is no `ch27_roi.json` in the tree. Its crops
are 75×101 (vs ch29's 51×92), so ch29's geometry is not transferable.

### 1c. BUILD IS HELD until blank_1 and up are respectable (as of 2026-09-08)

**Do not run `door_calib.py --build` for ch29 yet.** The corpus is 278 crops and every glyph the
camera has today is covered except one, but two counts are not yet good enough to cut over on:

| glyph | have | need | why |
|---|---:|---:|---|
| `blank_1` | 10 | **40+** | see below — this is the template the incident actually broke |
| `up` | 1 | **3+** (`min_examples`) | genuinely rarer than `down` in a 5-day sample |

Everything else is ready: `0:15 1:65 2:86 3:98 4:41 5:82 6:40 7:41 8:12 9:16 G:10 P:40 down:5`,
`blank_0:278`.

**Why `blank_1` is the hard one, and why 10 is not enough.** `blank_1` is taught ONLY by
single-character floors — `_cell_labels` right-aligns, so a 1-char label pads BOTH left cells with
blank while a 2-char label pads only one. And single-character floors are exactly what the broken
reader cannot produce: the empty tens cell is where the junk digit appears. **The fault has starved
its own repair evidence.** All 10 current `blank_1` exemplars come from the operator-confirmed
lobby frames; the 3,829-crop bootstrap contributed none. For scale, the August build had 87.

That matters more than the raw count suggests: `blank_1` on the tens cell is the template whose NCC
fell 0.95 -> 0.86 and crossed `blank_strong`, which is the whole mechanism of the lobby loss. Cutting
over with 10 exemplars would rebuild the reader around a thin version of the one template that
failed.

The fix is more single-character confirmations, and **any single-digit floor counts** — 6, 7, 8, 9
alone on the panel, not just the lobby. `find_single.py` proposes candidates; the operator confirms.

**`up`**: direction has been live again since 2026-09-08 14:20:51 IST (the `_match2` fix). Re-export
`floor_sample` after a couple of days and `up` should bootstrap with no manual work — that is the
plan of record, rather than hand-labelling arrows now.

### 1b. The lobby — operator-confirmed windows only

`G` crops come from PL1A windows you confirm by eye on the Monday capture. Add them to
`/tmp/boot_ch29` with the next free `_calib_crop_NNNNN.png` index, append to `labels.json`, and
**add a matching `labels_bind.json` entry** (`sha256(png)[:16]`) or the build drops them silently.

### 1c. ch27 single-character cell

PL3B's lobby cannot register at all without a single-character cell. That is a `DIGIT_CELLS`
geometry change, so it needs the current image:

    door_calib.py --panelcheck --gw site-A --cam ch27     # live panel vs the calib crop, cells drawn
    door_calib.py --index ; door_calib.py --anchor N      # pick an anchor crop, read anchor px
    door_calib.py --cells --anchors '<tens_left,units_left,digit_top,digit_bottom,arrow_left>'
    door_calib.py --fitcells                              # auto-fit from labeled crops

Changing cells moves `geom_sig` and therefore `door_version` on its own — expected, and the reason
step 6 exists.

## 2. Build and check the floor templates (liftlab-gpu)

    door_calib.py --build                 # labels.json is picked up automatically
    door_calib.py --labelcheck            # flags probable MISLABELS for re-review — run it
    door_calib.py --readtest              # current reader on reviewed /floorcheck fixtures

**Held-out verification before cut-over.** `--readtest` scores against reviewed fixtures the build
did not consume. Do not cut over on `--labelcheck` alone: it measures the fit to its own labels,
which a self-consistent wrong set passes.

Acceptance: numeric glyph scores back into the 0.87–0.90 band, and `G` winning its cell with the
tens cell blanking — i.e. `invalid_label:6G` gone from the census, not merely reduced.

## 3. PL2B / ch30 — full rebuild, including geometry

ch30 **moved**, so its zones and ROIs are rebuilt, not just its templates:

    door_calib.py --frames 40 --fresh     # fresh door + panel montages at the new aim
    door_calib.py --index / --anchor / --cells / --fitcells
    overlay_zones.py / zone_verify.py     # zones against the new framing
    # then steps 1-2 above for panel cells + templates, and step 4 for the door template

Its floor alphabet re-derives itself from evidence once rows accumulate on the new era
(`alphabet_job.py site-A ch30`) — the retention guard added on 09-08 will hold its previously
admitted floors while the new era's evidence is thin, and the sweep prints what it retained.

## 4. Door state templates — ch27/29/30/32/34/37

    python3 tools/build_state_templates.py --cam <cam> --out-dir door_state_templates
    python3 tools/verify_state_template.py --cam <cam> --video <cam>_cal.mp4 --anchor-s <sec>

**Separation, not LOO.** `verify_state_template.py` exists precisely because leave-one-out cannot
catch a template built from the wrong door state — a template cut entirely from *open* frames has
excellent LOO because every input resembles every other. Separation scores against frames the
template was not built from, split by the declared closed windows.

**Rebuild only what has narrowed.** Cameras whose closed/open separation still holds are left
alone: a rebuild there would move `door_version` and cost an era boundary for nothing. Record the
before/after separation for each camera you touch, and for each you deliberately skip.

ch30's door template comes from the 23 windows. Its state band is `y15-85` and is stored per camera
in the artefact — it is **not** the travel band in `tools/band_coords.json`; moving it is a
measurement, not an edit.

## 5. Cut over, then let one sweep run

Deploy the coupled set (`door_calib.py` and `alphabet_audit.py` ship as one — the version guard at
the top of `alphabet_audit.py` will refuse a mixed install). Restart the workers, then confirm the
new `door_version` is being stamped:

    sqlite3 -readonly 'file:/var/lib/liftlab/gateway.db?mode=ro' \
      "SELECT cam, door_version, COUNT(*), datetime(MIN(ts)+19800,'unixepoch')
       FROM gw_door_event WHERE ts > <cutover_epoch> GROUP BY cam, door_version;"

## 6. Record why each boundary is there (liftlab-cloud)

**Run this after the rebuilt worker has posted at least one row** — `--current` reads the newest
stamped `door_version`, so running it early stamps the era you just left.

    for C in ch27 ch29 ch30; do
      era_note.py --cam $C --current --reason "recalibrated against the post-2026-09-02 camera
        image profile (INCIDENT_decode_regression_0902.md). Templates and cells rebuilt from
        current-image exemplars; pre-09-02 data is valid under the previous era and MUST NOT be
        pooled across this boundary."
    done
    era_note.py --list

The reason then appears in `eras.csv` (`era_reason`, `reason_noted_ist`) and under the era selector
on each camera panel. `era_note.py` refuses to overwrite an existing note without `--replace`.

## 7. What the health line will do next

Signal 5 (`READING WORSE, NOT MISSING`) scopes its baseline to the **current** `door_version`, so
every recalibrated camera reports `baseline building` for `HEALTH_CONF_MIN_BASE_DAYS` (3) judged
days and then arms itself against the new era. **Expect three quiet days per camera, not an
alarm** — that is the era scoping working. If a rebuilt camera fires immediately, the baseline it
found is from the old era and something did not move `door_version` as expected.

---

## Verification checklist

| | check | pass condition |
|---|---|---|
| 1 | `bootstrap_exemplars.py --dry-run` | every digit 0-9 has ≥1 exemplar |
| 2 | `door_calib.py --labelcheck` | no unresolved probable mislabels |
| 3 | `door_calib.py --readtest` | held-out fixtures, numeric scores back to 0.87-0.90 |
| 4 | `tools/verify_state_template.py` | separation restored on every rebuilt door template |
| 5 | `gw_door_event` | new `door_version` stamped on every rebuilt camera |
| 6 | ch29 reason census | `invalid_label:6G` **absent**, `floor='G'` back to ~2,800/day |
| 7 | ch27 reason census | `no_read` back from 99.7% to ~20%, floor reads ~25,000/day |
| 8 | ch30 reason census | `invalid_label:666`/`:366`/`:333` absent |
| 9 | `era_note.py --list` | a reason recorded on each new era |
| 10 | health line | rebuilt cameras `baseline building`, not `READS:` |

## Not doing

- **No site visit, no profile revert.** Nobody on site holds the prior settings; a near-miss
  restore is worse than none, because it produces reads that score just well enough to be believed.
- **No gate loosening.** `DOOR_BLANK_STRONG` / `DOOR_BLANK_LIT_MARGIN` / `DOOR_MIN_SCORE` are
  process-wide, and widening them admits stale-template reads under a confident label — a silent
  outage in place of a visible one, with no era boundary marking where the data changed.
- **`probe_decode_ab.py` is deferred.** The two-clip control already exonerated the decode.
