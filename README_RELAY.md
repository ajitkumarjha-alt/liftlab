# liftlab-relay — the Pi's only job, and the two defects that cost 33 hours

The Pi is a dumb streamer: keep one ffmpeg per lift cabin up, PUT their HLS segments to the VM,
log a CSV row every 30 s, restart anything that dies or stops delivering. All analysis happens on
the GPU box against the same feed.

## What went wrong on 2026-08-01

Two defects, one root cause: **"no measurable segment age yet" was treated as failure rather than
as unknown.**

1. **A 33-hour outage went completely unnoticed.** A soft `bcmgenet` NIC TX wedge stopped all
   delivery. Every ffmpeg stayed alive, so every per-stream check passed and the heartbeat
   reported healthy — `streams_alive: 7` — while `sum_delivered_mbps` was `0.00` on all seven
   streams. Nothing watched the *sum*.
2. **On recovery the supervisor entered a restart loop**, killing every stream roughly every
   170 s for 20 minutes. First-segment latency exceeded the 120 s grace, so streams were killed
   just before they established, restarted, and killed again.

A third defect made the first one worse: on the reboot, the channel-map fetch returned nothing and
the relay **silently fell back to a hardcoded channel list** — `27 28 29 30 32 33 34`, which
contains `ch28` and `ch33` (which do not exist) and omits `ch16` and `ch37` (which do). The Pi
streamed the wrong seven cameras for 17 minutes. The fetch is intermittent, so it fired again on a
later restart.

Per-channel coverage in the last export was 17 %–57 %. **Coverage is the binding constraint on the
whole study**, which is why these are relay defects worth this much attention.

## The measurements behind the new defaults

Taken from 720 `relay_status` rows (2026-08-02 04:45–11:55 IST), 29 fleet recovery events:

| statistic | value |
|---|---|
| p50 recovery latency | 160 s |
| p90 | 350 s |
| p95 | 376 s |
| max | 972 s |

**The old 120 s grace sat below the median.** More than half of all recoveries were killed before
the stream could establish. That is the restart loop, quantified.

Note what this data can and cannot say: in all 720 samples the seven cameras **never once
disagreed** about whether they were delivering. The zero-runs are therefore fleet-wide relay
restarts, not per-camera establishment times — the existing telemetry cannot give per-camera
first-segment latency. The relay now measures it per stream on every successful start and reports
the rolling estimate as `first_segment_est_s` in the heartbeat, so the next tuning round has real
per-camera numbers instead of a fleet proxy.

## What the relay does now

**Channels are never guessed.** There is no builtin list. On fetch failure the relay retries with
backoff (5, 10, 20, 40, 60 s cap) indefinitely and launches nothing until it has a list from
`channel_map` or an explicit `CHANNELS=` override. A relay streaming the wrong cameras is worse
than one not streaming: the second failure is visible, the first produces plausible data attributed
to the wrong lifts. The list is re-resolved every `RELAY_CHAN_REFRESH_S` (default 300 s) so a
registry change is picked up without a restart, and a failed refresh keeps the current list.

**Three stream states**, because the missing one is what killed us:

| state | meaning | restartable |
|---|---|---|
| `STARTING` | launched, nothing delivered yet, inside grace | **never** |
| `DELIVERING` | delivered-bytes counter advancing | **never** |
| `STALLED` | alive, bytes not advancing, past grace | yes, via strikes |

**Delivered bytes are ground truth.** A stream whose byte counter is advancing is never restarted,
whatever its segment age says. Segment age is a lagging observation read from a VM in Mumbai, and
reporting lag alone can exceed any sane stall threshold on a healthy stream — the logs show streams
killed as "never delivered" while their own counter read 1,273,954 kbps. Age is now a log signal
only.

**Local death is distinguished from remote staleness.** A process that has exited is restarted
immediately with no grace; a stale-but-alive stream goes through the strike logic.

**Grace comes from measurement.** `grace = max(RELAY_GRACE_FLOOR_S, 3 × rolling first-segment
latency)`, capped at `RELAY_GRACE_CAP_S`. The estimate jumps up immediately on a slow start and
decays down slowly, because under-estimating grace caused the outage while over-estimating only
delays a restart.

**A fleet delivery watchdog**, independent of all per-stream logic. If total delivery across all
streams stays below `RELAY_FLEET_MIN_KBPS` for `RELAY_FLEET_DOWN_S` (default 5 min), that is
fleet-down regardless of what any stream reports. Escalation runs in order, one stage per pass:

1. restart all ffmpeg;
2. bounce the interface (`ip link set <iface> down; … up`) and re-test — the 33-hour outage was a
   soft TX wedge that a reboot cleared, and a bounce may clear it without one;
3. log `ERROR` once a minute so it is unmissable in the journal.

**The heartbeat now distinguishes "alive" from "delivering".** It could not, which is the entire
reason 33 hours passed. New fields: `fleet_down` (explicit boolean), `fleet_zero_for_s`,
`fleet_stage`, `first_segment_est_s`, `grace_s`, `channel_source`, and `stream_states` per camera.

Every state change logs a transition line, so the next incident is diagnosable from the journal
alone.

### The escalation ladder (rewritten 2026-08-04)

```
stage 1  restart all seven ffmpeg
stage 2  reload the NIC driver  <-- UNAVAILABLE ON THIS PI, see below
stage 3  reboot — gated: fleet down >= 15 min AND no reboot attempt in the last hour
```

### ⚠ On this Pi the ladder is really: restart ffmpeg, then reboot

**`bcmgenet` is compiled into the kernel, not built as a module.** `lsmod` does not list it and
`modprobe -r bcmgenet` answers `Module bcmgenet not found`. Verified 2026-08-04. **Stage 2 is a
permanent no-op on this hardware — there is no automatic step between "restart ffmpeg" and "reboot
the machine".**

This is detected at startup with `modinfo`, not assumed, because it is a property of the kernel
build and a **USB ethernet adapter's driver IS loadable** — fitting one restores the rung. The
relay logs the ladder's real shape once at startup rather than leaving it to be inferred from a
`modprobe` failing every six minutes:

```
FLEET ladder: 1) restart all ffmpeg  2) UNAVAILABLE (bcmgenet is built into the kernel,
              not a module — cannot be reloaded)  3) reboot (gated: ...)
  There is NO automatic step between restarting ffmpeg and rebooting on this hardware.
  A USB ethernet adapter would restore stage 2 — its driver IS loadable.
```

When stage 2 is unavailable the ladder does not spend a six-minute pass proving it again: it moves
to the reboot gate immediately. The gate's own timing conditions still apply, so this cannot
shortcut the 15-minute requirement.

### The reboot gate is now the only automatic recovery — how it is guarded

Four conditions, all verified by behavioural tests in `test_relay_soak.sh`:

| Condition | Behaviour |
|---|---|
| Fleet down < 15 min | **Refuse.** Give stage 1 time to work. |
| Down >= 15 min, never rebooted | **Open.** |
| Down >= 15 min, rebooted < 1 h ago | **Refuse.** This is the loop guard. |
| Down >= 15 min, rebooted > 1 h ago | **Open.** |

Two properties the gate depends on, both tested:

* **The reboot timestamp is written to disk *before* `reboot` is issued**, followed by `sync`. Write
  it afterwards and it is lost in the very reboot it is meant to limit, and every boot looks like
  the first.
* **"Down for" is computed from the *persisted* `FLEET_ZERO_SINCE`**, not from process start.
  Without that, the restart loop that caused the original defect would keep the outage looking
  freshly started and the 15-minute gate would never open either.

**And if the state cannot survive a reboot, the relay refuses to reboot at all.** The state
directory's filesystem is checked with `stat -f`; on `tmpfs` or `ramfs` the cooldown would evaporate
during the reboot and this would become a boot loop on a machine reachable only over the network it
just took down. Better to wait for a human than to power-cycle forever. Startup logs which it is:

```
FLEET escalation state: /var/lib/liftlab-relay/fleet_escalation (fs=ext4, survives reboot=yes)
```

`/var/lib/liftlab-relay` must exist and be writable by `askjitk` — the service cannot create it. If
it is missing the script falls back to `$HOME/.liftlab-relay`, and only then to `/tmp`, which trips
the guard above and disables reboot recovery.

**Two defects fixed, both of which cost real hours.**

**1. The ladder could never reach stage 2.** On 2026-08-04 the watchdog fired correctly every ~6
minutes for two hours and ran stage 1/3 *every time*. Stage 1 does not restart the supervisor — it
restarts the ffmpeg children — but its seven kill+launch cycles over a wedged NIC push one loop
iteration past `RELAY_LOOP_STALL_S` (120 s), so the **supervisor self-watchdog** declares the loop
wedged and SIGKILLs it. systemd restarts it 15 s later with `FLEET_STAGE=0` and the ladder begins
again at the bottom. Supervisor PIDs walked 1018884 → 1020675 → 1022463 → 1024258 → 1026086.

The stage now lives on disk (`$RELAY_STATE_DIR/fleet_escalation`, default
`/var/lib/liftlab-relay/`) and is re-read at startup, so escalation resumes wherever it got to
regardless of *why* the supervisor died. `FLEET_ZERO_SINCE` is persisted with it, because the
reboot gate needs the true outage length — a restart loop would otherwise keep the outage looking
freshly started and the gate would never open either. State older than
`RELAY_FLEET_STATE_TTL` (30 min) is discarded so a stale stage-3 cannot send a later, unrelated
blip straight to a reboot.

**2. Stage 2 was the wrong action — and on this hardware there is no right one.**
`ip link set eth0 down` returns `RTNETLINK answers: Connection timed out` on a wedged bcmgenet PHY
— verified 2026-08-01 and 2026-08-04. The command cannot reach the hardware, so it cannot fix the
hardware. The bounce is still attempted (cheap, harmless) but no longer *gates* anything.

A driver reload **would** re-initialise the controller, and that is what stage 2 does — except that
`bcmgenet` is built into this kernel and cannot be unloaded. So on this Pi the rung does not exist,
and the honest ladder is stage 1 then a gated reboot. That is a hardware limitation, not a software
one, and the fix is the USB adapter in `SITE_VISIT_REQUIRED.md`.

### There is NO software signal for this fault except tx_packets

**`dmesg` shows nothing at all from the wedge** — no error, no timeout, no reset, nothing but
ordinary post-boot lines. The driver does not know it has failed, so nothing logs it and nothing
traps. Every recovery step is therefore verified by reading
`/sys/class/net/<iface>/statistics/tx_packets` and checking it *advances*. Do not go looking for a
kernel message to trigger on; there isn't one, and waiting for one is how 33 hours passed.

`tx_packets` and `last_reboot_epoch` are now published in the heartbeat, so the ladder's position
and the NIC's real state are visible from the cloud.

### Why the reboot is gated

A wedge that only a reboot clears must not need a human to notice it: on 2026-08-04 that cost 2
hours and on 2026-08-01 it cost 33. But an ungated reboot on a *persistent* fault is a boot loop
that destroys more data than the wedge. Both conditions must hold, and the reboot timestamp is
written to disk **before** the reboot is issued — otherwise the cooldown is lost across the very
reboot it is meant to limit.

Knobs: `RELAY_FLEET_REBOOT=0` disables stage 3, `RELAY_FLEET_REBOOT_MIN_DOWN_S` (900),
`RELAY_FLEET_REBOOT_COOLDOWN_S` (3600), `RELAY_NIC_MODULE` (bcmgenet).

### Stages 2 and 3 need sudo

The service runs as `askjitk`; `modprobe` and `reboot` need root. Without a sudoers entry each
stage logs an ERROR naming the missing permission and the wedge still needs a manual reboot — which
is exactly the failure this ladder exists to remove. Add:

```
askjitk ALL=(root) NOPASSWD: /usr/sbin/modprobe, /sbin/reboot
askjitk ALL=(root) NOPASSWD: /usr/sbin/ip link set * up, /usr/sbin/ip link set * down
```

## Remove the temporary drop-ins once this lands

Two systemd drop-ins under `/etc/systemd/system/liftlab-relay.service.d/` were added by hand to
work around these defects. **Both are now redundant and should be deleted**, followed by
`systemctl daemon-reload && systemctl restart liftlab-relay`.

| drop-in | what it pinned | what it was covering | why it can go |
|---|---|---|---|
| `grace.conf` | `RELAY_UNKNOWN_GRACE_S=420`, `RELAY_SEG_STALL_S=180`, `RELAY_STALL_STRIKES=3` | Defect 2 — the 120 s grace killing streams mid-establishment, and a 60 s stall threshold shorter than VM reporting lag | The unit now ships `RELAY_GRACE_FLOOR_S=420` and `RELAY_STALL_STRIKES=3` directly, grace is derived from measured latency on top of that floor, and segment age no longer triggers restarts at all. `RELAY_UNKNOWN_GRACE_S` is still read as a fallback alias for the floor, so leaving the drop-in in place is harmless — but it pins a value the relay would otherwise raise on its own when a camera is genuinely slow. |
| `channels.conf` | `CHANNELS=16 27 29 30 32 34 37` | Defect 1 — the builtin fallback list streaming the wrong seven cameras | The builtin list is gone. The relay now blocks until `channel_map` answers, and re-resolves periodically. Keeping this drop-in **pins the channel list and disables the periodic refresh**, so a registry change will be silently ignored. |

Neither is dangerous to leave, but both freeze behaviour the relay is now able to work out for
itself — `channels.conf` in particular will mask a registry change.

## Deploy

Deploy is `curl` from GitHub, so committed files only:

```
B=https://raw.githubusercontent.com/ajitkumarjha-alt/liftlab/pi-scripts
for f in apply_relay.sh relay_soak.sh liftlab-relay.service; do curl -fsSL -o /tmp/$f $B/$f; done
sudo bash /tmp/apply_relay.sh
```

Gates, in order — nothing mutates until all of them pass:

* `bash -n` on the script;
* **md5 of every artifact printed**, and enforced when `RELAY_MD5` / `UNIT_MD5` are supplied — a
  truncated download or a stale `/tmp` copy otherwise looks exactly like a successful deploy;
* a refusal if the fetched copy still contains the builtin channel fallback;
* an ffmpeg **CLI parse gate** (`--selftest`) that builds the real command from the same
  `ffmpeg_args` the launcher uses, so the gate and the launcher cannot drift;
* after restart, a **PID-must-change gate** — `systemctl restart` returning 0 does not prove the
  service restarted — with a **polled** health check rather than a fixed sleep;
* proof that the supervisor watchdog armed, and that ffmpeg processes are actually alive
  (`is-active` is not evidence).

The unit sets `TimeoutStopSec=5` with `KillMode=mixed` so a wedged child cannot hold the restart
open long enough for the PID gate to read a stale `MainPID`.

## Tests

```
bash test_relay_soak.sh
```

Runs the real decision logic out of `relay_soak.sh` rather than reimplementing it. Covers: an empty
channel map retries forever and launches nothing; grace derived from measured latency and not
collapsed by one fast start; every state-machine transition including the exact 2026-08-01
regression (170 s alive, killed under 120 s grace, safe under the new floor); fleet-down escalation
order and reset; and every deploy gate above.

## Still open

`RELAY_LOOP_STALL_S` (the supervisor's own watchdog) is unchanged at 120 s, deliberately — it
watches whether the supervisor loop is turning, which is a different question from whether a stream
is establishing.
