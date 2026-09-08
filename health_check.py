#!/usr/bin/env python3
"""DAILY HEALTH LINE — one sentence a human reads, gateway-side, off the request path.

WHAT IT IS FOR: a silent gap should be found by the system, not by someone asking. The whole feature
is one line — "all 7 cameras posted within the last hour: YES", or "NO — ch29 silent since 04:12".

WHY THE ALARM IS NOT "A TRANSIT IN THE LAST HOUR". That was the obvious rule and it is measurably
wrong. Over the 16,753 consecutive transit gaps in the restored gateway snapshot (2026-07-16 →
08-02, seven cameras):

    * 51-55% of DAYTIME hours (06:00-23:59 IST) on days a camera was demonstrably live contain
      ZERO transits. Per camera: ch27 54.6%, ch16 54.4%, ch34 54.2%, ch29 53.7%, ch37 51.4%,
      ch32 51.1%, ch30 51.1%.
    * On ch29's single busiest day ever recorded (2026-07-17, 1,310 transits) THREE hours held
      none: 03:00, 17:00, 18:00.

An hourly transit alarm would therefore have fired on a healthy camera more than half of all live
hours, and three times on the best day this fleet has ever had. A monitor that cries wolf gets
muted, and a muted monitor is worse than none — it converts an unknown into a false assurance.

WHAT THE ALARM IS INSTEAD. Four signals, each covering a failure the others cannot see:

  1. HEARTBEAT AGE (analyzer_status.ts). The worker posts this every segment, whether or not anyone
     walks through the door. Stale => that camera's worker is not running. Catches GPU stalls and a
     dead fleet supervisor.
  2. SEGMENTS NOT ADVANCING (analyzer_status.segments, monotonic per worker run). Heartbeat fresh
     but the counter frozen => the worker is alive and processing nothing. This is what a WEDGED PI
     or a stalled relay looks like from the gateway: the GPU is fine and there is no video.
  3. NO TRANSIT IN 6 HOURS. Kept, but at a threshold derived from the same data rather than
     assumed: only 14 of 16,753 gaps exceed 6h (0.08%), and those coincide with the declared
     outages. This is the signal that survives a worker which heartbeats happily while counting
     nobody.
  4. STARVED, NOT SILENT (2026-09-07). All three signals above ask "is anything ARRIVING", and on
     2026-09-02 PL2B started answering yes while counting 47-115 boardings/day against ~500
     before, with its door cycles at a normal 601/day. Nothing fired for five days and nothing
     was wrong by the rules above: fresh heartbeat, advancing segments, transits every few
     minutes. No absolute threshold can see this — demand varies by an order of magnitude across
     a week, which is the same measured reason an hourly transit alarm was rejected. The quantity
     that does not is the RATIO of what was counted to what the doors did: boardings per door
     open, compared only against THAT CAMERA'S OWN baseline. See STARVED, NOT SILENT below.
  5. READING WORSE, NOT MISSING (2026-09-08). Signal 4 watches the COUNTER; nothing watched the
     READER. On 2026-09-02 17:50 a camera-side image-profile change dropped every NCC score by
     ~0.06 fleet-wide: ch27 fell from 25,197 confident floor reads a day to 18, ch30 lost 75% of
     its reads, ch29 lost its entire ground floor. Doors, transits, heartbeats and segments were
     all normal throughout, so signals 1-4 were silent for five days and were right to be. What
     moved was the QUALITY of each read, which the gateway has recorded per row all along in
     `gw_door_event.read_conf` and never once looked at. See READING WORSE, NOT MISSING below.

Transit age is REPORTED for every camera whether or not it breaches, because that is the number
that was asked for and it is informative; it is simply not the trigger.

WHAT IT CANNOT SEE, said out loud. If this VM is down, no line is sent — so ABSENCE OF THE DAILY
LINE IS ITSELF A BREACH, and that is precisely why the daily line is sent on healthy days too. A
monitor that only speaks when something is wrong is indistinguishable, when it goes quiet, from a
monitor whose box has died.

DELIVERY. Configured by environment, never by code edit. If no channel is configured the line still
lands in the journal, in the state table, and on the dashboard banner — and it SAYS SO, on the
surfaces that do exist, so "no alert arrived" can never be read as "all is well".

Run:  python3 health_check.py [site-A] [--daily] [--json]
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db")

# ── Thresholds. Every one of these is derived; see the module docstring. ──────────────────────
HB_STALE_S = float(os.environ.get("HEALTH_HB_STALE_S", "900"))        # 15 min: survives a restart
TRANSIT_STALE_S = float(os.environ.get("HEALTH_TRANSIT_STALE_S", "3600"))     # 60 min, spec
# ACTIVE HOURS. Outside them a silent camera is REPORTED and does not alarm — a residential lift at
# 03:00 carries nobody, and a rule that fires then trains the reader to ignore the one that matters.
ACTIVE_FROM = int(os.environ.get("HEALTH_ACTIVE_FROM", "7"))          # 07:00 IST inclusive
ACTIVE_TO = int(os.environ.get("HEALTH_ACTIVE_TO", "23"))             # 23:00 IST exclusive
# Pi telemetry: relay_status posts every ~36 s (measured, 720 rows over 7.2 h in the restored
# snapshot), so 10 min is ~16 missed posts. Deliberately relay_status and NOT watch_status: the Pi
# door-watch was RETIRED 2026-07-21 and its table has been frozen ever since, so keying on it would
# breach permanently and for the wrong reason.
PI_STALE_S = float(os.environ.get("HEALTH_PI_STALE_S", "600"))
LITESTREAM_UNIT = os.environ.get("HEALTH_LITESTREAM_UNIT", "litestream")
# OUT-OF-SERVICE detection. A parked lift shows a static indicator and never opens its doors, so the
# discriminator is stability plus door silence — not the floor value, which this reader cannot be
# trusted to render (ch16: 20.4% of its floor strings are implausible by shape).
# HALF THE PRECOMPUTE TIMER INTERVAL (apply_gwprecompute.sh installs EVERY=1h). A sweep past this
# still fits, but it has used up half its headroom and the trend is the point: the fill cost tracks
# CURRENT-ERA rows, which grow with ingest and reset on door_version rollover, so it sawtooths
# upward rather than crossing a line once. Measured baseline: 8m10s on the live VM (13.6%).
# NOT A BREACH. Nothing is broken at 16 minutes — this is reported every time until it is addressed,
# the same rule the config gaps follow.
PRECOMPUTE_SLOW_S = float(os.environ.get("HEALTH_PRECOMPUTE_SLOW_S", "900"))   # 15 min
# The study bundle's own budget is 60s. Half of it is the warning line, on the same reasoning the
# precompute threshold uses: the number worth reporting is the one approaching the cliff, not the
# one that has already gone over it and been seen by a user.
BUNDLE_SLOW_S = float(os.environ.get("HEALTH_BUNDLE_SLOW_S", "30"))
OOS_MIN_S = float(os.environ.get("HEALTH_OOS_MIN_S", "3600"))       # silent at least this long
OOS_MIN_READS = int(os.environ.get("HEALTH_OOS_MIN_READS", "50"))   # enough reads to call it stable
OOS_STABLE_FRAC = float(os.environ.get("HEALTH_OOS_STABLE_FRAC", "0.9"))
OOS_MAX_OPENS_HR = float(os.environ.get("HEALTH_OOS_MAX_OPENS_HR", "1.0"))
# A camera is expected to be posting only if the registry says it is enabled. The denominator comes
# from the REGISTRY, never from "cameras that happen to have rows" — a camera that vanished entirely
# would otherwise drop out of the count and the line would cheerfully report all-of-nothing healthy.
# That is the same defect class as the gate that keyed on conditionally-populated evidence.

DAILY_HOUR_IST = int(os.environ.get("HEALTH_DAILY_HOUR", "8"))
DAILY_MIN_IST = int(os.environ.get("HEALTH_DAILY_MIN", "30"))

WEBHOOK_URL = os.environ.get("HEALTH_WEBHOOK_URL", "").strip()
WEBHOOK_FIELD = os.environ.get("HEALTH_WEBHOOK_FIELD", "").strip()   # "" = POST the raw text
SMTP_HOST = os.environ.get("HEALTH_SMTP_HOST", "").strip()
SMTP_TO = os.environ.get("HEALTH_SMTP_TO", "").strip()


def _db(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS health_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
        ok INTEGER, n_cams INTEGER, n_bad INTEGER, line TEXT, detail TEXT,
        sent INTEGER DEFAULT 0, delivered TEXT, delivery_error TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_health_gw_ts ON health_status(gateway_id, ts)")
    db.commit()
    return db


def _hhmm(epoch, now=None):
    """HH:MM for today, 'MMM-DD HH:MM' otherwise.

    A bare "since 11:54" for something that stopped ten days ago reads as this morning, which is the
    difference between a blip and an outage. The date appears exactly when it changes the meaning.
    """
    if not epoch:
        return "never"
    t = datetime.fromtimestamp(epoch, IST)
    ref = datetime.fromtimestamp(time.time() if now is None else now, IST)
    return t.strftime("%H:%M") if t.date() == ref.date() else t.strftime("%b-%d %H:%M")


def _age_phrase(sec):
    if sec is None:
        return "never"
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{sec / 60:.0f}m"
    return f"{sec / 3600:.1f}h"


def _has_col(db, table, col):
    try:
        return any(r[1] == col for r in db.execute(f"PRAGMA table_info({table})"))
    except sqlite3.OperationalError:
        return False                              # no such table either


def _one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return r[0] if r else None


def _active(now):
    """Is `now` inside the active hours the transit rule alarms in (IST)."""
    h = datetime.fromtimestamp(now, IST).hour
    return ACTIVE_FROM <= h < ACTIVE_TO


def _litestream():
    """(active?, note). None = could not determine, which is NOT the same as broken.

    Runs on the VM that litestream runs on, so `systemctl is-active` is the direct observation
    rather than an inference. A box without systemd, or a unit that is not installed, reports
    unknown — an unknown must never be rendered as a breach, or the line cries wolf on every
    development box.
    """
    import shutil
    import subprocess
    if not shutil.which("systemctl"):
        return None, "not checked (no systemctl on this host)"
    try:
        # LOADSTATE FIRST. `is-active` returns "inactive" for a unit that was never installed, which
        # is indistinguishable from one that died — and reporting a missing unit as a broken backup
        # makes the line cry wolf on every box that is not the gateway. A unit that does not exist
        # is UNKNOWN; only a loaded unit can be judged.
        ld = subprocess.run(["systemctl", "show", "-p", "LoadState", "--value", LITESTREAM_UNIT],
                            capture_output=True, text=True, timeout=10)
        load = (ld.stdout or "").strip()
        if load != "loaded":
            return None, f"not checked ({LITESTREAM_UNIT} unit is {load or 'absent'} on this host)"
        r = subprocess.run(["systemctl", "is-active", LITESTREAM_UNIT],
                           capture_output=True, text=True, timeout=10)
        state = (r.stdout or "").strip() or (r.stderr or "").strip()
    except Exception as e:
        return None, f"not checked ({type(e).__name__})"
    if state == "active":
        return True, "active"
    if state in ("inactive", "failed", "activating", "deactivating"):
        return False, f"unit is {state}"
    return None, f"not checked (unit {state or 'unknown'})"


def expected_cams(db, gw):
    """The cameras that SHOULD be posting, and where that list came from."""
    rows = db.execute("SELECT cam FROM camera_registry WHERE gateway_id=? AND enabled=1 "
                      "ORDER BY cam", (gw,)).fetchall()
    if rows:
        return [r["cam"] for r in rows], "camera_registry (enabled=1)"
    rows = db.execute("SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1 "
                      "ORDER BY channel", (gw,)).fetchall()
    return [f"ch{r['channel']}" for r in rows], "channel_map (is_lift=1) — registry was empty"


# ============================================================ STARVED, NOT SILENT
# THE FOURTH SIGNAL, and the gap that found it. On 2026-09-07 PL2B (ch30) had been counting 47-115
# boardings/day since the Sep 2 restart against ~500 before, while its door cycles ran at 601/day —
# normal. 0.11 boardings per door cycle against 0.67-1.0 on every other lift. The health line said
# nothing for five days, and it was right by its own rules: the camera was not silent. Its
# heartbeat was fresh, its segments advanced, and transits arrived every few minutes. Every one of
# the three signals in this file's docstring asks "is anything ARRIVING", and the answer was yes.
#
# What was wrong is that far too LITTLE was arriving, and no threshold on an absolute count can see
# that — a lift's demand varies by an order of magnitude between a Monday peak and a Sunday
# afternoon, which is the same reason an hourly transit alarm was rejected as crying wolf. The
# quantity that does not vary that way is the RATIO of what was counted to what the doors did:
# people board when the doors open, so boardings per door open is roughly stable for a given lift
# and is a property of the COUNTER, not of the traffic.
#
# EVERY CAMERA IS COMPARED ONLY WITH ITSELF. The fleet ratios span 0.67-1.0 in normal operation, so
# a fleet-wide floor would either miss a starved busy camera or convict a healthy quiet one. The
# baseline is the camera's own median hourly ratio over the preceding days.
#
# THIN HOURS ARE NOT JUDGED. An hour with a handful of door opens produces a ratio built on noise,
# and firing on it is exactly the wolf-crying the docstring above rejects. Below
# DEGRADED_MIN_OPENS the hour is skipped and says so in the payload.
#
# REPORTED, NOT A BREACH — the rule the config gaps follow. Nothing is DOWN: the lift is running,
# the camera is working, and the number is wrong. It is stated on the line every time until it is
# addressed, which is the difference between a warning and a warning nobody reads.
DEGRADED_LOOKBACK_H = float(os.environ.get("HEALTH_DEGRADED_LOOKBACK_H", "24"))
DEGRADED_BASELINE_D = float(os.environ.get("HEALTH_DEGRADED_BASELINE_D", "7"))
DEGRADED_FRAC = float(os.environ.get("HEALTH_DEGRADED_FRAC", "0.40"))
DEGRADED_MIN_HOURS = int(os.environ.get("HEALTH_DEGRADED_MIN_HOURS", "6"))
# An hour with fewer door opens than this is not judged. 10 is deliberately low: the point is to
# exclude hours whose ratio is arithmetic on two or three events, not to require a busy hour.
DEGRADED_MIN_OPENS = int(os.environ.get("HEALTH_DEGRADED_MIN_OPENS", "10"))
# A baseline built on a handful of hours is not a baseline. 24 active hours is roughly two days of
# a normal lift's active window.
DEGRADED_MIN_BASE_HOURS = int(os.environ.get("HEALTH_DEGRADED_MIN_BASE_HOURS", "24"))
# HOW OFTEN THE SCAN ACTUALLY RUNS. evaluate() is called every ~10 minutes; this walks several days
# of gw_door_event through a window function and must not run on that cadence. The signal it
# measures is 6+ hours wide, so a half-hourly recomputation is far finer than the thing it watches.
# In between, the stored result is served WITH ITS AGE — the same read-never-derives rule the
# dashboard follows, applied to a check.
DEGRADED_INTERVAL_S = float(os.environ.get("HEALTH_DEGRADED_INTERVAL_S", "1800"))
_IST_OFFSET_S = 19800                       # +05:30, fixed — IST has no DST


def _starvation_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS starvation_check (
        gateway_id TEXT, computed_at REAL, compute_ms INTEGER,
        -- The thresholds this result was computed UNDER. A cached verdict from before someone
        -- changed HEALTH_DEGRADED_FRAC is a verdict about a different question, and serving it
        -- would let a threshold change appear to have taken effect when it had not.
        lookback_h REAL, baseline_d REAL, frac REAL, min_hours INTEGER, min_opens INTEGER,
        payload TEXT,
        PRIMARY KEY (gateway_id, computed_at))""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_starvation ON starvation_check(gateway_id, computed_at)")


def _starvation_params():
    return (DEGRADED_LOOKBACK_H, DEGRADED_BASELINE_D, DEGRADED_FRAC, DEGRADED_MIN_HOURS,
            DEGRADED_MIN_OPENS)


def starvation_compute(db, gw, cams, now=None):
    """Boardings per door OPEN, per camera per IST hour, recent vs the camera's own baseline.

    ONE DERIVATION OF 'DOOR OPEN', and it is rtt_core.opens_with_floor's, rendered in SQL: a
    transition INTO 'open' from anything else, with NULL door_state kept in the sequence (h3 maps
    its internal 'unknown' to NULL on the wire and dropping those rows would merge two opens into
    one). Numerator and denominator come from the SAME query on both sides of the comparison, so
    the ratio cannot drift because two definitions of a cycle disagreed.

    NOT door CYCLES. A cycle is a transition into 'closed' and lives in dash_api._h3_cycle_ts;
    reimplementing it here would be a second copy of the number the whole MEP-02 sheet resolves to.
    An open is the event a boarding actually belongs to, it is exactly expressible in SQL, and the
    ratio is compared only against itself — so the choice costs nothing and duplicates nothing.
    """
    now = time.time() if now is None else now
    t_start = time.time()
    t_base = now - DEGRADED_BASELINE_D * 86400.0
    t_recent = now - DEGRADED_LOOKBACK_H * 3600.0
    hourkey = "CAST(strftime('%%Y%%m%%d%%H', %s + %d, 'unixepoch') AS INTEGER)"
    opens, boards = {}, {}
    note = None
    try:
        # The window function partitions per camera, so the FIRST row of each camera inside the
        # window has prev=NULL and counts as an open if it is one. That is one row per camera per
        # scan at most, and it is the same boundary behaviour rtt_core has at the head of its list.
        for r in db.execute(
                f"SELECT cam, {hourkey % ('ts', _IST_OFFSET_S)} hr, COUNT(*) n FROM ("
                "  SELECT cam, ts, door_state,"
                "         LAG(door_state) OVER (PARTITION BY cam ORDER BY ts, id) prev"
                "  FROM gw_door_event WHERE gateway_id=? AND ts>=?"
                ") WHERE door_state='open' AND (prev IS NULL OR prev<>'open') "
                "GROUP BY cam, hr", (gw, t_base)):
            opens[(r["cam"], r["hr"])] = r["n"]
    except sqlite3.OperationalError as e:
        # LAG needs SQLite >= 3.25, and a gateway older than the door schema has no door_state at
        # all. Either way this is a check that DOES NOT APPLY, which is not the same as a fleet
        # that is fine — it is reported as unknown and nothing is claimed.
        return {"state": "unavailable", "note": f"door-open derivation unavailable: {e}",
                "cams": {}, "degraded": [], "computed_at": now,
                "compute_ms": int((time.time() - t_start) * 1000)}
    try:
        for r in db.execute(
                f"SELECT cam, {hourkey % ('ts', _IST_OFFSET_S)} hr, COUNT(*) n FROM transit_event "
                "WHERE gateway_id=? AND ts>=? AND direction='in' GROUP BY cam, hr", (gw, t_base)):
            boards[(r["cam"], r["hr"])] = r["n"]
    except sqlite3.OperationalError as e:
        return {"state": "unavailable", "note": f"boardings unavailable: {e}", "cams": {},
                "degraded": [], "computed_at": now,
                "compute_ms": int((time.time() - t_start) * 1000)}

    # ERA CROSSING IS A CONFOUND AND IT IS NAMED, NOT HIDDEN. A counting build that changed inside
    # the window can move this ratio all by itself, and "the counter was rebuilt" is a different
    # finding from "the camera is starved" — they lead to different boxes.
    vers = {}
    for tbl, col, tcol in (("gw_door_event", "door_version", "ts"),
                           ("validation_item", "counting_version", "ts_start")):
        try:
            for r in db.execute(f"SELECT cam, COUNT(DISTINCT {col}) n FROM {tbl} WHERE "
                                f"gateway_id=? AND {tcol}>=? AND {col} IS NOT NULL AND {col}<>'' "
                                f"GROUP BY cam", (gw, t_base)):
                vers.setdefault(r["cam"], {})[col] = r["n"]
        except sqlite3.OperationalError:
            pass                                    # an older schema simply cannot report this

    recent_key = int(datetime.fromtimestamp(t_recent, IST).strftime("%Y%m%d%H"))
    out, degraded = {}, []
    for cam in cams:
        hrs = sorted({h for (c, h) in opens if c == cam} | {h for (c, h) in boards if c == cam})
        base_ratios, recent = [], []
        n_thin = n_offhours = 0
        for h in hrs:
            hod = h % 100
            if not (ACTIVE_FROM <= hod < ACTIVE_TO):
                n_offhours += 1
                continue                            # a lift at 03:00 carries nobody; not judged
            o = opens.get((cam, h), 0)
            if o < DEGRADED_MIN_OPENS:
                n_thin += 1
                continue                            # a ratio built on noise is not evidence
            ratio = boards.get((cam, h), 0) / float(o)
            (recent if h >= recent_key else base_ratios).append((h, ratio, o))
        base_vals = sorted(r for _h, r, _o in base_ratios)
        if len(base_vals) < DEGRADED_MIN_BASE_HOURS or not base_vals:
            out[cam] = {"state": "no baseline", "n_base_hours": len(base_vals),
                        "need": DEGRADED_MIN_BASE_HOURS, "n_recent_hours": len(recent),
                        "n_thin_hours": n_thin,
                        "note": f"only {len(base_vals)} judgeable active hour(s) of history in the "
                                f"last {DEGRADED_BASELINE_D:g} days — too few to say what this "
                                f"camera's normal is. UNKNOWN, not healthy."}
            continue
        base = base_vals[len(base_vals) // 2]        # MEDIAN: a few bad hours must not move it
        floor = DEGRADED_FRAC * base
        under = [(h, r, o) for h, r, o in recent if r < floor]
        # MEDIAN, matching the baseline. The mean was a worse answer AND an incoherent one: with
        # the collapse 20 hours into a 24-hour window, four healthy hours dragged it to 0.256 and
        # the phrase read "32% of baseline" beside "14 of 18 hours below 40%" — two numbers about
        # the same camera that a reader has to reconcile. Both sides are a median now.
        _rvals = sorted(r for _h, r, _o in recent)
        cur = (_rvals[len(_rvals) // 2] if _rvals else None)
        # NO "CONSECUTIVELY" COUNT. The obvious version counted adjacent entries in the judged-hour
        # LIST, which is not the same as adjacent hours: thin hours and the overnight gap are
        # skipped, so a run reported as consecutive could span a night. The count of judged hours
        # plus the hour it started is the same information without the claim that can be wrong.
        rec = {"state": "ok", "baseline": round(base, 3),
               "floor": round(floor, 3),
               "recent_median": (round(cur, 3) if cur is not None else None),
               "n_recent_hours": len(recent), "n_under": len(under),
               "n_thin_hours": n_thin, "n_offhours_skipped": n_offhours,
               "n_base_hours": len(base_vals),
               "first_under": (min(h for h, _r, _o in under) if under else None),
               "door_versions": (vers.get(cam) or {}).get("door_version"),
               "counting_versions": (vers.get(cam) or {}).get("counting_version")}
        if len(under) >= DEGRADED_MIN_HOURS:
            rec["state"] = "degraded"
            degraded.append(cam)
        out[cam] = rec
    return {"state": "ok", "cams": out, "degraded": degraded, "computed_at": now,
            "compute_ms": int((time.time() - t_start) * 1000),
            "params": {"lookback_h": DEGRADED_LOOKBACK_H, "baseline_d": DEGRADED_BASELINE_D,
                       "frac": DEGRADED_FRAC, "min_hours": DEGRADED_MIN_HOURS,
                       "min_opens": DEGRADED_MIN_OPENS}}


def _starvation(db, gw, cams, now=None):
    """-> (phrase|None, payload). Recomputes at most every DEGRADED_INTERVAL_S; serves the stored
    result with its age in between.

    evaluate() runs every ~10 minutes and this walks days of gw_door_event through a window
    function. The signal is 6+ hours wide, so recomputing on the tick cadence would spend 144x the
    work to learn the same thing — the read-never-derives rule this system runs on, applied to a
    check rather than a view.
    """
    now = time.time() if now is None else now
    try:
        _starvation_table(db)
        r = db.execute("SELECT computed_at, compute_ms, payload, lookback_h, baseline_d, frac, "
                       "min_hours, min_opens FROM starvation_check WHERE gateway_id=? "
                       "ORDER BY computed_at DESC LIMIT 1", (gw,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    fresh = None
    if r and (now - (r["computed_at"] or 0)) < DEGRADED_INTERVAL_S:
        # SAME QUESTION, OR RECOMPUTE. A cached verdict from before a threshold changed answers a
        # different question, and serving it would make the change look applied when it was not.
        if (r["lookback_h"], r["baseline_d"], r["frac"], r["min_hours"],
                r["min_opens"]) == _starvation_params():
            try:
                fresh = json.loads(r["payload"] or "{}")
                fresh["age_s"] = round(now - (r["computed_at"] or 0), 1)
            except (ValueError, TypeError):
                fresh = None
    if fresh is None:
        fresh = starvation_compute(db, gw, cams, now)
        fresh["age_s"] = 0.0
        try:
            db.execute("INSERT OR REPLACE INTO starvation_check (gateway_id, computed_at, "
                       "compute_ms, lookback_h, baseline_d, frac, min_hours, min_opens, payload) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (gw, fresh["computed_at"], fresh["compute_ms"], *_starvation_params(),
                        json.dumps(fresh)))
            db.commit()
            db.execute("DELETE FROM starvation_check WHERE gateway_id=? AND computed_at < ?",
                       (gw, now - 14 * 86400))
            db.commit()
        except sqlite3.OperationalError:
            pass                                    # a read-only DB must not break the check
    if fresh.get("state") != "ok" or not fresh.get("degraded"):
        return None, fresh
    bits = []
    for cam in fresh["degraded"]:
        d = fresh["cams"][cam] or {}
        # THE PHRASE MUST SAY WHY THIS IS NOT SILENCE. Whoever reads this line has been trained by
        # every other entry on it that a named camera means "nothing is arriving". Here something
        # is arriving and it is too little, and the first sentence has to say so or the reader goes
        # and checks a stream that is fine.
        _pct = round(100.0 * (d.get("recent_median") or 0)
                     / max(d.get("baseline") or 1e-9, 1e-9))
        _first = str(d.get("first_under") or "")
        bits.append(
            f"{cam} counting {d.get('recent_median')} boardings per door open (median) against its "
            f"own baseline {d.get('baseline')} ({_pct}% of it) for {d.get('n_under')} of "
            f"{d.get('n_recent_hours')} judged active hours"
            + (f", first at {_first[:4]}-{_first[4:6]}-{_first[6:8]} {_first[8:10]}:00"
               if len(_first) == 10 else "")
            + (f" — NOTE this window spans {d['counting_versions']} counting builds, which can "
               f"move the ratio on its own" if (d.get("counting_versions") or 1) > 1 else "")
            + (f" — NOTE this window spans {d['door_versions']} door eras"
               if (d.get("door_versions") or 1) > 1 else ""))
    return ("STARVED, NOT SILENT — " + "; ".join(bits)
            + ". These cameras ARE posting and their doors ARE working; the counter is returning "
              "too little. Check the counting worker, not the stream."), fresh


# ============================================================ READING WORSE, NOT MISSING
# THE FIFTH SIGNAL. Signal 4 asks whether the COUNTER is returning too little. This asks whether
# the READER is returning worse — and it is a different failure, on a different box, with a
# different fix.
#
# WHAT IT WOULD HAVE CAUGHT. 2026-09-02 17:50 IST, a camera-side image-profile change (see
# INCIDENT_decode_regression_0902.md) lifted local contrast and crushed the space between the LED
# strokes. Every NCC score fell ~0.06 with no geometric shift, and three cameras failed at three
# different gates inside that band:
#
#   cam    confident floor reads/day    mean read_conf
#   ch27   25,197  ->      18           0.652 -> 0.584
#   ch30    7,416  ->   1,820           0.896 -> 0.708
#   ch29   35,494  ->  26,501           0.843 -> 0.772   (lobby 'G' gone entirely)
#
# Heartbeats fresh, segments advancing, transits arriving, doors cycling normally. Signals 1-4 were
# correct to stay quiet, and the fault ran for five days.
#
# TWO ARMS, BECAUSE ONE CANNOT SEE BOTH FAILURES. The obvious check — mean read_conf against the
# camera's own baseline — catches ch29 and ch30 but would have SKIPPED ch27, the camera that was
# worst broken: its 18 surviving reads are too thin a sample to judge a mean on, and any honest
# min-sample guard excludes them. The reads did not get worse there; they stopped existing. So:
#
#   (a) CONFIDENCE arm — mean read_conf dropped by more than CONF_DROP from the camera's own
#       baseline, judged only on days with enough reads to mean anything.
#   (b) VOLUME arm — confident floor reads collapsed below CONF_VOL_FRAC of the camera's own
#       baseline. This is what makes a camera that has gone quiet visible without needing a
#       trustworthy mean from the handful of reads it still emits.
#
# A camera fires if EITHER arm does, and the phrase says which — they send you to the same place
# but they are not the same finding.
#
# THE BASELINE IS ERA-SCOPED, AND THAT IS THE WHOLE DIFFERENCE BETWEEN THIS AND A NUISANCE.
# read_conf is a score against a specific template set at a specific geometry: rebuild either and
# the number moves BY DESIGN. `door_version` is exactly that instrument's identity, so the baseline
# is built only from days inside the camera's CURRENT era. A recalibration therefore resets this
# check instead of tripping it — which matters immediately, because the response to the 09-02
# incident is to recalibrate every affected camera onto a new era. A check that fired on its own
# remedy would be turned off within a week, and then the next 09-02 runs unseen again.
#
# While an era is younger than CONF_MIN_BASE_DAYS the camera is reported as "baseline building",
# not judged. An instrument with no history has nothing to be compared against, and saying so is
# the honest answer — not silence, and not a verdict.
#
# REPORTED, NOT A BREACH — the rule signal 4 and the config gaps follow. Nothing is DOWN.
#
# THE BASELINE IS N JUDGED DAYS, NOT A CALENDAR WINDOW — and that distinction is not academic.
# Measured on the live gateway: ch29 has NO rows at all between 2026-08-19 and 2026-09-02, a 13-day
# ingest gap. A calendar 14-day baseline evaluated on 09-03 reaches back only to 08-20 and finds
# nothing, so the check would have declined to judge on exactly the morning it existed to speak.
# Taking the most recent N JUDGED days inside the era instead — however far back they lie — the
# baseline is Aug 13-19 and the check fires. A gap in the data is not a reason to forget what the
# camera used to do; it is a reason to reach further for it.
CONF_LOOKBACK_D = float(os.environ.get("HEALTH_CONF_LOOKBACK_D", "1"))
CONF_BASELINE_D = int(os.environ.get("HEALTH_CONF_BASELINE_D", "14"))     # judged DAYS, not calendar
# How far back to LOOK for those days. Generous, because it costs one indexed GROUP BY and the
# alternative is the failure above. An era rarely spans this much (observed era ages 1.1-20.0 days),
# so in practice this bounds the scan rather than the baseline.
CONF_SEARCH_D = float(os.environ.get("HEALTH_CONF_SEARCH_D", "60"))
# ABSOLUTE, not fractional. The ask was ">0.05 from the camera's 14-day baseline", and it is the
# right shape: NCC is already a normalised 0-1 score, so 0.05 means the same thing on ch27's 0.65
# as on ch30's 0.90. A fractional threshold would demand a bigger absolute move from the camera
# that had the least headroom to begin with.
CONF_DROP = float(os.environ.get("HEALTH_CONF_DROP", "0.05"))
CONF_VOL_FRAC = float(os.environ.get("HEALTH_CONF_VOL_FRAC", "0.40"))
# A day with fewer confident reads than this cannot support a mean. Deliberately well above the
# handful ch27 still emits — a mean of 18 reads is arithmetic, not evidence. Such days are excluded
# from BOTH the baseline and the judgement, and the volume arm is what covers them.
CONF_MIN_READS = int(os.environ.get("HEALTH_CONF_MIN_READS", "200"))
CONF_MIN_BASE_DAYS = int(os.environ.get("HEALTH_CONF_MIN_BASE_DAYS", "3"))
CONF_INTERVAL_S = float(os.environ.get("HEALTH_CONF_INTERVAL_S", "1800"))
# The reasons that mean "the reader named a floor and stood behind it". Same set dash_api uses
# (DOOR_OK_REASONS): ch29 runs single-panel, so filtering to 'ok' alone returns zero rows there and
# would render a working camera as no-data.
_CONF_OK_REASONS = ("ok", "single_panel")


def _pct_str(frac):
    """A collapse to 0.1% must not print as '0% of it' — that reads as a rounding artefact rather
    than the total loss it is. One decimal below 1%, whole numbers above."""
    v = 100.0 * (frac or 0.0)
    return f"{v:.1f}%" if v < 1 else f"{v:.0f}%"


def _readconf_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS readconf_check (
        gateway_id TEXT, computed_at REAL, compute_ms INTEGER,
        -- The thresholds this verdict was computed UNDER, for the same reason starvation_check
        -- carries them: a cached answer to a different question must not look like a fresh one.
        lookback_d REAL, baseline_d REAL, drop_abs REAL, vol_frac REAL,
        min_reads INTEGER, min_base_days INTEGER,
        payload TEXT,
        PRIMARY KEY (gateway_id, computed_at))""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_readconf ON readconf_check(gateway_id, computed_at)")


def _readconf_params():
    return (CONF_LOOKBACK_D, float(CONF_BASELINE_D), CONF_DROP, CONF_VOL_FRAC,
            CONF_MIN_READS, CONF_MIN_BASE_DAYS)


def readconf_compute(db, gw, cams, now=None):
    """Daily mean read_conf and confident-read volume per camera, recent vs the camera's own
    era-scoped baseline. -> payload dict; never raises on a schema that cannot answer."""
    now = time.time() if now is None else now
    t_start = time.time()
    t_base = now - CONF_SEARCH_D * 86400.0
    daykey = "CAST(strftime('%%Y%%m%%d', ts + %d, 'unixepoch') AS INTEGER)" % _IST_OFFSET_S
    rows = []
    try:
        # door_version comes back per (cam, day) so a day that straddles a rebuild is visible as
        # two rows and can be dropped from the baseline rather than averaged across instruments.
        rows = list(db.execute(
            f"SELECT cam, {daykey} d, door_version dv, COUNT(*) n, AVG(read_conf) conf "
            "FROM gw_door_event WHERE gateway_id=? AND ts>=? AND floor IS NOT NULL "
            f"AND read_conf IS NOT NULL AND reason IN ({','.join('?' * len(_CONF_OK_REASONS))}) "
            "GROUP BY cam, d, dv", (gw, t_base, *_CONF_OK_REASONS)))
    except sqlite3.OperationalError as e:
        # An older gateway schema has no read_conf/door_version. That is a check that DOES NOT
        # APPLY, which is not the same as a fleet that is fine — claim nothing.
        return {"state": "unavailable", "note": f"read_conf unavailable: {e}", "cams": {},
                "degraded": [], "computed_at": now,
                "compute_ms": int((time.time() - t_start) * 1000)}

    # Current era per camera = the door_version of its newest floor-bearing row. Same rule
    # dash_api.alphabet_refresh uses, so "the current instrument" means one thing across the system.
    cur_era = {}
    try:
        for r in db.execute(
                "SELECT cam, door_version dv FROM gw_door_event g WHERE gateway_id=? "
                "AND door_version IS NOT NULL AND door_version<>'' AND ts = "
                "(SELECT MAX(ts) FROM gw_door_event WHERE gateway_id=g.gateway_id AND cam=g.cam "
                " AND door_version IS NOT NULL AND door_version<>'') GROUP BY cam", (gw,)):
            cur_era[r["cam"]] = r["dv"]
    except sqlite3.OperationalError:
        pass

    per_cam = {}
    for r in rows:
        per_cam.setdefault(r["cam"], []).append(r)
    recent_day = int(datetime.fromtimestamp(now - CONF_LOOKBACK_D * 86400.0, IST).strftime("%Y%m%d"))
    today = int(datetime.fromtimestamp(now, IST).strftime("%Y%m%d"))

    out, degraded = {}, []
    for cam in cams:
        era = cur_era.get(cam)
        drs = [r for r in per_cam.get(cam, []) if r["dv"] == era] if era else []
        if not drs:
            out[cam] = {"state": "no reads", "era": era,
                        "note": "no confident floor reads in this era in the window — either a "
                                "door-only camera or one that has stopped reading entirely"}
            continue
        # TODAY IS NOT JUDGED AND IS NOT BASELINE. A partial day's mean is a mean of the hours that
        # have happened, and the lift's traffic is not uniform across a day.
        hist = sorted((r for r in drs if r["d"] < today), key=lambda r: r["d"])
        judged = [r for r in hist if r["n"] >= CONF_MIN_READS]
        # The most recent CONF_BASELINE_D judged days BEFORE the recent window — reached for, not
        # bounded by the calendar. See the note above the constants.
        base = [r for r in judged if r["d"] < recent_day][-CONF_BASELINE_D:]
        recent = [r for r in hist if r["d"] >= recent_day]
        rec = {"era": era, "n_days_in_era": len(hist), "n_base_days": len(base),
               "recent_days": [r["d"] for r in recent],
               "recent_conf": (round(statistics.mean([r["conf"] for r in recent
                                                      if r["n"] >= CONF_MIN_READS]), 4)
                               if any(r["n"] >= CONF_MIN_READS for r in recent) else None),
               "recent_reads": sum(r["n"] for r in recent) or 0}
        if len(base) < CONF_MIN_BASE_DAYS and not rec["recent_reads"] and not judged:
            # Never judged, nothing arriving: this is not an instrument warming up, it is a camera
            # that does not read floors. Saying "baseline building" of it would imply a verdict is
            # coming, and none ever will.
            rec.update({"state": "no reads",
                        "note": "no confident floor reads in this era — a door-only camera, or one "
                                "that has never read"})
            out[cam] = rec
            continue
        if len(base) < CONF_MIN_BASE_DAYS:
            rec.update({"state": "baseline building",
                        "note": f"era has {len(base)} judged day(s) of history, needs "
                                f"{CONF_MIN_BASE_DAYS} — a rebuilt instrument has nothing to be "
                                f"compared against yet"})
            out[cam] = rec
            continue
        base_conf = statistics.median([r["conf"] for r in base])
        base_reads = statistics.median([r["n"] for r in base])
        rec.update({"state": "ok", "baseline_conf": round(base_conf, 4),
                    "baseline_reads": int(base_reads)})
        why = []
        if rec["recent_conf"] is not None and (base_conf - rec["recent_conf"]) > CONF_DROP:
            rec["conf_drop"] = round(base_conf - rec["recent_conf"], 4)
            why.append("confidence")
        if not recent:
            # No rows at all in the recent window is signal 1/2/3 territory, not this one.
            rec["note"] = "no rows in the recent window — see the heartbeat/transit signals"
        elif base_reads > 0 and rec["recent_reads"] < CONF_VOL_FRAC * base_reads * max(CONF_LOOKBACK_D, 1):
            rec["vol_frac"] = round(rec["recent_reads"]
                                    / max(base_reads * max(CONF_LOOKBACK_D, 1), 1e-9), 3)
            why.append("volume")
        if why:
            rec["why"] = why
            rec["state"] = "degraded"
            degraded.append(cam)
        out[cam] = rec
    return {"state": "ok", "cams": out, "degraded": degraded, "computed_at": now,
            "compute_ms": int((time.time() - t_start) * 1000),
            "params": {"lookback_d": CONF_LOOKBACK_D, "baseline_d": CONF_BASELINE_D,
                       "drop_abs": CONF_DROP, "vol_frac": CONF_VOL_FRAC,
                       "min_reads": CONF_MIN_READS, "min_base_days": CONF_MIN_BASE_DAYS}}


def _readconf(db, gw, cams, now=None):
    """-> (phrase|None, payload). Recomputes at most every CONF_INTERVAL_S; serves the stored
    result with its age in between — the same read-never-derives rule _starvation follows."""
    now = time.time() if now is None else now
    try:
        _readconf_table(db)
        r = db.execute("SELECT computed_at, payload, lookback_d, baseline_d, drop_abs, vol_frac, "
                       "min_reads, min_base_days FROM readconf_check WHERE gateway_id=? "
                       "ORDER BY computed_at DESC LIMIT 1", (gw,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    fresh = None
    if r and (now - (r["computed_at"] or 0)) < CONF_INTERVAL_S:
        if (r["lookback_d"], r["baseline_d"], r["drop_abs"], r["vol_frac"], r["min_reads"],
                r["min_base_days"]) == _readconf_params():
            try:
                fresh = json.loads(r["payload"] or "{}")
                fresh["age_s"] = round(now - (r["computed_at"] or 0), 1)
            except (ValueError, TypeError):
                fresh = None
    if fresh is None:
        fresh = readconf_compute(db, gw, cams, now)
        fresh["age_s"] = 0.0
        try:
            db.execute("INSERT OR REPLACE INTO readconf_check (gateway_id, computed_at, "
                       "compute_ms, lookback_d, baseline_d, drop_abs, vol_frac, min_reads, "
                       "min_base_days, payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (gw, fresh["computed_at"], fresh["compute_ms"], *_readconf_params(),
                        json.dumps(fresh)))
            db.execute("DELETE FROM readconf_check WHERE gateway_id=? AND computed_at < ?",
                       (gw, now - 14 * 86400))
            db.commit()
        except sqlite3.OperationalError:
            pass                                    # a read-only DB must not break the check
    if fresh.get("state") != "ok" or not fresh.get("degraded"):
        return None, fresh
    bits = []
    for cam in fresh["degraded"]:
        d = fresh["cams"][cam] or {}
        why = d.get("why") or []
        parts = []
        if "confidence" in why:
            parts.append(f"mean read_conf {d.get('recent_conf')} against its own era baseline "
                         f"{d.get('baseline_conf')} (down {d.get('conf_drop')})")
        if "volume" in why:
            parts.append(f"{d.get('recent_reads')} confident floor reads against a baseline "
                         f"{d.get('baseline_reads')}/day ({_pct_str(d.get('vol_frac'))} of it)")
        bits.append(f"{cam} " + " and ".join(parts))
    return ("READING WORSE, NOT MISSING — " + "; ".join(bits)
            + ". These cameras ARE posting and their doors ARE cycling; the floor READER is "
              "returning worse or fewer reads within one era, so this is not a rebuild. Check the "
              "camera image (focus, contrast/gamma, sharpness profile) before the software."), fresh


def _bundle_slow(db, gw, now=None):
    """-> (phrase|None, payload|None) for the most recent study-bundle download.

    WHY THIS IS ON THE HEALTH LINE AT ALL. The bundle timed out at 60.26s in a user's browser and
    handed them a page of raw JSON. Nothing had reported the approach: the per-file cost had been
    climbing for weeks and the only instrument was slowlog, which prints when a handler is ALREADY
    over its threshold — by which time a person has seen it. The signal that matters is the phase
    creeping towards the budget, and bundle_run records every request precisely so this line can
    show it before anyone clicks.

    SILENT WHEN NOBODY HAS DOWNLOADED ONE. An absent record is an unknown, and an unknown is not a
    breach — the same rule _precompute_slow and _litestream follow.
    """
    try:
        r = db.execute("SELECT started_at, total_s, period, n_withheld, withheld, phases, "
                       "slowest, slowest_s, ok FROM bundle_run WHERE gateway_id=? "
                       "ORDER BY started_at DESC LIMIT 1", (gw,)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if not r or r["total_s"] is None:
        return None, None
    bd = {k: r[k] for k in ("started_at", "total_s", "period", "n_withheld", "slowest",
                            "slowest_s", "ok")}
    bd["age_s"] = round((now or time.time()) - (r["started_at"] or 0), 1)
    try:
        bd["withheld"] = json.loads(r["withheld"] or "[]")
    except (ValueError, TypeError):
        bd["withheld"] = []
    slow = (r["total_s"] or 0) >= BUNDLE_SLOW_S
    if not slow and not r["n_withheld"]:
        return None, bd                    # carried on the payload regardless, so the dash can plot it
    # THE PHRASE NAMES THE FILE, not just the duration. "the bundle is slow" sends the reader to
    # read seven producers; "rtt_trips 41s of 46s" sends them to one.
    bits = []
    if slow:
        bits.append(f"last study bundle took {r['total_s']:.0f}s against a "
                    f"{BUNDLE_SLOW_S:.0f}s warning line (budget "
                    f"{float(os.environ.get('DASH_BUNDLE_BUDGET_S', '60')):.0f}s)"
                    + (f", slowest file {r['slowest']} {r['slowest_s']:.1f}s" if r["slowest"]
                       else ""))
    if r["n_withheld"]:
        bits.append(f"{r['n_withheld']} file(s) withheld ({', '.join(bd['withheld'])}) — the "
                    f"download completed and is INCOMPLETE, which the README inside it says too")
    return ("; ".join(bits) + f" [period={r['period']}, {_age_phrase(bd['age_s'])} ago]"), bd


def _precompute_slow(db, gw, now=None):
    """-> (phrase|None, payload|None) for the last recorded precompute sweep.

    SILENT WHEN THE JOB HAS NEVER RUN. A box without the precompute timer installed must not have
    this line cry wolf at it — the same rule _litestream follows for a unit that does not exist. An
    absent record is an unknown, and an unknown is not a breach.
    """
    try:
        # COALESCE on every stage column added after the table shipped, and the column list read
        # from the schema rather than asserted: a box that has not yet run the sweep carrying the
        # newest column would otherwise raise here and the whole precompute line would go silent —
        # reported as "the job has never run" on a gateway with two weeks of history.
        _have = {x[1] for x in db.execute("PRAGMA table_info(precompute_run)")}
        _stages = [c for c in ("alphabet_s", "aggregate_s", "rtt_s", "trends_cams_s",
                               "trends_fleet_s", "study_s") if c in _have]
        # A TRAILING COMMA IS A SYNTAX ERROR, and this except-clause swallows it as "the job has
        # never run" — the exact outcome the column-list read exists to prevent. With no stage
        # column at all the sweep TOTAL is still a record worth reporting.
        r = db.execute("SELECT started_at, finished_at, total_s"
                       + ("".join(f", COALESCE({c},0) {c}" for c in _stages))
                       + " FROM precompute_run "
                       "WHERE gateway_id=? ORDER BY started_at DESC LIMIT 1", (gw,)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    if not r or r["total_s"] is None:
        return None, None
    pc = {k: 0.0 for k in ("alphabet_s", "aggregate_s", "rtt_s", "trends_cams_s",
                           "trends_fleet_s", "study_s")}
    pc.update({k: r[k] for k in ("started_at", "finished_at", "total_s", *_stages)})
    pc["age_s"] = round((now or time.time()) - (r["finished_at"] or 0), 1)
    if r["total_s"] <= PRECOMPUTE_SLOW_S:
        return None, pc                    # carried on the payload regardless, so the dash can plot it
    tc, tf = (pc["trends_cams_s"] or 0.0), (pc["trends_fleet_s"] or 0.0)
    tot = r["total_s"] or 1.0
    # THE PHRASE NAMES THE BIGGEST STAGE, not a favourite fix. A duration alone tells the reader to
    # worry without telling them where to look, and the stage that dominates is not the one the
    # trends work made famous: measured on a full sweep, aggregate was 50.6% and all of trends
    # 28.7%. The shared read IS the first lever *within trends* — fleet and per-camera derive the
    # same era rows twice, so ~2x on that stage — but that is ~14% of the sweep, not ~2x of it.
    # Quoting the whole-sweep saving as 2x would send the next person to the smaller half.
    parts = sorted((("aggregate", pc["aggregate_s"] or 0.0), ("alphabet", pc["alphabet_s"] or 0.0),
                    ("rtt", pc["rtt_s"] or 0.0), ("trends", tc + tf),
                    ("study", pc["study_s"] or 0.0)), key=lambda x: -x[1])
    phrase = (f"precompute sweep {_age_phrase(tot)} — over the "
              f"{_age_phrase(PRECOMPUTE_SLOW_S)} threshold (default: half the 1h timer interval). "
              f"Stages: "
              + ", ".join(f"{n} {_age_phrase(v)} ({100*v/tot:.0f}%)" for n, v in parts)
              + f"; trends splits {_age_phrase(tc)} per-camera + {_age_phrase(tf)} fleet. "
              f"Cheapest fix inside trends is the SHARED READ — fleet and per-camera derive the "
              f"same era rows twice, ~2x on that stage — but size the work against the stage that "
              f"actually dominates above. Incremental fill is the harder step: _trends_key builds "
              f"the fleet key from every camera's key joined, so one camera changing era "
              f"invalidates the fleet entry anyway. See DEPLOY_trends_perf.md 3c")
    return phrase, pc


def _prev(db, gw):
    r = db.execute("SELECT ts, ok, detail FROM health_status WHERE gateway_id=? "
                   "ORDER BY ts DESC LIMIT 1", (gw,)).fetchone()
    if not r:
        return None
    try:
        return {"ts": r["ts"], "ok": bool(r["ok"]), "detail": json.loads(r["detail"] or "{}")}
    except ValueError:
        return {"ts": r["ts"], "ok": bool(r["ok"]), "detail": {}}


def evaluate(db, gw, now=None):
    """-> the full result. Pure read; writes nothing."""
    now = time.time() if now is None else now
    cams, cam_source = expected_cams(db, gw)
    prev = _prev(db, gw)
    prev_seg = {c: (prev or {}).get("detail", {}).get(c, {}).get("segments") for c in cams}
    prev_ok_since = {c: (prev or {}).get("detail", {}).get(c, {}).get("bad_since") for c in cams}

    hb = {r["cam"]: r for r in db.execute(
        "SELECT cam, ts, segments, dropped, posted, last_transit_ts, mode "
        "FROM analyzer_status WHERE gateway_id=?", (gw,))}
    tr = {r["cam"]: r["mx"] for r in db.execute(
        "SELECT cam, MAX(ts) mx FROM transit_event WHERE gateway_id=? GROUP BY cam", (gw,))}
    dr = {r["cam"]: r["mx"] for r in db.execute(
        "SELECT cam, MAX(ts) mx FROM gw_door_event WHERE gateway_id=? GROUP BY cam", (gw,))}

    detail, bad = {}, []
    for cam in cams:
        h = hb.get(cam)
        hb_ts = h["ts"] if h else None
        segments = h["segments"] if h else None
        hb_age = (now - hb_ts) if hb_ts else None
        tr_ts, dr_ts = tr.get(cam), dr.get(cam)
        tr_age = (now - tr_ts) if tr_ts else None

        reasons = []
        # Hoisted: the transit-class logic below needs the previous segment sample whether or not the
        # heartbeat is fresh. Left inside the else-branch it was a NameError on every stale-heartbeat
        # camera — i.e. on exactly the cameras a breach line is about.
        ps = prev_seg.get(cam)
        if hb_ts is None:
            reasons.append("no heartbeat ever — no analyzer_status row for this camera, so its "
                           "worker has never reported to the gateway (check the fleet supervisor)")
        elif hb_age > HB_STALE_S:
            reasons.append(f"worker silent {_age_phrase(hb_age)} (since {_hhmm(hb_ts, now)}) — the "
                           f"analyzer heartbeat stopped; the camera itself may be fine")
        else:
            # Only meaningful while the heartbeat is FRESH: a stale heartbeat carries a stale
            # counter, and reporting "processing nothing" about a worker that is not running at all
            # would name the wrong fault and send someone to the wrong box.
            if (ps is not None and segments is not None and segments == ps
                    and prev and (now - prev["ts"]) > 120):
                reasons.append(f"alive but processing nothing — segments stuck at {segments} "
                               f"since {_hhmm(prev['ts'], now)} (no video reaching the worker)")
        # ── OUT OF SERVICE IS A CLASS, NOT A FAULT ───────────────────────────────────────────
        # ch16, 2026-08-18: silent from 11:07 with segments flowing and a fresh heartbeat, reported
        # as "worker stall class". The lift was OUT OF SERVICE, parked at P4 — the cabin indicator
        # read "P4 OUT" and zero transits were CORRECT. Nothing was broken except the classification.
        #
        # The evidence to tell them apart was already on the wire: a working camera watching a
        # parked lift sees a STATIC display. So the discriminator is not the floor VALUE (which the
        # reader cannot be trusted to render — see below) but its STABILITY: one floor, unchanged,
        # while the door never opens. A lift in service moves; a lift out of service does not.
        #
        # THE FLOOR STRING IS REPORTED BUT NOT TRUSTED. ch16's reader emits 85 distinct floor
        # strings, 20.4% of them implausible by shape — '1G' alone is 2,902 rows — and every one is
        # marked as a good read because no FLOOR_ALPHABET is configured. So the indicator text is
        # quoted as "the reader's assembly", never as a fact about the building.
        oos = None
        # A gateway older than the floor column has no indicator to read, and that is a MISSING
        # FEATURE rather than a lift in service — the check simply does not apply. Asking the schema
        # is cheaper than discovering it through an OperationalError in the one code path whose job
        # is to report calmly.
        if tr_ts is not None and tr_age > OOS_MIN_S and _has_col(db, "gw_door_event", "floor"):
            fl = db.execute(
                "SELECT floor, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts FROM gw_door_event "
                "WHERE gateway_id=? AND cam=? AND ts>=? AND floor IS NOT NULL "
                "GROUP BY floor ORDER BY n DESC", (gw, cam, now - tr_age)).fetchall()
            n_reads = sum(r["n"] for r in fl)
            if n_reads >= OOS_MIN_READS and fl:
                top = fl[0]
                share = top["n"] / n_reads
                # Door activity during the same window would mean the lift IS working and merely
                # carrying nobody — a different statement, and not this one.
                opened = _one(db, "SELECT COUNT(*) FROM gw_door_event WHERE gateway_id=? AND cam=? "
                                  "AND ts>=? AND door_state='open'", (gw, cam, now - tr_age)) or 0
                # DOOR ACTIVITY AS A RATE, NOT AS ZERO. Requiring exactly zero opens is brittle:
                # a parked lift can be opened once by an engineer, and one row in four hours would
                # have disqualified it. A lift IN SERVICE opens its doors tens of times an hour, so
                # the two populations are orders of magnitude apart and a rate separates them
                # cleanly without depending on a single row.
                opens_hr = opened / max(1e-9, tr_age / 3600.0)
                if share >= OOS_STABLE_FRAC and opens_hr < OOS_MAX_OPENS_HR:
                    oos = {"floor": top["floor"], "share": round(100.0 * share, 1),
                           "n_reads": n_reads, "since": tr_ts, "opens_hr": round(opens_hr, 2)}

        # ── TRANSIT SILENCE, and WHICH CLASS OF SILENCE it is ────────────────────────────────
        # The class is the whole value of the line. Three states share one symptom (no transits):
        #   segments FROZEN   -> the worker is stalled; the stream may be fine (the resume stall)
        #   segments FLOWING  -> video is being processed and nothing is being counted
        #   heartbeat STALE   -> the worker is not running at all, already named above
        # Naming it turns "ch29 is quiet" into "go and look at the worker" or "go and look at the
        # lift", which are different errands.
        quiet_offhours = False
        klass = None
        transit_breach = False
        if oos:
            # REPORTED, NOT ALARMED. A parked lift is a fact about the building, not a fault in the
            # fleet, and calling it a breach is how an operator learns to ignore the line.
            klass = (f"LIFT OUT OF SERVICE — indicator has read {oos['floor']!r} on "
                     f"{oos['share']:.0f}% of {oos['n_reads']} reads and the doors have opened "
                     f"{oos['opens_hr']:.2f}/hr since {_hhmm(oos['since'], now)} "
                     f"(reader assembly, not a verified string)")
        if tr_ts is None:
            reasons.append("no transit ever recorded")
            klass, transit_breach = "no transit ever recorded", True
        elif tr_age > TRANSIT_STALE_S:
            seg_moved = (ps is not None and segments is not None and segments != ps)
            if oos:
                pass                              # the class is already set, and it is not a stall
            elif hb_ts is None or (hb_age or 0) > HB_STALE_S:
                klass = "worker not running"
            elif ps is None:
                klass = "segments unknown = first check"       # no previous sample to compare
            elif seg_moved:
                klass = "segments flowing = worker stall class"
            else:
                klass = "segments frozen too = upstream/relay class"
            if oos:
                quiet_offhours = True             # carried on the payload as REPORTED, never bad
            elif _active(now):
                transit_breach = True
                reasons.append(f"no transit in {_age_phrase(tr_age)} (last {_hhmm(tr_ts, now)}) "
                               f"[{klass}]")
            else:
                # OUTSIDE ACTIVE HOURS: reported, never alarmed. A residential lift at 03:00 carries
                # nobody, and a rule that fires then teaches the reader to ignore the one that does.
                quiet_offhours = True

        bad_since = prev_ok_since.get(cam) if reasons else None
        if reasons and not bad_since:
            bad_since = now                      # first check that saw it — the honest "since"
        detail[cam] = {
            "ok": not reasons, "reasons": reasons, "segments": segments,
            "klass": klass, "quiet_offhours": quiet_offhours,
            "transit_breach": transit_breach,
            "silent_since": tr_ts,
            "hb_ts": hb_ts, "hb_age_s": None if hb_age is None else round(hb_age, 1),
            "transit_ts": tr_ts, "transit_age_s": None if tr_age is None else round(tr_age, 1),
            "door_ts": dr_ts, "mode": (h["mode"] if h else None),
            "bad_since": bad_since,
        }
        if reasons:
            bad.append(cam)

    # ── INFRASTRUCTURE, not cameras: the two failures that make every camera figure meaningless ──
    infra = []
    pi_ts = _one(db, "SELECT MAX(ts) FROM relay_status WHERE gateway_id=?", (gw,))
    pi_age = (now - pi_ts) if pi_ts else None
    if pi_ts is None:
        infra.append("Pi telemetry: relay_status has never reported")
    elif pi_age > PI_STALE_S:
        infra.append(f"Pi telemetry {_age_phrase(pi_age)} old (last {_hhmm(pi_ts, now)}) — the relay "
                     f"is not reporting, so every camera figure below may be describing a dead feed")
    # ── CONFIG GAPS, reported alongside faults ──────────────────────────────────────────────
    # The floor whitelist has been logged as "NONE" at worker startup since it was written and
    # nothing consumed it, so ch16 ran with 20.4% of its floor attribution accepted as good reads
    # for as long as it has existed. A warning nobody reads is not a warning. It is now on the wire
    # (analyzer_status.floor_alphabet_n) and reported here per camera, so the next camera cannot
    # regress into the same silence.
    gaps = []
    if _has_col(db, "analyzer_status", "floor_alphabet_n"):
        for r in db.execute("SELECT cam, floor_alphabet_n FROM analyzer_status WHERE gateway_id=?",
                            (gw,)):
            if r["cam"] in cams and not (r["floor_alphabet_n"] or 0):
                gaps.append(f"{r['cam']} floor whitelist: NONE")
    pc_phrase, pc = _precompute_slow(db, gw, now)
    bd_phrase, bd = _bundle_slow(db, gw, now)
    dg_phrase, dg = _starvation(db, gw, cams, now)
    rc_phrase, rc = _readconf(db, gw, cams, now)
    ls_active, ls_note = _litestream()
    if ls_active is False:
        infra.append(f"litestream {ls_note} — the gateway DB is NOT being replicated. On 2026-08-04 "
                     f"the backup chain was found unrestorable at every timestamp because nobody had "
                     f"tried in weeks; silence is how that happened")

    # A CONFIG GAP IS NOT A FAULT. It does not make the line say BREACH — nothing is broken right
    # now — but it is stated every time until it is closed, which is the difference between a
    # warning and a warning nobody reads.
    ok = not bad and not infra
    n = len(cams)
    hhmm = datetime.fromtimestamp(now, IST).strftime("%H:%M")
    if ok:
        act = "within the hour" if _active(now) else "as expected for the hour"
        line = f"LiftLab health {hhmm}: all {n} cameras posted {act} — OK"
    else:
        parts = []
        for c in bad:
            # BUILT FROM THE ACTUAL REASONS, not from an assumption about which one fired.
            #
            # This used to render EVERY breach as transit silence: "{cam} silent since {last
            # transit} ({klass})". For a camera that breached on its HEARTBEAT while its transits
            # were flowing, that printed the last-transit time under the word "silent" — a camera
            # posting three seconds ago described as silent — and "(None)", because klass is only
            # assigned in the transit branch. Live first run, 2026-08-12 20:07: "ch29 silent since
            # 20:07 (None)" while ch29 had just posted. The check was right; the sentence was false.
            #
            # A monitor whose one line can misdescribe the fault is worse than a quiet one: it sends
            # the reader to the wrong box, and the first thing they learn is not to trust it.
            d = detail[c]
            if d.get("klass") and d.get("transit_breach"):
                # the specified format, for the case it was specified for
                parts.append(f"{c} silent since {_hhmm(d.get('silent_since'), now)} ({d['klass']})")
            else:
                parts.append(f"{c} {'; '.join(d['reasons'])}")
        parts += infra
        line = f"LiftLab health {hhmm} BREACH: " + " · ".join(parts)
    # Cameras quiet outside active hours: REPORTED, never alarmed. Carried on the payload so the
    # dashboard and the daily line can show them without the word BREACH attached.
    quiet = [c for c in cams if detail[c].get("quiet_offhours")]
    # .get("klass") is None for a healthy camera, not "" — dict.get's default only applies to a
    # MISSING key, and this key is always present.
    oos_cams = [c for c in cams
                if (detail[c].get("klass") or "").startswith("LIFT OUT OF SERVICE")]
    if gaps:
        line += (" [CONFIG GAP: " + "; ".join(gaps)
                 + " — every assembled string is accepted as a good read until set]")
    # SAME RULE AS A CONFIG GAP: stated every time until it is addressed, never the word BREACH.
    # A sweep at 16 minutes still fits inside its hour; what matters is that it is now visible at
    # all, because "watch the reported fill duration" needs something to do the reporting.
    # BEFORE the infra notes, because it is about the DATA and whoever reads this line came for
    # the data. A camera that is up and counting a fifth of what it should is the finding; the
    # sweep duration underneath it is housekeeping.
    if dg_phrase:
        line += f" [DEGRADED: {dg_phrase}]"
    # BESIDE ITS SIBLING. Signal 4 says the counter returned too little; signal 5 says the reader
    # returned worse. On 2026-09-02 the same event produced both on ch30, and seeing them together
    # is what distinguishes "one camera's counter broke" from "the picture changed for everyone".
    if rc_phrase:
        line += f" [READS: {rc_phrase}]"
    if pc_phrase:
        line += f" [PRECOMPUTE: {pc_phrase}]"
    if bd_phrase:
        line += f" [BUNDLE: {bd_phrase}]"
    if oos_cams and ok:
        line += (" (" + ", ".join(f"{c}: {detail[c]['klass']}" for c in oos_cams) + ")")
    elif quiet and ok:
        line += f" (outside active hours: {', '.join(quiet)} quiet — reported, not alarmed)"
    return {"gw": gw, "ts": now, "ok": ok, "cams": cams, "cam_source": cam_source,
            "n_cams": n, "bad": bad, "detail": detail, "line": line, "infra": infra,
            "quiet_offhours": quiet, "active_hours": _active(now), "config_gaps": gaps,
            "pi_age_s": (None if pi_age is None else round(pi_age, 1)),
            "litestream_ok": ls_active, "litestream_note": ls_note,
            # Carried whether or not it breached, so the dashboard can show the trend rather than
            # only the moment it crossed. None means the job has never run — an unknown, not a zero.
            "precompute": pc, "precompute_slow": bool(pc_phrase),
            "bundle": bd, "bundle_slow": bool(bd_phrase),
            # DEGRADED IS NOT A BREACH and does not touch `ok`. Nothing is down: the lift runs, the
            # camera works, and the number is wrong. It is stated every time until it is addressed
            # — the rule the config gaps follow — and carried here so the dashboard can show it.
            "degraded": (dg or {}).get("degraded") or [],
            "starvation": dg,
            # Same contract as `degraded`: carried whether or not it fired, so the dashboard can
            # plot the per-camera confidence trend instead of only the moment it crossed.
            "reads_degraded": (rc or {}).get("degraded") or [],
            "readconf": rc,
            "prev_ok": (None if prev is None else prev["ok"])}


def should_send(db, res, now=None):
    """Send on a BREACH EDGE, on recovery, and once a day. -> (bool, why).

    Edge-triggered, not level-triggered: a camera down for three days must not produce 432 messages,
    or the daily line drowns in them and stops being read. The daily line still restates the
    outstanding breach every morning, so a standing fault cannot fade from view either.

    THE DAILY DE-DUPLICATION LIVES HERE, not in the caller. It keys on whether a send was ATTEMPTED
    since today's trigger time — not on whether one succeeded. Keying on success means that with no
    channel configured (or a channel that is down) every single run counts as "not sent yet" and the
    line goes out on every tick forever, which is precisely the flood this function exists to stop.
    """
    now = time.time() if now is None else now
    if res["prev_ok"] is None:
        return True, "first run"
    if res["prev_ok"] and not res["ok"]:
        return True, "breach"
    if res["ok"] and not res["prev_ok"]:
        return True, "recovered"
    ist = datetime.fromtimestamp(now, IST)
    target = ist.replace(hour=DAILY_HOUR_IST, minute=DAILY_MIN_IST,
                         second=0, microsecond=0).timestamp()
    if now >= target and not sent_since(db, res["gw"], target):
        return True, "daily"
    return False, "no change"


def sent_since(db, gw, since_epoch):
    """Was a send ATTEMPTED since this epoch. Attempted, not delivered — see should_send."""
    r = db.execute("SELECT COUNT(*) n FROM health_status WHERE gateway_id=? AND ts>=? AND sent=1",
                   (gw, since_epoch)).fetchone()
    return (r["n"] or 0) > 0


def deliver(res):
    """-> (channel, error). Never raises: a delivery failure must not lose the recorded status."""
    text = (f"[liftlab {res['gw']}] {datetime.fromtimestamp(res['ts'], IST):%Y-%m-%d %H:%M} IST\n"
            f"{res['line']}")
    if WEBHOOK_URL:
        try:
            if WEBHOOK_FIELD:
                body = json.dumps({WEBHOOK_FIELD: text}).encode()
                hdr = {"Content-Type": "application/json"}
            else:
                body, hdr = text.encode(), {"Content-Type": "text/plain"}
            req = urllib.request.Request(WEBHOOK_URL, data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return f"webhook HTTP {r.status}", None
        except Exception as e:
            return None, f"webhook failed: {type(e).__name__}: {str(e)[:160]}"
    if SMTP_HOST and SMTP_TO:
        try:
            import smtplib
            from email.message import EmailMessage
            m = EmailMessage()
            m["Subject"] = f"liftlab {res['gw']}: {'OK' if res['ok'] else 'CAMERA SILENT'}"
            m["From"] = os.environ.get("HEALTH_SMTP_FROM", f"liftlab@{os.uname().nodename}")
            m["To"] = SMTP_TO
            m.set_content(text)
            port = int(os.environ.get("HEALTH_SMTP_PORT", "587"))
            with smtplib.SMTP(SMTP_HOST, port, timeout=30) as s:
                if os.environ.get("HEALTH_SMTP_STARTTLS", "1") == "1":
                    s.starttls()
                user = os.environ.get("HEALTH_SMTP_USER", "")
                if user:
                    s.login(user, os.environ.get("HEALTH_SMTP_PASS", ""))
                s.send_message(m)
            return f"smtp {SMTP_HOST} -> {SMTP_TO}", None
        except Exception as e:
            return None, f"smtp failed: {type(e).__name__}: {str(e)[:160]}"
    # NO CHANNEL. Not an error, and not silence either: it is recorded as a fact so the dashboard
    # banner and the journal can both say that nothing was pushed anywhere.
    return None, ("no push channel configured (set HEALTH_WEBHOOK_URL or HEALTH_SMTP_HOST/TO) — "
                  "this line exists only on the dashboard, in the journal, and in health_status")


def record(db, res, channel, err, sent=False):
    db.execute("INSERT INTO health_status (gateway_id, ts, ok, n_cams, n_bad, line, detail, "
               "sent, delivered, delivery_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (res["gw"], res["ts"], int(res["ok"]), res["n_cams"], len(res["bad"]),
                res["line"], json.dumps(res["detail"]), int(bool(sent)),
                channel or "", err or ""))
    db.commit()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    force = "--daily" in argv or "--force" in argv
    args = [a for a in argv if not a.startswith("-")]
    gw = args[0] if args else os.environ.get("GATEWAY_ID", "site-A")

    db = _db()
    try:
        res = evaluate(db, gw)
        send, why = should_send(db, res)
        if force:
            send, why = True, (why if why in ("breach", "recovered", "first run") else "forced")
        channel, err = (deliver(res) if send else (None, None))
        record(db, res, channel, err if send else None, sent=send)
    finally:
        db.close()

    if as_json:
        print(json.dumps({**res, "sent": bool(send), "why": why,
                          "channel": channel, "delivery_error": err}, indent=1))
    else:
        print(res["line"])
        print(f"  cameras from {res['cam_source']}; sent={bool(send)} ({why})"
              + (f"; channel={channel}" if channel else "")
              + (f"; DELIVERY: {err}" if err else ""))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
