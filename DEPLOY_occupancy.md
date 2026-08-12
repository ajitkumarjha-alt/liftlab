# Peak car occupancy — deploy, two boxes (2026-08-12)

Gateway columns + dashboard on the **VM**, the measuring worker on the **GPU box**, and the report
package on the VM alongside the dashboard. No registry change, no restart of anything that is not
named here.

**Every md5 below is of the file as published on the `pi-scripts` branch by this commit** — the
same bytes `curl` will fetch. Verify after each copy, before restarting; a file that does not match
is the failure that looks most like a successful deploy.

---

## 0. ORDER — VM FIRST. This one costs data, quietly.

The worker posts the occupancy fields as extra JSON keys. A gateway that predates the columns does
not error on them — it **ignores** them, and the episode is stored without a measurement that was
genuinely taken. Nothing logs it, nothing retries it, and the row is indistinguishable afterwards
from an episode where the car was never looked at.

    1. VM   validation_api.py  (the columns)      -> restart -> verify the columns exist
    2. VM   dash_api.py + liftlab_report/         -> restart
    3. GPU  gpu_analyze.py + counting.py          -> stop + pkill + start

Deploying the GPU side first loses every episode measured in the gap. Deploying the VM side first
loses nothing at all: the columns simply sit NULL until the workers catch up, which is exactly what
they mean.

---

## 1. VM — the gateway columns

| box | path | md5 |
|---|---|---|
| VM | `validation_api.py` | `3a4d8f3417db302bdc820b5b525165f8` |

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
APP=/opt/liftlab-b3/cloud

curl -fsSL "$B/validation_api.py" -o /tmp/validation_api.py
md5sum /tmp/validation_api.py        # must be 3a4d8f3417db302bdc820b5b525165f8
sudo install -o liftlab -g liftlab -m 644 /tmp/validation_api.py "$APP/validation_api.py"
sudo systemctl restart liftlab-cloud
```

The five columns (`occupancy_max`, `occupancy_frames`, `occupancy_degraded`, `analysed_frames`,
`human_occupancy`) are added by `ALTER TABLE` inside `_db()`, so the first request after the restart
migrates. Old rows read **NULL**, never 0 — that distinction is the whole feature and is worth
checking rather than assuming:

```bash
sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "PRAGMA table_info(validation_item);" | grep -E "occupancy|analysed_frames"
# expect five lines, and every existing row NULL:
sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "SELECT COUNT(*), COUNT(occupancy_max) FROM validation_item;"
# expect: <n>|0   — a non-zero second column here means something already wrote occupancy
```

## 2. VM — the dashboard and the report package

| box | path | md5 |
|---|---|---|
| VM | `dash_api.py` | `cd7eecf8572655289e7617ae4516b297` |
| VM | `lift_capacity.json` | `ad58ef45a9291f042921dc3e496e30ff` |
| VM | `liftlab_report/cli.py` | `d3d28d5365cecfc820ddef2cdae74117` |
| VM | `liftlab_report/eras.py` | `af9e07702337bb71cd3887d31f1740c5` |
| VM | `liftlab_report/model.py` | `00a59767dfbd52b638e5fe98acedbd6f` |
| VM | `liftlab_report/reader.py` | `268f04a0e8c3df8b0f014f2ebe4433b5` |
| VM | `liftlab_report/workbook.py` | `0c9b73e4963628d0b4ac14d32e14f907` |
| VM | `liftlab_report/fixtures.py` | `6ccbe1b3559e77a994acfee109a3e08d` |

```bash
curl -fsSL "$B/dash_api.py" -o /tmp/dash_api.py
md5sum /tmp/dash_api.py              # cd7eecf8572655289e7617ae4516b297
sudo install -o liftlab -g liftlab -m 644 /tmp/dash_api.py "$APP/dash_api.py"

for f in cli.py eras.py model.py reader.py workbook.py fixtures.py; do
  curl -fsSL "$B/liftlab_report/$f" -o "/tmp/lr_$f"
  sudo install -o liftlab -g liftlab -m 644 "/tmp/lr_$f" "$APP/liftlab_report/$f"
done
md5sum $APP/liftlab_report/*.py      # compare against the table above

# the capacity sidecar sits BESIDE the package, not inside it — eras.load_capacity()
# resolves __file__.parent.parent, the same rule lift_banks.json follows
curl -fsSL "$B/lift_capacity.json" -o /tmp/lift_capacity.json
md5sum /tmp/lift_capacity.json       # ad58ef45a9291f042921dc3e496e30ff
sudo install -o liftlab -g liftlab -m 644 /tmp/lift_capacity.json "$APP/lift_capacity.json"

sudo systemctl restart liftlab-cloud
```

`lift_capacity.json` ships **deliberately empty**. That is not an unfinished deploy: with no
confirmed basis the CAR LOADING sheet prints measured occupancy in absolute people and withholds the
loading factor. Filling it in is a separate, deliberate act — see §5.

Verify the dashboard end to end rather than by eye on a page:

```bash
curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/site-A/data" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["occupancy_note"]["calibration"]); \
    [print(c["cam"], (c.get("occupancy") or {}).get("peak"), (c.get("occupancy") or {}).get("n")) for c in d["cameras"]]'
```

Immediately after the deploy every camera prints `None None` — no worker has posted an occupancy
field yet. That is the correct output at this point, and it is why §3 comes next.

## 3. GPU box — the measuring worker

| box | path | md5 |
|---|---|---|
| GPU | `gpu_analyze.py` | `baae9bca3eb9ac382d3426d49d3905c6` |
| GPU | `counting.py` | `f12405bf9f8d019fa92d2e2fc204eb21` |

**Check first — these may already be in place.** `counting.py` at this md5 went to the box for the
anchor pair test, and `gpu_analyze.py` carries the episode-gate fix that was deployed on its own.

```bash
md5sum __APPDIR__/gpu_analyze.py __APPDIR__/counting.py
```

If both already match, **stop here for the GPU box** — nothing to do, and a restart buys nothing.
If either differs:

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
curl -fsSL "$B/gpu_analyze.py" -o /tmp/gpu_analyze.py
curl -fsSL "$B/counting.py"    -o /tmp/counting.py
md5sum /tmp/gpu_analyze.py /tmp/counting.py
#   baae9bca3eb9ac382d3426d49d3905c6  gpu_analyze.py
#   f12405bf9f8d019fa92d2e2fc204eb21  counting.py
sudo install -o liftlab -g liftlab -m 644 /tmp/gpu_analyze.py __APPDIR__/gpu_analyze.py
sudo install -o liftlab -g liftlab -m 644 /tmp/counting.py    __APPDIR__/counting.py

sudo systemctl stop liftlab-gpu-fleet
sudo pkill -f gpu_analyze.py || true
sudo systemctl start liftlab-gpu-fleet
```

**stop + pkill + start, not restart.** `cycle_stale_code` watches `gpu_analyze.py` only; a
`counting.py` change does not trigger it, and a plain restart can leave a worker running the old
module.

Watch one camera through a door-open:

```bash
journalctl -u liftlab-gpu-fleet -f | grep -E "episode occupancy|episode POST"
```

Expect, per episode:

```
episode occupancy: peak N MEASURED MINIMUM in cabin over M analysed frames
episode POST -> HTTP 200
```

`peak 0 over M frames` is a real measurement of an empty car and is fine. `over 0 analysed frames`
is not — it means the episode carried no coverage, and the dashboard will count it under "no
coverage" rather than as a zero.

Then confirm the number actually landed:

```bash
sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "SELECT cam, COUNT(*), MAX(occupancy_max), SUM(occupancy_frames>0), SUM(occupancy_degraded)
     FROM validation_item WHERE ts_start > strftime('%s','now')-3600 GROUP BY cam;"
```

## 4. What is TRUE after this that was not before

* Every door-open episode carries a peak car occupancy and the frame count behind it.
* The dashboard shows it per camera (today / window peak / p95), by hour-of-day, and in a new
  `episodes` CSV — each labelled **measured minimum** with the calibration attached.
* `liftlab-report` gains a **CAR LOADING** sheet.
* **No loading factor is printed anywhere**, on any surface, until §5 is done.

## 5. NOT part of this deploy — the capacity basis

The one outstanding decision, and it is not a code change:

1. Decide the basis — **nameplate persons** (rating plate / lift licence) or **design persons per
   MEP-02 v28**.
2. Find out which of those the sheet's **80%** loading assumption is written on. If it is the other
   one, the workbook prints its own figure and withholds the comparison rather than crossing the
   two.
3. Fill in `$APP/lift_capacity.json` — `basis`, `basis_confirmed_by` (a document, not just a name),
   `sheet_basis`, and `persons` per camera — and re-export. No restart needed; the sidecar is read
   per run.

Until then CAR LOADING says exactly what is missing, and the measured occupancy is already usable in
absolute people.

## 6. Rollback

Per box, file-level, no data migration to undo. The columns can stay: they are additive, every old
reader ignores them, and dropping them would discard measurements already taken.

```bash
# VM
sudo install -o liftlab -g liftlab -m 644 /path/to/previous/validation_api.py "$APP/validation_api.py"
sudo install -o liftlab -g liftlab -m 644 /path/to/previous/dash_api.py       "$APP/dash_api.py"
sudo systemctl restart liftlab-cloud
# GPU
sudo install -o liftlab -g liftlab -m 644 /path/to/previous/gpu_analyze.py __APPDIR__/gpu_analyze.py
sudo systemctl stop liftlab-gpu-fleet; sudo pkill -f gpu_analyze.py || true
sudo systemctl start liftlab-gpu-fleet
```

Rolling back the VM while the GPU box stays current is the one combination that silently drops
measurements — the same failure as deploying in the wrong order. Roll back the GPU box first.
