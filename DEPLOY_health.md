# Daily health line — deploy, VM only (2026-08-12)

One script, one timer, one dashboard banner. Nothing on the GPU box, nothing on the Pi, no registry
change. The check is read-only over every table it judges and writes only its own `health_status`.

**md5s as published on `pi-scripts` at `d91c7bf`** — verified by fetching them back from raw, not by
hashing the working tree.

| box | path | md5 |
|---|---|---|
| VM | `health_check.py` | `959172c42ae923fa47dd7a99df5ba70b` |
| VM | `dash_api.py` | `5cfae4bd1536855c542a248f9bc4e6ce` |
| VM | `apply_health.sh` | `62ae4cdc6ee2de70e29ce6f7cc7cfddc` |

`dash_api.py` here **supersedes** the `cd7eecf8…` in `DEPLOY_occupancy.md`: it is that file plus the
health banner and `/dash/{gw}/health`. Install this one and the occupancy deploy's dash step is also
satisfied.

---

## 1. Install

```bash
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for f in health_check.py dash_api.py apply_health.sh; do
  curl -fsSL "$B/$f" -o "/tmp/$f"
done
md5sum /tmp/health_check.py /tmp/dash_api.py /tmp/apply_health.sh
#   959172c42ae923fa47dd7a99df5ba70b  health_check.py
#   5cfae4bd1536855c542a248f9bc4e6ce  dash_api.py
#   62ae4cdc6ee2de70e29ce6f7cc7cfddc  apply_health.sh

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
liftlab-health.timer     every 10 min (override: sudo EVERY=5min bash /tmp/apply_health.sh)
liftlab-health.service   oneshot, Nice=10, idle I/O
```

It sends on a **breach edge**, on **recovery**, and **once a day past 08:30 IST**. A camera down for
three days produces one message plus one line each morning — not 432.

```bash
systemctl list-timers liftlab-health --no-pager
journalctl -u liftlab-health -n 20 --no-pager
curl -fsS -u "$OPERATOR" https://lift.gargi.online/dash/site-A/health
```

The line reads either

```
all 7 cameras posting: YES — ch16 12m, ch27 4m, ... since last transit
```

or

```
all 7 cameras posting: NO — ch29 worker silent 41m (since 04:12) [breach first seen 04:27]
```

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
| heartbeat stale | 15 min | `HEALTH_HB_STALE_S` | long enough to survive a worker restart (~30 s) without flapping |
| segments frozen | any check ≥2 min apart with no change | — | the counter advances every segment; frozen + fresh heartbeat = no video reaching a live worker |
| no transit | 6 h | `HEALTH_TRANSIT_STALE_S` | 14 of 16,753 historical gaps exceed it (0.08%), and those coincide with the declared outages |

**An hourly transit check was specified and is not what shipped.** Measured on the restored snapshot,
51-55% of daytime hours on days a camera was demonstrably live contain zero transits, and ch29's
busiest day ever still held three empty hours. That alarm would have fired on a healthy camera more
than half the time. Transit age is still *reported* for every camera on every line — it is simply not
the trigger. If you want the stricter rule anyway, `HEALTH_TRANSIT_STALE_S=3600` restores it without
a code change.

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
