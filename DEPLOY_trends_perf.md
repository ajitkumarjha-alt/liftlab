# /trends at 1.8M door rows — deploy (2026-08-19, rev 2)

> **REV 1 DID NOT WORK, AND THE HEADLINE NUMBER IN IT WAS WRONG.** `ix_door_era` is built and used
> — the plan is right — and `/trends` went 29 s → 37.7 s, `/data` 4.8 s → 9.0 s. The "1780x" was
> measured on ch29, whose current era holds **72 of 232,956 rows (0.0%)**: the era filter helps in
> proportion to what it EXCLUDES and I picked the camera where it excluded everything. On ch16
> (51.5% in era) the same query with the same index still costs 582 ms. Row VOLUME is the residual
> cost and no index removes rows the answer needs. Rev 2 moves the derivation to the timer.
> **The index stays** — it is proven good and it makes the precompute cheaper.

VM only. One index built by hand **before** the restart, then three files. No GPU box, no Pi, no
registry change.

**md5s as published on `pi-scripts`** (`3721a1a`), verified by fetching them back from raw.

| box | path | md5 |
|---|---|---|
| VM | `dash_api.py` | `dda2e83b73d9f37acbe5939b77b5b7be` |
| VM | `door_event_api.py` | `ee93e0c070c4e229475f81241af1db11` |
| VM | `precompute_job.py` | `5c7f09795a5089b2897894d04cf5c04a` |
| VM | `rtt_core.py` | `ffbaf2522e698b7193b8a87b1b20591a` |

This dash_api **supersedes** `3c40b00c…` (the precomputed RTT re-land) — it is that plus the era
range rewrite. `precompute_job.py` must land with it or the RTT column is never filled.

---

## 0. MEASURE FIRST, ON THE LIVE FILE. This is the step that has been skipped twice.

```bash
sudo cp /var/lib/liftlab/gateway.db /tmp/gw_copy.db && sudo chown "$USER" /tmp/gw_copy.db
python3 tools/trends_profile.py --db /tmp/gw_copy.db --cam ch29        # before
python3 tools/test_era_clause.py --db /tmp/gw_copy.db                  # correctness
```

`trends_profile.py` runs the **real** `dash_trends` and times every statement it issues with
EXPLAIN QUERY PLAN beside it. `test_era_clause.py` proves the rewritten filter selects exactly the
rows the old `LIKE` selected, on this database, for every era prefix on every camera. **Run it
before the deploy, not after** — it is the only thing standing between a 10x speedup and a silent
change to every door-derived number on the dashboard.

If it reports case-folding differences, stop and look: `LIKE` was pooling two `door_version`s that
differ only in case, which is two instruments in one bucket, and the numbers will move.

## 1. Build the index by hand, with the writer stopped

`CREATE INDEX` locks `gw_door_event` for the whole build — **5.4 s over 652k rows locally, so expect
tens of seconds over 1.81M** — and the door-event ingest writes to that table continuously. Doing it
by hand, with the writer down, makes it a controlled pause instead of a stall inside a request.

```bash
sudo systemctl stop liftlab-cloud
time sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "CREATE INDEX IF NOT EXISTS ix_door_era ON gw_door_event(gateway_id,cam,door_version,ts);
   ANALYZE gw_door_event;"
sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gw_door_event';"
#   ix_door_event
#   ix_door_era      <-- both, not one
```

`door_event_api.py` also creates it `IF NOT EXISTS` at startup, so it can never simply be missing on
a fresh gateway. Building it here makes that call a no-op instead of a restart that blocks POSTs.

**Disk**: ~447 bytes/row is the current whole-DB figure; the index itself adds roughly
`(door_version + ts + rowid)` per row — budget ~100 MB on 1.81M rows and check free space first.

## 2. Install and restart

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for f in dash_api.py door_event_api.py precompute_job.py rtt_core.py; do
  curl -fsSL "$B/$f?cb=$(date +%s)" -o "/tmp/$f"
done
md5sum /tmp/dash_api.py /tmp/door_event_api.py /tmp/precompute_job.py /tmp/rtt_core.py
# compare against the table above BEFORE installing

APP=/opt/liftlab-b3/cloud
STAMP=$(date +%Y%m%d-%H%M%S)
for f in dash_api.py door_event_api.py precompute_job.py rtt_core.py; do
  sudo cp "$APP/$f" "$APP/$f.bak.$STAMP" 2>/dev/null
  sudo install -o liftlab -g liftlab -m 644 "/tmp/$f" "$APP/$f"
done
sudo systemctl start liftlab-cloud
```

## 3. THE PRECOMPUTE IS NOW MANDATORY, NOT A WARM-UP

`/trends` no longer derives anything for the default view. Until the job runs, every trends page
reads **"NOT COMPUTED YET"** — the honest state, deliberately not an empty chart beside a zero.

```bash
sudo -u liftlab GATEWAY_DB=/var/lib/liftlab/gateway.db \
  $APP/.venv/bin/python $APP/precompute_job.py site-A
#   [precompute] trends site-A/ch29: 29KB in 0.21s (dv=260d4a0fh3-stateT5471cb)
#   [precompute] trends site-A/fleet: 9KB in 0.40s
```

Run it **once by hand now**, then the timer owns it. A cached payload is served even when stale,
with its age attached, because a number from an hour ago that renders beats a current one that
times out — `DASH_TRENDS_STALE_S` (default 5400) only controls when the UI labels it stale.

Custom date ranges, era overrides and hour filters still derive live: those are deliberate questions
and are not what was timing out.

## 3b. Fill the RTT column once, then let the timer own it

RTT no longer runs in any request handler. Until the precompute has run, every RTT panel reads
**"RTT NOT YET COMPUTED"** — which is the honest state, and deliberately not "no round trips".

```bash
sudo -u liftlab GATEWAY_DB=/var/lib/liftlab/gateway.db \
  $APP/.venv/bin/python $APP/precompute_job.py site-A
# each line now reports RTT separately, with its own cost:
#   [precompute] aggregate site-A/ch29: 41233 rows, tier2=yes, rtt=1569 trips in 2.10s, ...
systemctl list-timers liftlab-precompute --no-pager
```

**Watch the per-camera `rtt=… in …s` figures.** They are the number that decides whether the sweep
still fits inside its timer interval as the table grows; a total that hides them cannot show the job
drifting towards its own period.

## 4. Verify

```bash
time curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/site-A/trends?cam=ch29" -o /dev/null
time curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/site-A/data" -o /dev/null
time curl -fsS -u "$OPERATOR" "https://lift.gargi.online/dash/site-A/rtt?cam=ch29" -o /dev/null
python3 tools/trends_profile.py --db /tmp/gw_copy.db --cam ch29     # after
#   wall Xs   SQL Ys across N statements   Python Zs
#   SEARCH gw_door_event USING INDEX ix_door_era (… AND door_version>? AND door_version<?)
# It now profiles the DERIVATION (_trends_compute), not the cache read, and splits SQL from Python
# — if most of the time is Python it drops into cProfile and names the frames. That split is what
# rev 1 lacked: it timed only SQL, so a run spending its seconds walking rows would have shown a
# fast query list and no explanation.
```

Local, 652k rows: trends **1.1–1.7 ms** served from cache (miss 1.7 ms, and a miss now renders a
named state instead of charts); precompute 66–404 ms per entry; `/rtt` 0.1–2.5 ms. The request path
no longer scales with the table, so the live figure should look like these rather than like a
fraction of 37 s — if it does not, the profiler will say why in one run.

And open the page — `/trends` returning fast is not the same as the trends section rendering. The
browser showed "loading…" indefinitely, which is a client-side symptom of the same timeout and needs
its own confirmation.

## 5. Rollback

```bash
sudo systemctl stop liftlab-cloud
for f in dash_api.py door_event_api.py precompute_job.py rtt_core.py; do
  sudo cp "$APP/$f.bak.$STAMP" "$APP/$f"
done
sudo systemctl start liftlab-cloud
```

**Leave `ix_door_era` in place.** It is additive, the old code simply ignores it, and dropping it
costs another table lock for nothing. The `rtt`/`rtt_ms` columns on `door_aggregate` are additive
too — nothing else reads them.

## 6. The growth driver — not today's fix, but the reason today's fix is not permanent

Measured on the snapshot, per camera, rows written per day against **door-state changes** per day:

| cam | rows/day | state changes/day | rows per change |
|---|---|---|---|
| ch16 | 5,967 | 3,883 | 1.5 |
| ch27 | 26,718 | 10,907 | 2.4 |
| ch29 | 10,745 | 800 | **13.4** |
| ch30 | 15,717 | 7,887 | 2.0 |

**59,147 rows/day across four cameras**, at ~447 bytes/row all-in — and the live table is seven
cameras and 1.81M rows. ch29 writes 13.4 rows per actual state change; the arrow/state census put
the chatter at 28–66x the liveness floor.

The era range rewrite changes the scaling from *rows in the camera's whole history* to *rows in the
current era*, which is why it survives growth better than any index would on its own: a rebuild
resets the working set. But within an era the cost is still linear in rows, so chatter spends the
headroom this deploy just bought. **A chatter fix is a storage and query fix, not only a signal
one** — and it belongs on the board with a number against it, which is what the table above is for.
