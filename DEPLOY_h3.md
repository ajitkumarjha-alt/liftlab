# h3 state-only — deploy manifest (2026-08-05)

Rollout: **per camera, ch27 + ch30 only.** ch16/29/32/34/37 stay on h2 with their known defects
until each has a validated template. Nothing in the code names a camera — the rollout is a registry
edit, and every default is h2.

**Two boxes.** The VM must go first; see ORDER below.

---

## 0. ORDER MATTERS — VM before GPU

`gpu_fleet.start()` sets `DOOR_TRACKER` on **every** worker from the registry's `door_tracker`
field. That is deliberate (a fleet-level env would otherwise leak h3 onto all seven cameras), and it
means **no env setting on the GPU box can turn h3 on.** The only switch is the registry, served by
`camera_registry_api.py` on the VM.

Deploy the GPU box first and it comes up with all seven cameras resolving h2, logging nothing
unusual — a silent no-op.

    1. VM:   camera_registry_api.py  -> restart the API
    2. VM:   POST the two registry rows (section 3)
    3. GPU:  files + templates       -> restart the fleet
    4. WATCH (section 5)

Steps 1-2 are safe on their own: until the GPU box has the new code, `door_tracker=h3` in the
registry is a field nobody reads.

---

## 1. Rollback set — RECORD BEFORE OVERWRITING

I cannot read the boxes. The repo's previous commit is a guess at what is deployed; the box is the
authority. Capture the real thing first:

```bash
# on the GPU box
md5sum __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py __APPDIR__/gpu_fleet.py \
  | tee ~/rollback-gpu-$(date +%Y%m%d-%H%M).md5
cp -a __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py __APPDIR__/gpu_fleet.py ~/rollback-gpu/

# on the VM
md5sum __APPDIR__/camera_registry_api.py | tee ~/rollback-vm-$(date +%Y%m%d-%H%M).md5
cp -a __APPDIR__/camera_registry_api.py ~/rollback-vm/
```

Rolling back the GPU box alone is sufficient to return every camera to h2 — the registry field
becomes inert the moment the old `gpu_fleet.py` is back, because it never reads it.

---

## 2. Files to pull

Source: `https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts/<path>`

### GPU box (`__APPDIR__`)

| file | md5 |
|---|---|
| `gpu_door.py` | `fd48ad6d5e81d903aa2e842eef2c0501` |
| `gpu_analyze.py` | `c9724902cccc49181df55d8dd5091669` |
| `gpu_fleet.py` | `2fb51bc5698b0e7d1b6d203a7cedb0f7` |
| `door_state_templates/ch27.json` | `ff736998a759e78b92501369d6e9454d` |
| `door_state_templates/ch30.json` | `8e21077aa0f091a291d9fbe9305218ce` |

### VM (`__APPDIR__`)

| file | md5 |
|---|---|
| `camera_registry_api.py` | `6e65e66357c96a190dbe21827f9c6d53` |

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
mkdir -p __APPDIR__/door_state_templates
for f in gpu_door.py gpu_analyze.py gpu_fleet.py \
         door_state_templates/ch27.json door_state_templates/ch30.json; do
  curl -fsSL "$B/$f" -o "__APPDIR__/$f.new" && mv "__APPDIR__/$f.new" "__APPDIR__/$f"
done
md5sum __APPDIR__/gpu_door.py __APPDIR__/gpu_analyze.py __APPDIR__/gpu_fleet.py \
       __APPDIR__/door_state_templates/ch27.json __APPDIR__/door_state_templates/ch30.json
```

Compare every line against the table before restarting anything. **A template whose md5 does not
match is not a degraded template — `load_state_template` rejects it and that camera silently falls
back to h2**, which looks like a successful deploy that did nothing.

---

## 3. Registry — the ENABLE setting

There is no camera list in any code or apply script. These two POSTs ARE the rollout.

```bash
# on the VM, or anywhere with operator basicauth
for CAM in ch27 ch30; do
  curl -fsS -u "$OPERATOR" -X POST \
    "https://lift.gargi.online/api/gw/site-A/cameras/$CAM" \
    -H 'Content-Type: application/json' \
    -d '{"door_tracker":"h3"}'
  echo
done

# verify — every other camera must read h2
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/api/gw/site-A/cameras" \
  | python3 -c 'import json,sys; [print(f"{c[\"cam\"]:6s} {c.get(\"door_tracker\",\"h2\")}") for c in json.load(sys.stdin)["cams"]]'
```

`door_tracker` accepts only `h2` or `h3` (400 otherwise) and is part of the registry config hash, so
setting it restarts that worker within `FLEET_POLL_S` (30s) and era-splits its data. Omitting the
field on any other POST leaves it untouched.

**Setting h3 is a REQUEST, not a guarantee.** The gateway still refuses h3 without a template that
loads and verifies.

---

## 4. systemd / env

**No env change is required to enable h3.** One is recommended as belt-and-braces:

```
# /etc/liftlab-gpu.env
DOOR_STATE_TPL_DIR=__APPDIR__/door_state_templates
```

The default `./door_state_templates` already resolves correctly because `gpu_fleet.py:173` spawns
workers with `cwd=os.path.dirname(WORKER_SCRIPT)`. That is an implicit dependency on a `cwd=`
argument three files away; an absolute path costs nothing.

`liftlab-gpu-fleet.service` itself needs **no edit**. Note that `liftlab-gpu.service` (the
single-camera ch29 unit) has no `WorkingDirectory`, so if it is ever pointed at an h3 camera the
relative default resolves against `/` and the template is not found -> silent fallback to h2. ch29
is h2, so this does not bite today.

```bash
sudo systemctl restart liftlab-gpu-fleet
journalctl -u liftlab-gpu-fleet -n 200 --no-pager | grep -E "GPU_DOOR ENGINE|door_tracker"
```

### What the log must say

Seven `GPU_DOOR ENGINE:` lines, one per camera:

```
GPU_DOOR ENGINE: ch27 running h3-state — template f4ffffc4 from ch27_clean.mp4 (48 closed frames, ...
GPU_DOOR ENGINE: ch27 emits close_travel_s=NULL on every cycle by design — ...
GPU_DOOR ENGINE: ch30 running h3-state — template 4419213e from ch30_peak.mp4 (36 closed frames, ...
GPU_DOOR ENGINE: ch16 running h2
GPU_DOOR ENGINE: ch29 running h2
... (ch32, ch34, ch37 likewise h2)
```

**A `FALLING BACK TO h2` line for ch27 or ch30 means the deploy did not take.** Do not proceed;
the reason is on the same line.

Expected `door_version` values (verified, not predicted):

| cam | engine | door_version |
|---|---|---|
| ch27 | h3-state | `<hash>h3-stateTf4ffff+<geom>` |
| ch30 | h3-state | `<hash>h3-stateT441921+<geom>` |
| h2 cameras | h2 | `<hash>h2+<geom>` — **byte-identical to before**, no era split |

---

## 5. Watch — 10 minutes on ch29

ch29 is the floor-rich camera and stays on h2, so it is the control for the one thing this deploy's
offline acceptance could NOT prove: that the `process()` refactor did not disturb floor OCR.

```bash
# floor_sample arrival rate and plausibility
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/floorcheck?cam=ch29" | head -40

# h2 door events unchanged: door_version must still be the h2 string, close_travel_s still numeric
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/events?cam=ch29&limit=50"
```

PASS looks like: floor_sample rows arriving at the prior rate with floors inside ch29's alphabet;
ch29's `door_version` unchanged; ch29 `close_travel_s` still populated with plausible values;
ch27/ch30 emitting door events with `close_travel_s` NULL and a `closing`/`closed`/`open` state.

**Any floor anomaly on ch29 -> roll back immediately** to the md5s recorded in section 1. The floor
path is the accepted deploy-time risk; it is the only thing this watch is really testing.

Also check, because it is the failure that looks like success: if ch27/ch30 emit door events whose
`door_version` contains `h2`, the templates did not load. Grep the startup log for the reason.

---

## 6. Known consequences, accepted at deploy time

* **dash's cycle funnel reads zero in its opening stage for ch27/ch30.** h3 has no `opening` state;
  it goes `closed -> open` directly. The funnel walks h2's four-state machine and has no h3 branch
  yet. Not a fault, not fixed here.
* **`close_travel_s` is NULL for ch27/ch30 forever**, with a reason string. Every compliance figure
  that depends on door travel now rests on the weekly hand-timed sample in
  `tools/weekly_stopwatch.md` — whose LEG 2 has never run, because dev-box has no ffmpeg and no
  operator credential is available to the harness. **Closing that gap is the highest-priority
  follow-up**: right now, travel for these two cameras has no source at all.
* **`openness` changes meaning for ch27/ch30.** h2's is a normalised edge column; h3's is
  `1 - normalised closedness` off the validated NCC signal. Same column, different instrument,
  distinguished by `door_version`.
* **ch27 emits 51 cycles per 47 min of corpus where h2 emitted 166**, and 38 remain unmatched
  against hand truth. Those are probably real closes nobody timed — probably is not measured.
