# ch32 + ch34 + ch37 → h3 state-only — batch deploy (2026-08-13)

Three data files to the GPU box, three registry POSTs. **No code changes in this deploy** — the h3
loader has been on the box since the ch27/ch30 rollout.

This ships **door state only**. Floor attribution on these three is NOT part of it; see §6.

---

## 0a. DOOR GEOMETRY PRECEDES THE TRACKER FLAG — the step this note originally skipped

**A camera with no `door_roi_frame` comes up `door=off (geometry incomplete)` and the tracker flag
does nothing.** The first attempt at this batch failed exactly there on all three: templates in
place, POSTs accepted, workers up with the door engine disabled.

Geometry does NOT live in the registry. `camera_registry_api._geom_for` READS
`CALIB_DIR/{gw}/{cam}/roi.json` and serves it in the string shapes gpu_analyze parses, so there is
no registry column to POST a door band into — it has to be written to roi.json, and `/calib-roi`'s
save endpoint is what does that. It also records `frame_wh`, `saved_at`, `source_image` and
`drawn_on_wh`; `frame_wh` is the provenance that makes a later resolution change detectable, so a
hand-written roi.json without it is geometry that cannot be checked later.

So, per camera, BEFORE section 1:

    1. draw the door band at /calib-roi/site-A/{cam}  (Aj: approx x240-405, full door height)
    2. confirm roi.json has door_roi_frame AND frame_wh
    3. only then the template file and the POST

## 0. ORDER — ALL THREE FILES FIRST, THEN THE POSTS. This one bites.

`door_tracker` is in the registry config hash, so a POST restarts that camera's worker within
`FLEET_POLL_S` (30 s). If the POST lands **before** the template file is in place, the worker
rebuilds, finds no `chNN.json`, logs `FALLING BACK TO h2` — and then **nothing restarts it again**,
because the config hash will not change a second time. That camera sits on h2, correctly logged and
not what you asked for, until someone forces a restart by hand.

    1. GPU   all three template files in place, md5 verified
    2. VM    the three registry POSTs (section 3)
    3. WATCH (section 4)

## 1. The files

| box | path | md5 |
|---|---|---|
| GPU | `door_state_templates/ch32.json` | `c4aec4d3a14cfaa73b75f6d3caef6592` |
| GPU | `door_state_templates/ch34.json` | `7d34c00a5c9a998015c23c941ecea8d8` |
| GPU | `door_state_templates/ch37.json` | `ebc769008735298b0a352bdc6612c092` |

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for C in ch32 ch34 ch37; do
  curl -fsSL "$B/door_state_templates/$C.json?cb=$(date +%s)" -o "/tmp/$C.json"
done
md5sum /tmp/ch32.json /tmp/ch34.json /tmp/ch37.json
#   c4aec4d3a14cfaa73b75f6d3caef6592  ch32.json
#   7d34c00a5c9a998015c23c941ecea8d8  ch34.json
#   ebc769008735298b0a352bdc6612c092  ch37.json
for C in ch32 ch34 ch37; do
  sudo install -m 644 -o liftlab -g liftlab "/tmp/$C.json" \
       "__APPDIR__/door_state_templates/$C.json"
done
```

If `DOOR_STATE_TPL_DIR` is set in `/etc/liftlab-gpu.env`, install there instead — the loader reads
that variable and falls back to `./door_state_templates` relative to the worker's cwd.

**A template whose md5 does not match is REJECTED and that camera silently falls back to h2.** That
is the failure that looks like a successful deploy. Verify before the POSTs, not after.

## 2. What was verified, and how

LOO NCC is **self-consistency only** and cannot see the failure that matters: a template built
entirely from OPEN frames has excellent LOO, because every frame it was built from looks like every
other one. Each of these was scored against frames it was **not** built from, split into the declared
closed windows and everything outside (`tools/verify_state_template.py`):

| cam | frames / windows | held-out closed p05 | outside p10 | outside below the closed floor | anchor |
|---|---|---|---|---|---|
| ch32 | 203 / 34 | 0.972 | 0.100 | 89.4% | t=0 s → NCC 0.999, inside a window |
| ch34 | 125 / 21 | 0.969 | 0.232 | 77.6% | t=200 s → NCC 0.999, inside a window |
| ch37 | 161 / 27 | 0.992 | −0.224 | 88.1% | — |

Band **y15-120 x240-400** on all three, at the 704x576 substream framing. All three ACCEPT.

The outside-window median is high on every camera (0.639 / 0.741 / 0.442) and that is expected, not
a warning: only the declared windows are verified, and a lift is closed most of the day. What
convicts a wrong-state template is the closed floor against the outside LOW tail, which is the
column above.

ch34's anchor is the frame you confirmed by eye after your first scan used an open frame — it scores
0.999 against a template built without it.

## 3. The registry POSTs — one per camera

```bash
for C in ch32 ch34 ch37; do
  curl -fsS -u "$OPERATOR" -X POST \
    "https://lift.gargi.online/api/gw/site-A/cameras/$C" \
    -H 'Content-Type: application/json' -d '{"door_tracker":"h3"}'
done

curl -fsS -u "$OPERATOR" "https://lift.gargi.online/api/gw/site-A/cameras" \
  | python3 -c 'import json,sys; [print(f"{c[\"cam\"]:6s} {c.get(\"door_tracker\",\"h2\"):9s}") for c in json.load(sys.stdin)["cams"]]'
```

No `floor_stride` change: these three have no calibrated panel, so there is no floor OCR to thin.

## 4. Watch

```bash
journalctl -u liftlab-gpu-fleet -n 120 --no-pager | grep -E 'ch32|ch34|ch37|GPU_DOOR ENGINE'
```

Each camera must show:

```
GPU_DOOR ENGINE: chNN running h3-state — template <hash8> from chNN_cal.mp4 (N closed frames, ...
GPU_DOOR ENGINE: chNN emits close_travel_s=NULL on every cycle by design — ...
```

A `FALLING BACK TO h2` line means that camera's deploy did not take; the reason is on the same line.

```sql
SELECT cam, COUNT(*), SUM(close_travel_s IS NOT NULL), COUNT(DISTINCT door_version)
FROM gw_door_event WHERE cam IN ('ch32','ch34','ch37')
  AND ts > strftime('%s','now')-1800 GROUP BY cam;
-- expect per camera: rows > 0, non-null travel = 0, one door_version
```

## 5. Take a census before and after

These three are currently on h2, and the census is the instrument of record for what the door stream
looks like. Take the **before** now and the **after** a day later, in the same era each time:

```bash
python3 tools/door_event_census.py --db /var/lib/liftlab/gateway.db \
        --cam ch32 --cam ch34 --cam ch37 --recent-hours 24
```

`--recent-hours` is what keeps every window inside one era. Without it the search is unbounded and
silently picks whichever era was busiest, which is how a cross-camera table came to compare h2
against h3 on 2026-08-13.

Expect door_state to get calmer: ch29's h3 hour showed dwell p50 2.45 s against h2's 0.32–1.67 s,
and door_state fell to 4.7% of rows from 19–98%.

## 6. FLOOR ATTRIBUTION IS NOT IN THIS DEPLOY — and this needed a code change to be true

These cameras get door state and nothing else. Floor needs `digit_cells` / `arrow_cell`, and the
derivation from pixel variance **failed**: the row band it produces changes with how many frames are
sampled (ch34 gave three different answers over 300/600/1200 frames), so it is not a measurement.
Column structure converged on ch32 and ch37 and is contributed as evidence to `/calibrate`; ch34
shows no column gap at any sample size.

**The original version of this note promised behaviour the code could not perform.** It said these
cameras would ship door state and emit `floor=NULL`, but `build_door_engine` required
DOOR_ROI_FRAME **and** PANEL_ROIS **and** DIGIT_CELLS **and** ARROW_CELL, and `DoorFloorEngine`
raised outright on an empty panel list. A camera with a perfect door band and no cells could not run
the engine at all. That is why all three came up `door=off`.

Door-only is now a first-class mode: the door band is the only hard requirement, and a camera
without panel geometry reports `floor=NULL` with reason **`no_panel_geometry`** on every row — a
reason that names the cause, so it can never be mistaken for a failed OCR on a real panel. It
requires the GPU code at the md5 in section 1a.

The alternative — inventing panel geometry to satisfy the constructor — was available and is worse
than useless: `valid_floors` is None by default, so any assembled digit string is accepted, and the
result would be confident WRONG floor reads. That is exactly what the OCR gate exists to prevent.

Until cells exist and pass the standing gate — **n ≥ 20 correct-or-abstain, zero confident
misreads** — floor on these three is not trusted and not attributed.

## 7. Rollback

```bash
for C in ch32 ch34 ch37; do
  curl -fsS -u "$OPERATOR" -X POST \
    "https://lift.gargi.online/api/gw/site-A/cameras/$C" \
    -H 'Content-Type: application/json' -d '{"door_tracker":"h2"}'
done
```

Registry-only, effective within one poll, no file changes. The templates can stay on disk — they are
inert while `door_tracker` is `h2`. Roll back one camera or all three; they are independent.
