# Daily health line — deploy, VM only (2026-08-12)

One script, one timer, one dashboard banner. Nothing on the GPU box, nothing on the Pi, no registry
change. The check is read-only over every table it judges and writes only its own `health_status`.

**md5s as published on `pi-scripts`** — verified by fetching them back from raw, not by
hashing the working tree.

| box | path | md5 |
|---|---|---|
| VM | `health_check.py` | `b46c6aa3af093667c0d3662413083db5` |
| VM | `dash_api.py` | `e897b861181817535ff97719906f5e38` |
| VM | `apply_health.sh` | `6162789c3f89a72bfc0f13d93c68a443` |

`dash_api.py` here **supersedes** both `cd7eecf8…` (occupancy) and `5cfae4bd…` (health banner): it is
those plus the render-audit fixes. Install this one and every other note's dash step is satisfied.

---

## 1. Install

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for f in health_check.py dash_api.py apply_health.sh; do
  curl -fsSL "$B/$f" -o "/tmp/$f"
done
md5sum /tmp/health_check.py /tmp/dash_api.py /tmp/apply_health.sh
#   b46c6aa3af093667c0d3662413083db5  health_check.py
#   e897b861181817535ff97719906f5e38  dash_api.py
#   6162789c3f89a72bfc0f13d93c68a443  apply_health.sh

sudo bash /tmp/apply_health.sh
```

The script **runs the check in the foreground before installing the timer** and aborts if it
crashes. A monitor that is installed but broken is worse than none — its silence reads as health.

It treats **exit 1 as a finding, not a failure**: exit 1 means a camera is currently silent, which is
the condition the monitor exists to report, and blocking the install on it would be backwards. The
systemd unit carries `SuccessExitStatus=0 1` for the same reason — otherwise every breach would also
show as a failed unit and bury the real signal.

## 2. What runs afterwards

```
liftlab-health.timer     every 15 min (override: sudo EVERY=5min bash /tmp/apply_health.sh)
liftlab-health.service   oneshot, Nice=10, idle I/O
```

It sends on a **breach edge**, on **recovery**, and **once a day past 08:30 IST**. A camera down for
three days produces one message plus one line each morning — not 288.

```bash
systemctl list-timers liftlab-health --no-pager
journalctl -u liftlab-health -n 20 --no-pager
curl -fsS -u "$OPERATOR" https://lift.gargi.online/dash/site-A/health
```

The line reads either

```
LiftLab health 08:30: all 7 cameras posted within the hour — OK
LiftLab health 09:15 BREACH: ch29 silent since 06:12 (segments flowing = worker stall class)
```

The parenthesised class is the useful half. Three states share one symptom — no transits — and they
send you to different boxes:

* `segments flowing = worker stall class` — video is being processed and nothing is being counted;
* `segments frozen too = upstream/relay class` — the worker is fine and there is no video;
* `worker not running` — the heartbeat is stale; it is not a counting problem at all.

Outside 07:00–23:00 IST a silent camera is **reported, not alarmed**, and the OK line says so.

## 3. DELIVERY — read this before assuming you will be told

**There is no push channel in this deployment.** Nothing in the repo or in any deploy script carries
an SMTP credential, a webhook URL, or a token of any kind, so out of the box this line exists in
exactly three places, all of them pull:

* the **dashboard banner**, above every panel on `/dash`;
* `GET /dash/{gw}/health`, plain text;
* `journalctl -u liftlab-health`.

The banner and the endpoint both **say so**, in the message itself, so "no alert arrived" can never
be read as "all is well".

### To make it push — one environment variable, no new dependency

The cheapest real channel is an HTTP webhook; `health_check.py` posts to it with stdlib `urllib`, no
library to install. Any of these gives you a URL:

| option | what you need | cost |
|---|---|---|
| **ntfy.sh** | pick a hard-to-guess topic, install the phone app, subscribe | free |
| **Google Chat** space | a webhook URL from the space (works with the existing Workspace account) | free |
| **Slack** incoming webhook | a workspace | free |

```bash
sudo systemctl edit liftlab-cloud     # or edit the env file the unit already reads
# [Service]
# Environment=HEALTH_WEBHOOK_URL=https://ntfy.sh/<your-secret-topic>
#   Google Chat / Slack want JSON, so also set:
# Environment=HEALTH_WEBHOOK_FIELD=text
sudo systemctl daemon-reload && sudo systemctl restart liftlab-cloud
sudo bash /tmp/apply_health.sh        # re-run: it copies HEALTH_* into the timer unit
```

SMTP works too if a relay is available — `HEALTH_SMTP_HOST`, `HEALTH_SMTP_TO`, optionally
`HEALTH_SMTP_USER`/`HEALTH_SMTP_PASS`/`HEALTH_SMTP_PORT`. I cannot verify from here that this VM can
reach a mail relay, so I have not claimed it works; the webhook path needs nothing but outbound HTTPS,
which the box demonstrably has.

**Verify delivery once, deliberately** — a channel that has never carried a message is not a channel:

```bash
sudo -u liftlab GATEWAY_DB=/var/lib/liftlab/gateway.db \
  /opt/liftlab-b3/cloud/.venv/bin/python /opt/liftlab-b3/cloud/health_check.py site-A --force
# then check your phone, and:
sudo -u liftlab sqlite3 /var/lib/liftlab/gateway.db \
  "SELECT datetime(ts,'unixepoch','+5 hours','+30 minutes'), sent, delivered, delivery_error
     FROM health_status ORDER BY ts DESC LIMIT 3;"
```

## 4. Thresholds, and where each number came from

| signal | threshold | env | basis |
|---|---|---|---|
| no transit, **active hours only** | 60 min, 07:00–23:00 IST | `HEALTH_TRANSIT_STALE_S`, `HEALTH_ACTIVE_FROM/TO` | as specified |
| heartbeat stale | 15 min | `HEALTH_HB_STALE_S` | long enough to survive a worker restart (~30 s) without flapping |
| segments frozen | any check ≥2 min apart with no change | — | the counter advances every segment; frozen + fresh heartbeat = no video reaching a live worker |
| Pi telemetry stale | 10 min | `HEALTH_PI_STALE_S` | `relay_status` posts every ~36 s (measured: 720 rows over 7.2 h), so 10 min is ~16 missed posts |
| litestream | unit not active | `HEALTH_LITESTREAM_UNIT` | a unit that is *absent* reports UNKNOWN, never a breach |

**Pi telemetry is `relay_status`, deliberately not `watch_status`.** The Pi door-watch was retired
2026-07-21 and its table has been frozen ever since; keying on it would breach permanently and for
the wrong reason.

### The measured alert rate of the 60-minute rule

Replayed over the restored snapshot (16.5 days, 14,432 active-hour gaps), the specified rule fires
**60 times — about 3.6 alerts/day fleet-wide.** Most are real: the top twelve are 3.9 h–53 h and
line up with the declared outages (the Jul 19–20 relay stall, the Jul 31 fleet outage where five
cameras breach within ten minutes of each other). The remainder are 1–4 h quiet spells on lifts that
are genuinely quiet at that hour.

Two things already blunt this: breaches are **edge-triggered**, so a standing fault sends once and
is then restated only in the daily line; and one fleet outage arrives as one message naming several
cameras.

If it still reads as noisy after a week, the measured refinement is to suppress a breach when the
camera's own median for that hour-of-day is ~0 — that halves it to **1.6/day** and every survivor in
the replay is a genuine outage. It is deliberately **not** shipped: it is a second threshold to
reason about, and the rule as specified is the one to judge from real weeks rather than from a
replay of a period containing two multi-day outages.

## 5. What this cannot see

If the VM itself is down, no line is sent and no banner is served. That is why the healthy line goes
out daily: **the absence of the 08:30 message is itself the signal.** Nothing inside this box can
close that gap; only something outside it can.

## 6. Rollback

```bash
sudo systemctl disable --now liftlab-health.timer
sudo rm -f /etc/systemd/system/liftlab-health.{timer,service}
sudo systemctl daemon-reload
# the dashboard banner degrades to nothing on its own if health_status stops updating,
# but to remove it entirely restore the previous module:
sudo install -o liftlab -g liftlab -m 644 /opt/liftlab-b3/cloud/dash_api.py.bak.<stamp> \
     /opt/liftlab-b3/cloud/dash_api.py
sudo systemctl restart liftlab-cloud
```

`health_status` can stay — it is additive, nothing else reads it, and it is the only record of when
the fleet was last known good.
