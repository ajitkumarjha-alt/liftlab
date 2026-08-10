# FLOOR_STRIDE deploy — ch29 capacity fix (2026-08-10)

Stage 1 only: the floor OCR gets its own cadence, separate from the door pass. Ships **inert** —
`FLOOR_STRIDE=0` is today's behaviour, and a camera only changes when its registry row says so.

Expected on ch29: **4192–4531 ms → ~1700 ms, 2.10–2.27x → ~0.85x.** Nothing else changes.

---

## 0. ORDER — VM first, then GPU

The VM must accept `floor_stride` (registry) and `floor_age_s` (door_event) before the GPU sends
either. In the other order the registry POST 400s and every door_event row loses its age column.

    1. VM   camera_registry_api.py + door_event_api.py  -> restart the API
    2. VM   POST ch29's floor_stride (section 3)
    3. GPU  gpu_door.py + gpu_analyze.py + gpu_fleet.py -> full restart sequence (section 2)
    4. WATCH (section 4)

Steps 1–2 are safe alone: until the GPU has the new code, `floor_stride` is a field nobody reads
and `floor_age_s` is a column nobody writes.

## 1. Files

Source: `https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts/<file>`

| box | file | md5 |
|---|---|---|
| VM | `camera_registry_api.py` | `da3d44e0f1aa5d1c0ac5a37c79a6d1a7` |
| VM | `door_event_api.py` | `cc1b203e4f89cff18937bf3fe1e04db7` |
| GPU | `gpu_door.py` | `8c90ba6fd5c4503e063ce0b30b76654b` |
| GPU | `gpu_analyze.py` | `c170a20868351bce1b54e383829a4d6b` |
| GPU | `gpu_fleet.py` | `2297dc0fa647a278de3fdaed2d03f60a` |

Record the rollback set from the boxes BEFORE overwriting — the repo's previous commit is a guess at
what is deployed, the box is the authority:

```bash
md5sum __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py __APPDIR__/gpu_fleet.py \
  | tee ~/rollback-gpu-$(date +%Y%m%d-%H%M).md5
```

## 2. GPU box — THE FULL SEQUENCE, and why a plain restart is not enough

**`systemctl restart` alone is not sufficient to swap worker code.** Workers are separate processes
spawned from `WORKER_SCRIPT`; replacing that file changes what the NEXT spawn runs and nothing about
processes already running. Today's deploy needed stop + pkill + start to actually take effect.

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for f in gpu_door.py gpu_analyze.py gpu_fleet.py; do
  curl -fsSL "$B/$f" -o "/tmp/$f.new" && sudo install -m 644 "/tmp/$f.new" "__APPDIR__/$f"
done
md5sum __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py __APPDIR__/gpu_fleet.py   # match section 1

sudo systemctl stop liftlab-gpu-fleet
pgrep -af gpu_analyze                      # expect NOTHING; anything listed is an orphan
sudo pkill -f gpu_analyze.py || true       # kill orphans the supervisor is not tracking
sleep 2
pgrep -af gpu_analyze                      # must now be empty
sudo systemctl start liftlab-gpu-fleet

journalctl -u liftlab-gpu-fleet -n 60 --no-pager | grep -E 'started pid=|FLOOR_STRIDE'
pgrep -af gpu_analyze | wc -l              # expect 7, and only 7
```

`pkill` is the load-bearing step. `stop` ends the supervisor and its tracked children; an orphan from
an earlier unclean supervisor death is invisible to it and would keep running old code — and, worse,
would double up with the new worker for that camera.

**Going forward this is self-correcting for `gpu_analyze.py` only.** `cycle_stale_code()` (shipped
49c309a) compares each worker's spawn-time md5 against the file on disk every poll and cycles the
mismatches, one camera per poll. It watches `WORKER_SCRIPT`, so a change to **`gpu_fleet.py` itself
still needs the sequence above**. This deploy changes both, so run the full sequence.

## 3. Registry — ch29's FLOOR_STRIDE

```bash
curl -fsS -u "$OPERATOR" -X POST \
  "https://lift.gargi.online/api/gw/site-A/cameras/ch29" \
  -H 'Content-Type: application/json' \
  -d '{"floor_stride": 12}'

# verify — every other camera must read 0
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/api/gw/site-A/cameras" \
  | python3 -c 'import json,sys; [print(f"{c[\"cam\"]:6s} floor_stride={c.get(\"floor_stride\",0)}") for c in json.load(sys.stdin)["cams"]]'
```

**Why 12.** `DOOR_STRIDE=2`, so 12 frames = every 6th door pass ≈ **4 floor reads per 50-frame
segment** (down from 25), max floor age **0.44 s**. It must be a multiple of `DOOR_STRIDE` or a floor
read cannot land on a door-pass frame; the worker rounds up and says so rather than reading
erratically. `floor_stride` is in the registry config hash, so this restarts ch29's worker within
`FLEET_POLL_S` (30 s) on its own — no GPU-box action needed for the value itself.

**It is a knob, per camera, evidence first.** `tools/test_floor_stride.py` locates its limit: with a
1.6 s dwell, strides of 2/6/12/25 all attribute every floor, and a stride of 60 (longer than the
dwell) loses two. Keep it comfortably under the shortest dwell of interest.

## 4. Watch

### The capacity result

```bash
journalctl -u liftlab-gpu-fleet --since '5 min ago' --no-pager | grep 'seg timing' | tail -8
```

ch29's line must show `door=` dropping from ~3000 ms to roughly a quarter of it and the ratio going
under 1.0x. The other six must be unchanged — they are on `floor_stride=0` and this ships inert for
them.

### The acceptance addition: attribution must not degrade

Fewer reads must mean fewer REDUNDANT reads, not fewer attributed cycles. Compare an equal window
before and after on the SAME camera and hour-of-day:

```bash
# distinct floors attributed, and the share of door rows carrying a floor at all
sudo sqlite3 /var/lib/liftlab/gateway.db "
  SELECT COUNT(*) rows,
         SUM(floor IS NOT NULL) with_floor,
         ROUND(100.0*SUM(floor IS NOT NULL)/COUNT(*),1) pct_attributed,
         COUNT(DISTINCT floor) distinct_floors,
         ROUND(AVG(floor_age_s),3) mean_age_s,
         ROUND(MAX(floor_age_s),3) max_age_s
  FROM gw_door_event
  WHERE cam='ch29' AND ts > strftime('%s','now')-3600;"

# stops still being detected (FloorTracker sees only real reads now)
sudo sqlite3 /var/lib/liftlab/gateway.db "
  SELECT COUNT(*) FROM gw_door_event
  WHERE cam='ch29' AND ts > strftime('%s','now')-3600 AND reason='ok';"
```

PASS: `pct_attributed` and `distinct_floors` hold within noise of the pre-change window, `max_age_s`
≤ ~0.44 s, ratio under 1.0x. **If attribution drops, lower `floor_stride` — that is the knob, and the
evidence is this query, not a judgement.**

`floor_age_s` is **additive and does NOT move the door era**: it records the age of a lag that
already existed implicitly and is now visible per row. No `door_version` change, no era split, no
pool break.

## 5. Rollback

```bash
# registry alone reverts the behaviour without touching code
curl -fsS -u "$OPERATOR" -X POST \
  "https://lift.gargi.online/api/gw/site-A/cameras/ch29" \
  -H 'Content-Type: application/json' -d '{"floor_stride": 0}'
```

That restores read-every-door-pass within one poll. Code rollback (section 1 md5s + the section 2
sequence) is only needed if the split itself misbehaves, not to undo the cadence.
