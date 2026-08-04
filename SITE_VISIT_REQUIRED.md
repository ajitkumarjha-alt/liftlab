# Site visit required — four jobs that cannot be done remotely

**For the facilities team. No software change can fix any of these; they all need someone at the
building — three inside the lift cars, one in the comms room.**

We have cameras watching seven lifts. They measure how long the doors take to close, how many people
get on and off, and which floor the car stopped at. Four separate problems are currently limiting
what we can report, and all four need physical work on site.

**One of them (Job 4) needs a part ordered before the visit** — a USB ethernet adapter, about the
price of a phone charger.

Rough total: **half a day**, plus about a week of waiting afterwards for fresh data to accumulate.

Some shorthand used below:

* **"ch16", "ch27"…** — the camera channel numbers on the recorder. In building terms:
  ch16 = lift 1, ch27 = lift 2, ch29 = lift 3, ch30 = lift 4, ch32 = lift 5, ch34 = lift 6,
  ch37 = lift 7.
* **"floor indicator"** — the little illuminated display inside the car showing the floor number and
  an up/down arrow. Our cameras read it the way a person would.
* **"ROI"** — the small rectangle of the camera image we look at to read that display. If the
  display moves out of that rectangle, or the camera is nudged, we stop being able to read it.
* **"calibration"** — showing the system some example pictures of the display and telling it what
  each one says, so it learns to read that particular indicator.
* **"close travel"** — how long the doors take to close. This is the study's headline measurement.

---

## Job 1 — Lift 1 (ch16): the camera can no longer see the floor indicator

**What is wrong.** On **25 July 2026, between 06:00 and 12:00 UTC** (last good reading 08:12 UTC),
this camera stopped being able to read the floor indicator, and has not read it since. Before that it
was reading the floor successfully about 46 % of the time — roughly 19,400 successful reads. Since
then it has managed **one**.

> **DATE CORRECTED 4 August.** An earlier version of this note said 29 July at 04:15. That was our
> mistake — we had misread a software-version boundary as the moment of failure. **The camera stopped
> working on 25 July, four days earlier.** If you are checking maintenance logs, cleaning schedules or
> access records, please look at **25 July morning**, not 29 July. Nothing happened to this camera on
> 29 July; that date was one of our own software updates, fleet-wide.

Nothing in our software changed at that moment. We checked: the settings, the reading templates and
the region we look at were all identical either side of the break. The system also stopped even
seeing *partial* or *ambiguous* glimpses of the display, which is the signature of the display no
longer being in view at all — rather than being harder to read.

**Most likely cause:** the camera was knocked, re-aimed, refocused or otherwise moved at around
04:15 on 29 July, or something now blocks its view of the indicator. Cleaning, maintenance or a
service visit that morning would fit.

**What to do on site**

1. Go into lift 1's car with a phone or laptop that can see the camera's live picture.
2. Check whether the floor indicator is visible in the picture at all, and whether anything is
   obstructing, reflecting off, or glaring on it.
3. If the camera has moved, aim it back so the indicator is clearly in frame and in focus.
4. Tell us when you are done; we re-run the calibration remotely and confirm reads resume.
5. If the camera looks untouched and the indicator is plainly visible, say so — that changes the
   diagnosis and we investigate differently.
6. **While you are in front of this camera, also photograph the DOORS as the picture sees them** —
   one frame with the doors fully open, one mid-close, one fully shut. Send all three.

   *Why:* separately from the floor indicator, lift 1 produces roughly **50 % more short false
   "door moved" blips than the other lifts** (47 % of its door events, against 30 % on lifts 2 and 3)
   — within the same software version, so it is not a software difference. Something in this
   camera's view is being mistaken for door movement: a reflection, a mirror, a poster, passing
   people, or the doors sitting only partly in frame. Three photographs would tell us which, and we
   cannot get them remotely — the system only stores its *conclusions* about the doors, never the
   picture it drew them from.

**Time:** about 30 minutes (plus 5 minutes for the three door photographs). **Blocks:** all
floor-related data on lift 1 — which floors it serves, how long it travels between them, and where
people get on and off. Door-close *timing* on lift 1 remains valid; what the photographs would
unblock is why lift 1 registers so many false door blips, which currently costs us usable door
cycles on that lift.

---

## Job 2 — Lifts 2 and 4 (ch27, ch30): the direction arrow was taught with only half the examples

**What is wrong.** To read the up/down arrow, the system was shown a set of example pictures and
told what each showed. For these two lifts, the examples only ever covered **one** direction:

* **Lift 2 (ch27)** — every labelled example showed a **down** arrow. None showed up.
* **Lift 4 (ch30)** — every labelled example showed an **up** arrow. None showed down. Worse, 51 of
  the 61 examples were taken while the car sat on **floor 6**, so it barely saw any floor numbers
  either.

The consequence is that these cameras could only ever report the one direction they were taught.
Lift 2 reported "going down" 100 % of the time and lift 4 reported "going up" 83 % of the time —
both obviously impossible, since a lift goes both ways.

**We have already stopped the system reporting a direction from these two cameras**, because a
camera that can only say one thing is not measuring anything. That is a software change already
made; it does not need you. What needs you is producing a proper set of examples.

**What to do on site**

1. Ride each of lift 2 and lift 4 for roughly **10–15 minutes each**.
2. Travel the **full height of the building**, both up and down, several times — not just between
   two adjacent floors.
3. Stop at as many different floors as you reasonably can, including the top and bottom.
4. That is all. The system is recording the whole time; we collect the pictures afterwards and do
   the labelling remotely.

The single most important thing is **variety** — many different floors, and genuinely both
directions. A long ride between two floors is far less useful than many short ones across the whole
building.

**Time:** about 15 minutes per lift, 30 minutes total. Best done outside peak hours so you are not
competing with passengers. **Blocks:** the up/down split for these two lifts. This feeds the
"probable stops per trip" figures the design review asks for.

> Worth noting: this same thin sample is also why these two lifts read the floor number correctly
> only 44 % and 28 % of the time, against 84 % on lift 3, which was calibrated properly. Riding the
> full building fixes both problems at once.

---

## Job 3 — Lifts 5, 6 and 7 (ch32, ch34, ch37): never set up to read doors at all

**What is wrong.** These three lifts have cameras and are counting passengers correctly, but they
have **never been calibrated for door reading**. They produce no door data whatsoever — not poor
data, none. Three of the seven lifts are simply absent from the door-timing study.

**What to do on site**

1. Confirm each of these three cars actually has a working camera with a clear view of the doorway
   and the floor indicator. (This is the step that might turn up a surprise — if any of these
   cameras are misaimed or dead, that is a different job.)
2. Ride each car for **10–15 minutes**, as in Job 2: full height of the building, both directions,
   stopping at as many floors as possible.
3. Let us know when done; the rest of the setup is remote.

**Time:** about 15 minutes per lift, 45 minutes total, plus a few minutes each to check the camera
views first. **Blocks:** door-close timing on three of seven lifts. Right now the headline finding
describes four lifts and is being read as if it describes the building.

---

## Job 4 — USB ethernet adapter for the Raspberry Pi — **REQUIRED, not optional**

**This one needs a part ordering, not just a visit. It has been escalated from "worth doing" to
"required" since this note was first written — see why below.**

**What is wrong.** The small computer in the comms room (a Raspberry Pi) sends all the video to the
cloud through its built-in network port. **Twice in the last four days that built-in port has
silently stopped sending.** It does not report an error. It does not drop the network link. The
computer still looks completely healthy from every angle — the only way to tell is to notice that
the counter of packets sent has stopped moving.

Both times, the only thing that fixed it was a physical reboot:

* **1 August** — 33 hours of video lost before anyone noticed.
* **4 August** — 2 hours lost.

We have just rebuilt the automatic recovery so the system reloads the network driver and, failing
that, reboots itself — so a repeat should cost minutes rather than hours. But that is damage
control, not a cure. The port itself is the problem.

**What to buy.** A **USB 3.0 gigabit ethernet adapter** — a small dongle, widely available, roughly
the price of a phone charger. Any reputable brand is fine; there is nothing special about the
requirement.

**What to do on site.** Plug it into a blue (USB 3) port on the Pi and move the network cable from
the built-in port to the dongle. We do the configuration remotely. **About 10 minutes**, and it can
be done during the same visit as the other jobs.

**Why this is now required rather than a nice-to-have.**

We built automatic recovery in three steps: (1) restart the video senders, (2) reset the network
driver, (3) reboot the whole machine as a last resort. Step 2 is the gentle one — it fixes the
fault in about twenty seconds without disturbing anything else.

**On this Pi, step 2 turns out to be impossible.** The built-in network driver is welded into the
system software rather than being a separate loadable piece, so it cannot be reset on its own.
We confirmed this on 4 August. That leaves nothing between "restart the video senders" — which does
not fix this fault — and "reboot the entire machine".

So today, every occurrence of this fault costs a full reboot. We have made that reboot automatic and
put safety limits on it, but rebooting a machine in a comms room, over the network it has just
stopped using, is not something anyone should be relying on as routine recovery.

**A USB adapter fixes that directly.** Its driver *is* a separate loadable piece, so plugging one in
restores the gentle middle step. Recovery becomes a twenty-second driver reset instead of a full
reboot — and the failing built-in port is bypassed entirely, so the fault should stop happening at
all.

**Blocks:** it does not block a measurement, but it is the difference between "this recurs every few
days and each time costs a reboot" and "this stops happening". Given two occurrences in four days,
and that reboot is now our only automatic remedy, **please order the part before booking the
visit.**

## Suggested order and combined visit

All of this can be done in one visit:

| Order | Job | Lifts | Time |
|---|---|---|---|
| 1 | Check camera view, re-aim if moved | lift 1 | 30 min |
| 2 | Check the three cameras have a clear view | lifts 5, 6, 7 | 15 min |
| 3 | Ride full height, both directions, many floors | lifts 2, 4, 5, 6, 7 | 75 min |
| 4 | Fit the USB ethernet adapter on the Pi (**bring the part — required**) | comms room | 10 min |

**Total on site: roughly 2 hours 10 minutes.** Then about a week of normal running for enough fresh data to
accumulate before the numbers are usable.

**Job 4 needs a part ordered in advance and is now REQUIRED** — without the adapter in hand that job
cannot happen, so order it before booking the visit. It is the only one of the four that changes how
the system recovers from a fault rather than what it can measure.

If time is short, **Job 3 is the highest value** — it takes three lifts from no data at all to
usable data, which changes what the study can say about the building as a whole.

## What we need back from you

* Confirmation of what you found on lift 1 — had the camera moved, or was the view already clear?
* Rough times when you rode each lift, so we can find the right recordings.
* Anything that stopped you completing a job, and which lift it was.
* Whether the USB adapter was fitted, and which port it went into.

Nothing needs to be labelled, configured or written down beyond that. The rest is ours.
