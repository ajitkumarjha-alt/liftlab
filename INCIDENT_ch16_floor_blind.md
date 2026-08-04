# INCIDENT — ch16 (lift 1) went floor-blind on 2026-07-25, and nothing noticed for ten days

**Status: OPEN, not investigated. Written up only.** Found while correcting the Tier-2 confident-read
definition (see `README_REPORT.md`); it is a separate fault and is deliberately not fixed there.

**Severity: this camera has produced no usable floor data since 2026-07-25 08:12 UTC.** Every
floor-dependent figure for lift 1 is derived from 1 confident read.

> **DATE CORRECTED 2026-08-04 — the original write-up named 2026-07-29 04:15. That is wrong,**
> **and it would have sent a site engineer to the wrong day.** See "Correction" below. The
> failure is four days earlier; 07-29 04:15 is an unrelated software deploy.

---

## What happened

ch16's floor reading collapsed from 46 % of door observations to effectively zero, and stayed there.

| door_version | rows | confident | rate | window (UTC) |
|---|---:|---:|---:|---|
| `e79e50d3+495e8f48` | 556 | 0 | 0 % | 07-22 13:31 → 14:37 |
| `e79e50d3+7b1c0bad` | 41,789 | **19,424** | **46 %** | 07-22 14:50 → **07-29 04:14** |
| `e79e50d3h2+7b1c0bad` | 6,240 | **0** | **0 %** | **07-29 04:15** → 07-30 06:01 |
| `e79e50d3h2+87e5c93f` | 3 | 0 | 0 % | 07-30 06:16 → 06:20 |
| `e79e50d3h2+54509e75` | 23,160 | **1** | **0.004 %** | 07-30 06:25 → present |

~~The break is sharp: the last healthy row is 07-29 04:14:03 and the first dead row is 07-29 04:15:09,
about one minute apart.~~

## Correction (2026-08-04): the table above misreads an era boundary as the failure

The table aggregates by `door_version`, so it reports **when each era ended**, not when reads stopped.
The `e79e50d3+7b1c0bad` era does contain 19,424 confident reads — but **every one of them predates
2026-07-25 08:12:39 UTC**, and the era then ran for four more days producing zero. "Last healthy row
07-29 04:14:03" is the last row *of that era*, not the last row *with a confident read*.

Confident reads (`reason` in `ok` / `single_panel` / `''`), 6-hour buckets:

| bucket (UTC) | rows | confident | rate |
|---|---:|---:|---:|
| 07-25 00:00 | 7,607 | 4,862 | 63.9 % |
| 07-25 06:00 | 3,869 | 2,115 | 54.7 % |
| **07-25 12:00** | 1,388 | **0** | **0 %** |
| 07-25 18:00 | 724 | 0 | 0 % |
| 07-26 00:00 | 3,452 | 0 | 0 % |
| … through 07-30 18:00 | | 0 | 0 % |

**Last confident floor read: 2026-07-25 08:12:39 UTC.** The failure window is 07-25 between 06:00 and
12:00 UTC — a different day, and a different maintenance window, from the one originally recorded.

### What actually happened on 07-29 04:15

That timestamp is the **`h2` tracker-logic deploy**, and it is fleet-wide, not a ch16 event:

| cam | first `h2` row |
|---|---|
| ch27 | 07-29 04:15:07 |
| ch29 | 07-29 04:15:08 |
| ch16 | 07-29 04:15:09 |
| ch30 | 07-30 15:17:30 |

Three cameras within two seconds. `h2` is `TRACKER_LOGIC` in `gpu_door.py:79` — "close-start
hysteresis band + debounce (2026-07-29 flap fix)". It has nothing to do with floor OCR, and it did
not cause the blindness, which had already been in place for four days.

The open question in this document — *"what is `h2`, which commit or calibration applied it on
07-29 ~04:15"* — is therefore **answered, and it is not the culprit.**

## The templates did not change — and neither did the geometry

`door_version` is `templates_hash[:8] + "+" + geometry_hash[:8]`, with a hand-added era tag.

* templates half: `e79e50d3` **before and after**. Byte-identical content hash.
* geometry half: `7b1c0bad` **before and after** the break.

**Only the `h2` tag changed.** So this was not a template rebuild and not an ROI move — both inputs
the version string is supposed to describe were unchanged across the break. Whatever `h2` marks, it
changed behaviour without changing either hash, which is itself worth fixing: an era tag that can
alter reading behaviour while both content hashes stay constant defeats the point of a content hash.

The geometry did move later (`87e5c93f`, then `54509e75` on 07-30 06:25), but the reads were already
dead by then, so the geometry changes are consequence or coincidence, not cause.

## What the reader is doing now

Per-reason counts tell a clear story — this is not degraded reading, it is *no* reading:

```
e79e50d3+7b1c0bad    single_panel=19,424   no_read=17,616   ambiguous=4,749   mean read_conf 0.726
e79e50d3h2+7b1c0bad  single_panel=0        no_read=6,206    ambiguous=34      mean read_conf None
e79e50d3h2+54509e75  single_panel=1        no_read=22,483   ambiguous=676     mean read_conf 0.566
```

`ambiguous` also collapsed (4,749 → 34). An ambiguous read means the reader saw candidate glyphs and
could not separate the top two. Losing *both* successful and ambiguous reads means the panel reader
is not finding glyph candidates at all — consistent with the ROI no longer containing the indicator,
the crop being blank/saturated, or the reader being handed the wrong region — rather than with a
harder-to-read but still-present panel.

## It is ch16-specific

The same `h2` tag was applied to other cameras without this effect:

| cam | before → after `h2` |
|---|---|
| ch27 | `425f92e1` 52 % → `425f92e1h2` 44 % (mild drop, still working) |
| ch29 | `260d4a0f` 77 % → `260d4a0fh2` 84 % (improved) |
| **ch16** | `e79e50d3` 46 % → `e79e50d3h2` **0 %** |

So `h2` alone does not break a camera. Something about ch16's configuration interacts with it.

## Prior art on this camera

ch16 already has a known reading pathology. The gateway service runs with `NO_HUNDREDS_CAMS=ch16`,
and ch16's derived floor alphabet contains phantom three-digit floors (`122`, `126`, `128`, `133`,
`160`–`167`) that the building does not have. Whatever is wrong with its panel geometry or template
set was already producing structured misreads before this break.

## Why it went unnoticed for six days

The Tier-2 sheet reported **zero confident reads for every camera** because the confident-read test
excluded `reason='single_panel'` — the only reason a single-panel camera ever emits. ch16 going from
19,424 to 1 was invisible: it read as zero before and zero after. Correcting the definition is what
surfaced it.

Two reporting gaps kept it hidden and are worth closing regardless of the cause:

1. **No alert on a collapse in confident-read rate.** A camera dropping from 46 % to 0 % between two
   consecutive rows is exactly the "floor-read collapse" shape `/ops` already detects for the recent
   6h vs trailing 7d — but nothing watches it per *era boundary*.
2. **`gpu_era_id()` truncates the templates half to 8 characters**, so `e79e50d3h2` and `e79e50d3`
   collapse to the same era id. Any per-camera total in the report merges the healthy era with the
   dead one and shows a reassuring 19,425. The Tier-2 evidence table now uses `templates_era()`
   (untruncated) for exactly this reason — but `gpu_era_id` still groups door **cycles**, so
   cycle-derived figures may be pooling across this boundary. See the open item in
   `README_REPORT.md`.

## To investigate

1. What is `h2` — which commit, config change or calibration action applied it on 2026-07-29 ~04:15,
   and why did it change reading behaviour with both content hashes unchanged?
2. Pull a current ch16 frame and its panel ROI crop (`/calib/site-A/ch16/`) and confirm whether the
   floor indicator is inside the ROI at all. The loss of `ambiguous` reads points here first.
3. Compare ch16's panel/cell calibration against ch27 and ch29, which survived the same tag.
4. Decide whether the phantom-hundreds pathology and this break share a root cause.

## Not to do

Do not "fix" this by widening the confident-read definition further, and do not reinstate ch16 into
any floor-dependent figure until it reads again. One confident read in 23,160 observations is not a
thin sample — it is a dead instrument, and the Tier-2 evidence table should keep saying so.
