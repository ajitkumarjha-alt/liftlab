# ch29 → h3 state-only, floor_stride 6 (2026-08-11)

One data file to the GPU box, one registry POST. **No code changes in this deploy** — the h3 loader
and the floor_pass split are already on the box from the ch27/ch30 rollout and the floor_stride
deploy.

---

## 0. ORDER — TEMPLATE FILE FIRST, REGISTRY POST SECOND. This one bites.

`floor_stride` and `door_tracker` are both in the registry config hash, so the POST restarts ch29's
worker within `FLEET_POLL_S` (30 s). If the POST lands **before** the template file is in place, the
worker rebuilds, finds no `ch29.json`, logs `FALLING BACK TO h2` — and then **nothing restarts it
again**, because the config hash will not change a second time. ch29 would sit on h2, correctly
logged but not what you asked for, until someone forces a restart by hand.

    1. GPU   door_state_templates/ch29.json  -> in place, md5 verified
    2. VM    the registry POST (section 3)
    3. WATCH (section 4)

## 1. The file

| box | path | md5 |
|---|---|---|
| GPU | `door_state_templates/ch29.json` | `aed3e6d73b2a49760b349e2a21530363` |

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
curl -fsSL "$B/door_state_templates/ch29.json" -o /tmp/ch29.json
md5sum /tmp/ch29.json          # must be aed3e6d73b2a49760b349e2a21530363
sudo install -m 644 -o liftlab -g liftlab /tmp/ch29.json \
     __APPDIR__/door_state_templates/ch29.json
```

If `DOOR_STATE_TPL_DIR` is set in `/etc/liftlab-gpu.env`, put it there instead — the loader reads
that variable and falls back to `./door_state_templates` relative to the worker's cwd.

**A template whose md5 does not match is REJECTED, and that camera silently falls back to h2.** That
is the failure that looks like a successful deploy; verify the md5 before the POST, not after.

### Prerequisite — confirm the box already has the h3 code

This deploy assumes the loader and the floor split are present. If these do not match, the box is
behind and needs the code deploy from `DEPLOY_floor_stride.md` first:

```bash
md5sum __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py
# expected at this commit:
#   8c90ba6fd5c4503e063ce0b30b76654b  gpu_door.py
#   4975d725ec2750fea176ca134aab49ad  gpu_analyze.py
```

`gpu_analyze.py` also carries the async POST queue (`89e779c`) and `gpu_door.py` the floor_pass
split. If the box predates those, deploy them by the `DEPLOY_floor_stride.md` sequence — including
`stop + pkill + start`, because `cycle_stale_code` watches `gpu_analyze.py` only and a `gpu_door.py`
change needs the full sequence.

## 2. The template, for the record

```
band y15-130 x260-470   (localized by open/closed frame differencing on the 704x576 substream;
                         the registry's 1920x1080 geometry does NOT scale onto this framing)
102 frames across all 17 verified door-closed windows, capped 6 per window
source ch29_tue.mp4, OSD = video_t + 08:37:16 IST
LOO NCC        median 0.9895   min 0.8943      (ch27 0.9863, ch30 0.9775)
held-out closed  p50 0.992  p05 0.902  min 0.889
open tail        p10 0.613  p25 0.736  min 0.473
```

Separation is wide — open p25 0.736 against closed min 0.889 — and the tracker normalises against
rolling p10/p90 anyway, so the gap matters more than either absolute value.

## 3. The registry POST — one call, both fields

```bash
curl -fsS -u "$OPERATOR" -X POST \
  "https://lift.gargi.online/api/gw/site-A/cameras/ch29" \
  -H 'Content-Type: application/json' \
  -d '{"door_tracker":"h3","floor_stride":6}'

# verify
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/api/gw/site-A/cameras" \
  | python3 -c 'import json,sys; [print(f"{c[\"cam\"]:6s} {c.get(\"door_tracker\",\"h2\"):9s} floor_stride={c.get(\"floor_stride\",0)}") for c in json.load(sys.stdin)["cams"]]'
```

Both fields are in the config hash, so this is **one** worker restart, not two.

**Why 6 and not the deployed 12** — measured on 18,380 door-pass frames of `ch29_tue.mp4`:

| stride | OCR work | disagreement vs a fresh read |
|---:|---:|---:|
| 6 | 33% | **3.45%** |
| 12 | 17% | 8.56% |

`pct_attributed` is ~81% at every stride and cannot decide this — carrying a value forward keeps it
high by construction. Disagreement is the metric. With the POST queue absorbing the HTTP cost,
ch29's remaining compute is ~53–104 ms/segment against a 2000 ms budget, so 33% OCR is affordable.

## 4. Watch

```bash
journalctl -u liftlab-gpu-fleet -n 80 --no-pager | grep -E 'ch29|GPU_DOOR ENGINE'
```

Must show:

```
GPU_DOOR ENGINE: ch29 running h3-state — template 5471cbd9 from ch29_tue.mp4 (102 closed frames, ...
GPU_DOOR ENGINE: ch29 emits close_travel_s=NULL on every cycle by design — ...
```

A `FALLING BACK TO h2` line for ch29 means the deploy did not take; the reason is on the same line.

Then, in the seg-timing line, `door=` should fall further (floor OCR at 33% instead of 17%… it will
rise slightly against stride 12, and that is intended) and `close_travel_s` must be NULL on every
ch29 row:

```sql
SELECT COUNT(*), SUM(close_travel_s IS NOT NULL), COUNT(DISTINCT door_version)
FROM gw_door_event WHERE cam='ch29' AND ts > strftime('%s','now')-1800;
-- expect: rows > 0, non-null travel = 0, one door_version
```

## 5. Two things that are true after this and were not before

**The compliance panel loses its only live GPU travel figure.** ch29 is the sole camera in
`DOOR_SPECS`, so once its era resolves h3 the panel shows: the retired Pi-watch line, the hand-timed
validated line, and an h3 line stating travel is not measured. Verified against the real dash code
with synthetic h3 rows — nothing errors, `spec` is still carried (sheet 2.00 s, cliff 2.31 s, Bank
C), the funnel reports `engine=h3-state` with conservation balanced. The h2-era numbers stay
reachable through the era selector, where the SUPERSEDED marking still fires.

**The weekly hand-timed sample is now load-bearing for the entire panel.** It already was in
substance; after this it is in form too, and `tools/weekly_stopwatch.md` LEG 2 has still never run.

## 6. An inert tag worth knowing about

ch29 carries `door_levels={'close_th': 0.2}`. `DoorTrackerH3()` takes no levels — those knobs belong
to the h2 edge-column tracker — so under h3 that value **does nothing**. But `levels_tag` in
`build_door_engine` is computed from the level env vars regardless of engine, so ch29's h3
`door_version` will read roughly:

    260d4a0fh3-stateL<hash>T5471cb+495e8f48

The `L<hash>` advertises a recalibration the engine ignores. Harmless — it is only an era
discriminator, and a distinct era is correct here anyway — but misleading to anyone reading versions
later. Clearing `door_levels` in the same POST would tidy it at the cost of another config-hash
change; left alone deliberately, so this deploy changes exactly the two fields agreed.

## 7. Rollback

```bash
curl -fsS -u "$OPERATOR" -X POST \
  "https://lift.gargi.online/api/gw/site-A/cameras/ch29" \
  -H 'Content-Type: application/json' \
  -d '{"door_tracker":"h2","floor_stride":12}'
```

Registry-only, effective within one poll, no file changes. The template can stay on disk — it is
inert while `door_tracker` is `h2`.
