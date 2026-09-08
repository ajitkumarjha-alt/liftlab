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

    cp /var/lib/liftlab/calib/site-A/<cam>/templates.npz  templates.npz.pre0902
    cp /var/lib/liftlab/calib/site-A/<cam>/labels.json    labels.json.pre0902
    cp -a /var/lib/liftlab/calib/site-A/<cam>/            calib-pre0902-<cam>/

The old templates are the only remaining description of the August image. `ch29_templates.npz` in
the repo is byte-identical to the live ch29 set (hash `260d4a0f67ff0587…`) and is the reference
copy for that camera; ch27 and ch30 have no such copy in the tree, so **their backup is the only
one**. Do not skip this.

## 1. Bootstrap exemplars from the current image — ch29, ch27 (liftlab-cloud)

`bootstrap_exemplars.py` selects crops from `floor_sample` (which carries the real panel JPEGs,
post-09-02, on the cloud box) and labels them using the **old** templates, keeping only reads where
every glyph still clears 0.85 — the upper tail the degradation did not reach.

    python3 bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --dry-run     # census first
    python3 bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --out /tmp/boot_ch29

It writes `_calib_crop_NNNNN.png`, `labels.json` **and `labels_bind.json`** — the content binding
is mandatory, or `build_from_crops` excludes every label (the 2026-07-30 ch16 label-inheritance
rule). Check the tail of the output for two things:

- **`NO EXEMPLAR for digit(s) …`** — a digit with no exemplar is a digit the rebuild will *lose*.
  Widen `--since`, lower `--min-score`, or supply it from a confirmed window. Do not build past it.
- **The lobby is absent by design.** `G` scores ~0.79–0.83 now and cannot clear the bar; admitting
  it on a lower threshold is the circularity the tool exists to avoid.

**ch27 needs `--cells`/`--arrow` explicitly** — there is no `ch27_roi.json` in the tree. Its crops
are 75×101 (vs ch29's 51×92), so ch29's geometry is not transferable.

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
