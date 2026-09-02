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

WHAT THE ALARM IS INSTEAD. Three signals, each covering a failure the others cannot see:

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
    if pc_phrase:
        line += f" [PRECOMPUTE: {pc_phrase}]"
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
