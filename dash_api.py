"""
/dash — the ONE page. Tabbed by camera + an always-on top strip, auto-refresh.

ADDITIVE and READ-ONLY: aggregates data that already lives across the other routers'
tables (channel_map, gw_event/gw_source, transit_event, analyzer_status,
camera_validation, watch_status, relay_status) into one view. It writes nothing and
creates no tables. /ops, /events, /validate, /pihealth stay as the deep views; /dash
links out to them.

The point of the project is one panel: the sheet ASSUMPTION sitting beside the
OBSERVATION, same eyeline, no verdict — "ch29 door close: observed median 2.81s
(p85 6.44s, n=1012) · sheet assumes 2.00s · non-compliant above 2.31s · 80% of
observed closes exceed 2.31s". Facts side by side; the reader draws the conclusion.

HONEST BLANKS + STALENESS: a camera with no analyser says "not analysed — relay
only"; a stale panel says stale rather than showing an old number as if it's live.
Every panel carries its own timestamp.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nav_common as nc
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
SNAP_DIR = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
SNAP_STALE_S = float(os.environ.get("SNAP_STALE_S", "20"))
# The Pi fleet overview lives in main.py, not here, so its path is configuration rather than a
# guess. Set DASH_FLEET_URL to wherever that page ends up when it moves off "/".
FLEET_URL = os.environ.get("DASH_FLEET_URL", "/fleet")
HB_STALE_S = 120.0                          # analyzer heartbeat older than this = down
IST = timezone(timedelta(hours=5, minutes=30))   # the building's clock; door ts are +05:30 local ISO

# Door-close + transfer compliance spec per camera (from the sheet). observed vs assumption side by side.
# Only cameras with an entry get the headline compliance panel; others show observed-only.
#   sheet_s / compliance_s / bank : door-close (2.31s = Bank C non-compliance line)
#   transfer_sheet_s              : C26 passenger transfer, sheet assumes 1.50 s/person
DOOR_SPECS = {
    "ch29": {"sheet_s": 2.00, "compliance_s": 2.31, "bank": "C", "transfer_sheet_s": 1.50},
}

# Comparability boundaries — data across these isn't directly comparable; charts MARK them and the
# close-travel headline uses only the current (post-boundary) regime.
CLOSE_TRAVEL_MAX_BOUNDARY = "2026-07-16T11:48:11+00:00"   # CLOSE_TRAVEL_MAX 10->30 (admits longer real closes)
# d7a7a49 retired the Pi door-watch and moved door cycles to the GPU engine. gw_event (Pi) and
# gw_door_event (GPU) are DIFFERENT INSTRUMENTS — different edge detector, clock, sampling — so their
# close-travel numbers are not comparable and must never be pooled. This is a comparability boundary
# like the other three; the compliance panel labels every close-travel stat with its instrument.
DOORWATCH_RETIRED_BOUNDARY = "2026-07-21T00:00:00+00:00"   # d7a7a49; Pi gw_event frozen, GPU gw_door_event live
_BOUNDARY_EPOCH = datetime.fromisoformat(CLOSE_TRAVEL_MAX_BOUNDARY).timestamp()
_RETIRED_EPOCH = datetime.fromisoformat(DOORWATCH_RETIRED_BOUNDARY).timestamp()
# 5f1488a's DoorTracker TIME GUARDS (max_gap 15s, plausible close 0.3-30s) changed what gets
# EMITTED without changing the era (templates/geometry untouched), so an era-filtered close-travel
# pool mixes pre-guard mispairings (0.08s / 4641s cycles) with clean rows — 115/154 impossible
# drowned the LIVE median. This boundary cuts the pool at the guard deploy: set it to that moment
# (ISO8601 with offset, or bare epoch seconds). Unset = no cut, old behavior + honest labeling.
DOOR_GUARD_BOUNDARY = os.environ.get("DASH_DOOR_GUARD_TS", "").strip()


def _guard_epoch():
    if not DOOR_GUARD_BOUNDARY:
        return None
    try:
        return float(DOOR_GUARD_BOUNDARY)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(DOOR_GUARD_BOUNDARY).timestamp()
    except ValueError:
        return None


_GUARD_EPOCH = _guard_epoch()

# DATA GAPS — windows where NO data was collected (relay/collect blind). Demand/counting/door numbers
# in these windows are MISSING, not zero; charts must mark them so a dip isn't read as low demand.
# Times are UTC; the human note gives the IST window operators reported it in.
DATA_GAPS = [
    # Relay stall: ffmpeg wedged alive-but-no-output; the DIED detector couldn't see it (fixed in
    # relay_soak.sh: alive-but-not-delivering => restart). Counting + door collect blind. Includes
    # Monday AM peak. Reported Jul 19 15:20 – Jul 20 09:47 IST => UTC below.
    {"start": "2026-07-19T09:50:00+00:00", "end": "2026-07-20T04:17:00+00:00",
     "cause": "relay-stall", "cams": ["ch29"],
     "note": "relay stall — collect blind ~Jul 19 15:20 to Jul 20 09:47 IST (incl. Monday AM peak); "
             "demand/counting/door here is MISSING, not low"},
]
for _g in DATA_GAPS:
    _g["start_epoch"] = datetime.fromisoformat(_g["start"]).timestamp()
    _g["end_epoch"] = datetime.fromisoformat(_g["end"]).timestamp()

# Fixed peak windows (local hours). The PEAK TRAP: sheet coefficients describe a PEAK design
# condition, not an all-day average — report both separately; the ratio is itself a finding.
PEAK_WINDOWS = {"am_peak": (8, 10), "pm_peak": (18, 20)}

# ── PEAK CAR OCCUPANCY — what the number is, stated wherever it appears ──────────────────────
# occupancy_max is the largest number of DISTINCT TRACKS simultaneously inside the cabin zone during
# one door-open episode. It is a FLOOR, not a count: anyone the detector missed, anyone occluded
# behind another body, and anyone whose foot point fell outside the cabin polygon is absent from it.
#
# The 0.5x figure is measured, and its provenance is deliberately part of the sentence. On ch30
# f5350-5650 the probe reported a cabin peak of 3 against 5-7 people visible in frame. That is ONE
# scene on ONE camera, so it calibrates the direction and the rough size of the undercount and
# nothing more; quoting it without "n=1" would turn a single observation into a correction factor.
#
# The membership point is the FOOT anchor — the same point the transit counter uses. A box-CENTER
# anchor was tested live against it and reported FEWER people, not more, on 117/151 crowd frames and
# 63/63 boarding frames, because zone_cabin is a FLOOR polygon: a body's middle sits above it. Foot
# also showed zero lobby bleed while the door was open (a constant 2 of 63 frames). Recovering the
# undercount needs occupancy-specific body-volume zones, which is backlog, not a label change.
OCC_CALIBRATION = ("measured minimum; ~0.5x at heavy crowding (n=1 scene, ch30); "
                   "undercount grows with crowding")
OCC_LABEL = "peak car occupancy (measured minimum)"
OCC_ANCHOR_NOTE = ("foot anchor — the same membership point as the transit counter; a centre anchor "
                   "measured LOWER against a floor polygon, so it is not the fix")

# Fixed lift-camera fallback if channel_map is empty (the set the operator named).
FALLBACK_CHANNELS = [16, 27, 29, 30, 32, 34, 37]

# The GPU's PROCESSING frame size per camera (the sub resolution). The /snap jpg is DOWNSCALED for the
# web grid, so /calibrate must emit coords in THIS space, not snapshot px. Override per-cam via ?fw=&fh=.
FRAME_SIZES = {"ch29": (704, 576), "ch16": (1280, 720)}
FRAME_SIZE_DEFAULT = (704, 576)

dash_router = APIRouter()


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST TIME BUDGET
#
# WHY THIS EXISTS. /dash/{gw}/data does not merely run slow — it does not terminate. Measured
# 2026-08-03: the page shell returns 200 in 6.5s, but the data fetch returned 000 after a full
# 600s with 0 bytes (DevTools shows it Pending forever). The page auto-refreshes, so an open tab
# strands one non-terminating request after another, each holding its row set, until the kernel
# reaps the process: two OOM kills in ~13h, anon-rss 1.48 GB and 1.49 GB, on unmodified code.
#
# A budget does not make the endpoint fast. It converts an OUTAGE into an ERROR MESSAGE — the
# request lets go of its memory and the caller learns why. That is worth shipping on its own,
# ahead of the query rewrite, because it is what stops a slow page from killing the gateway.
#
# TWO MECHANISMS, because either alone leaves a hole:
#   * set_progress_handler aborts SQL that is ALREADY EXECUTING. Without it a single long query is
#     uninterruptible from Python and no between-phase check ever gets a turn.
#   * _budget_check() between phases catches the Python-side walking of rows, which SQLite cannot
#     see and the progress handler therefore never fires for.
#
# The deadline is thread-local: Starlette runs `def` endpoints in the anyio threadpool, so each
# concurrent request gets its own, and a budget armed by one request cannot abort another's query.
DATA_BUDGET_S = float(os.environ.get("DASH_DATA_BUDGET_S", "25"))
_PROGRESS_STEPS = 20000                      # VM instructions between progress-handler calls


class DashTimeout(Exception):
    """This request exceeded its wall-clock budget and was aborted mid-flight."""


_budget = threading.local()


def _budget_expired():
    d = getattr(_budget, "deadline", None)
    return d is not None and time.monotonic() >= d


def _budget_arm(db, budget_s):
    """Start this request's clock and make running SQL interruptible."""
    _budget.deadline = time.monotonic() + budget_s
    _budget.budget_s = budget_s
    # Returning non-zero from the handler makes SQLite abort the statement with
    # OperationalError('interrupted'), which _q turns into DashTimeout below.
    db.set_progress_handler(lambda: 1 if _budget_expired() else 0, _PROGRESS_STEPS)


def _budget_disarm(db):
    _budget.deadline = None
    try:
        db.set_progress_handler(None, 0)
    except Exception:
        pass


def _budget_elapsed():
    b = getattr(_budget, "budget_s", None)
    d = getattr(_budget, "deadline", None)
    if b is None or d is None:
        return None
    return round(b - (d - time.monotonic()), 2)


def _budget_check(where):
    """Between-phase guard. Call at points where a lot of Python work is about to start."""
    if _budget_expired():
        raise DashTimeout(where)


def _q(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        # A budget abort arrives here as OperationalError('interrupted'). Returning [] would turn a
        # TIMEOUT into a silently empty panel — the dashboard would report "no rows" for a camera
        # holding tens of thousands of them, which is worse than an error because it looks like data.
        # Only the genuine "table not created yet" case may fall through to the honest empty.
        if _budget_expired():
            raise DashTimeout("sql")
        return []                            # table not created yet -> honest empty, never a 500


def _ist_today_str():
    return datetime.now(IST).date().isoformat()          # 'YYYY-MM-DD' (local ISO date-prefix)


def _iso_ist(ep):
    """Epoch -> local ISO, so a spreadsheet shows the building's clock next to the raw epoch."""
    try:
        return datetime.fromtimestamp(ep, IST).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _range_bounds(period, from_d="", to_d=""):
    """(t0, t1, label) as epochs on the BUILDING's clock. period = day|week|month|all, or an explicit
    from_d/to_d pair of YYYY-MM-DD. Ranges are inclusive of both end dates and align to IST midnight,
    because "last week" to an operator means seven local days, not 168 hours back from now."""
    now = datetime.now(IST)
    if from_d or to_d:
        try:
            d0 = datetime.fromisoformat(from_d).replace(tzinfo=IST) if from_d else now - timedelta(days=6)
            d1 = datetime.fromisoformat(to_d).replace(tzinfo=IST) if to_d else now
        except ValueError:
            d0, d1 = now - timedelta(days=6), now
        label = f"{d0.date()} to {d1.date()}"
    else:
        days = {"day": 1, "week": 7, "month": 30}.get(period or "all", 0)
        if not days:
            return None, None, "all data"
        d0, d1 = now - timedelta(days=days - 1), now
        label = f"last {days} day{'s' if days > 1 else ''} ({d0.date()} to {d1.date()})"
    t0 = datetime(d0.year, d0.month, d0.day, tzinfo=IST).timestamp()
    t1 = datetime(d1.year, d1.month, d1.day, tzinfo=IST).timestamp() + 86400
    return t0, t1, label


def _in_range(ep, t0, t1):
    """Half-open [t0, t1). Either bound may be None meaning unbounded on that side — a "last 7 days"
    window has a floor but no ceiling, so rows arriving mid-request are not silently dropped."""
    if t0 is None and t1 is None:
        return True
    if ep is None:
        return False
    if t0 is not None and ep < t0:
        return False
    if t1 is not None and ep >= t1:
        return False
    return True


# ── the /dash read window ─────────────────────────────────────────────────────
# /dash/{gw}/data used to read every camera's ENTIRE history on every page load, which is why it
# never terminated. It is now bounded: `days` (default 7) is a floor on ts, pushed into SQL so the
# rows are never fetched, not filtered in Python after the fact. ix_door_event(gateway_id,cam,ts)
# serves exactly this shape, so the window is an index range scan.
#
# ALL HISTORY is still reachable, but only by asking: days=0. It is labelled expensive in the
# response so a caller cannot stumble into it, and it is never what a default load runs.
WINDOW_DAYS = float(os.environ.get("DASH_WINDOW_DAYS", "7"))


def _ts_clause(t0, t1):
    """SQL fragment + args for a ts window. Appended to an existing WHERE, so it starts with AND.
    Pushing the bound into SQL is the whole point: the rows must never be fetched."""
    sql, args = "", []
    if t0 is not None:
        sql += " AND ts >= ?"; args.append(t0)
    if t1 is not None:
        sql += " AND ts < ?"; args.append(t1)
    return sql, args


def _era_clause(prefix):
    """SQL fragment + args selecting one era's rows by templates-hash PREFIX, as a RANGE.

    THIS REPLACES `door_version LIKE prefix||'%'`, AND THE REWRITE IS THE WHOLE FIX.
    SQLite's LIKE is case-insensitive for ASCII by default, so the optimiser may not convert it to a
    range and no index on door_version can serve it. Measured on ch29 (232,956 rows in the camera,
    72 in the era), period=all:

        LIKE, index (gateway_id,cam,ts)                  426.0 ms   SEARCH (gateway_id=? AND cam=?)
        LIKE, after adding (…,door_version,ts)           543.3 ms   SEARCH (gateway_id=? AND cam=?)
        range, index (gateway_id,cam,ts)                 298.9 ms   SEARCH (gateway_id=? AND cam=?)
        range, after adding (…,door_version,ts)            0.2 ms   SEARCH (… AND door_version>? AND <?)

    The index ALONE changes nothing — LIKE still cannot use it. The rewrite alone changes little.
    Together they are ~1780x, because only then does the era stop being a post-filter evaluated on
    every row the camera has ever stored.

    `>= p AND < p⁺` is exactly LIKE's prefix semantics under BINARY collation, with one deliberate
    difference: it is CASE-SENSITIVE. The prefix always comes from a door_version already in the
    table (_era_for reads one), so exact case is the correct match; LIKE's case-folding was
    accidental and could only ever have pooled two eras differing by case.
    """
    p = str(prefix or "")
    if not p:
        return "", []
    # p⁺ = the smallest string greater than every string starting with p.
    hi = p[:-1] + chr(ord(p[-1]) + 1)
    return " AND door_version >= ? AND door_version < ?", [p, hi]


def _window(days=None):
    """-> (t0, t1, meta). t1 stays None: 'last N days' has no ceiling."""
    d = WINDOW_DAYS if days is None else float(days)
    if d <= 0:
        return None, None, {"days": None, "all_history": True, "expensive": True,
                            "label": "all history",
                            "note": "unbounded read — explicitly requested via days=0"}
    t0 = time.time() - d * 86400.0
    return t0, None, {"days": d, "from": t0, "all_history": False, "expensive": False,
                      "label": f"last {d:g}d"}


def _ist_today_epoch():
    d = datetime.now(IST).date()
    return datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()   # tz-aware local ISO -> absolute epoch
    except Exception:
        return None


def _local_hour(iso):
    try:
        return datetime.fromisoformat(iso).hour          # local hour (ts carry +05:30)
    except Exception:
        return None


def _stats(cts):
    cts = sorted(cts)
    n = len(cts)
    return {"n": n, "median": round(_pctl(cts, 0.5), 2) if n else None,
            "p85": round(_pctl(cts, 0.85), 2) if n else None,
            "min": round(cts[0], 2) if n else None, "max": round(cts[-1], 2) if n else None}


def _cameras(db, gw):
    rows = _q(db, "SELECT channel, label, is_lift FROM channel_map WHERE gateway_id=? ORDER BY channel", (gw,))
    lifts = [(r["channel"], r["label"]) for r in rows if r["is_lift"]]
    if not lifts:                            # channel_map unmarked -> the named fallback set
        lifts = [(c, None) for c in FALLBACK_CHANNELS]
    return [{"cam": f"ch{c}", "channel": c, "label": lbl} for c, lbl in lifts]


def _snap(cam, gw):
    f = SNAP_DIR / gw / f"{cam}.jpg"
    try:
        age = time.time() - f.stat().st_mtime
        return {"age_s": round(age, 1), "stale": age > SNAP_STALE_S}
    except Exception:
        return None                          # no snapshot decoded yet


# ---- close-travel histogram: 0-1,1-2,...,6-8,8+ seconds ----
_HIST_EDGES = [1, 2, 3, 4, 5, 6, 8]


def _hist(vals):
    h = [0] * (len(_HIST_EDGES) + 1)
    for v in vals:
        placed = False
        for i, e in enumerate(_HIST_EDGES):
            if v < e:
                h[i] += 1; placed = True; break
        if not placed:
            h[-1] += 1
    return h


def _door_by_cam(db, gw):
    """One pass over gw_event -> per-camera door stats. 'Clean' close = quality NULL/'ok' AND
    close_travel_s present (the /events headline definition; the read-time duplicate withhold and full
    breakdown live on /events, linked from the panel)."""
    rows = _q(db, "SELECT s.camera cam, e.close_travel_s ct, e.door_open_start_ts os, e.quality q "
                  "FROM gw_event e JOIN gw_source s ON s.id = e.source_id WHERE s.gateway_id=?", (gw,))
    today = _ist_today_str()
    by = {}
    for r in rows:
        d = by.setdefault(r["cam"], {"total": 0, "today": 0, "cts": [], "last": None})
        d["total"] += 1
        os_ = r["os"] or ""
        if os_ >= today:
            d["today"] += 1
        if d["last"] is None or os_ > d["last"]:
            d["last"] = os_
        if r["ct"] is not None and (r["q"] is None or r["q"] == "ok"):
            d["cts"].append(float(r["ct"]))
    out = {}
    for cam, d in by.items():
        cts = sorted(d["cts"])
        n = len(cts)
        med = _pctl(cts, 0.5)
        p85 = _pctl(cts, 0.85)
        spec = DOOR_SPECS.get(cam)
        spec_out = None
        if spec and cts:
            over = sum(1 for v in cts if v > spec["compliance_s"])
            spec_out = {**spec, "pct_exceed": round(100.0 * over / n)}
        out[cam] = {
            "total": d["total"], "today": d["today"], "last_open": d["last"],
            "n": n, "median": round(med, 2) if med is not None else None,
            "p85": round(p85, 2) if p85 is not None else None,
            "min": round(cts[0], 2) if cts else None, "max": round(cts[-1], 2) if cts else None,
            "hist": _hist(cts), "hist_edges": _HIST_EDGES, "spec": spec_out,
        }
    return out


def _join_diagnostics(stops, transits, matched):
    """Item 4: WHY do transits fail to join door windows, quantified. The 5x per-camera rate
    difference (ch16 ~9.5% vs ch29 ~2%) is not noise, so measure the mechanism rather than guess:
      - window duration: how long each door-open window is (close_ts - ts)
      - in-window fraction: transits landing inside ANY window (== the join rate)
      - nearest-cycle gap: for the transits that MISS, how far to the nearest window edge. Tight
        clustering just outside a window => an anchoring/width problem (widen or re-anchor);
        scattered far => genuinely missing door cycles.
    Both series are ascending, so the nearest-gap scan is a single forward pass, not a rescan.
    """
    durs = sorted(max(0.0, (s["close_ts"] or s["ts"]) - s["ts"]) for s in stops)
    # ERA-OVERLAP ONLY. The door cycles span [win_lo, win_hi]; a transit outside that window predates
    # or postdates the door-read era and can never join. Including it as a "miss" is why ch29 showed a
    # ~4.5-day gap median. Judge join only over transits that overlap the door-active window.
    n_all = len(transits)
    if stops:
        win_lo = min(s["ts"] for s in stops)
        win_hi = max((s["close_ts"] or s["ts"]) for s in stops)
        transits = [t for t in transits if win_lo <= t[0] <= win_hi]
    n_tr = len(transits)
    excluded_out_of_era = n_all - n_tr
    # nearest gap for each transit to the union of windows (0 if inside one)
    gaps = []
    inside = 0
    si = 0
    for tts, _d in transits:
        while si < len(stops) and (stops[si]["close_ts"] or stops[si]["ts"]) < tts:
            si += 1                                  # advance to the first window that could contain/follow tts
        best = None
        for k in (si - 1, si):                       # nearest window is the one ending before, or the next one
            if 0 <= k < len(stops):
                lo, hi = stops[k]["ts"], (stops[k]["close_ts"] or stops[k]["ts"])
                g = 0.0 if lo <= tts <= hi else min(abs(tts - lo), abs(tts - hi))
                best = g if best is None else min(best, g)
        if best == 0.0:
            inside += 1
        elif best is not None:
            gaps.append(round(best, 2))
    gaps.sort()
    def _p(a, q):
        return round(a[min(len(a) - 1, int(q * len(a)))], 2) if a else None
    return {
        "n_windows": len(stops), "n_transits_in_era": n_tr, "n_transits_total": n_all,
        "excluded_out_of_era": excluded_out_of_era, "matched": matched,
        "join_rate_pct": round(100.0 * inside / n_tr, 1) if n_tr else None,
        "window_dur_s": {"median": _p(durs, 0.5), "p85": _p(durs, 0.85),
                         "min": (durs[0] if durs else None), "max": (durs[-1] if durs else None)},
        "miss_gap_s": {"n": len(gaps), "median": _p(gaps, 0.5), "p85": _p(gaps, 0.85),
                       "within_2s": sum(1 for g in gaps if g <= 2.0),
                       "beyond_10s": sum(1 for g in gaps if g > 10.0)},
        "reading": (
            "misses cluster within 2s of a window — window too narrow or mis-anchored (widen/re-anchor)"
            if gaps and sum(1 for g in gaps if g <= 2.0) >= 0.5 * len(gaps)
            else "misses scattered far from any window — genuinely missing door cycles (see DoorTracker census)"
            if gaps else "all transits joined"),
    }


def _door_transition_census_h3(db, gw, cam, era, _w, _wargs):
    """The same funnel for the STATE-ONLY engine, which has a different state machine.

    TWO THINGS DIFFER, and both change the arithmetic:

    1. NULL door_state IS A STATE HERE and the sequence keeps it. h3 maps its internal 'unknown' to
       NULL and enters it when it ABANDONS a descent past max_descent_s, emitting no cycle. The h2
       walk drops NULLs and then collapses runs, turning closing -> NULL -> closed into
       closing -> closed and counting an abandonment as a completed cycle. Measured on the live
       rows: 25 such patterns on ch27, 13 on ch30. That is why this is a separate function rather
       than a flag on the old one — the NULL filter is in the h2 SQL itself.

    2. THERE IS NO 'opening' STATE. h3 goes closed -> open directly, so the h2 funnel's first two
       stages (closed->opening, opening->open) are structurally zero and their yields are
       meaningless rather than bad. The h3 funnel starts at closed->open.

    A completed cycle is a transition into 'closed' from 'closing' or from 'open' — the same rule as
    _h3_cycle_ts, deliberately, so the funnel and the cycle count cannot disagree.
    """
    _ec, _eargs = _era_clause(era)
    rows = _q(db, "SELECT ts, door_state FROM gw_door_event WHERE gateway_id=? AND cam=? "
                  + _ec + _w + " ORDER BY ts, id",
              (gw, cam, *_eargs, *_wargs))
    if _GUARD_EPOCH is not None:
        rows = [r for r in rows if r["ts"] is not None and float(r["ts"]) >= _GUARD_EPOCH]
    NUL = "~null"
    seq = []
    for r in rows:                                   # collapse consecutive identical states
        st = r["door_state"] if r["door_state"] is not None else NUL
        if not seq or seq[-1] != st:
            seq.append(st)
    trans = {}
    for a, b in zip(seq, seq[1:]):
        trans[f"{a}->{b}"] = trans.get(f"{a}->{b}", 0) + 1
    opened = trans.get("closed->open", 0)
    began_closing = trans.get("open->closing", 0) + trans.get("closed->closing", 0)
    completed = trans.get("closing->closed", 0) + trans.get("open->closed", 0)
    reopened = trans.get("closing->open", 0)
    abandoned = trans.get(f"closing->{NUL}", 0)

    def _pct(a, b):
        return round(100.0 * a / b, 1) if b else None

    # CONSERVATION. Every entry into 'closing' must leave it exactly once — to closed, back to open,
    # or into the abandoned NULL. A mismatch means the walk lost a transition and no number below is
    # trustworthy, so it is reported rather than silently tolerated.
    exits = trans.get("closing->closed", 0) + reopened + abandoned
    balanced = (exits == began_closing)
    return {
        "era": era, "engine": "h3-state", "guard_boundary": (DOOR_GUARD_BOUNDARY or None),
        "state_runs": len(seq), "transitions": trans,
        "cycle_funnel": {
            # Keys kept identical to the h2 shape so the UI needs no branch. h3 has no 'opening',
            # so that stage is reported as its real value — zero — and named in the diagnosis
            # rather than left to read as a failure to open.
            "closed->opening": 0, "opening->open": 0,
            "open->closing": began_closing, "closing->closed (CYCLE)": completed},
        "losses": {
            "opening_never_confirmed": 0,
            "open_never_closed": max(0, opened - began_closing),
            "closing_never_completed": max(0, began_closing - completed - reopened - abandoned),
            "reopened_mid_close": reopened,
            "abandoned_over_max_descent": abandoned},
        "yield": {
            "open_confirm_rate_pct": None,                              # no 'opening' stage in h3
            "close_complete_rate_pct": _pct(completed, began_closing),
            "cycle_per_open_pct": _pct(completed, opened)},
        "opened_h3": opened,
        "conservation_balanced": balanced,
        "pairing_suspect": (not balanced),
        "diagnosis": (
            f"h3 STATE-ONLY: no 'opening' stage exists (closed -> open directly), so the first two "
            f"funnel numbers are structurally 0, not a fault. {completed} cycles, {reopened} reopens, "
            f"{abandoned} descents abandoned over max_descent_s. Travel is not measured by this "
            f"engine." if balanced else
            f"WALK UNBALANCED: {began_closing} entries into 'closing' but {exits} exits "
            f"({trans.get('closing->closed', 0)} closed / {reopened} reopened / {abandoned} "
            f"abandoned) — a transition was lost; treat these numbers as unreliable."),
    }


def _door_transition_census(db, gw, cam, era, t0=None, t1=None):
    """WHERE do this camera's door cycles die — walked from the stored door_state sequence, so it
    works on existing rows with no GPU change. The GPU DoorTracker moves closed -> opening -> open ->
    closing -> closed; a completed cycle is the full path, and close_travel is only emitted on
    closing -> closed. If ch16 reaches 'opening' but rarely 'open', near_open (0.90) is too high for
    its edge; if it reaches 'open'/'closing' but rarely 'closed', close_th (0.10) is too low. This
    tells which threshold to move instead of guessing.

    door_state changes are captured by the emit-on-change gate (a state change always changes the
    key), so consecutive rows with a state change are real transitions. Heartbeat re-emits of the
    SAME state are collapsed here, so a run of identical states counts as one occupancy, not many.
    """
    _w, _wargs = _ts_clause(t0, t1)
    if _is_h3_era(era):
        return _door_transition_census_h3(db, gw, cam, era, _w, _wargs)
    _ec, _eargs = _era_clause(era)
    rows = _q(db, "SELECT ts, door_state FROM gw_door_event WHERE gateway_id=? AND cam=? "
                  + _ec + " AND door_state IS NOT NULL" + _w + " ORDER BY ts, id",
              (gw, cam, *_eargs, *_wargs))
    # Same guard-regime cut as the close-travel pool: pre-guard transitions are the mispairing era's
    # artifacts; a funnel over them diagnoses a tracker that no longer runs.
    if _GUARD_EPOCH is not None:
        rows = [r for r in rows if r["ts"] is not None and float(r["ts"]) >= _GUARD_EPOCH]
    seq = []
    for r in rows:                                   # collapse consecutive identical states
        st = r["door_state"]
        if not seq or seq[-1] != st:
            seq.append(st)
    trans, reached = {}, {"opening": 0, "open": 0, "closing": 0}
    for a, b in zip(seq, seq[1:]):
        trans[f"{a}->{b}"] = trans.get(f"{a}->{b}", 0) + 1
        if b in reached:
            reached[b] += 1
    # cycle accounting from the transitions that matter
    opened = trans.get("closed->opening", 0)
    confirmed_open = trans.get("opening->open", 0)
    aborted_opening = trans.get("opening->closed", 0)     # blip: opened a crack, never fully
    began_closing = trans.get("open->closing", 0)
    completed = trans.get("closing->closed", 0)           # == a cycle (close_travel emitted)
    reopened = trans.get("closing->open", 0)
    def _pct(a, b):
        return round(100.0 * a / b, 1) if b else None
    return {
        "era": era, "guard_boundary": (DOOR_GUARD_BOUNDARY or None),
        "state_runs": len(seq), "transitions": trans,
        "cycle_funnel": {
            "closed->opening": opened, "opening->open": confirmed_open,
            "open->closing": began_closing, "closing->closed (CYCLE)": completed},
        "losses": {
            "opening_never_confirmed": aborted_opening,
            "open_never_closed": max(0, confirmed_open - began_closing - reopened),
            "closing_never_completed": max(0, began_closing - completed - reopened),
            "reopened_mid_close": reopened},
        "yield": {
            "open_confirm_rate_pct": _pct(confirmed_open, opened),      # opening -> open
            "close_complete_rate_pct": _pct(completed, began_closing),  # closing -> closed
            "cycle_per_open_pct": _pct(completed, opened)},             # end to end
        # A reopen (closing->open) legitimately mints an extra closing per cycle — obstruction
        # reopens run 30%+ here — so raw closings>openings is NOT a defect on its own. Suspect only
        # the excess reopens can't account for (2026-07-29 census: the old unadjusted flag fired on
        # three healthy-ish cameras at once).
        "pairing_suspect": (began_closing > opened + reopened or completed > confirmed_open + reopened),
        "diagnosis": (
            ("PAIRING SUSPECT: closings exceed openings BEYOND what reopens account for — spurious "
             "closing entries (threshold flap); close_travel unreliable pending the tracker "
             "hysteresis pass"
             if _GUARD_EPOCH is not None else
             "PAIRING SUSPECT beyond reopens: era may mix pre-time-guard rows — set "
             "DASH_DOOR_GUARD_TS to the 5f1488a deploy moment before concluding")
            if (began_closing > opened + reopened or completed > confirmed_open + reopened)
            else "few opening->open: near_open threshold too high for this edge" if opened and _pct(confirmed_open, opened) is not None and _pct(confirmed_open, opened) < 50
            else "few closing->closed: close_th too low, doors never read fully shut" if began_closing and _pct(completed, began_closing) is not None and _pct(completed, began_closing) < 50
            else "cycles completing normally" if completed else "no completed cycles — see the funnel"),
    }


def _is_h3_era(era):
    """Is this era prefix the state-only engine? door_version is '<hash><logic><tags>+<geom>' and
    _era_for hands back the part before the '+', so the logic tag is a substring test."""
    return bool(era) and "h3-state" in str(era)


H3_TRAVEL_NOTE = "h3: travel unmeasured by design"
# The home floor a round trip is measured from. G on this site; a lift whose lobby is not G would
# need this per camera, and would then need saying on the label — it changes what RTT MEANS.
RTT_HOME = os.environ.get("DASH_RTT_HOME", "G")
# RTT is walked on the request path, so it is capped and cached. The cap refuses rather than
# truncates: a partial walk drops round trips and reports a median from part of the window, which is
# worse than saying no.
RTT_MAX_ROWS = int(os.environ.get("DASH_RTT_MAX_ROWS", "120000"))
# DWELL VALIDATION: RUN, AND IT REFUTED THE PROPOSED FIX (2026-08-13).
#
# The census found 41-64% of door_state flips lasting under a second, and the obvious remedy was a
# minimum dwell before a transition into 'closed' counts as a cycle. tools/dwell_validation.py
# graded exactly that against the frame-anchored hand truth, replaying the real h3 tracker over
# ch27_clean and ch30_peak and running _h3_cycle_ts verbatim at 0/1/2/3s:
#
#   ch27  13 hand-timed closes:  dwell 0 -> 13 matched, 0 missed, 0 PHANTOM, 38 unmatched
#                                dwell 1 -> 11 matched, 2 MISSED       dwell 3 -> 5 matched, 8 MISSED
#   ch30   8 hand-timed closes:  dwell 0 ->  8 matched, 0 missed, 0 PHANTOM,  9 unmatched
#                                dwell 2 ->  5 matched, 3 MISSED       dwell 3 -> 0 matched, 8 MISSED
#
# ZERO PHANTOMS AT EVERY THRESHOLD: not one claimed cycle falls inside a window verified door-CLOSED,
# so the truth offers no evidence that any claim is false. And every threshold above 0 destroys real
# closes — REAL closes on this corpus have short preceding dwells. Dwell does not separate chatter
# from cycles here; it only separates cycles from nothing.
#
# So no dwell threshold is applied, on evidence rather than by default. What remains uncertain is
# the UNMATCHED claims (38 on ch27, 9 on ch30) — moments nobody hand-timed, which are unknown, not
# false. That is what this caveat now says, because "may include chatter" implied a remedy the
# corpus has ruled out.
H3_CYCLE_CAVEAT = ("cycle counts include transitions no hand-timed close corroborates "
                   "(ch27 38 of 51, ch30 9 of 17 on the corpus replay). NOT proven false — none "
                   "fall in a verified door-CLOSED window — and a minimum-dwell filter was tested "
                   "and rejected: every threshold above 0s destroyed real closes")
# One sentence, one place. Every surface that prints an h2-era travel figure prints this beside it.
H2_SUPERSEDED_NOTE = ("SUPERSEDED — h2 edge-column instrument invalidated 2026-08-05: offline replay "
                      "against hand-timed video showed it detects ~62% of real closes and emitted 41 "
                      "phantom cycles inside verified door-CLOSED windows. Not a current measurement")
UNCALIBRATED_NOTE = ("no door calibration on this camera — door cycles and close-travel are "
                     "UNAVAILABLE, not zero, until it is calibrated")
H3_TRAVEL_REASON = (
    "h3 is a state-only engine — it detects door cycles and does not time them. "
    "close_travel_s is NULL on every h3 cycle with a reason string, because three passes of TEST B "
    "failed to validate a travel estimator (README_DOORWATCH.md). Travel for these cameras comes "
    "from the weekly hand-timed sample, not from the engine.")


def _h3_cycle_ts(rows):
    """Timestamps of COMPLETED CYCLES in the h3 schema, walked from the door_state stream.

    DERIVED FROM THE ROWS, NOT FROM THE EMITTER. h3 writes no cycle marker column — close_travel_s
    is NULL on every row by design, which is exactly why the h2 rule ("close_travel_s NOT NULL")
    reports 0 cycles for a healthy engine. What a completed cycle looks like in the data is a
    transition INTO 'closed' from 'closing' (the normal path) or from 'open' (a close so fast that
    the emit-on-change gate never posted a 'closing' row).

    NULL door_state IS PART OF THE SEQUENCE AND MUST NOT BE FILTERED OUT. h3 maps its internal
    'unknown' to NULL on the wire, and it enters 'unknown' when it ABANDONS a descent that ran past
    max_descent_s — no cycle is emitted. That leaves 'closing' -> NULL -> 'closed' in the rows, and
    dropping the NULL collapses it to 'closing' -> 'closed', counting an abandoned descent as a
    cycle. Measured on the live rows for 2026-08-05T22:01 onward: ch27 25 such patterns against 208
    real cycles, ch30 13 against 176 — a 12% and 7% overcount. The existing funnel at _door_funnel
    collapses NULLs and has this bug; it is a separate view and is not touched here.

    Conservation check on the same rows, which is why this marker is trusted: exits from 'closing'
    (closed 208 + reopen 185 + abandoned 25 = 418) exactly equal entries to 'closing' (418) on ch27,
    and 251 = 251 on ch30. Nothing leaks.
    """
    out, prev = [], None
    for r in rows:
        st = r["door_state"] if r["door_state"] is not None else None
        if st == "closed" and prev in ("closing", "open"):
            out.append(r["ts"])
        prev = st
    return out


def _h3_reopens(rows):
    """'closing' -> 'open' transitions: the door started shutting and went back. Available for h3
    even though travel is not, and it is a real operational number the sheet never contemplated."""
    n, prev = 0, None
    for r in rows:
        st = r["door_state"] if r["door_state"] is not None else None
        if st == "open" and prev == "closing":
            n += 1
        prev = st
    return n


def _door_gpu_by_cam(db, gw, cams, t0=None, t1=None):
    """GPU-era close-travel, from gw_door_event COMPLETED CYCLES (close_travel_s not null), per camera
    and per that camera's own era. This is the live instrument; _door_by_cam is the retired Pi one.
    They are reported SEPARATELY and never merged — different sensors on different clocks.

    Distinguishes three states, because condition (b) hinges on it:
      no era rows          -> the door engine is not running / not configured for this camera
      era rows, 0 cycles   -> floor reads exist but no usable open->close pair — the REAL gap
      cycles               -> real close-travel numbers
    """
    out = {}
    for cam in cams:
        era, era_src = _era_for(db, gw, cam)
        if not era:
            out[cam] = {"era": None, "reason": "no gw_door_event rows in any era", "n_rows": 0,
                        "n_cycles": 0, "n": 0}
            continue
        w, wargs = _ts_clause(t0, t1)
        # NOTE the door_state IS NOT NULL filter below is the h2 path's. The h3 path re-reads
        # WITHOUT it, because for h3 a NULL state is a meaningful element of the sequence — see
        # _h3_cycle_ts. Filtering it there would count abandoned descents as cycles.
        if _is_h3_era(era):
            _ec, _eargs = _era_clause(era)
            h3rows = _q(db, "SELECT ts, door_state, close_travel_s ct FROM gw_door_event "
                            "WHERE gateway_id=? AND cam=?" + _ec
                            + w + " ORDER BY ts, id", (gw, cam, *_eargs, *wargs))
            cyc_ts = _h3_cycle_ts(h3rows)
            reopens = _h3_reopens(h3rows)
            spec = DOOR_SPECS.get(cam)
            # The spec is carried so a spec'd camera still gets its compliance line, but pct_exceed
            # is None and MUST stay None: there is no travel to exceed a threshold with. A 0 here
            # would read as "never exceeds", which is a claim this engine cannot make.
            out[cam] = {
                "era": era, "era_source": era_src,
                "instrument": "GPU door engine (gw_door_event) — h3 STATE-ONLY",
                "n_rows": len(h3rows), "n_cycles": len(cyc_ts), "n": 0,
                "travel_unmeasured": True, "travel_note": H3_TRAVEL_NOTE,
                "cycles_provisional": True, "cycle_caveat": H3_CYCLE_CAVEAT,
                "pool": "completed cycles (state transitions; travel not measured)",
                "median": None, "p85": None, "min": None, "max": None,
                "hist": None, "hist_edges": _HIST_EDGES,
                "spec": ({**spec, "pct_exceed": None} if spec else None),
                "n_flap_excluded": 0, "n_reopened_excluded": 0,
                "reopen_rate_pct": (round(100.0 * reopens / (len(cyc_ts) + reopens))
                                    if (len(cyc_ts) + reopens) else None),
                "reopened_median": None, "n_reopens": reopens,
                "measurement_suspect": False, "n_impossible": 0,
                "plausible_n": 0, "plausible_median": None, "plausible_p85": None,
                "guard_boundary": None, "n_preguard_excluded": 0,
                "reason": H3_TRAVEL_REASON,
            }
            continue
        _ec, _eargs = _era_clause(era)
        rows = _q(db, "SELECT ts, door_state, close_travel_s ct FROM gw_door_event "
                      "WHERE gateway_id=? AND cam=?" + _ec + " AND door_state IS NOT NULL"
                      + w + " ORDER BY ts, id", (gw, cam, *_eargs, *wargs))
        # GUARD-REGIME CUT. The 5f1488a time-guards changed what gets emitted without moving the era,
        # so pre-guard mispairings share the era with clean rows. With DASH_DOOR_GUARD_TS set, the
        # quotable pool is post-guard rows only; the excluded count stays visible, never silent.
        n_preguard = 0
        if _GUARD_EPOCH is not None:
            n_preguard = sum(1 for r in rows if r["ct"] is not None
                             and (r["ts"] is None or float(r["ts"]) < _GUARD_EPOCH))
            rows = [r for r in rows if r["ts"] is not None and float(r["ts"]) >= _GUARD_EPOCH]
        # FLAP-AWARE CYCLE CLASSIFICATION (2026-07-29 census finding: the reopen-split alone still
        # let flap-born cycles poison the clean pool — ch29 "clean" median 0.08s, sub-frame). Walk
        # the state stream, classify each completed close:
        #   FLAP     any closing->open bounce under DASH_FLAP_GAP_S inside the cycle, or the open
        #            dwell before closing was under DASH_MIN_OPEN_DWELL_S (the closing began from a
        #            state that was never really open) -> excluded, counted
        #   REOPENED a real (>= flap-gap) closing->open obstruction reopen -> excluded from the
        #            sheet-comparable headline (the sheet assumes unobstructed closes), reported —
        #            the reopen RATE is itself a finding the sheet never contemplated
        #   CLEAN    everything else -> the quotable pool
        min_dwell = float(os.environ.get("DASH_MIN_OPEN_DWELL_S", "1.5"))
        flap_gap = float(os.environ.get("DASH_FLAP_GAP_S", "1.0"))
        seq = []
        for r in rows:
            if not seq or seq[-1][1] != r["door_state"]:
                seq.append([float(r["ts"]), r["door_state"], r["ct"]])
            elif r["ct"] is not None and seq[-1][2] is None:
                seq[-1][2] = r["ct"]
        clean, reopened_cts, flap_cts = [], [], []
        open_since = None
        flap_w = reopen_w = False
        dwell = None
        for k, (t, st, ct) in enumerate(seq):
            prev = seq[k - 1][1] if k else None
            if st == "opening" and prev == "closed":
                flap_w = reopen_w = False
                open_since = None
                dwell = None
            elif st == "open":
                open_since = t
                if prev == "closing":
                    if (t - seq[k - 1][0]) < flap_gap:
                        flap_w = True
                    else:
                        reopen_w = True
            elif st == "closing" and prev == "open":
                dwell = (t - open_since) if open_since is not None else None
                if dwell is not None and dwell < min_dwell:
                    flap_w = True
            elif st == "closed" and prev == "closing":
                v = seq[k - 1][2] if seq[k - 1][2] is not None else ct
                if v is not None and float(v) > 0:
                    v = float(v)
                    if flap_w:
                        flap_cts.append(v)
                    elif reopen_w:
                        reopened_cts.append(v)
                    else:
                        clean.append(v)
                flap_w = reopen_w = False
                dwell = None
        all_cts = sorted(clean + reopened_cts + flap_cts)
        PLAUS_LO, PLAUS_HI = 0.3, 30.0
        cts = sorted(v for v in clean if PLAUS_LO <= v <= PLAUS_HI)   # THE quotable pool
        impossible = [v for v in clean if v < PLAUS_LO or v > PLAUS_HI]
        plaus = [v for v in all_cts if PLAUS_LO <= v <= PLAUS_HI]
        n = len(cts)
        # An impossible value surviving the flap filter AND the guards is a real, current defect.
        suspect = (len(impossible) > 0)
        spec = DOOR_SPECS.get(cam)
        # Carry the spec even at n=0, so a spec'd camera ALWAYS gets a compliance line — a gap must be
        # visible on the headline, not silently omitted (condition b). pct_exceed only when there's data.
        spec_out = None
        if spec:
            over = sum(1 for v in cts if v > spec["compliance_s"])
            spec_out = {**spec, "pct_exceed": (round(100.0 * over / n) if n else None)}
        out[cam] = {
            "era": era, "era_source": era_src, "instrument": "GPU door engine (gw_door_event)",
            "n_rows": len(rows), "n_cycles": len(all_cts), "n": n,
            "pool": "clean cycles (flap + reopen excluded)",
            "median": round(_pctl(cts, 0.5), 2) if n else None,
            "p85": round(_pctl(cts, 0.85), 2) if n else None,
            "min": round(cts[0], 2) if n else None, "max": round(cts[-1], 2) if n else None,
            "hist": _hist(cts), "hist_edges": _HIST_EDGES, "spec": spec_out,
            "n_flap_excluded": len(flap_cts), "n_reopened_excluded": len(reopened_cts),
            "reopen_rate_pct": (round(100.0 * len(reopened_cts) / len(all_cts))
                                if all_cts else None),
            "reopened_median": (round(_pctl(sorted(reopened_cts), 0.5), 2) if reopened_cts else None),
            "measurement_suspect": suspect, "n_impossible": len(impossible),
            "plausible_n": len(plaus),
            "plausible_median": (round(_pctl(plaus, 0.5), 2) if plaus else None),
            "plausible_p85": (round(_pctl(plaus, 0.85), 2) if plaus else None),
            "guard_boundary": (DOOR_GUARD_BOUNDARY or None),
            "n_preguard_excluded": n_preguard,
            "reason": (None if n else "era rows exist but NO clean completed cycle "
                       "(all cycles flap/reopen-classed, or no usable pairs) — see the funnel")}
    return out


def _transfer_by_cam(db, gw):
    """C26 passenger transfer: (door_close_start - door_open_full) / (boarded+alighted), per person,
    on rows where boarded/alighted are non-NULL. PROVISIONAL — depends on the transit counts (ch29 at
    80% precision on n=66, re-validating under 11m), so it's only firm once the camera goes live."""
    rows = _q(db, "SELECT s.camera cam, e.door_open_full_ts of, e.door_close_start_ts cs, "
                  "e.boarded b, e.alighted a FROM gw_event e JOIN gw_source s ON s.id=e.source_id "
                  "WHERE s.gateway_id=? AND e.boarded IS NOT NULL AND e.alighted IS NOT NULL", (gw,))
    by = {}
    for r in rows:
        load = (r["b"] or 0) + (r["a"] or 0)
        if load <= 0:
            continue
        o, c = _epoch(r["of"]), _epoch(r["cs"])
        if o is None or c is None or c <= o:
            continue
        by.setdefault(r["cam"], []).append((c - o) / load)
    return {cam: {**_stats(v), "provisional": True} for cam, v in by.items()}


def _floor_coverage(db, gw):
    """How many gw_event rows carry a floor label. NULL on all of them => the whole Tier-2 / per-floor
    family (stops-per-floor, C17/C18 up-down, C21/C22 speed factors) is NOT AVAILABLE — say so, loudly."""
    rows = _q(db, "SELECT COUNT(*) t, COUNT(e.floor) f FROM gw_event e "
                  "JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?", (gw,))
    if not rows:
        return {"total": 0, "with_floor": 0}
    return {"total": rows[0]["t"] or 0, "with_floor": rows[0]["f"] or 0}


# ══════════════════════════════════════════════════════════════════════════════
# TIER-2 from gw_door_event — stops, up/down (C17/C18), speed factors (C21/C22),
# and per-floor demand. Everything here is gated on ONE era and ONE quality bar,
# and every number it emits carries n + the era it was measured under.
# ══════════════════════════════════════════════════════════════════════════════

# ERA. door_version is `templates_hash[:8] + "+" + geometry_hash[:8]` (gpu_analyze.build_door_engine)
# — a CONTENT HASH, so ">=" over it is meaningless: hashes have no order, and a rebuild of the same
# templates can sort either side of any bound. The era is therefore an explicit PREFIX match on the
# templates half. Reads from any other template set are a different instrument and are excluded, not
# ranked. Change this constant (and say so on the panel) when a rebuild opens a new era.
# ERA IS PER CAMERA. This was a single global prefix defaulted to ch29's templates hash, which
# silently filtered out every other camera: ch16 runs e79e50d3+495e8f48 (its own templates, fetched
# correctly by the fleet — right design, wrong filter), so its reads existed and the dash showed
# nothing. A camera's era is a property of the camera.
#
# DASH_DOOR_ERA accepts:
#   ""/"auto"                  per-camera, from the newest door_version that camera has posted
#   "f7b2c37e"                 one prefix for every camera (the old behaviour, for pinning)
#   "ch29=f7b2c37e,ch16=e79e"  explicit per camera
# "auto" is the default because the truth already lives in the stream — every row is version-stamped,
# so the current era can be read rather than configured, and a rebuild moves the era by itself.
DOOR_ERA = os.environ.get("DASH_DOOR_ERA", "auto")

# QUALITY BAR. DoorFloorEngine.process emits reason ∈ ok | single_panel | disagree | ambiguous | no_read.
#   ok           = two panels read the same floor (agree-or-discard)
#   single_panel = one panel configured; read succeeded, no cross-check available
# ch29 currently runs SINGLE-PANEL (PANEL1_DIGIT_CELLS/PANEL1_ARROW_CELL are not calibrated), so
# EVERY good read it has ever produced is 'single_panel' and NONE are 'ok'. Filtering to reason='ok'
# alone would return zero rows and render as "no data" — indistinguishable from a dead camera. Both
# count as confident; the per-reason census below is published so the distinction stays visible.
DOOR_OK_REASONS = ("ok", "single_panel")

# Floor label -> physical index, for speed. Numeric labels map by int(); anything else (G, LG, MEP,
# P3) needs a declared order or it is EXCLUDED from speed — never guessed at.
FLOOR_ORDER = [s.strip() for s in os.environ.get("DASH_FLOOR_ORDER", "").split(",") if s.strip()]
# Per-camera floor whitelist, applied at READ TIME so the 262 impossible-floor rows already stored
# (167, 133, "7G") stop polluting the heatmap and C21/C22 immediately — the source fix in gpu_door
# MANUAL OVERRIDE ONLY. The alphabet is now DERIVED from evidence per camera+era (see
# _derive_floor_alphabet); this is the escape hatch for the day the derivation is wrong. When set it
# WINS. Format mirrors DASH_DOOR_ERA: "ch16=P3,P2,P1,G,1,...,26;ch29=..." per camera, or a single
# comma list for every camera. Empty (the default) = use the derived alphabet.
DASH_FLOOR_ALPHABET = os.environ.get("DASH_FLOOR_ALPHABET", "")


def _floor_alphabet(cam):
    """The set of floors this camera's tower actually has, or None for 'no whitelist'."""
    spec = DASH_FLOOR_ALPHABET.strip()
    if not spec:
        return set(FLOOR_ORDER) or None          # DASH_FLOOR_ORDER doubles as a global alphabet
    if "=" in spec:
        for part in spec.split(";"):
            k, _, v = part.partition("=")
            if k.strip() == cam and v.strip():
                return {f.strip() for f in v.split(",") if f.strip()}
        return None                              # a per-cam map that omits this cam -> no whitelist
    return {f.strip() for f in spec.split(",") if f.strip()}
# Plausibility ceiling for a floors/second segment. The Jul-21 live gate found real OCR slips (1→G,
# 7→77); a 7→77 misread manufactures a 70-floor "move" in seconds. Such segments are DISCARDED and
# COUNTED (never clamped — a clamped outlier is a fabricated measurement).
MAX_FLOORS_PER_S = float(os.environ.get("DASH_MAX_FLOORS_PER_S", "3.0"))
DOOR_ATTR_S = float(os.environ.get("DASH_DOOR_ATTR_S", "10"))       # how far back a stop may borrow a floor
DOOR_OPEN_MAX_S = float(os.environ.get("DASH_DOOR_OPEN_MAX_S", "60"))  # cap on an unterminated open window


def _ist_hour(ts):
    """Hour-of-day on the building's clock. Door/transit ts are absolute epoch, and the heatmap is
    read by people who think in local time, so the bucketing has to be IST — not UTC."""
    try:
        return datetime.fromtimestamp(ts, IST).hour
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _expected_era(gw, cam):
    """The era the last BUILD produced, from _calib_build.json — plain JSON, no numpy needed here.

    Observed (what a worker reports) is the right thing to FILTER by: it is what actually produced
    the rows. Expected is the right thing to CHECK against: if a worker is still running templates
    from before the last build, its reads are real but stale, and charting them as current is how a
    known-bad geometry keeps looking fine.
    """
    try:
        d = json.loads((CALIB_DIR / gw / cam / "_calib_build.json").read_text())
    except (OSError, ValueError):
        return None, None
    era = d.get("era") or (str(d.get("templates_hash") or "")[:8] or None)
    return (era or None), d.get("built_at")


def _era_for(db, gw, cam, override=""):
    """The templates-hash prefix to filter this camera's door rows by, and where it came from.

    Auto-resolution takes the newest row's door_version — a rebuild therefore moves the era on its
    own, which is correct: the new templates ARE a new instrument. The panel prints whichever era
    was used, so an auto-resolved era is never silent.

    override (2026-07-30): a REQUEST-level era selection, same format as DASH_DOOR_ERA ("cam=era,..."
    or a single era). Auto-newest made yesterday's h2 rollover HIDE a week of history on every camera
    — 41,789 pre-h2 ch16 rows unreachable from the UI read as data loss. The selector makes viewing
    an old era a deliberate act; it changes only what THIS response aggregates, never what is stored."""
    ov = (override or "").strip()
    if ov and ov != "auto":
        if "=" in ov:
            for part in ov.split(","):
                k, _, v = part.partition("=")
                if k.strip() == cam and v.strip():
                    return v.strip(), "selected in the UI (per-camera)"
            # a per-cam selection that omits this cam falls through to the normal resolution —
            # picking an era for ch16 must not blank every other camera's panel
        else:
            return ov, "selected in the UI"
    spec = (DOOR_ERA or "auto").strip()
    if spec and spec != "auto":
        if "=" in spec:
            for part in spec.split(","):
                k, _, v = part.partition("=")
                if k.strip() == cam and v.strip():
                    return v.strip(), "pinned per-camera"
            return None, "no pin for this camera"        # explicit map that omits the cam
        return spec, "pinned (all cameras)"
    # Newest by EVENT TIME, not by insert order. Ordering by id would let a late-arriving or
    # retried row from a previous era redefine the current one — a stale POST landing after a
    # rebuild would silently roll the whole dash back to the old templates.
    rows = _q(db, "SELECT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? "
                  "AND door_version IS NOT NULL AND door_version<>'' AND ts IS NOT NULL "
                  "ORDER BY ts DESC LIMIT 1", (gw, cam))
    if not rows:
        return None, "no door rows for this camera"
    dv = str(rows[0]["door_version"])
    return dv.split("+")[0], "auto (newest door_version)"


def _floor_idx(label):
    if label is None:
        return None
    s = str(label).strip()
    if FLOOR_ORDER and s in FLOOR_ORDER:
        return FLOOR_ORDER.index(s)
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _labels_evidence(gw, cam):
    """Human-verified evidence from labels.json: the GLYPH set (distinct chars the operator confirmed,
    arrows stripped) and the LABELED floor strings themselves (each label IS a real floor the human
    saw). This is the ground truth non-numeric floors (G, P3, MEP) rest on — you cannot corroborate a
    letter floor by numeric transition, so the human label is its evidence."""
    try:
        d = json.loads((CALIB_DIR / gw / cam / "labels.json").read_text())
    except (OSError, ValueError):
        return set(), set()
    glyphs, floors = set(), set()
    for v in (d or {}).values():
        f = str(v or "").strip().rstrip("^vV")       # drop the direction arrow
        if not f or f == "-":
            continue
        floors.add(f)
        glyphs.update(f)
    return glyphs, floors


def _glyph_image(a, b):
    """True when one floor string is reachable from the other by ONE systematic misread: a single
    same-position glyph substitution (19->79) or a spurious leading '1' (29->129) — the two failure
    shapes the ch16 flip data actually shows. Longer edits are not images; they stay out of the
    shadow machinery and must earn rejection on their own evidence."""
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if len(a) == len(b) + 1:
        return a == "1" + b
    if len(b) == len(a) + 1:
        return b == "1" + a
    return False


def _derive_floor_alphabet(rows, gw_cam_labels, min_sightings=3, max_fps=None):
    """Derive the valid floor set from EVIDENCE, not a typed list (the ask). A floor string is admitted
    when it corroborates — never on mere occurrence, which is circular (a misread would whitelist
    itself). Admission = all glyphs human-verified AND one of:
      (a) it is a human-LABELED floor (labels.json) — direct evidence, covers non-numeric floors, or
      (b) it is numeric with >= min_sightings confident sightings AND transition support: at least once
          it sat next to a temporal-neighbour read reachable at <= max_fps floors/sec (i.e. it appears
          inside a sequential run, not as a teleport). 129 between two 19s is 110 floors in one read
          interval -> no support -> rejected; the phantom-hundreds rule falls straight out of this.

    v2 (2026-07-28): transition support alone is defeated by SYSTEMATIC misreads — a stable
    single-glyph confusion maps a real run onto a well-formed image run (19->18->17 read as
    79->78->77), which supplies its own internal support. Two rules use evidence from OUTSIDE the
    run's internal structure:
      (c) ANCHORED SUPPORT: corroboration only counts if the floor's component — over short
          plausible-speed edges between consecutive reads — reaches a human-labeled floor. A shadow
          band is an island: every edge into the real graph is a teleport.
      (d) FLIP-KILL: F<->X alternation between glyph-image floors at a physically impossible speed
          AND an implausible floor gap (adjacent floors are images too — travel increments are not
          flips) is confusion caught in the act; >=2 such flips quarantine the lower-confidence
          member — but only toward a twin that is itself admitted, evaluated to fixpoint so kills
          never cascade off already-dead floors. Twin recorded.
    Both are QUARANTINE, not verdicts: detail carries twin/anchored, and stronger later evidence (a
    labeled crop, a strong-margin read) re-admits on the next derive.
    Returns (admitted:set, detail:{floor: {...}}). max_fps defaults to MAX_FLOORS_PER_S.
    """
    if max_fps is None:
        max_fps = MAX_FLOORS_PER_S
    glyph_set, labeled = gw_cam_labels
    seq = [(r["ts"], str(r["floor"])) for r in rows
           if r["floor"] is not None and r["reason"] in DOOR_OK_REASONS]
    seen = {}
    for ts, f in seq:
        d = seen.setdefault(f, {"n": 0, "first": ts, "last": ts, "supported": False})
        d["n"] += 1
        d["first"] = min(d["first"], ts)
        d["last"] = max(d["last"], ts)
    # transition support (numeric floors only): reachable from a temporal neighbour at plausible speed
    for i, (t, f) in enumerate(seq):
        fi = _floor_idx(f)
        if fi is None:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(seq):
                tj, fj = seq[j]
                fji = _floor_idx(fj)
                if fji is not None and abs(t - tj) > 0 and abs(fi - fji) / abs(t - tj) <= max_fps:
                    seen[f]["supported"] = True
                    break
    # ── v2: anchored components + impossible-speed flips (rules, no floor lists) ─────────────────
    # Component edges demand BOTH plausible speed AND a short gap: consecutive floor-bearing reads
    # during real travel are seconds apart (emit-on-change fires per floor), so a pair spanning a
    # long gap is a data gap, not an observed traversal — without the gap cap, an overnight
    # 162->17 pair at 8h would "plausibly" weld the shadow island onto the real graph.
    edge_window = float(os.environ.get("DASH_ALPHA_EDGE_WINDOW_S", "15"))
    flip_window = float(os.environ.get("DASH_ALPHA_FLIP_WINDOW_S", "45"))
    # A flip is only confusion evidence when the floor gap is IMPLAUSIBLE. Adjacent floors are
    # single-glyph images too (21/22, 28/29), and a panel incrementing during fast travel produces
    # 1 floor in <0.33s = ">max_fps" — the 2026-07-28 over-fire killed real 11/21/25/26/28 that way.
    flip_min_gap = float(os.environ.get("DASH_ALPHA_FLIP_MIN_GAP", "5"))
    parent = {f: f for f in seen if _floor_idx(f) is not None}

    def _root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    flips = {}
    for i in range(len(seq) - 1):
        (t1, f1), (t2, f2) = seq[i], seq[i + 1]
        if f1 == f2:
            continue
        i1, i2 = _floor_idx(f1), _floor_idx(f2)
        if i1 is None or i2 is None:
            continue
        dt = t2 - t1
        if dt <= 0:
            continue
        if abs(i1 - i2) / dt <= max_fps:
            if dt <= edge_window:
                ra, rb = _root(f1), _root(f2)
                if ra != rb:
                    parent[ra] = rb
        elif dt <= flip_window and abs(i1 - i2) >= flip_min_gap and _glyph_image(f1, f2):
            for a, b in ((f1, f2), (f2, f1)):
                fd = flips.setdefault(a, {"twin": b, "n": 0})
                if fd["twin"] == b:
                    fd["n"] += 1
    anchor_roots = {_root(f) for f in labeled if f in parent}
    conf_sum = {}
    for r in rows:
        if r["floor"] is not None and r["reason"] in DOOR_OK_REASONS and r["read_conf"] is not None:
            s = conf_sum.setdefault(str(r["floor"]), [0.0, 0])
            s[0] += r["read_conf"]
            s[1] += 1

    def _mean_conf(f):
        s = conf_sum.get(f)
        return s[0] / s[1] if s and s[1] else None

    # Admission. The glyph set is NOT a hard gate: a sparse label sample omits digits that real floors
    # use (labels 12,19,20 never show an 8, yet floor 18 is real), and the reader can only emit glyphs
    # it has templates for anyway. Corroboration — numeric run + sightings — is the stronger evidence.
    # So: labeled floors are trusted directly; numeric floors admit on corroboration; a non-numeric
    # floor the human never labeled has no corroboration path (letters can't be placed on the number
    # line) and is rejected — which is exactly what kills 7G while keeping a labeled G.
    # anchor_roots empty (no labeled floor appears in this era's numeric reads) disables the anchor
    # rule rather than rejecting everything — a failsafe, and the panel's via strings make it visible.
    admitted, detail = set(), {}
    comp_size = {}
    for f in parent:
        r = _root(f)
        comp_size[r] = comp_size.get(r, 0) + 1
    for f, d in seen.items():
        is_labeled = f in labeled
        numeric = _floor_idx(f) is not None
        anchored = (not anchor_roots) or (f in parent and _root(f) in anchor_roots)
        # Island evidence needs an island: a size-1 component has no mutually-corroborating members
        # to convict it, so a lone sparse floor (57 with no <=15s neighbour) falls back to plain
        # corroboration instead of dying unanchored. Real shadow bands cluster (77-79, 12x, 16x).
        lone = numeric and f in parent and comp_size.get(_root(f), 1) <= 1
        if is_labeled:
            via = "labeled"; admitted.add(f)
        elif numeric and d["n"] >= min_sightings and d["supported"] and not anchored and not lone:
            via = "quarantine:unanchored_island (no plausible-speed path to a labeled floor)"
        elif numeric and d["n"] >= min_sightings and d["supported"]:
            via = "corroborated" if anchored else "corroborated (lone component — island rule n/a)"
            admitted.add(f)
        elif numeric and not d["supported"]:
            via = "reject:no_transition_support (teleport/phantom cell)"
        elif numeric and d["n"] < min_sightings:
            via = f"reject:only_{d['n']}_sightings (<{min_sightings})"
        else:
            via = "reject:non_numeric_unlabeled (no corroboration path)"
        detail[f] = {"n": d["n"], "first": d["first"], "last": d["last"],
                     "supported": d["supported"], "admitted": f in admitted, "via": via}
        if numeric:
            detail[f]["anchored"] = anchored
    # Flip-kill AFTER base admission, to fixpoint: a floor dies only toward a twin that is ITSELF
    # currently admitted (the 2026-07-28 over-fire chained kills off already-dead floors: 25 died
    # as shadow of a 26 that was itself quarantined) and only with strictly lower mean confidence
    # (labeled floors never die). Per round, defer any kill whose twin is also up for killing this
    # round — the chain's top dies first and its dependents re-evaluate against the survivors.
    while True:
        kills = []
        for f in admitted:
            if f in labeled:
                continue
            fl = flips.get(f)
            if not fl or fl["n"] < 2:
                continue
            tw = fl["twin"]
            if tw not in admitted:
                continue
            cf, ct = _mean_conf(f), _mean_conf(tw)
            if cf is not None and ct is not None and cf < ct:
                kills.append((f, tw, fl["n"]))
        pending = {k[0] for k in kills}
        apply_now = [(f, tw, n) for f, tw, n in kills if tw not in pending]
        if not apply_now:
            break
        for f, tw, n in apply_now:
            admitted.discard(f)
            detail[f]["admitted"] = False
            detail[f]["via"] = f"quarantine:glyph_shadow_of_{tw} ({n} impossible-speed flips)"
            detail[f]["twin"] = tw
    # Label upgrade: an island-quarantined floor with qualifying flip evidence against an ADMITTED
    # twin gets the more specific glyph_shadow label (same quarantine, better diagnostics + twin).
    for f, dd in detail.items():
        fl = flips.get(f)
        if (dd["via"].startswith("quarantine:unanchored_island") and fl and fl["n"] >= 2
                and fl["twin"] in admitted):
            cf, ct = _mean_conf(f), _mean_conf(fl["twin"])
            if cf is not None and ct is not None and cf < ct:
                dd["via"] = f"quarantine:glyph_shadow_of_{fl['twin']} ({fl['n']} impossible-speed flips)"
                dd["twin"] = fl["twin"]
    return admitted, detail


# ── era census, cached per (gw, cam) ──────────────────────────────────────────
# This is the "every era this camera ever wrote" list behind the era selector. It CANNOT be
# windowed: its whole job is to surface eras the auto-newest default would hide (the 41,789 pre-h2
# ch16 rows that read as data loss), so a 7-day bound would defeat it. But it is a GROUP BY over
# every row the camera has ever written — with a TEMP B-TREE — on every page load, per camera.
#
# It is also almost perfectly cacheable: the answer only changes when a NEW era appears, which is a
# rebuild-sized event, not a per-second one. So compute it at most once per _CENSUS_TTL.
#
# Bounded key space, deliberately: `gw` is a path parameter, so a caller controls part of the key.
# An unbounded dict here would be a memory leak reachable from outside — the same hole that had to
# be closed on the heavy cache. Newest _CENSUS_MAX entries are kept; a dropped entry costs one
# recompute, never a wrong answer.
_CENSUS_TTL = float(os.environ.get("DASH_CENSUS_TTL_S", "900"))     # 15 min
_CENSUS_MAX = 32
_census_lock = threading.Lock()
_census_cache = {}                                  # (gw, cam) -> {"t": epoch, "v": [...]}


def _era_census(db, gw, cam):
    """-> (eras_list, computed_at). Newest-era-first list of {era, rows, first, last}."""
    key = (gw, cam)
    with _census_lock:
        hit = _census_cache.get(key)
        if hit and (time.time() - hit["t"]) < _CENSUS_TTL:
            return hit["v"], hit["t"]
        # Computed INSIDE the lock so concurrent requests wait for one result instead of each
        # starting its own full-table GROUP BY — the stacking that produced the OOM.
        acc = {}
        for r in _q(db, "SELECT door_version dv, COUNT(*) n, MIN(ts) t0, MAX(ts) t1 "
                        "FROM gw_door_event WHERE gateway_id=? AND cam=? "
                        "AND door_version IS NOT NULL AND door_version<>'' "
                        "GROUP BY door_version", (gw, cam)):
            pfx = str(r["dv"]).split("+")[0]
            e = acc.setdefault(pfx, {"era": pfx, "rows": 0, "first": r["t0"], "last": r["t1"]})
            e["rows"] += r["n"]
            e["first"] = min(e["first"], r["t0"])
            e["last"] = max(e["last"], r["t1"])
        v = sorted(acc.values(), key=lambda e: (e["last"] or 0), reverse=True)
        t = time.time()
        _census_cache[key] = {"t": t, "v": v}
        if len(_census_cache) > _CENSUS_MAX:
            for k, _ in sorted(_census_cache.items(), key=lambda kv: kv[1]["t"])[:-_CENSUS_MAX]:
                _census_cache.pop(k, None)
        return v, t


# ── per-camera aggregates: computed on a SCHEDULE, served from a table ────────
# The last thing on the request path. door_gpu and tier2 walk a camera's era rows in Python — the
# flap/reopen state machine and the stop walk are SEQUENTIAL, so they do not reduce to SQL
# aggregates, and windowing does not help: gw_door_event spans ~15 days, so a 7-day bound is 87% of
# the table and a sweep from days=7 down to days=0.1 (70x less data) changed the outcome not at all.
# So the walk moves off the request path entirely, the same way the alphabet did.
#
# ERA HYGIENE IS THE POINT, not an extra. The key is (gateway_id, cam, counting_version,
# door_version) and a read must match the CURRENT values exactly. An aggregate computed under a
# different door engine or a different counting version describes a different instrument; serving
# it because the camera name matches would be a silent lie. A key mismatch reads as "not computed
# for the current era", never as data.
#
# The window is part of the contract too: an aggregate is computed for ONE window, so a request for
# a different one is reported as not-precomputed rather than served from the wrong range. A custom
# window can never trigger a live walk — that is exactly the hang coming back.
_AGG_TABLE_READY = set()


def _aggregate_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS door_aggregate (
        gateway_id TEXT, cam TEXT,
        counting_version TEXT,      -- '' when the analyser has not reported one
        door_version TEXT,          -- FULL door_version, not the era prefix
        window_days REAL,           -- the window this aggregate describes
        door_gpu TEXT,              -- JSON: _door_gpu_by_cam's entry for this cam
        tier2 TEXT,                 -- JSON: _tier2's return, or NULL when it had nothing to say
        computed_at REAL,
        source_rows INTEGER,        -- rows the walk consumed, so a thin aggregate is visible
        compute_ms INTEGER,
        PRIMARY KEY (gateway_id, cam, counting_version, door_version, window_days))""")
    # RTT joins door_gpu and tier2 as a third precomputed payload on the SAME era key. It shipped
    # first as a live walk on the request path and reintroduced exactly the hang the paragraph above
    # exists to prevent: /trends went to 152.8 s and /data to 24.5 s on the live box.
    # ALTER, not a recreate: the table is already on every gateway and holds the only record of what
    # the fleet looked like in retired eras.
    cols = {r[1] for r in db.execute("PRAGMA table_info(door_aggregate)")}
    if "rtt" not in cols:
        db.execute("ALTER TABLE door_aggregate ADD COLUMN rtt TEXT")
    if "rtt_ms" not in cols:
        db.execute("ALTER TABLE door_aggregate ADD COLUMN rtt_ms INTEGER")


def _current_keys(db, gw, cam):
    """(counting_version, door_version) as they are RIGHT NOW — the era-hygiene key."""
    dv = db.execute("SELECT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? "
                    "AND door_version IS NOT NULL AND door_version<>'' AND ts IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1", (gw, cam)).fetchone()
    cv = db.execute("SELECT counting_version FROM analyzer_status WHERE gateway_id=? AND cam=?",
                    (gw, cam)).fetchone()
    return ((cv["counting_version"] if cv and cv["counting_version"] else ""),
            (dv["door_version"] if dv else None))


def _aggregate_read(db, gw, cam, window_days):
    """Serve the stored aggregate. NEVER computes. -> (door_gpu|None, tier2|None, meta).

    meta['state'] is always set and is what distinguishes "not computed yet" from a real zero."""
    cv, dv = _current_keys(db, gw, cam)
    if dv is None:
        return None, None, {"state": "no door rows for this camera in any era",
                            "computed_at": None, "current_door_version": None}
    try:
        r = db.execute("SELECT door_gpu, tier2, computed_at, source_rows, compute_ms "
                       "FROM door_aggregate WHERE gateway_id=? AND cam=? AND counting_version=? "
                       "AND door_version=? AND window_days=?",
                       (gw, cam, cv, dv, float(window_days))).fetchone()
    except sqlite3.OperationalError:
        return None, None, {"state": "not yet computed — the precompute job has never run",
                            "computed_at": None, "current_door_version": dv}
    if not r:
        return None, None, {
            "state": "not yet computed for the current era/window",
            "computed_at": None, "current_counting_version": cv, "current_door_version": dv,
            "window_days": window_days,
            "detail": "an aggregate exists only for the era, counting version and window it was "
                      "computed under; nothing is served across those boundaries"}
    try:
        dg = json.loads(r["door_gpu"]) if r["door_gpu"] else None
        t2 = json.loads(r["tier2"]) if r["tier2"] else None
    except (ValueError, TypeError):
        return None, None, {"state": "stored aggregate unreadable", "computed_at": r["computed_at"]}
    return dg, t2, {"state": "ok", "computed_at": r["computed_at"],
                    "age_s": (round(time.time() - r["computed_at"], 1) if r["computed_at"] else None),
                    "source_rows": r["source_rows"], "compute_ms": r["compute_ms"],
                    "counting_version": cv, "door_version": dv, "window_days": window_days}


def _rtt_read(db, gw, cam, window_days):
    """Serve the STORED round-trip summary. NEVER walks. -> (summary|None, meta).

    Same era key and same never-across-boundaries rule as _aggregate_read: an RTT computed under one
    door_version describes one instrument, and serving it beside another pools two.

    A miss is reported as PENDING, never as "no round trips". Those are opposite claims — one says
    the lift did not move, the other says nobody has looked yet — and the panel must not print the
    first when the second is true."""
    cv, dv = _current_keys(db, gw, cam)
    if dv is None:
        return None, {"state": "no door rows for this camera in any era"}
    try:
        r = db.execute("SELECT rtt, rtt_ms, computed_at FROM door_aggregate WHERE gateway_id=? "
                       "AND cam=? AND counting_version=? AND door_version=? AND window_days=?",
                       (gw, cam, cv, dv, float(window_days))).fetchone()
    except sqlite3.OperationalError:
        return None, {"state": "not yet computed — the precompute job has never run on this schema",
                      "current_door_version": dv}
    if not r or not r["rtt"]:
        return None, {"state": "not yet computed for the current era/window",
                      "current_door_version": dv, "window_days": window_days,
                      "detail": "the precompute job (liftlab-precompute.timer) fills this off the "
                                "request path; this is pending computation, NOT an absence of "
                                "round trips"}
    try:
        S = json.loads(r["rtt"])
    except (ValueError, TypeError):
        return None, {"state": "stored RTT unreadable", "computed_at": r["computed_at"]}
    return S, {"state": "ok", "computed_at": r["computed_at"],
               "age_s": (round(time.time() - r["computed_at"], 1) if r["computed_at"] else None),
               "compute_ms": r["rtt_ms"], "door_version": dv, "window_days": window_days,
               "source": "door_aggregate (precomputed off the request path)"}


def aggregate_refresh(db, gw, cam, window_days=None):
    """Walk the rows and STORE. Scheduler/CLI ONLY — never a request handler."""
    _aggregate_table(db)
    d = WINDOW_DAYS if window_days is None else float(window_days)
    t0, t1, _ = _window(d)
    cv, dv = _current_keys(db, gw, cam)
    if dv is None:
        return {"gw": gw, "cam": cam, "skipped": "no door rows"}
    t_start = time.time()
    dg_all = _door_gpu_by_cam(db, gw, [cam], t0, t1)
    tj = _transits_for_join(db, gw)
    t2 = _tier2(db, gw, cam, tj.get(cam, []), t0, t1)
    dg = dg_all.get(cam)
    src = (dg or {}).get("n_rows") or (t2 or {}).get("rows_in_era") or 0
    ms = int((time.time() - t_start) * 1000)
    # THE ONLY PLACE THE RTT WALK RUNS. Off the request path, once per era per window, on the timer.
    _r0 = time.time()
    _rt, _rerr = _rtt_by_cam(db, gw, [cam], t0, t1)
    rtt = (_rt or {}).get(cam)
    rtt_ms = int((time.time() - _r0) * 1000)
    db.execute("INSERT INTO door_aggregate (gateway_id,cam,counting_version,door_version,"
               "window_days,door_gpu,tier2,computed_at,source_rows,compute_ms,rtt,rtt_ms) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(gateway_id,cam,counting_version,door_version,window_days) DO UPDATE SET "
               "door_gpu=excluded.door_gpu, tier2=excluded.tier2, computed_at=excluded.computed_at, "
               "source_rows=excluded.source_rows, compute_ms=excluded.compute_ms, "
               "rtt=excluded.rtt, rtt_ms=excluded.rtt_ms",
               (gw, cam, cv, dv, d, json.dumps(dg) if dg else None,
                json.dumps(t2) if t2 else None, time.time(), src, ms,
                json.dumps(rtt) if rtt else None, rtt_ms))
    db.commit()
    # Keep the table from growing an entry per retired era forever.
    db.execute("DELETE FROM door_aggregate WHERE gateway_id=? AND cam=? AND NOT "
               "(counting_version=? AND door_version=?)", (gw, cam, cv, dv))
    db.commit()
    return {"gw": gw, "cam": cam, "source_rows": src, "compute_ms": ms,
            "has_tier2": t2 is not None, "counting_version": cv, "door_version": dv,
            "window_days": d, "rtt_ms": rtt_ms,
            "rtt_trips": (rtt or {}).get("n_trips"), "rtt_error": _rerr}


# ── floor alphabet: derived on a SCHEDULE, served from a table ─────────────────
# The alphabet is deliberately ALL-ERA. Floors are physical: a template rebuild moves door_version
# but not the building, and deriving admission from the current era alone made every young era start
# floor-blind fleet-wide. So it cannot be windowed — but it was being re-derived from 247,019 rows
# per request across cameras (ch29 alone: 141,430), which is the single largest cost on the page.
#
# THE RULE THAT MATTERS: a read NEVER triggers a derivation. `_alphabet_read` does a single indexed
# lookup and returns whatever the table holds, even if it is hours stale or absent. If a read could
# fall back to "just derive it now", the 140k-row walk would come straight back on the request path
# under a new name, and with it the hang. Staleness is reported, never repaired inline.
#
# The stored row carries the era and door_version the derivation was computed under, so a consumer
# can tell it was derived under a DIFFERENT instrument than the one it is now being applied to
# rather than silently reading across an era boundary.
_ALPHA_TABLE_READY = set()


def _alphabet_table(db):
    """Create the table once per connection-generation. Cheap: CREATE TABLE IF NOT EXISTS."""
    db.execute("""CREATE TABLE IF NOT EXISTS floor_alphabet (
        gateway_id TEXT, cam TEXT,
        alphabet TEXT,              -- JSON list of admitted floor strings, or NULL for 'accept all'
        detail TEXT,                -- JSON per-floor derivation detail (n, via, twin, anchored)
        derived_at REAL,            -- epoch of the derivation
        evidence_rows INTEGER,      -- how many floor-bearing rows it was derived from
        era TEXT,                   -- the era resolved at derivation time
        door_version TEXT,          -- the exact door_version of the newest evidence row
        PRIMARY KEY (gateway_id, cam))""")


def _alphabet_read(db, gw, cam):
    """Serve the stored alphabet. NEVER derives. -> (alphabet:set|None, detail:dict, meta:dict)."""
    try:
        r = db.execute("SELECT alphabet, detail, derived_at, evidence_rows, era, door_version "
                       "FROM floor_alphabet WHERE gateway_id=? AND cam=?", (gw, cam)).fetchone()
    except sqlite3.OperationalError:
        return None, {}, {"state": "no table — alphabet job has never run", "derived_at": None}
    if not r:
        return None, {}, {"state": "not yet derived — alphabet job has not covered this camera",
                          "derived_at": None}
    try:
        alpha = json.loads(r["alphabet"]) if r["alphabet"] else None
        detail = json.loads(r["detail"]) if r["detail"] else {}
    except (ValueError, TypeError):
        return None, {}, {"state": "stored alphabet unreadable", "derived_at": r["derived_at"]}
    return (set(alpha) if alpha else None), detail, {
        "state": "ok", "derived_at": r["derived_at"],
        "age_s": (round(time.time() - r["derived_at"], 1) if r["derived_at"] else None),
        "evidence_rows": r["evidence_rows"], "era": r["era"], "door_version": r["door_version"]}


def alphabet_refresh(db, gw, cam):
    """Derive and STORE. Call from the scheduler/CLI ONLY — never from a request handler.

    Kept out of the read path on purpose: this is the 140k-row walk whose removal is the point of
    the change. Returns the meta it wrote."""
    _alphabet_table(db)
    rows = _q(db, "SELECT ts, floor, reason, read_conf FROM gw_door_event "
                  "WHERE gateway_id=? AND cam=? AND floor IS NOT NULL ORDER BY ts", (gw, cam))
    derived, detail = _derive_floor_alphabet(rows, _labels_evidence(gw, cam))
    dv = db.execute("SELECT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? "
                    "AND door_version IS NOT NULL AND door_version<>'' AND ts IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1", (gw, cam)).fetchone()
    door_version = dv["door_version"] if dv else None
    era = str(door_version or "").split("+")[0] or None
    now = time.time()
    db.execute("INSERT INTO floor_alphabet "
               "(gateway_id,cam,alphabet,detail,derived_at,evidence_rows,era,door_version) "
               "VALUES (?,?,?,?,?,?,?,?) "
               "ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "alphabet=excluded.alphabet, detail=excluded.detail, derived_at=excluded.derived_at, "
               "evidence_rows=excluded.evidence_rows, era=excluded.era, "
               "door_version=excluded.door_version",
               (gw, cam, json.dumps(sorted(derived)) if derived else None,
                json.dumps(detail), now, len(rows), era, door_version))
    db.commit()
    return {"gw": gw, "cam": cam, "n_admitted": len(derived or ()), "evidence_rows": len(rows),
            "derived_at": now, "era": era, "door_version": door_version}


def _age_phrase(ts):
    if not ts:
        return "never derived"
    a = time.time() - ts
    if a < 90:
        return f"{a:.0f}s ago"
    if a < 5400:
        return f"{a/60:.0f}m ago"
    return f"{a/3600:.1f}h ago"


def _tier2(db, gw, cam, transits, t0=None, t1=None, era_override=""):
    """Tier-2 for one camera, from the gw_door_event stream of ONE era.

    transits: [(ts, direction)] for this cam, ascending — joined to door-open windows for per-floor
    demand. Returns None when the era has no rows at all (nothing to say), otherwise a dict whose
    every metric carries its own n plus the era/quality filter that produced it.
    era_override: request-level era selection (see _era_for) — metrics from any era, deliberately.
    """
    era, era_src = _era_for(db, gw, cam, era_override)
    if not era:
        return None
    # Era census for the selector: every era this camera has EVER written, with span + row count,
    # so the UI can offer "view that week" instead of auto-newest silently hiding it.
    eras_list, eras_computed_at = _era_census(db, gw, cam)
    _w, _wargs = _ts_clause(t0, t1)
    _ec, _eargs = _era_clause(era)
    rows = _q(db, "SELECT ts, floor, direction, door_state, reason, read_conf FROM gw_door_event "
                  "WHERE gateway_id=? AND cam=?" + _ec + _w + " ORDER BY ts",
              (gw, cam, *_eargs, *_wargs))
    # Optional DATE RANGE on top of the era. Both sides are filtered together: leaving transits
    # unfiltered while narrowing the door rows would join riders to windows that are no longer in
    # the result, and the per-floor totals would exceed the range they claim to describe.
    # `rows` is already windowed by SQL above — filtering it again here would be the exact
    # materialise-then-slice this change removes. Transits come from a separate read and still need
    # the same bound applied, because joining riders to windows outside the range would inflate the
    # per-floor totals past the range they claim to describe.
    if t0 is not None or t1 is not None:
        transits = [t for t in transits if _in_range(t[0], t0, t1)]
    if not rows:
        return None

    n_rows = len(rows)          # hoisted: the stop loop below indexes to the end of this list

    # Per-reason census FIRST. If the quality filter matches nothing, this is what tells you it was
    # the FILTER and not the camera — the failure mode that would otherwise look like an empty panel.
    census = {}
    for r in rows:
        k = r["reason"] or "(none)"
        census[k] = census.get(k, 0) + 1
    # READ-TIME FLOOR WHITELIST. A read can be reason='ok' and still name a floor the tower does not
    # have — a stored row from before the gpu_door whitelist, or a rebuild-era misread. Reject it here
    # too, so already-stored garbage never reaches stops/speed/per-floor. Rejects are counted as
    # off_alphabet:<floor> in the census, so a filtered read is visible, not silently dropped.
    # DASH_FLOOR_ALPHABET is an optional manual override for the day the derivation is wrong.
    #
    # ALPHABET EVIDENCE IS ALL-ERA BY DESIGN (2026-07-29): floors are physical — a tracker-logic or
    # template change moves door_version, not the building. Deriving admission from the current era
    # alone made every young era start floor-blind fleet-wide (the e79e50d3h2 blank: weeks of
    # corroboration discarded at each logic tag). METRICS stay era+range-scoped (the rows above);
    # only ADMISSION evidence spans eras. Cross-era welds cannot happen: the derivation's edge and
    # flip rules cap neighbour gaps at seconds, and an era change is a restart-sized gap. Labels
    # (labels.json) were always era-independent.
    manual = _floor_alphabet(cam)
    # READ ONLY. The derivation itself (the 140k-row all-era walk) belongs to alphabet_refresh(),
    # which the scheduler runs off the request path. A stale table serves the read; it never
    # triggers a recompute, because that is exactly how the hang would return under a new name.
    derived, alpha_detail, alpha_meta = _alphabet_read(db, gw, cam)
    if manual is not None:
        alphabet, alpha_source = manual, "manual override (DASH_FLOOR_ALPHABET)"
    elif derived:
        alphabet, alpha_source = derived, (
            f"derived from all-era evidence ({alpha_meta.get('evidence_rows')} rows, era "
            f"{alpha_meta.get('era')}, {_age_phrase(alpha_meta.get('derived_at'))})")
    else:
        alphabet, alpha_source = None, f"none — {alpha_meta.get('state', 'unavailable')} (accepting all)"
    # An alphabet derived under a DIFFERENT era than the one these metrics use is still valid
    # evidence (floors are physical) but the mismatch is worth showing rather than hiding.
    if alpha_meta.get("era") and era and alpha_meta["era"] != era:
        alpha_meta = dict(alpha_meta, era_mismatch=(
            f"alphabet derived under era {alpha_meta['era']}, metrics are era {era}"))
    conf = []
    for r in rows:
        if r["floor"] is None or r["reason"] not in DOOR_OK_REASONS:
            continue
        if alphabet is not None and str(r["floor"]) not in alphabet:
            # visible in the census with the DERIVATION's reason, so a derivation mistake is auditable
            why = (alpha_detail.get(str(r["floor"]), {}).get("via") or "not_in_alphabet")
            census[f"not_in_derived_alphabet:{r['floor']} ({why})"] =                 census.get(f"not_in_derived_alphabet:{r['floor']} ({why})", 0) + 1
            continue
        conf.append(r)

    # ── C21/C22 — speed between CONSECUTIVE confident reads that changed floor ──────────────
    seg_up, seg_dn = [], []
    skipped_unmappable = skipped_implausible = 0
    prev = None
    for r in conf:
        if prev is not None and r["floor"] != prev["floor"]:
            i0, i1 = _floor_idx(prev["floor"]), _floor_idx(r["floor"])
            dt = (r["ts"] or 0) - (prev["ts"] or 0)
            if i0 is None or i1 is None:
                skipped_unmappable += 1
            elif dt > 0:
                fps = abs(i1 - i0) / dt
                if fps > MAX_FLOORS_PER_S:
                    skipped_implausible += 1        # OCR slip, not a lift that fast
                else:
                    (seg_up if i1 > i0 else seg_dn).append(fps)
        prev = r

    # ── C17/C18 — stops per door CYCLE, split by the arrow shown ────────────────────────────
    # A stop is one door cycle: the transition out of 'closed' into 'opening'/'open', through to the
    # return to 'closed'.
    #
    # It was previously the 'open' plateau alone — open_ts to the first 'closing' row — and that is
    # why only 31 of 2484 transits joined. DoorTracker enters 'open' only at openness >= near_open
    # (0.90) and leaves it the instant openness dips below, so on a jittery edge the fully-open
    # plateau can be a fraction of a second. Passengers cross throughout 'opening' and 'closing';
    # attributing them to the plateau discards nearly all of them. The cycle is the passenger
    # exchange, so the cycle is the window.
    stops = []                                     # {ts, close_ts, floor, direction}
    unattributed = 0
    last_conf = None
    prev_state = None
    open_states = ("opening", "open")
    for idx, r in enumerate(rows):
        if (r["floor"] is not None and r["reason"] in DOOR_OK_REASONS
                and (alphabet is None or str(r["floor"]) in alphabet)):
            last_conf = r
        st = r["door_state"]
        if st in open_states and prev_state not in open_states:
            end_ts = None
            floor_src = r if (r["floor"] is not None and r["reason"] in DOOR_OK_REASONS
                              and (alphabet is None or str(r["floor"]) in alphabet)) else None
            # INDEX, not islice, and not rows[idx+1:]. All three visit the same handful of rows —
            # the loop breaks within ~6 on average — but only indexing STARTS there.
            #   rows[idx+1:]                copies the tail       O(n) time + O(n) memory
            #   islice(rows, idx+1, None)   SEEKS by discarding   O(n) time, no memory
            #   range(idx+1, len(rows))     starts at idx+1       O(1) to begin
            # islice on a list does not seek: it iterates from the front throwing away idx items, so
            # replacing the slice with it removed the allocation and kept the quadratic term. That is
            # why the earlier "islice fixes it" prediction failed — 303s became ~96s, not fast.
            # Measured on the live snapshot, ch27 158,710 rows / 25,214 transitions:
            #   islice 82.89s   index 0.11s   -> 745x, byte-identical result (ch29: 13.18s -> 0.07s)
            # THIS is what made /dash/{gw}/trends?cam= take 43s and then time out entirely.
            for j in range(idx + 1, n_rows):
                nxt = rows[j]
                if (nxt["ts"] or 0) - (r["ts"] or 0) > DOOR_OPEN_MAX_S:
                    break
                # A confident read from INSIDE the cycle is the best floor evidence: the car is
                # stopped, so the floor cannot change, and mid-cycle frames are often cleaner than
                # the opening frame (the leaf is out of the panel's way).
                if (floor_src is None and nxt["floor"] is not None and nxt["reason"] in DOOR_OK_REASONS
                        and (alphabet is None or str(nxt["floor"]) in alphabet)):
                    floor_src = nxt
                if nxt["door_state"] == "closed":
                    end_ts = nxt["ts"]
                    break
            src = floor_src or last_conf
            if src is None or src["floor"] is None or abs(r["ts"] - src["ts"]) > DOOR_ATTR_S:
                unattributed += 1
            else:
                stops.append({"ts": r["ts"], "floor": str(src["floor"]),
                              "direction": src["direction"],
                              "close_ts": end_ts if end_ts is not None else (r["ts"] + DOOR_OPEN_MAX_S)})
        if st:
            prev_state = st

    up_stops = sum(1 for s in stops if s["direction"] == "up")
    dn_stops = sum(1 for s in stops if s["direction"] == "down")
    no_arrow = len(stops) - up_stops - dn_stops

    # ── stops per floor + boardings per floor (transits inside each door-open window) ───────
    per_floor = {}
    matched_transits = 0
    open_seconds = 0.0
    ti = 0                                          # both lists are ascending -> single forward pass,
    for s in stops:                                 # not a rescan per stop (this is the one page everyone loads)
        f = per_floor.setdefault(s["floor"], {"stops": 0, "up_stops": 0, "down_stops": 0,
                                              "boarded": 0, "alighted": 0,
                                              "stops_by_hour": [0] * 24, "riders_by_hour": [0] * 24})
        f["stops"] += 1
        hr = _ist_hour(s["ts"])
        if hr is not None:
            f["stops_by_hour"][hr] += 1
        if s["direction"] == "up":
            f["up_stops"] += 1
        elif s["direction"] == "down":
            f["down_stops"] += 1
        open_seconds += max(0.0, (s["close_ts"] or s["ts"]) - s["ts"])
        while ti < len(transits) and transits[ti][0] < s["ts"]:
            ti += 1                                 # transits before this window belong to no open door
        j = ti
        while j < len(transits) and transits[j][0] <= s["close_ts"]:
            f["boarded" if transits[j][1] == "in" else "alighted"] += 1
            if hr is not None:
                f["riders_by_hour"][hr] += 1
            matched_transits += 1
            j += 1

    floors = [dict(v, floor=k, floor_idx=_floor_idx(k)) for k, v in per_floor.items()]
    floors.sort(key=lambda x: (x["floor_idx"] is None, x["floor_idx"], x["floor"]))
    era_t0 = rows[0]["ts"]
    era_t1 = rows[-1]["ts"]
    joinable = sum(1 for t, _ in transits if era_t0 <= t <= era_t1)

    exp_era, built_at = _expected_era(gw, cam)
    stale_templates = None
    if exp_era and era and exp_era != era:
        stale_templates = (f"this camera is REPORTING era {era} but the last build produced "
                           f"{exp_era} — the worker is running stale templates. Its reads are real "
                           f"but they are not from the current geometry; restart the worker (or "
                           f"wait for the templates refetch) before trusting these numbers.")
    era_note = (f"door_version starting {era} [{era_src}] · reads with reason "
                f"{'/'.join(DOOR_OK_REASONS)} and a non-null floor")
    return {
        "era": era,
        "era_source": era_src,
        "eras": eras_list,                        # every era this camera ever wrote (selector data)
        "eras_computed_at": eras_computed_at,     # cached; age visible so staleness is not silent
        "eras_age_s": round(time.time() - eras_computed_at, 1),
        "expected_era": exp_era, "built_at": built_at, "stale_templates": stale_templates,
        "era_filter": era_note,
        "quality_reasons": list(DOOR_OK_REASONS),
        "floor_whitelist": (sorted(alphabet) if alphabet else None),
        "floor_alphabet_source": alpha_source,
        "floor_alphabet_meta": alpha_meta,        # derived_at / age / evidence_rows / era — staleness visible
        "floor_alphabet_detail": [
            {"floor": f, "n": d["n"], "first": round(d["first"], 0), "last": round(d["last"], 0),
             "admitted": d["admitted"], "via": d["via"],
             **({"twin": d["twin"]} if "twin" in d else {}),
             **({"anchored": d["anchored"]} if "anchored" in d else {})}
            for f, d in sorted(alpha_detail.items(), key=lambda kv: (-kv[1]["n"], kv[0]))],
        "off_alphabet_rejected": sum(v for k, v in census.items()
                                     if str(k).startswith(("off_alphabet", "not_in_derived_alphabet"))),
        "rows_in_era": len(rows),
        "confident_reads": len(conf),
        "reason_census": census,
        # C17/C18
        "stops": {"n": len(stops), "up": up_stops, "down": dn_stops, "no_arrow": no_arrow,
                  "unattributed": unattributed},
        # Stated in the heatmap caption. A cycle whose nearest confident read is older than this is
        # placed on no floor at all and is invisible in both charts — worth naming, because on some
        # cameras it is the majority of cycles.
        "attr_window_s": DOOR_ATTR_S,
        # C21/C22 — separate directions; a lift is not symmetric and averaging them hides that
        "speed_up": dict(_stats(seg_up), unit="floors/s"),
        "speed_down": dict(_stats(seg_dn), unit="floors/s"),
        "speed_excluded": {"unmappable_floor": skipped_unmappable,
                           "implausible_gt_%.1f_fps" % MAX_FLOORS_PER_S: skipped_implausible},
        "join_diagnostics": _join_diagnostics(stops, transits, matched_transits),
        "per_floor": floors,
        # THE HONEST DENOMINATOR. "31 of 2484" was two problems, not one: the plateau window above,
        # and a denominator counting every transit this camera has EVER posted — including all the
        # ones from before this door era existed, which could never join to anything. The joinable
        # denominator is transits inside the era's own time span; the lifetime total stays for
        # context but is no longer the thing the ratio is against.
        "transits_matched": matched_transits,
        "transits_joinable": joinable,
        "transits_total": len(transits),
        "era_span": [era_t0, era_t1],
        "door_open_seconds": round(open_seconds, 1),
        "transition_census": _door_transition_census(db, gw, cam, era, t0, t1),
        "floor_order_declared": bool(FLOOR_ORDER),
    }


def _registry(db, gw):
    """The GPU camera registry (wizard piece 5) — what the fleet is asked to run. Read-only here;
    /dash toggles it through the registry API, which is the single writer."""
    rows = _q(db, "SELECT cam, enabled, stride, analyze_fps, updated_at FROM camera_registry "
                  "WHERE gateway_id=? ORDER BY cam", (gw,))
    return {r["cam"]: {"enabled": bool(r["enabled"]), "stride": r["stride"],
                       "analyze_fps": r["analyze_fps"], "updated_at": r["updated_at"]} for r in rows}


def _transits_for_join(db, gw):
    """(ts, direction) per cam, ascending — the join side for per-floor demand."""
    rows = _q(db, "SELECT cam, ts, direction FROM transit_event WHERE gateway_id=? AND ts IS NOT NULL "
                  "ORDER BY ts", (gw,))
    by = {}
    for r in rows:
        by.setdefault(r["cam"], []).append((r["ts"], r["direction"]))
    return by


def _transit_by_cam(db, gw):
    """Per-camera boarded/alighted totals + today's counts, AGGREGATED IN SQL.

    This used to SELECT every transit row for the gateway and tally them in Python — 22k+ row
    objects materialised on every /dash load to produce five numbers per camera. The counting rule
    is preserved exactly: 'in' is a boarding and ANYTHING ELSE is an alighting (so a NULL or
    unrecognised direction still lands where it always did), and last_ts still ignores rows with
    no timestamp. Only the arithmetic moved; no reported number changes.
    """
    today = _ist_today_epoch()
    rows = _q(db, "SELECT cam, "
                  "SUM(direction='in') b, "
                  "SUM(direction IS NULL OR direction<>'in') a, "
                  "SUM(direction='in' AND ts IS NOT NULL AND ts>=?) bt, "
                  "SUM((direction IS NULL OR direction<>'in') AND ts IS NOT NULL AND ts>=?) at, "
                  "MAX(NULLIF(ts, 0)) last_ts "     # NULLIF: the Python version tested `if r['ts']`,
                                                    # which skipped ts=0 as falsy. MAX would not.
                  "FROM transit_event WHERE gateway_id=? GROUP BY cam", (today, today, gw))
    return {r["cam"]: {"boarded_today": r["bt"] or 0, "alighted_today": r["at"] or 0,
                       "boarded_total": r["b"] or 0, "alighted_total": r["a"] or 0,
                       "last_ts": r["last_ts"]}
            for r in rows}


def _rtt_by_cam(db, gw, cams, t0=None, t1=None, era_override=""):
    """ROUND TRIP TIME per camera — the derivation is rtt_core's, never a second copy here.

    RTT is what the whole MEP-02 sheet resolves to, so it must not be computable two ways. This
    reads the era-scoped door rows and hands them to the same function tools/rtt.py calls.

    ERA SCOPED ON THE FULL door_version. The 8-char prefix is the TEMPLATES hash and the engine tag
    follows it, so a prefix pools h2 with h3 — measured on ch29, where one prefix matched three
    eras. _era_for returns the full string and that is what is matched.
    """
    try:
        import rtt_core
    except Exception as e:                      # the dash must not fail because a tool is missing
        return {}, f"rtt_core unavailable: {type(e).__name__}"
    _ = rtt_core
    out = {}
    for cam in cams:
        # ERA RESOLVED BY rtt_core, IDENTICALLY TO THE CLI. _era_for returns everything before the
        # '+' — a PREFIX, which every other dash consumer matches with LIKE. This matched it with
        # `=`, so it selected NOTHING: ch29 reported no rows while tools/rtt.py found 1569 trips on
        # the same database two hours earlier. Sharing the walk was never enough; era selection is
        # part of the derivation.
        # NO IN-PROCESS CACHE. There was one, keyed on (gw, cam, era, t0, t1), and it never fired:
        # t0 arrives as time.time()-minus-a-span, so every request minted a new key. It measured
        # 0ms on a fixture that passed a stable (None, None) and 9.0s on the live box. Rather than
        # repair the key, the walk moved off the request path entirely — a process cache cannot help
        # a cold start, a restart, or a first view, and this is the only caller that remains.
        # BOUNDED BY THE SAME RANGE. Unbounded this was a full scan of the camera's whole history
        # — 115,936 rows on ch29 — once per camera per request, before a single trip was walked.
        _vw, _vargs = _ts_clause(t0, t1)
        vers = [(r["door_version"], r["mx"]) for r in _q(
            db, "SELECT door_version, MAX(ts) mx FROM gw_door_event WHERE gateway_id=? AND cam=? "
                "AND door_version IS NOT NULL AND door_version<>''" + _vw
                + " GROUP BY door_version", (gw, cam, *_vargs))]
        _ov, _src = _era_for(db, gw, cam, era_override)
        # _era_for's value is a prefix (or a user pin); expand it to the one full version it names.
        era, _err = rtt_core.expand_era(vers, _ov if (era_override or DOOR_ERA not in ("", "auto"))
                                        else None)
        if _err:
            out[cam] = {"state": "era_ambiguous", "note": _err}
            continue
        if not era:
            out[cam] = {"state": "no_era", "note": "no door-engine reads in any era"}
            continue
        w, wargs = _ts_clause(t0, t1)
        # ROW CAP. A wide range on a chattering camera is hundreds of thousands of rows, and this
        # runs on the request path. Refuse loudly rather than spend the budget: a truncated walk
        # would silently drop round trips and report a median computed from part of the window.
        n_rows = _q(db, "SELECT COUNT(*) n FROM gw_door_event WHERE gateway_id=? AND cam=? "
                        "AND door_version = ?" + w, (gw, cam, era, *wargs))[0]["n"]
        if n_rows > RTT_MAX_ROWS:
            out[cam] = {"state": "too_many_rows", "era": era, "n_rows": n_rows,
                        "note": f"{n_rows} door rows in this range exceeds the {RTT_MAX_ROWS} the "
                                f"request path will walk. Narrow the range — a truncated walk would "
                                f"drop round trips and report a median from part of the window."}
            continue
        rows = _q(db, "SELECT ts, floor, door_state FROM gw_door_event WHERE gateway_id=? AND cam=? "
                      "AND door_version = ?" + w + " ORDER BY ts, id", (gw, cam, era, *wargs))
        if not rows:
            out[cam] = {"state": "no_rows", "era": era}
            continue
        n_floor = sum(1 for r in rows if r["floor"] is not None)
        if not n_floor:
            # THE ABSENCE HAS A NAME. A camera with no floor attribution cannot have an RTT at all —
            # the home floor is what defines the trip — and that is a different statement from
            # "this lift made no round trips".
            out[cam] = {"state": "no_floor", "era": era, "n_rows": len(rows),
                        "note": "no floor attribution on this camera — RTT needs a home-floor read, "
                                "so it is UNAVAILABLE, not zero"}
            continue
        S = rtt_core.summarise(rows, home=RTT_HOME)
        S.update({"state": "ok", "era": era, "n_rows": len(rows),
                  "floor_attributed_pct": round(100.0 * n_floor / len(rows), 1)})
        out[cam] = S
    return out, None


def _occupancy_by_cam(db, gw, t0=None, t1=None):
    """Per-camera PEAK CAR OCCUPANCY over the window, from the door-open episodes in validation_item.

    THE EVIDENCE RULE, same one the episode gate enforces at the worker: an episode counts only if
    `occupancy_frames > 0`. A NULL means a worker that predates the feature; a 0 means the episode
    was posted with no analysed frames behind the peak. Neither is "the cabin was empty", and
    treating them as 0 would pull every fleet figure down with observations that were never made.
    Those episodes are reported as `n_no_evidence` rather than dropped silently — a small headline n
    beside a large episode count is the honest shape of thin coverage.

    ERA SCOPING. Occupancy is a product of the COUNTING logic (the cabin polygon and the anchor live
    in counting.py), so it is scoped by counting_version exactly as precision is. Peaks counted under
    a different zone are not comparable to these, so they are excluded from the stats and named in
    `versions_seen` — visible, never pooled.

    DEGRADED episodes are INCLUDED. occupancy_max is a floor; thin coverage can only make it lower,
    never higher, so including them cannot inflate the number it can only understate it. The count is
    carried so a peak resting mostly on degraded episodes can be seen for what it is.
    """
    w, wargs = "", []
    if t0 is not None:
        w += " AND ts_start >= ?"; wargs.append(t0)
    if t1 is not None:
        w += " AND ts_start < ?"; wargs.append(t1)
    rows = _q(db, "SELECT cam, ts_start, occupancy_max, occupancy_frames, occupancy_degraded, "
                  "analysed_frames, counting_version FROM validation_item "
                  "WHERE gateway_id=?" + w + " ORDER BY ts_start", (gw, *wargs))
    # The version each camera is CURRENTLY counting under — the same source _validations reads. A
    # camera with no row yet falls back to the newest version its own episodes carry.
    cur_ver = {r["cam"]: r["counting_version"] for r in
               _q(db, "SELECT cam, counting_version FROM camera_validation WHERE gateway_id=?", (gw,))}
    today = _ist_today_epoch()
    by = {}
    for r in rows:
        b = by.setdefault(r["cam"], {"peaks": [], "today": [], "n_episodes": 0, "n_no_evidence": 0,
                                     "n_degraded": 0, "frames": 0, "last_ts": None,
                                     "versions_seen": {}, "off_era": 0})
        b["n_episodes"] += 1
        v = r["counting_version"]
        b["versions_seen"][v] = b["versions_seen"].get(v, 0) + 1
        want = cur_ver.get(r["cam"]) or None
        if want and v != want:
            b["off_era"] += 1
            continue                                   # counted under a different cabin zone
        if not (r["occupancy_frames"] or 0) > 0:
            b["n_no_evidence"] += 1                    # no coverage behind the peak -> not evidence
            continue
        p = int(r["occupancy_max"] or 0)
        b["peaks"].append(p)
        b["frames"] += int(r["occupancy_frames"] or 0)
        if r["occupancy_degraded"]:
            b["n_degraded"] += 1
        if (r["ts_start"] or 0) >= today:
            b["today"].append(p)
        b["last_ts"] = r["ts_start"]
    out = {}
    for cam, b in by.items():
        pk = sorted(b["peaks"])
        out[cam] = {
            "n": len(pk),                              # episodes with evidence — the real denominator
            "n_episodes": b["n_episodes"],             # episodes seen in the window, evidence or not
            "n_no_evidence": b["n_no_evidence"],
            "n_off_era": b["off_era"],
            "n_degraded": b["n_degraded"],
            "peak": pk[-1] if pk else None,            # window max — the headline
            "p95": _pctl(pk, 0.95) if pk else None,
            "median": _pctl(pk, 0.5) if pk else None,
            "today_peak": max(b["today"]) if b["today"] else None,
            "today_n": len(b["today"]),
            "frames": b["frames"],
            "last_ts": b["last_ts"],
            "counting_version": cur_ver.get(cam),
            "versions_seen": [{"version": k, "n": n} for k, n in sorted(
                b["versions_seen"].items(), key=lambda kv: -kv[1])],
            "measured_minimum": True,                  # never a count; see OCC_CALIBRATION
            "calibration": OCC_CALIBRATION,
            "anchor_note": OCC_ANCHOR_NOTE,
        }
    return out


def _analyzers(db, gw):
    rows = _q(db, "SELECT * FROM analyzer_status WHERE gateway_id=?", (gw,))
    now = time.time()
    return {r["cam"]: {**dict(r), "age_s": round(now - (r["ts"] or 0), 1),
                       "up": (now - (r["ts"] or 0)) < HB_STALE_S} for r in rows}


def _validations(db, gw):
    rows = _q(db, "SELECT cam,state,n_reviewed,n_exact,provenance,counting_version "
                  "FROM camera_validation WHERE gateway_id=?", (gw,))
    out = {}
    for r in rows:
        nr, ne = r["n_reviewed"] or 0, r["n_exact"] or 0
        out[r["cam"]] = {"state": r["state"], "n_reviewed": nr, "n_exact": ne,
                         "precision": round(100.0 * ne / nr) if nr else None,
                         "counting_version": r["counting_version"], "provenance": r["provenance"]}
    return out


def _health(db, gw):
    """The most recent recorded health check — READ ONLY, never computed here.

    health_check.py runs on a timer and writes health_status. The dashboard displays what was
    recorded and NEVER re-evaluates: a banner derived on the request path would disagree with the
    line that was actually sent, and the whole point of the feature is that one sentence exists in
    one place. A missing table means the checker has never run, which is itself worth saying —
    "no health check has ever run" is a different fact from "everything is fine".
    """
    try:
        r = db.execute("SELECT ts, ok, n_cams, n_bad, line, sent, delivered, delivery_error "
                       "FROM health_status WHERE gateway_id=? ORDER BY ts DESC LIMIT 1",
                       (gw,)).fetchone()
    except sqlite3.OperationalError:
        return {"state": "never_run", "note": "health_status table does not exist — "
                                              "liftlab-health has never run on this gateway"}
    if not r:
        return {"state": "never_run", "note": "no health check recorded for this gateway yet"}
    age = time.time() - (r["ts"] or 0)
    return {"state": ("ok" if r["ok"] else "breach"),
            "ts": r["ts"], "age_s": round(age, 1),
            # A CHECKER THAT STOPPED RUNNING IS ITSELF A FAULT, and it is the one failure the
            # checker cannot report about itself. Anything older than an hour is stale by the
            # 10-minute cadence, so say so instead of showing an old verdict as current.
            "stale": age > 3600,
            "n_cams": r["n_cams"], "n_bad": r["n_bad"], "line": r["line"],
            "delivered": r["delivered"] or None, "delivery_error": r["delivery_error"] or None}


def _latest(db, table, gw):
    rows = _q(db, f"SELECT * FROM {table} WHERE gateway_id=? ORDER BY id DESC LIMIT 1", (gw,))
    return dict(rows[0]) if rows else None


@dash_router.get("/dash/{gw}/data")
def dash_data(gw: str, era: str = "", days: float | None = None):
    db = _db()
    _budget_arm(db, DATA_BUDGET_S)
    try:
        return _dash_data_inner(db, gw, era, days)
    except DashTimeout as e:
        # 503 + Retry-After, NOT 500: the request was abandoned deliberately, the data is not known
        # to be broken, and a caller that retries later may well succeed. The body names the phase
        # that ran out so the next person does not have to re-derive where the time went.
        return JSONResponse(
            {"error": "timeout",
             "detail": (f"/dash/{gw}/data exceeded its {DATA_BUDGET_S:g}s budget and was aborted "
                        f"during '{e}'. This endpoint walks the full history of every camera on "
                        f"every request; until it is bounded in SQL it can outrun any budget."),
             "phase": str(e), "budget_s": DATA_BUDGET_S, "elapsed_s": _budget_elapsed(),
             "gw": gw, "t": time.time()},
            status_code=503, headers={"Retry-After": "30"})
    finally:
        _budget_disarm(db)
        db.close()


def _dash_data_inner(db, gw: str, era: str = "", days: float | None = None):
    now = time.time()
    t0, t1, window = _window(days)
    cams = _cameras(db, gw)
    _budget_check("door_by_cam")
    door = _door_by_cam(db, gw)                      # Pi-era (gw_event), RETIRED instrument
    # PRECOMPUTED, never derived here. Both door_gpu and tier2 come from door_aggregate, which the
    # precompute job fills off the request path. A missing entry is reported as pending — it is
    # NEVER allowed to read as "this camera has no data".
    door_gpu, tier2, agg_meta = {}, {}, {}
    for c in cams:
        _cam = c["cam"]
        dg, t2, meta = _aggregate_read(db, gw, _cam, window.get("days") if window else None)
        agg_meta[_cam] = meta
        if dg is not None:
            door_gpu[_cam] = dg
        if t2 is not None:
            tier2[_cam] = t2
    # INSTRUMENT INVALIDATION, ANNOTATED AT THE SOURCE. This used to be computed inside the
    # headline loop, which `continue`s on any camera with no DOOR_SPECS entry — i.e. on every camera
    # except ch29. So ch16's card printed "close median 1.47s · LIVE" from the same h2 edge-column
    # tracker the compliance panel marks SUPERSEDED, with no caveat at all. The rule belongs on the
    # data, where every consumer inherits it, rather than on one panel that happened to apply it.
    for _cam, _g in door_gpu.items():
        _g["superseded"] = (not _g.get("travel_unmeasured")) and (_g.get("n") or 0) > 0
        _g["superseded_note"] = (H2_SUPERSEDED_NOTE if _g["superseded"] else None)
    pending_cams = sorted(c for c, m in agg_meta.items() if m.get("state") != "ok")
    agg_computed_at = [m["computed_at"] for m in agg_meta.values() if m.get("computed_at")]
    _budget_check("transit_by_cam")
    trans = _transit_by_cam(db, gw)
    # Bounded to the SAME window as every other historical panel. Unbounded here would put an
    # all-history peak beside a 7-day cycle count under one heading, and the peak would win the
    # reader's attention while describing a different span.
    occ = _occupancy_by_cam(db, gw, t0, t1)
    # RTT IS NOT COMPUTED HERE ANY MORE. It walked every door row of all seven cameras on every
    # /data request and blew the 25s budget the moment the era fix made it match rows — the guard
    # named the phase, which is the only reason this was a five-minute diagnosis. A round trip is a
    # property of ONE shaft and the panel shows ONE camera, so it is fetched per camera from
    # /dash/{gw}/rtt instead. See _rtt_by_cam's cache and the row cap.
    xfer = _transfer_by_cam(db, gw)
    floor_cov = _floor_coverage(db, gw)
    registry = _registry(db, gw)
    ana = _analyzers(db, gw)
    val = _validations(db, gw)
    w = _latest(db, "watch_status", gw)
    r = _latest(db, "relay_status", gw)
    health = _health(db, gw)

    # ---- top strip: PI ----
    pi = None
    if w:
        oe, ce = w.get("opens_detected"), w.get("cycles_emitted")
        pi = {"ts": w.get("ts"), "age_s": round(now - (w.get("ts") or 0), 1),
              "state": w.get("state"), "signal_fps": w.get("signal_fps"),
              "soc_temp": w.get("soc_temp"), "throttle_live": w.get("throttle_live"),
              "opens_detected": oe, "cycles_emitted": ce,
              "detect_emit": round(oe / ce, 2) if oe and ce else None,
              "camera": w.get("camera")}
    # ---- top strip: RELAY ----
    relay = None
    if r:
        relay = {"ts": r.get("ts"), "age_s": round(now - (r.get("ts") or 0), 1),
                 "streams_delivering": r.get("streams_delivering"),
                 "sum_delivered_mbps": r.get("sum_delivered_mbps")}
    # ---- top strip: GPU (aggregate across cams) ----
    up = [c for c, a in ana.items() if a["up"]]
    gpu = {"any_up": bool(up), "cams_up": sorted(up),
           "worst": None}
    if ana:
        # show the busiest/worst analyser's throughput+drop as the fleet indicator
        worst = max(ana.values(), key=lambda a: (a.get("drop_frac") or 0, a.get("proc_ms") or 0))
        gpu["worst"] = {"proc_ms": worst.get("proc_ms"), "budget_ms": worst.get("seg_budget_ms"),
                        "drop_frac": worst.get("drop_frac"), "age_s": worst["age_s"]}

    # ---- per-camera assembly (honest blanks) ----
    out_cams = []
    for c in cams:
        cam = c["cam"]
        a = ana.get(cam)
        out_cams.append({
            **c,
            "snap": _snap(cam, gw),
            "door": door.get(cam),
            "transit": (dict(trans[cam], source=(a or {}).get("counting_version")) if cam in trans else None),
            "occupancy": occ.get(cam),
            "analyzer": (None if a is None else
                         {"up": a["up"], "age_s": a["age_s"], "mode": a.get("mode"),
                          "counting_version": a.get("counting_version"),
                          "proc_ms": a.get("proc_ms"), "budget_ms": a.get("seg_budget_ms"),
                          "drop_frac": a.get("drop_frac")}),
            "validation": val.get(cam),
        })

    # PROVENANCE SPLIT. Every close-travel stat is tagged with the instrument it came from, and the
    # Pi-era and GPU-era numbers sit side by side as SEPARATE lines — never averaged. A camera with a
    # spec appears once per instrument that has data for it, so ch29 shows its retired Pi history AND
    # its live GPU number, and ch16 (no Pi history) shows only the GPU line — which is the proof that
    # its cycles exist, or the honest zero if they do not.
    headline = []
    for cam in door:
        if not door[cam].get("spec") or not door[cam]["n"]:
            continue
        x = xfer.get(cam)
        headline.append(dict(door[cam]["spec"], cam=cam, median=door[cam]["median"],
                             p85=door[cam]["p85"], n=door[cam]["n"],
                             instrument="Pi door-watch (gw_event) — RETIRED " + DOORWATCH_RETIRED_BOUNDARY[:10],
                             era="pi", live=False,
                             transfer_median=(x or {}).get("median"), transfer_n=(x or {}).get("n"),
                             transfer_provisional=True))
    for cam, g in door_gpu.items():
        spec = g.get("spec") or DOOR_SPECS.get(cam)
        if not spec:
            continue                              # not a compliance-tracked camera
        # INSTRUMENT INVALIDATED 2026-08-05. Every h2-era GPU close-travel figure on this panel was
        # produced by an edge-column tracker that offline replay against hand-timed video showed does
        # not measure door travel: it detects ~62% of real closes, emitted 41 phantom cycles inside
        # verified door-CLOSED windows on ch27, and produced ONE travel value per camera against 4
        # and 11 timed closes (README_DOORWATCH.md). The numbers stay VISIBLE — era hygiene is
        # labelling, not deletion, and deleting them would hide that they were ever quoted — but they
        # must not read as current measurement. h3 rows carry travel_unmeasured instead and are not
        # superseded; they never made the claim.
        superseded = g.get("superseded", False)   # annotated above, for every camera
        headline.append(dict(spec, cam=cam, median=g["median"], p85=g["p85"], n=g["n"],
                             instrument=("GPU door engine (gw_door_event) — h3 STATE-ONLY"
                                         if g.get("travel_unmeasured")
                                         else "GPU door engine (gw_door_event) — LIVE"),
                             travel_unmeasured=g.get("travel_unmeasured", False),
                             travel_note=g.get("travel_note"),
                             n_reopens=g.get("n_reopens"),
                             superseded=superseded,
                             superseded_note=(g.get("superseded_note") if superseded else None),
                             era=g["era"], live=True, n_cycles=g["n_cycles"], reason=g.get("reason"),
                             measurement_suspect=g.get("measurement_suspect"),
                             n_impossible=g.get("n_impossible"), plausible_n=g.get("plausible_n"),
                             plausible_median=g.get("plausible_median"),
                             transfer_median=None, transfer_n=None, transfer_provisional=True))

    # NOT AVAILABLE (Tier-2 ceiling). This panel used to be unconditional, because the only floor
    # column was gw_event.floor and it was NULL on every row. Floor now arrives on a DIFFERENT stream
    # (gw_door_event, from the GPU door engine), so the ceiling is only real when THAT stream has
    # nothing in this era. Leaving it hardcoded would keep claiming Tier-2 is impossible while the
    # numbers sat one table over.
    unavailable = None
    # PENDING IS NOT ABSENCE. If tier2 is empty only because the precompute has not covered these
    # cameras yet, saying "no reads in this era" would be the same class of lie as the _q bug that
    # turned a timeout into an empty panel. The ceiling claim is only made when every camera has a
    # COMPUTED aggregate that genuinely had nothing to report.
    if not tier2 and pending_cams:
        unavailable = {"reason": "not yet computed",
                       "detail": (f"{len(pending_cams)} camera(s) have no precomputed aggregate for "
                                  f"the current era/window yet: {', '.join(pending_cams)}. This is a "
                                  f"pending computation, NOT an absence of data — the precompute job "
                                  f"(liftlab-precompute.timer) fills it off the request path."),
                       "pending_cams": pending_cams,
                       "blocks": ["stops per floor", "boardings/alightings per floor",
                                  "C17/C18 probable up/down stops", "C21/C22 speed factors"],
                       "unlock": "wait for the next precompute run, or run alphabet_job.py/"
                                 "precompute_job.py by hand"}
    elif not tier2:
        if floor_cov["with_floor"] == 0 and floor_cov["total"] > 0:
            detail = (f"gw_event.floor is NULL on all {floor_cov['total']} rows, and no camera has "
                      f"door reads in its own era (DASH_DOOR_ERA={DOOR_ERA})")
        else:
            detail = f"no camera has gw_door_event rows in its own era (DASH_DOOR_ERA={DOOR_ERA})"
        unavailable = {"reason": "needs floor attribution — no reads in this era",
                       "detail": detail,
                       "blocks": ["stops per floor", "boardings/alightings per floor",
                                  "C17/C18 probable up/down stops", "C21/C22 speed factors"],
                       "unlock": "floor OCR (template-match the LED digits + direction arrow)"}

    return JSONResponse({"t": now, "gw": gw, "ist_today": _ist_today_str(),
                         # WHAT RANGE THESE NUMBERS DESCRIBE. Every close-travel / stop / per-floor
                         # figure below is computed over this window, not over all history. Default
                         # is 7 days; days=0 asks for all history and is flagged expensive.
                         "window": window,
                         # STALENESS OF THE HISTORICAL PANELS. door_gpu/tier2/headline come from
                         # door_aggregate, computed off the request path. Per-camera state lives in
                         # aggregates.per_cam; pending cameras are listed explicitly so an operator
                         # can tell "not computed yet" from "nothing to report".
                         "aggregates": {
                             "per_cam": agg_meta,
                             "pending_cams": pending_cams,
                             "computed_at": (min(agg_computed_at) if agg_computed_at else None),
                             "oldest_age_s": (round(now - min(agg_computed_at), 1)
                                              if agg_computed_at else None),
                             "source": "door_aggregate (precomputed off the request path)"},
                         "health": health,
                         "pi": pi, "relay": relay, "gpu": gpu,
                         # The calibration travels WITH the data, not only in the page that draws it,
                         # so any consumer of this endpoint gets the number and its limits together.
                         "rtt_note": {"home": RTT_HOME, "lazy": f"/dash/{gw}/rtt?cam=<cam>",
                                      "definition": f"door CLOSED at {RTT_HOME} -> next door OPEN "
                                                    f"at {RTT_HOME}, both ends a STOP not a pass"},
                         "occupancy_note": {"label": OCC_LABEL, "calibration": OCC_CALIBRATION,
                                            "anchor": OCC_ANCHOR_NOTE,
                                            "basis": "distinct tracks simultaneously inside the cabin "
                                                     "zone during one door-open episode"},
                         "cameras": out_cams, "headline": headline, "registry": registry,
                         "floor_coverage": floor_cov, "tier2": tier2, "unavailable": unavailable,
                         "door_gpu": door_gpu,
                         "boundaries": {"doorwatch_retired": DOORWATCH_RETIRED_BOUNDARY,
                                        "close_travel_max": CLOSE_TRAVEL_MAX_BOUNDARY}})


@dash_router.get("/dash/{gw}/trends")
def dash_trends(gw: str, cam: str = "", from_h: int = -1, to_h: int = -1,
                period: str = "all", from_d: str = "", to_d: str = "", era: str = ""):
    """Hour-of-day profile + window stats for a DATE RANGE. cam='' -> FLEET (all lift cams).

    period = day | week | month | all, or an explicit from_d/to_d (YYYY-MM-DD). The hour-of-day
    profile is unchanged in shape — it is now computed over the selected range instead of all
    history, so "the morning peak" can be asked of last week rather than of everything ever
    collected. n_days reports how many distinct days actually contributed, which is what the
    per-hour averages divide by; a range with no data reports zero rather than dividing by one."""
    db = _db()
    cam_filter = ""
    args = [gw]
    if cam:
        cam_filter = " AND s.camera=?"
        args.append(cam)

    # RANGE FIRST, before anything reads it. This assignment used to sit ~40 lines BELOW the three
    # queries that bound on it, which is how 23e56aa shipped an UnboundLocalError on t0 that fired
    # on EVERY call — cache or live, camera or fleet. Pushing the predicates into SQL moved the
    # first USE of t0 above its definition and nothing caught it, because the equivalence harness
    # called _tier2/_aggregate_read directly and never executed this function.
    #
    # Both branches below therefore share one definition: there is no path through dash_trends on
    # which t0/t1/range_label are undefined. Door cycles carry a local ISO timestamp and transits an
    # epoch, so both are normalised to epoch before comparing — mixing the two representations is
    # how a range quietly drops one series and not the other.
    t0, t1, range_label = _range_bounds(period, from_d, to_d)

    # BOUND IN SQL, not in Python. These three loads previously fetched FULL HISTORY and were
    # filtered afterwards with _in_range — the same unbounded-work shape that made /dash never
    # terminate. The rows must never be fetched.
    #
    # gw_event stores door_open_start_ts as LOCAL ISO ('2026-07-20T07:16:44.382406+05:30'), so the
    # bound is a string compare. That is safe here and only here: IST has no DST, so every row
    # carries the same +05:30 suffix, and _iso_ist emits timespec='seconds' — at an equal second the
    # stored row's '.' (0x2E) sorts above the bound's '+' (0x2B), so a boundary row is INCLUDED by
    # >= and EXCLUDED by <, which is exactly the half-open window _in_range applies.
    ev_w, ev_args = "", []
    if t0 is not None:
        ev_w += " AND e.door_open_start_ts >= ?"; ev_args.append(_iso_ist(t0))
    if t1 is not None:
        ev_w += " AND e.door_open_start_ts < ?"; ev_args.append(_iso_ist(t1))
    ev = _q(db, "SELECT e.door_open_start_ts os, e.door_open_full_ts of, e.door_close_start_ts cs, "
                "e.close_travel_s ct, e.quality q, e.boarded b, e.alighted a "
                "FROM gw_event e JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?"
                + cam_filter + ev_w, (*args, *ev_args))
    tr_w, tr_args = _ts_clause(t0, t1)          # transit_event.ts is epoch — a plain numeric bound
    tr = _q(db, "SELECT ts, direction FROM transit_event WHERE gateway_id=?" +
            (" AND cam=?" if cam else "") + tr_w,
            ([gw, cam, *tr_args] if cam else [gw, *tr_args]))
    # GPU DOOR CYCLES. gw_event froze at the Pi-watch retirement (2026-07-21) but this profile kept
    # reading ONLY it, so every post-retirement range showed "0 cycles" while gw_door_event held
    # hundreds (07-30 ch16: 0 shown vs 253 with_travel in the DB — read as data loss, was a view
    # bug). One completed close (close_travel_s NOT NULL) = one cycle, era-scoped per camera exactly
    # like the panel, era override honoured. The instruments never overlap in time, so the hourly
    # cycle counts can share buckets; close-travel values must NOT pool — see close_instrument.
    gpu_cyc = []
    h3_cams = []
    uncal_cams = []          # no door engine has EVER posted for these — not calibrated, not idle
    for c in ([cam] if cam else [x["cam"] for x in _cameras(db, gw)]):
        e, _esrc = _era_for(db, gw, c, era)
        if not e:
            # WHICH KIND OF ABSENCE. A camera with no era either (a) has never had a door engine run
            # against it — no calibration, so door cycles and close-travel are not merely missing but
            # UNAVAILABLE — or (b) has history in some other era. Only the first is "uncalibrated",
            # and the difference decides whether the page says "no cycles" or "cannot measure cycles".
            # Rendering both as an empty axis with a 0 beside it says the lift stood still all week.
            _any = _q(db, "SELECT 1 FROM gw_door_event WHERE gateway_id=? AND cam=? LIMIT 1", (gw, c))
            if not _any:
                uncal_cams.append(c)
        if e:
            # PER-ERA CYCLE RULE. h2 marks a completed cycle with a non-null close_travel_s; h3 has
            # no travel at all, so the same rule reports 0 cycles for a healthy engine — the same
            # defect class as the July "0 cycles read as data loss" bug this loop already carries a
            # comment about, one era boundary later. For h3 the cycle is a state transition; see
            # _h3_cycle_ts for why NULL states are kept in the walk.
            if _is_h3_era(e):
                h3_cams.append(c)
                _ec, _eargs = _era_clause(e)
                h3rows = _q(db, "SELECT ts, door_state FROM gw_door_event WHERE gateway_id=? "
                                "AND cam=?" + _ec + _ts_clause(t0, t1)[0]
                                + " ORDER BY ts, id",
                            (gw, c, *_eargs, *_ts_clause(t0, t1)[1]))
                gpu_cyc += [{"ts": ts, "ct": None} for ts in _h3_cycle_ts(h3rows)]
                continue
            # FLEET CASE: this loop is the per-camera walk, so the bound goes on every camera's
            # query, not just the single-camera one. Unbounded here meant a fleet request read
            # every door row this gateway has ever stored, once per camera.
            _ec, _eargs = _era_clause(e)
            gpu_cyc += _q(db, "SELECT ts, close_travel_s ct FROM gw_door_event WHERE gateway_id=? "
                              "AND cam=?" + _ec + " AND close_travel_s IS NOT NULL"
                              + _ts_clause(t0, t1)[0],
                          (gw, c, *_eargs, *_ts_clause(t0, t1)[1]))
    # COUNTING-ERA SPANS, derived from validation_item stamps (first/last episode per version) —
    # never from a hardcoded date. A range that spans more than one era pools transits counted by
    # DIFFERENT logic; the payload names every era in range so the UI can label the pooling, and an
    # all-history view with two eras is a crossing by definition.
    era_rows = _q(db, "SELECT counting_version v, MIN(ts_start) lo, MAX(ts_start) hi, COUNT(*) n "
                      "FROM validation_item WHERE gateway_id=? AND counting_version IS NOT NULL"
                      + (" AND cam=?" if cam else "") + " GROUP BY counting_version ORDER BY MIN(ts_start)",
                  ([gw, cam] if cam else [gw]))
    # PEAK CAR OCCUPANCY by hour-of-day. One row per door-open episode, bucketed by the hour the
    # episode STARTED, and only episodes that carry coverage (occupancy_frames > 0) — same evidence
    # rule as _occupancy_by_cam, so the panel and this chart can never disagree about the denominator.
    # Era scoping is per camera, from camera_validation, for the same reason precision is.
    occ_ver = {r["cam"]: r["counting_version"] for r in
               _q(db, "SELECT cam, counting_version FROM camera_validation WHERE gateway_id=?", (gw,))}
    occ_w, occ_args = "", []
    if t0 is not None:
        occ_w += " AND ts_start >= ?"; occ_args.append(t0)
    if t1 is not None:
        occ_w += " AND ts_start < ?"; occ_args.append(t1)
    occ_rows = _q(db, "SELECT cam, ts_start, occupancy_max, occupancy_frames, occupancy_degraded, "
                      "counting_version FROM validation_item WHERE gateway_id=? AND occupancy_frames > 0"
                  + (" AND cam=?" if cam else "") + occ_w,
                  ([gw, cam, *occ_args] if cam else [gw, *occ_args]))
    occ_rows = [r for r in occ_rows
                if not occ_ver.get(r["cam"]) or r["counting_version"] == occ_ver.get(r["cam"])]
    # RTT by hour-of-day, for the chart. Single camera only: a fleet RTT would pool round trips from
    # different shafts, and a shaft is what a round trip is a property of.
    #
    # READ, NEVER WALK. This called _rtt_by_cam directly and that is what took /trends from ~60 s to
    # 152.8 s on the live box — a full walk added to every trends request, for a chart most requests
    # do not even show. The in-process cache could not save it: both callers build t0 from
    # time.time(), so every request produced a fresh cache key and a fresh walk.
    # AND NO LIVE FALLBACK, unlike tier2 below. tier2's inline path is affordable because every one
    # of its inputs is bounded in SQL; the RTT walk is not — the era filter is a LIKE that no index
    # covers, so it post-filters every row in the ts range and costs ~40 ms per 10k rows SCANNED
    # regardless of how few are in the era. On a custom range RTT is reported unavailable, and the
    # panel says which range would have it. An unavailable number is recoverable; a 152-second
    # request is not.
    rtt_tr, rtt_meta = None, None
    if cam:
        _rtt_cache_ok = (not era) and (not from_d) and (not to_d) and (period in ("", "all", "week"))
        if _rtt_cache_ok:
            rtt_tr, rtt_meta = _rtt_read(db, gw, cam, WINDOW_DAYS)
        else:
            rtt_meta = {"state": "not served for this range",
                        "detail": f"round trips are precomputed for the rolling {WINDOW_DAYS:g}-day "
                                  f"window on the current era only; this request overrides the era "
                                  f"or asks for a custom range, and RTT is never derived on the "
                                  f"request path",
                        "window_days": WINDOW_DAYS}
    # The first episode that EVER carried occupancy coverage for this selection, ignoring the range.
    _ofr = _q(db, "SELECT MIN(ts_start) mn FROM validation_item WHERE gateway_id=? "
                  "AND occupancy_frames > 0" + (" AND cam=?" if cam else ""),
              ([gw, cam] if cam else [gw]))
    occ_first_ts = (_ofr[0]["mn"] if _ofr else None)

    # Range-scoped Tier-2 for the heatmap: the join side reuses the transit rows already loaded
    # above (same cam, same table) instead of re-querying the fleet. Without this the heatmap kept
    # drawing all-history from /data while every other panel obeyed the picker.
    # ── TIER-2: CACHE FIRST, never derive on the request path ────────────────────────────
    # door_aggregate already holds _tier2's output for the default window, written hourly by
    # precompute_job. The /dash panel was wired to it; trends was missed and kept calling _tier2
    # inline — 26-51s per request, CPU-bound, on a box with two cores and seven live streams.
    #
    # WHEN THE CACHE APPLIES. The stored row is keyed on the CURRENT (counting_version,
    # door_version) and describes _window(WINDOW_DAYS) — a ROLLING now-7d window with no ceiling.
    # So it is served only when the caller has not overridden the era and has not asked for a
    # narrower custom range. Note the windows are not byte-identical: _range_bounds aligns to IST
    # midnight and 'all' is unbounded, while the aggregate is rolling. That is why the payload
    # carries tier2_window_days and tier2_computed_at — a 7-day tier2 must never sit silently
    # under an "all data" label. The rest of the payload still describes the requested range.
    tier2_range, tier2_source, tier2_meta = None, None, None
    if cam:
        cache_ok = (not era) and (not from_d) and (not to_d) and (period in ("", "all", "week"))
        if cache_ok:
            _dg, _t2, _m = _aggregate_read(db, gw, cam, WINDOW_DAYS)
            if _t2 is not None:
                tier2_range, tier2_source, tier2_meta = _t2, "cache", _m
        if tier2_range is None:
            # No usable aggregate (custom range, era override, or never computed). Derive here —
            # now with every input bounded in SQL above, which is what makes this affordable.
            tjoin = sorted((r["ts"], r["direction"]) for r in tr if r["ts"] is not None)
            tier2_range = _tier2(db, gw, cam, tjoin, t0, t1, era_override=era)
            tier2_source = "live"
            if cache_ok and tier2_range is not None:
                # Cache was eligible but empty: say WHY, so "slow" is attributable rather than a
                # mystery. NEVER call aggregate_refresh here — that rule is the precompute
                # docstring's and it is what keeps derivation off the request path.
                tier2_meta = {"state": (_m or {}).get("state", "no stored aggregate"),
                              "note": "computed live because no aggregate matched; the precompute "
                                      "job writes it hourly and is never triggered by a request"}
    db.close()
    if t0 is not None:
        ev = [r for r in ev if _in_range(_epoch(r["os"]), t0, t1)]
        tr = [r for r in tr if _in_range(r["ts"], t0, t1)]
        gpu_cyc = [r for r in gpu_cyc if _in_range(r["ts"], t0, t1)]
    eras_in_range = [{"version": e["v"], "first_seen": _iso_ist(e["lo"]), "last_seen": _iso_ist(e["hi"]),
                     "n_episodes": e["n"]}
                     for e in era_rows if e["lo"] is not None
                     and (t0 is None or (e["lo"] < t1 and e["hi"] >= t0))]

    prof = {h: {"cycles": 0, "boarded": 0, "alighted": 0, "closes": [], "xfer": [], "occ": [],
                "occ_degraded": 0} for h in range(24)}
    days = set()
    # ONE instrument per close-travel series: Pi (gw_event) and GPU (gw_door_event) measure the same
    # name with different edges/clocks and must never share a bucket. When the range has any GPU
    # cycles the GPU is the close instrument (it is the live one); a purely pre-retirement range
    # stays Pi. Cycle COUNTS may share buckets — the tables never overlap in time.
    close_instrument = "gpu" if gpu_cyc else ("pi" if ev else None)
    for r in ev:
        h = _local_hour(r["os"])
        if h is None:
            continue
        days.add((r["os"] or "")[:10])
        prof[h]["cycles"] += 1                                   # every opening = demand (flagged included)
        ep = _epoch(r["os"])
        clean = (r["q"] is None or r["q"] == "ok")
        if (close_instrument == "pi" and r["ct"] is not None and clean
                and ep is not None and ep >= _BOUNDARY_EPOCH):
            prof[h]["closes"].append(float(r["ct"]))             # comparable regime only
        load = (r["b"] or 0) + (r["a"] or 0)
        o, c = _epoch(r["of"]), _epoch(r["cs"])
        if load > 0 and o is not None and c is not None and c > o:
            prof[h]["xfer"].append((c - o) / load)
    for r in gpu_cyc:
        h = _ist_hour(r["ts"])
        if h is None:
            continue
        days.add(datetime.fromtimestamp(r["ts"], IST).date().isoformat())
        prof[h]["cycles"] += 1                                   # one completed close = one cycle
        if close_instrument == "gpu" and r["ct"] is not None:
            prof[h]["closes"].append(float(r["ct"]))
    for r in tr:
        try:
            h = datetime.fromtimestamp(r["ts"], IST).hour
        except Exception:
            continue
        days.add(datetime.fromtimestamp(r["ts"], IST).date().isoformat())
        prof[h]["boarded" if r["direction"] == "in" else "alighted"] += 1
    for r in occ_rows:
        h = _ist_hour(r["ts_start"])
        if h is None:
            continue
        # Episodes do NOT contribute to `days`. n_days divides the per-hour cycle and rider averages;
        # occupancy is a MAX and is not averaged over days, so letting an episode-only hour create a
        # day would change two other denominators to serve a series that does not use them.
        prof[h]["occ"].append(int(r["occupancy_max"] or 0))
        if r["occupancy_degraded"]:
            prof[h]["occ_degraded"] += 1

    # Days that actually CONTRIBUTED, reported honestly; the max(1,..) is only the divisor guard.
    # An empty range must read "0 days" rather than silently averaging over a day that had nothing.
    n_days_real = len(days)
    ndays = max(1, n_days_real)
    profile = [{"hour": h, "cycles": prof[h]["cycles"],
                "boarded": prof[h]["boarded"], "alighted": prof[h]["alighted"],
                # occ_peak is the MAX over the hour's episodes, across every day in range — the
                # busiest car this hour ever held, as a floor. occ_median gives the typical episode
                # so a single crowded lift-load cannot be read as the hourly norm.
                "occ_peak": (max(prof[h]["occ"]) if prof[h]["occ"] else None),
                "occ_median": (_pctl(sorted(prof[h]["occ"]), 0.5) if prof[h]["occ"] else None),
                "occ_n": len(prof[h]["occ"]), "occ_degraded": prof[h]["occ_degraded"],
                **{f"close_{k}": v for k, v in _stats(prof[h]["closes"]).items()}} for h in range(24)]

    def window(lo, hi):
        hrs = [h for h in range(24) if lo <= h < hi]
        cyc = sum(prof[h]["cycles"] for h in hrs)
        closes = [v for h in hrs for v in prof[h]["closes"]]
        xfer = [v for h in hrs for v in prof[h]["xfer"]]
        bo = sum(prof[h]["boarded"] for h in hrs); al = sum(prof[h]["alighted"] for h in hrs)
        span = max(1, len(hrs))
        oc = sorted(v for h in hrs for v in prof[h]["occ"])
        # C26 TRANSFER IS A PI-ERA METRIC, FULL STOP. It needs door_open_full and door_close_start,
        # which only gw_event carries — the Pi door-watch, frozen at its retirement. gw_door_event has
        # no equivalent pair, so there is no live source and there has not been one since 2026-07-21.
        #
        # Rendering it as a bare number made the metric switch instruments between tabs: a lift with
        # Pi history (ch29) showed a July figure beside GPU-era demand, while a lift without one
        # (ch27) showed "(n=0)" — two different KINDS of statement under one label, and neither said
        # which instrument it came from. The tag travels with the number now, and `applies` is False
        # when the selected range lies entirely after the retirement, where a value could not have
        # come from the range being displayed even if the pool is non-empty.
        xf_applies = (t0 is None) or (t0 < _RETIRED_EPOCH)
        return {"from": lo, "to": hi, "cycles": cyc, "cycles_per_hr": round(cyc / (span * ndays), 2),
                "boarded": bo, "alighted": al, "riders_per_hr": round((bo + al) / (span * ndays), 2),
                "close": _stats(closes),
                "transfer": {**_stats(xfer), "provisional": True,
                             "instrument": "pi_watch",
                             "instrument_note": "Pi door-watch (gw_event), RETIRED "
                                                + DOORWATCH_RETIRED_BOUNDARY[:10],
                             "applies_to_range": xf_applies,
                             "reason": (None if xf_applies else
                                        "the selected range is entirely after the Pi door-watch was "
                                        "retired; C26 has no live source, so no value can come from "
                                        "these dates")},
                # NOT per-hour and NOT averaged: a peak divided by hours is not a quantity anyone can
                # use. This is the max and the p95 of the per-episode peaks in the window, with n.
                "occupancy": {"peak": (oc[-1] if oc else None),
                              "p95": (_pctl(oc, 0.95) if oc else None),
                              "median": (_pctl(oc, 0.5) if oc else None),
                              "n": len(oc),
                              "degraded": sum(prof[h]["occ_degraded"] for h in hrs),
                              "measured_minimum": True},
                "transits_per_cycle": round((bo + al) / cyc, 2) if cyc else None}

    windows = {"all_day": window(0, 24), **{k: window(*v) for k, v in PEAK_WINDOWS.items()}}
    if 0 <= from_h < to_h <= 24:
        windows["custom"] = window(from_h, to_h)
    # THE PEAK TRAP made explicit: peak-vs-all-day ratio (a finding in itself)
    ad = windows["all_day"]["cycles_per_hr"] or 1
    for k in list(windows):
        if k != "all_day":
            windows[k]["demand_ratio_vs_allday"] = round((windows[k]["cycles_per_hr"] or 0) / ad, 2)

    return JSONResponse({"gw": gw, "cam": cam or "fleet", "n_days": n_days_real, "profile": profile,
                         "windows": windows, "tier2_range": tier2_range,
                         # WHERE tier2 CAME FROM, and WHAT WINDOW IT DESCRIBES. 'cache' is the
                         # precomputed aggregate (rolling WINDOW_DAYS, written hourly off the
                         # request path); 'live' was derived for the requested range just now.
                         # computed_at exists so the UI can show data-as-of instead of implying the
                         # numbers are current, and window_days exists because a cached 7-day tier2
                         # under an "all data" heading would be a wrong answer told confidently.
                         # first_ts is ALL-TIME, not range-scoped, on purpose: it is what lets the UI
                         # say "these hours predate the measurement" instead of printing a bare dash
                         # that reads as "the car was empty". A dash with no reason is the same
                         # failure as the 0-cycles bug two panels over.
                         "rtt": rtt_tr,
                         # The state travels with the value. Without it a pending RTT and a camera
                         # with genuinely no round trips arrive as the same null.
                         "rtt_state": (rtt_meta or {}).get("state"),
                         "rtt_detail": (rtt_meta or {}).get("detail"),
                         "rtt_computed_at": (rtt_meta or {}).get("computed_at"),
                         "rtt_window_days": (rtt_meta or {}).get("window_days"),
                         "rtt_note": {"home": RTT_HOME,
                                      "fleet": (None if cam else "RTT is per-shaft; a fleet figure "
                                                                "would pool round trips from "
                                                                "different shafts")},
                         "occupancy_note": {"label": OCC_LABEL, "calibration": OCC_CALIBRATION,
                                            "anchor": OCC_ANCHOR_NOTE,
                                            "n_episodes": len(occ_rows),
                                            "first_ts": occ_first_ts,
                                            "first_ist": (_iso_ist(occ_first_ts) if occ_first_ts
                                                          else None)},
                         "tier2_source": tier2_source,
                         "tier2_computed_at": (tier2_meta or {}).get("computed_at"),
                         "tier2_age_s": (tier2_meta or {}).get("age_s"),
                         "tier2_window_days": (WINDOW_DAYS if tier2_source == "cache" else None),
                         "tier2_cache_state": (tier2_meta or {}).get("state"),
                         "tier2_cache_note": (tier2_meta or {}).get("note"),
                         "counting_eras": {"in_range": eras_in_range, "crossing": len(eras_in_range) > 1,
                                           "note": "spans derived from validation_item episode stamps; "
                                                   "a crossing range pools transits counted by different logic"},
                         "range": {"period": period, "from_d": from_d, "to_d": to_d,
                                   "label": range_label, "t0": t0, "t1": t1,
                                   "cycles": len(ev) + len(gpu_cyc), "transits": len(tr),
                                   # provenance of the cycle count + which instrument the close
                                   # series uses — a pooled close median would be two instruments
                                   "cycles_pi": len(ev), "cycles_gpu": len(gpu_cyc),
                                   "close_instrument": close_instrument,
                                   # h3 cameras produce cycles but NO travel. Without this the
                                   # close-travel chart draws an empty axis, which reads as "no
                                   # activity" — the same misreading as the 0-cycles bug, one panel
                                   # over. The UI labels the chart from these two fields.
                                   "travel_unmeasured_cams": h3_cams,
                                   "travel_unmeasured_note": (H3_TRAVEL_NOTE if h3_cams else None),
                                   # Cameras with NO door engine history at all. Their door-derived
                                   # series are unavailable, not zero, and the difference is the
                                   # whole point: an empty cycles chart beside "cycles/hr 0" reads
                                   # as a lift that never moved.
                                   "uncalibrated_cams": uncal_cams,
                                   "uncalibrated_note": (UNCALIBRATED_NOTE if uncal_cams else None)},
                         "boundaries": {"close_travel_max": {"iso": CLOSE_TRAVEL_MAX_BOUNDARY, "epoch": _BOUNDARY_EPOCH,
                                        "note": "CLOSE_TRAVEL_MAX 10->30s; close-travel here uses the post-boundary regime only"}},
                         "data_gaps": [g for g in DATA_GAPS if (cam is None or cam in g.get("cams", []) or not g.get("cams"))]})


# ============================================================ CSV export
# The rows BEHIND every chart and table, filtered to the same range (and, for floor data, the same
# door era) the screen is showing. Anything else is a different dataset wearing the same name: an
# export that quietly spans all history would disagree with the chart above it and the chart would
# get blamed.
_EXPORT = {
    "door_cycles": "one row per door cycle (gw_event): the close-travel and load source",
    "transits":    "one row per counted crossing (transit_event)",
    "floor_events": "one row per GPU door/floor read (gw_door_event), era-filtered",
    "per_floor":   "the Tier-2 per-floor aggregate — stops, direction split, riders",
    "episodes":    "one row per door-open episode (validation_item): peak car occupancy + its coverage",
    "rtt":         "one row per ROUND TRIP: home-floor close -> next home-floor open, with stops",
}

# WHY OCCUPANCY IS ITS OWN DATASET AND NOT COLUMNS ON door_cycles. door_cycles is gw_event — the Pi
# door-watch, frozen at its retirement on 2026-07-21. Occupancy begins with the workers deployed
# 2026-08-11. The two tables do not overlap by a single row, so bolting the columns onto door_cycles
# would produce a file whose occupancy column is empty on every row it could ever contain, and a
# reader would take that as "occupancy was zero" rather than "these rows predate the measurement".
# The episode is the natural grain anyway: one door-open, one peak, one coverage denominator.


def _csv(rows, header, name):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


@dash_router.get("/dash/{gw}/health")
def dash_health(gw: str):
    """The one sentence, as plain text. Exit-code-able by anything that can curl.

    Deliberately NOT re-evaluated here — it returns what health_check.py last recorded, so this
    endpoint, the dashboard banner and whatever was pushed to a phone can never say three different
    things about the same fleet.
    """
    db = _db()
    h = _health(db, gw)
    db.close()
    if h.get("state") == "never_run":
        body = f"UNKNOWN — {h.get('note')}\n"
    else:
        body = (f"{_iso_ist(h['ts'])}  {h['line']}\n"
                + (f"NOTE: this check is {round(h['age_s'] / 3600, 1)}h old — the checker itself "
                   f"has stopped running\n" if h.get("stale") else "")
                + (f"DELIVERY: {h['delivery_error']}\n" if h.get("delivery_error") else ""))
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@dash_router.get("/dash/{gw}/rtt")
def dash_rtt(gw: str, cam: str = "", era: str = "", days: float | None = None):
    """RTT for ONE camera, over the SELECTED RANGE. Never for the fleet, never all-history.

    This used to ride /data, which serves seven cameras, so one request walked every door row of
    every camera — 115,936 on ch29 alone — and blew the 25s budget the moment the era fix made the
    walk match rows. A round trip is a property of one shaft and the panel shows one camera, so the
    work follows the selection instead of preceding it.

    cam is REQUIRED. A fleet RTT would pool round trips from different shafts, which is not a
    slower version of the right answer — it is a different and wrong one.

    AND IT READS, IT DOES NOT WALK. Taking RTT off /data was not enough: cold 21.0 s, "warm" 9.0 s
    on the live box, because the in-process cache was keyed on a t0 built from time.time() and so
    never once fired — three identical requests produced three keys and three full walks. The walk
    now runs only in aggregate_refresh, on the precompute timer, and this endpoint reads one
    indexed row.
    """
    if not cam:
        return JSONResponse({"error": "cam is required — a round trip is a property of one shaft, "
                                      "so there is no fleet RTT to compute"}, status_code=400)
    d = WINDOW_DAYS if days is None else float(days)
    _, _, window = _window(d)
    if era or abs(d - WINDOW_DAYS) > 1e-9:
        return JSONResponse(
            {"gw": gw, "cam": cam, "window": window, "rtt": None,
             "state": "not served for this range",
             "error": f"round trips are precomputed for the rolling {WINDOW_DAYS:g}-day window on "
                      f"the camera's current era. Deriving one here is what took this endpoint to "
                      f"21 s. For another range or era: tools/rtt.py, off the request path."},
            headers={"Cache-Control": "no-store"})
    db = _db()
    try:
        S, meta = _rtt_read(db, gw, cam, d)
    finally:
        db.close()
    return JSONResponse({"gw": gw, "cam": cam, "window": window,
                         "rtt": S, "state": meta.get("state"), "meta": meta,
                         # PENDING IS NOT ZERO. A miss means nobody has computed it yet; printing
                         # "no round trips" there would assert the lift never moved.
                         "error": (None if meta.get("state") == "ok" else meta.get("detail")
                                   or meta.get("state")),
                         "note": {"home": RTT_HOME,
                                  "definition": f"door CLOSED at {RTT_HOME} -> next door OPEN at "
                                                f"{RTT_HOME}, both ends a STOP not a pass"}},
                        headers={"Cache-Control": "no-store"})


@dash_router.get("/dash/{gw}/cams")
def dash_cams(gw: str):
    """Just the camera list — for the switcher on every per-camera page. Deliberately NOT
    /dash/{gw}/data: that builds the whole dashboard including Tier-2 for every camera, which is a
    lot of work to fill a dropdown, on every page load, on every page."""
    db = _db()
    cams = _cameras(db, gw)
    reg = _registry(db, gw)
    db.close()
    return JSONResponse({"gw": gw, "cameras": [
        {"cam": c["cam"], "label": c.get("label") or "",
         "enabled": bool((reg.get(c["cam"]) or {}).get("enabled"))} for c in cams]})


@dash_router.get("/dash/{gw}/export.csv")
def dash_export(gw: str, dataset: str = "door_cycles", cam: str = "",
                period: str = "all", from_d: str = "", to_d: str = "", era: str = ""):
    """Download the underlying rows. dataset = door_cycles | transits | floor_events | per_floor.

    era applies to the era-scoped datasets (floor_events, per_floor): "" = each camera's current
    era exactly as the panel resolves it; an explicit spec ("ch16=abc123h1" or a bare era) selects
    one deliberately; era=all (floor_events only) exports EVERY era with an `era` label column —
    the whole history in one Excel-able file, comparability carried per row instead of by the URL."""
    if dataset not in _EXPORT:
        return JSONResponse({"error": f"unknown dataset {dataset!r}", "datasets": _EXPORT}, status_code=400)
    t0, t1, label = _range_bounds(period, from_d, to_d)
    db = _db()
    rng = f"{from_d or 'start'}_to_{to_d or 'today'}" if (from_d or to_d) else (period or "all")
    tag = f"{gw}_{cam or 'fleet'}_{dataset}_{rng}" + ("_all-eras" if era.strip() == "all" else "")
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)

    if dataset == "door_cycles":
        rows = _q(db, "SELECT s.camera cam, e.door_open_start_ts os, e.door_open_full_ts of_, "
                      "e.door_close_start_ts cs, e.door_close_full_ts cf, e.close_travel_s ct, "
                      "e.quality q, e.boarded b, e.alighted a "
                      "FROM gw_event e JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?"
                      + (" AND s.camera=?" if cam else ""), ([gw, cam] if cam else [gw]))
        db.close()
        out = [(r["cam"], r["os"], r["of_"], r["cs"], r["cf"], r["ct"], r["q"], r["b"], r["a"])
               for r in rows if _in_range(_epoch(r["os"]), t0, t1)]
        return _csv(out, ["cam", "open_start", "open_full", "close_start", "close_full",
                          "close_travel_s", "quality", "boarded", "alighted"], f"{tag}.csv")

    if dataset == "transits":
        rows = _q(db, "SELECT cam, ts, direction, track_id FROM transit_event WHERE gateway_id=?"
                      + (" AND cam=?" if cam else "") + " ORDER BY ts", ([gw, cam] if cam else [gw]))
        db.close()
        out = [(r["cam"], r["ts"], _iso_ist(r["ts"]), r["direction"], r["track_id"])
               for r in rows if _in_range(r["ts"], t0, t1)]
        return _csv(out, ["cam", "ts_epoch", "ts_ist", "direction", "track_id"], f"{tag}.csv")

    if dataset == "rtt":
        # ONE ROW PER TRIP, plausible and anomalous alike, with the anomaly reason in a column.
        # Filtering here would hide the quality signal: the anomaly rate is a measurement of floor
        # attribution, and a file that contains only the trips that worked cannot show it.
        import rtt_core
        cams = [cam] if cam else [c["cam"] for c in _cameras(db, gw)]
        out = []
        for c in cams:
            vers = [(r["door_version"], r["mx"]) for r in _q(
                db, "SELECT door_version, MAX(ts) mx FROM gw_door_event WHERE gateway_id=? AND cam=? "
                    "AND door_version IS NOT NULL AND door_version<>'' GROUP BY door_version",
                (gw, c))]
            _ov, _s = _era_for(db, gw, c, era)
            e, _err = rtt_core.expand_era(vers, _ov if era else None)
            if _err or not e:
                continue                       # ambiguous or absent: no rows rather than wrong rows
            w, wargs = _ts_clause(t0, t1)
            rows = _q(db, "SELECT ts, floor, door_state FROM gw_door_event WHERE gateway_id=? "
                          "AND cam=? AND door_version = ?" + w + " ORDER BY ts, id",
                      (gw, c, e, *wargs))
            for t_a, t_b, dt, ns in rtt_core.trips(rows, RTT_HOME):
                why = rtt_core.classify(dt)
                out.append((c, t_a, _iso_ist(t_a), t_b, round(dt, 1), ns,
                            "" if why else "plausible", why or "", RTT_HOME, e))
        db.close()
        return _csv(out, ["cam", "start_epoch", "start_ist", "end_epoch", "rtt_s", "stops",
                          "class", "anomaly_reason", "home_floor", "era"], f"{tag}.csv")

    if dataset == "episodes":
        rows = _q(db, "SELECT cam, ts_start, ts_end, machine_boarded, machine_alighted, "
                      "occupancy_max, occupancy_frames, occupancy_degraded, analysed_frames, "
                      "human_occupancy, counting_version, status FROM validation_item "
                      "WHERE gateway_id=?" + (" AND cam=?" if cam else "") + " ORDER BY ts_start",
                  ([gw, cam] if cam else [gw]))
        db.close()
        # NO evidence filter here, unlike the panel and the chart. This is the raw grain: an episode
        # with occupancy_frames NULL or 0 stays in the file WITH its empty coverage column, because
        # dropping it would hide how much of the record carries no measurement. The `evidence` column
        # states the rule inline so the spreadsheet does not have to re-derive it — and so a filter
        # applied in Excel is the same filter the dashboard applied.
        out = [(r["cam"], r["ts_start"], _iso_ist(r["ts_start"]), r["ts_end"],
                r["machine_boarded"], r["machine_alighted"],
                r["occupancy_max"], r["occupancy_frames"], r["occupancy_degraded"],
                r["analysed_frames"], r["human_occupancy"], r["counting_version"], r["status"],
                1 if (r["occupancy_frames"] or 0) > 0 else 0)
               for r in rows if _in_range(r["ts_start"], t0, t1)]
        return _csv(out, ["cam", "ts_start_epoch", "ts_start_ist", "ts_end_epoch",
                          "machine_boarded", "machine_alighted",
                          "occupancy_max_MEASURED_MINIMUM", "occupancy_frames", "occupancy_degraded",
                          "analysed_frames", "human_occupancy", "counting_version", "status",
                          "evidence"], f"{tag}.csv")

    if dataset == "floor_events":
        # Per-camera era, exactly as the panel resolves it — a fleet export spans several cameras
        # with DIFFERENT eras, so one global LIKE would silently drop whole cameras from the file.
        # era=all: NO era filter — every row ever written, with an explicit era column, so history
        # is visible in one file and rows from different instruments are labelled, never pooled
        # silently (the 07-30 "where did my 41,789 pre-h2 rows go" — they were era-hidden, not lost).
        cams = [cam] if cam else [c["cam"] for c in _cameras(db, gw)]
        rows = []
        if era.strip() == "all":
            for c in cams:
                rows += _q(db, "SELECT cam, ts, floor, direction, door_state, read_conf, panels_agreed, "
                               "reason, close_travel_s, door_version FROM gw_door_event "
                               "WHERE gateway_id=? AND cam=? ORDER BY ts", (gw, c))
        else:
            for c in cams:
                e, _src = _era_for(db, gw, c, era)
                if not e:
                    continue
                _ec, _eargs = _era_clause(e)
                rows += _q(db, "SELECT cam, ts, floor, direction, door_state, read_conf, panels_agreed, "
                               "reason, close_travel_s, door_version FROM gw_door_event "
                               "WHERE gateway_id=? AND cam=?" + _ec + " ORDER BY ts",
                           (gw, c, *_eargs))
        db.close()
        out = [(r["cam"], r["ts"], _iso_ist(r["ts"]), r["floor"], r["direction"], r["door_state"],
                r["read_conf"], r["panels_agreed"], r["reason"], r["close_travel_s"], r["door_version"],
                str(r["door_version"] or "").split("+")[0])
               for r in rows if _in_range(r["ts"], t0, t1)]
        return _csv(out, ["cam", "ts_epoch", "ts_ist", "floor", "direction", "door_state", "read_conf",
                          "panels_agreed", "reason", "close_travel_s", "door_version", "era"], f"{tag}.csv")

    # per_floor — the aggregate as displayed, including the by-hour columns the heatmap draws
    tj = _transits_for_join(db, gw)
    cams = [cam] if cam else [c["cam"] for c in _cameras(db, gw)]
    out = []
    for c in cams:
        # era=all is a floor_events concept; per_floor is one era's aggregate by construction
        t2 = _tier2(db, gw, c, tj.get(c, []), t0, t1,
                    era_override=("" if era.strip() == "all" else era))
        if not t2:
            continue
        for f in t2["per_floor"]:
            out.append((c, f["floor"], f["floor_idx"], f["stops"], f["up_stops"], f["down_stops"],
                        f["boarded"], f["alighted"],
                        " ".join(str(v) for v in f["stops_by_hour"]),
                        " ".join(str(v) for v in f["riders_by_hour"]), t2["era"]))
    db.close()
    return _csv(out, ["cam", "floor", "floor_idx", "stops", "up_stops", "down_stops", "boarded",
                      "alighted", "stops_by_hour_0_23", "riders_by_hour_0_23", "era"], f"{tag}.csv")


@dash_router.get("/")
def root_redirect():
    """/ -> /dash. The dashboard is the front door; the fleet overview moved behind a link on it.

    NOTE: if main.py defines its own "/" route it wins over this one (whichever is registered first
    matches), and this becomes dead code rather than an error — apply_dash.sh checks for that and
    says so, because a redirect that silently never fires is worse than no redirect."""
    return RedirectResponse("/dash", status_code=307)


@dash_router.get("/dash", response_class=HTMLResponse)
def dash_page():
    gw = os.environ.get("DASH_GW", "site-A")
    return (_PAGE.replace("__GW__", gw).replace("__FLEET__", FLEET_URL)
                 .replace("__NAV__", nc.header("dash", gw))
                 .replace("</style>", nc.NAV_CSS + "</style>", 1))


_PAGE = r"""<!doctype html><meta charset=utf-8><title>liftlab · dash</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--mono:ui-monospace,Consolas,monospace;--b:#e2e2e2}
*{box-sizing:border-box}
body{background:#fafafa;color:#1a1a1a;font:14px system-ui;max-width:1080px;margin:auto;padding:14px}
h1{font-size:13px;letter-spacing:.18em;text-transform:uppercase;color:#333;margin:0 0 8px}
a{color:#0a6;text-decoration:none}a:hover{text-decoration:underline}
.mut{color:#777}.mono{font-family:var(--mono)}.big{font-size:22px;font-weight:600}
.ok{color:#127a3d}.warn{color:#b06a00}.bad{color:#c0392b}.stale{color:#c0392b}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin-bottom:10px}
.card{border:1px solid var(--b);border-radius:8px;padding:10px 12px;background:#fff}
.card h3{margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#888}
.kv{display:flex;justify-content:space-between;gap:8px;font-size:13px;padding:1px 0}
.kv b{font-family:var(--mono)}
.headline{border:2px solid #1a1a1a;border-radius:8px;padding:12px 14px;background:#fff;margin-bottom:12px}
.headline .obs{font-size:15px;line-height:1.5}
.tabs{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px;border-bottom:1px solid var(--b)}
.tab{padding:6px 12px;border:1px solid var(--b);border-bottom:none;border-radius:6px 6px 0 0;
     background:#f0f0f0;cursor:pointer;font-size:13px}
.tab.on{background:#fff;font-weight:600}
.tab .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
.panelwrap{display:grid;grid-template-columns:280px 1fr;gap:12px}
@media(max-width:720px){.panelwrap{grid-template-columns:1fr}}
.snap{width:100%;border:1px solid var(--b);border-radius:6px;background:#000;aspect-ratio:4/3;object-fit:contain}
.detail{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:520px){.detail{grid-template-columns:1fr}}
.bars{display:flex;gap:2px;align-items:flex-end;height:44px;margin:4px 0}
.bars > div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}
.bars .bar{width:100%;background:#127a3d}
.bars span{font-size:8px;color:#999;margin-top:1px}
.blank{color:#999;font-style:italic;font-size:13px;padding:6px 0}
.warnrow{background:#fff3e0;border-left:3px solid #b06a00;color:#7a5200;padding:6px 8px;border-radius:4px;font:11px/1.5 var(--mono);margin:0 0 6px}
.camlinks{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.cbtn{font:600 11px var(--mono);padding:4px 9px;border:1px solid var(--b);border-radius:12px;text-decoration:none;color:#2a6db0;background:transparent}
.cbtn:hover{background:rgba(42,109,176,.08)}
.dlbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 10px}
.dlbtn{font:600 11px var(--mono);padding:4px 9px;border:1px solid var(--line);border-radius:12px;text-decoration:none;color:#2a6db0;background:transparent}
.dlbtn:hover{background:rgba(42,109,176,.08)}
table.t2 tr.tot td{font-weight:600;border-top:2px solid var(--line)}
.gpubtn{font:600 12px system-ui;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
.gpubtn:disabled{opacity:.5;cursor:default}
/* Chart tooltip. pointerdown as well as pointerover, so a phone tap works — these charts are read
   on site as often as at a desk, and hover does not exist there. */
#tip{position:fixed;z-index:99;display:none;pointer-events:none;background:#11181d;color:#eef3f6;
  font:11px/1.4 var(--mono,ui-monospace,Menlo,monospace);padding:5px 8px;border-radius:6px;
  box-shadow:0 2px 10px rgba(0,0,0,.28);max-width:220px}
.intent{color:#6b7a84;font:12px/1.45 system-ui,sans-serif;margin:-2px 0 6px}
.hmwrap{overflow-x:auto}
.tog{font:600 11px var(--mono,monospace);padding:3px 9px;border:1px solid #d6dee3;border-radius:12px;
  background:transparent;color:#6b7a84;cursor:pointer;margin-left:6px}
.tog.on{background:#2a6db0;border-color:#2a6db0;color:#fff}
table.t2{width:100%;border-collapse:collapse;margin-top:8px;font-size:12px}
table.t2 th{text-align:right;color:#888;font-weight:500;padding:2px 6px;border-bottom:1px solid #e3e8ec}
table.t2 th:first-child,table.t2 td:first-child{text-align:left}
table.t2 td{text-align:right;padding:2px 6px;border-bottom:1px solid #f2f5f7;font-variant-numeric:tabular-nums}
.foot{margin-top:14px;font-size:12px}
</style>
__NAV__
<h1 style="padding:0 2px">liftlab · dash <span class=mut id=stamp></span></h1>
<div class=tabs id=nav></div>
<div id=healthbar></div>
<div class=strip id=strip></div>
<div id=headline></div>
<div id=unavail></div>
<div id=camview><div class=tabs id=tabs></div><div id=panel></div></div>
<div id=trendview style="display:none"></div>
<div id=tip></div>
<div class=foot mut>deep views: <a href="__FLEET__">Pi fleet</a> · <a href="/ops/__GW__">/ops</a> ·
  <a href="/events">/events</a> · <a href="/validate">/validate</a> ·
  <a href="/pihealth/__GW__">/pihealth</a></div>
<script>
var GW="__GW__", cur=null, DATA=null;
// DEEP LINK: /dash?cam=ch16 opens that camera's tab. Every wizard page breadcrumbs back here, and
// "back to dash" that dumps you on a different camera is not a breadcrumb.
(function(){var m=/[?&]cam=([A-Za-z0-9._-]+)/.exec(location.search); if(m)cur=m[1];})();
// ?view=trends was WRITTEN by selectCam and read by nothing — a URL parameter the page emitted and
// then ignored, so a shared link always landed on Cameras. Read here, applied once the first data
// load has run (setMode needs the DOM nodes and the camera list).
var WANT_VIEW=(/[?&]view=trends\b/.test(location.search))?'trends':'cams';
function esc(s){return s==null?'':(''+s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function age(s){return s==null?'—':(s<90?Math.round(s)+'s':Math.round(s/60)+'m')+' ago'}
function kv(k,v,c){return '<div class=kv><span class=mut>'+k+'</span><b class="'+(c||'')+'">'+v+'</b></div>'}
function staleCls(s,lim){return s==null?'stale':(s>lim?'stale':'ok')}

// ── DAILY HEALTH LINE ────────────────────────────────────────────────────────────────────
// The first thing on the page, above every panel, because it is the one line that answers "is
// anything silently missing". It is DISPLAYED, never derived here — health_check.py records it on a
// timer and this shows what was recorded, so the banner and whatever was pushed cannot disagree.
function healthbar(d){
  var el=document.getElementById('healthbar'); if(!el)return;
  var h=d.health;
  if(!h){el.innerHTML='';return;}
  function box(bg,bd,html){return '<div style="margin:4px 0 8px;padding:6px 10px;border-left:4px solid '
    +bd+';background:'+bg+';font-size:13px">'+html+'</div>';}
  if(h.state==='never_run'){
    // NOT the same as healthy. An absent checker must never render as a clean bill of health.
    el.innerHTML=box('rgba(176,106,0,.07)','#b06a00',
      '<b>HEALTH CHECK HAS NEVER RUN</b> — '+esc(h.note||'')
      +'. Nothing is watching for a silent camera; the absence of an alert means nothing yet.');
    return;
  }
  var extra='';
  // A STALE CHECK IS ITS OWN FAULT, and the one thing the checker cannot report about itself.
  if(h.stale)extra+=' <b class=bad>· this verdict is '+(h.age_s/3600).toFixed(1)+'h old — the CHECKER'
    +' has stopped running, so it is not evidence about now</b>';
  // No push channel means the only place this line exists is the screen you are looking at.
  if(h.delivery_error)extra+=' <span class=warn>· '+esc(h.delivery_error)+'</span>';
  if(h.state==='ok'){
    el.innerHTML=box('rgba(18,122,61,.06)','#127a3d',
      '<b class=ok>ALL '+h.n_cams+' CAMERAS POSTING</b> <span class=mut>checked '+age(h.age_s)
      +' ago</span>'+extra);
  } else {
    el.innerHTML=box('rgba(176,0,0,.07)','#b00',
      '<b class=bad>'+h.n_bad+' OF '+h.n_cams+' CAMERAS SILENT</b> — '+esc(h.line||'')
      +' <span class=mut>(checked '+age(h.age_s)+' ago)</span>'+extra);
  }
}
function strip(d){
  var p=d.pi,r=d.relay,g=d.gpu,h=[];
  // PI
  if(!p){h.push('<div class=card><h3>Pi</h3><div class=blank>no watch_status — is liftlab-watch running?</div></div>');}
  else{var ps=staleCls(p.age_s,60);
    h.push('<div class=card><h3>Pi · '+esc(p.camera||'')+'</h3>'
      +kv('state',esc(p.state)+'  <span class="'+ps+'">('+age(p.age_s)+')</span>')
      +kv('signal_fps',p.signal_fps==null?'—':(+p.signal_fps).toFixed(2),(p.signal_fps<6?'bad':'ok'))
      +kv('temp / throttle',(p.soc_temp==null?'—':(+p.soc_temp).toFixed(1)+'°C')+' · '+((p.throttle_live&&p.throttle_live!=='')?'<span class=bad>THROTTLE</span>':'clean'))
      +kv('opens / emitted',esc(p.opens_detected)+' / '+esc(p.cycles_emitted))
      +kv('detect:emit',p.detect_emit==null?'—':p.detect_emit+'×',(p.detect_emit>=1.5?'warn':'ok'))+'</div>');}
  // RELAY
  if(!r){h.push('<div class=card><h3>Relay</h3><div class=blank>no relay_status</div></div>');}
  else{h.push('<div class=card><h3>Relay</h3>'
      +kv('delivering',esc(r.streams_delivering)+' streams','ok')
      +kv('throughput',r.sum_delivered_mbps==null?'—':(+r.sum_delivered_mbps).toFixed(1)+' Mbps')
      +kv('updated','<span class="'+staleCls(r.age_s,30)+'">'+age(r.age_s)+'</span>')+'</div>');}
  // GPU
  var gd=g&&g.worst;
  var gc=g&&g.any_up?'ok':'bad';
  h.push('<div class=card><h3>GPU analyzer</h3>'
    +kv('status',(g&&g.any_up)?'<span class=ok>up</span>':'<span class=bad>down</span>')
    +kv('running',(g&&g.cams_up&&g.cams_up.length)?esc(g.cams_up.join(', ')):'—')
    +(gd?kv('throughput',(gd.proc_ms==null?'—':Math.round(gd.proc_ms)+'/'+Math.round(gd.budget_ms||2000)+'ms  ('+((gd.proc_ms/(gd.budget_ms||2000)).toFixed(2))+'x)'),(gd.proc_ms/(gd.budget_ms||2000)>=1?'bad':'ok')):'')
    +(gd?kv('drop',(gd.drop_frac==null?'—':(gd.drop_frac*100).toFixed(2)+'%'),(gd.drop_frac>=0.01?'bad':'ok')):'')+'</div>');
  // Same fixed-scope problem as the compliance panel: the Pi card is the ONE camera the Pi watch
  // runs, not the selected tab, and the GPU card is gateway-wide. Labelled, not made dynamic.
  document.getElementById('strip').innerHTML=h.join('')
    +'<div class=mut style="font-size:11px;flex-basis:100%;margin-top:-2px">Pi card = '
    +esc((d.pi&&d.pi.camera)||'ch29')+' (reference camera, the Pi watch runs one); '
    +'Relay and GPU cards are gateway-wide. None of these three follow the camera tabs below.</div>';
}

function headline(d){
  if(!d.headline||!d.headline.length){document.getElementById('headline').innerHTML='';return;}
  var h=d.headline.map(function(x){
    var susp=x.measurement_suspect?'<span class="pill bad" style="font-size:10px;margin-right:6px">MEASUREMENT SUSPECT</span>':'';
    var tag='<span class="pill '+(x.live?'ok':'mut')+'" style="font-size:10px;margin-right:6px">'
      +(x.live?'LIVE · GPU · era '+esc((x.era||'').slice(0,8)):'RETIRED · Pi-watch')+'</span>';
    var sup=x.superseded?'<span class="pill bad" style="font-size:10px;margin-right:6px">SUPERSEDED</span>':'';
    var dl;
    if(x.travel_unmeasured){
      // h3: cycles ARE measured, travel is NOT. Distinct from "no cycles" — the count is the proof
      // the engine is working, and stating it here stops the null median reading as a dead camera.
      dl=x.cam+' door close: <b>travel not measured</b> — state-only engine, '
        +'<b>'+esc(x.n_cycles)+'</b> completed cycles in range'
        +(x.n_reopens!=null?(' ('+esc(x.n_reopens)+' reopens)'):'')
        +' · sheet assumes <b>'+x.sheet_s.toFixed(2)+'s</b>'
        +' · Bank '+esc(x.bank)+' non-compliant above <b>'+x.compliance_s.toFixed(2)+'s</b>'
        +' · <span class=mut>no observed travel to compare — see the hand-timed line below</span>';
    } else if(x.median==null){
      // A GPU-era line with no cycles is the honest "gap is real" signal condition (b) asks for.
      dl=x.cam+' door close: '+(x.reason?('<b class=bad>'+esc(x.reason)+'</b>'):'no clean close measured yet');
    } else if(x.measurement_suspect){
      // Do NOT lead with the raw median (0.08s is an artifact). Show the plausible-only number and
      // name why the raw one is not to be quoted.
      dl=x.cam+' door close: <b class=bad>raw median '+x.median+'s NOT USABLE</b> — '+esc(x.n_impossible)
       +' of '+x.n+' cycles are physically impossible (sub-frame or gap-spanning, tracker pairing bug).'
       +' Plausible-only: '+(x.plausible_median==null?'—':('median <b>'+x.plausible_median+'s</b> (n='+x.plausible_n+')'))
       +' · not quotable until the time-guard fix repopulates the stream.';
    } else {
      dl=x.cam+' door close: observed median <b>'+x.median+'s</b> (p85 '+x.p85+'s, n='+x.n+')'
       +' · sheet assumes <b>'+x.sheet_s.toFixed(2)+'s</b>'
       +' · Bank '+esc(x.bank)+' non-compliant above <b>'+x.compliance_s.toFixed(2)+'s</b>'
       +' · <b class="'+((x.pct_exceed||0)>=50?'bad':'warn')+'">'+esc(x.pct_exceed)+'%</b> of observed closes exceed '+x.compliance_s.toFixed(2)+'s';
    }
    var out='<div class="obs mono">'+tag+sup+susp+dl+'</div>';
    if(x.superseded){
      out+='<div class=mut style="font-size:11px;border-left:3px solid #b00;padding-left:6px;margin:2px 0 4px">'
        +'<b>SUPERSEDED — instrument invalidated 2026-08-05, see validation.</b> This figure came from '
        +'the h2 edge-column tracker. Offline replay against hand-timed video found it detects ~62% of '
        +'real closes, emitted 41 phantom cycles inside verified door-CLOSED windows on ch27, and '
        +'produced one travel value per camera against 4 and 11 timed closes. Kept visible for era '
        +'hygiene; not a current measurement.</div>';
    }
    // C26 passenger transfer — beside door-close, same format. PROVISIONAL (transit precision).
    if(x.transfer_sheet_s!=null){
      var tl=(x.transfer_median==null)?(x.cam+' transfer: no counted cycles yet'):
        (x.cam+' transfer: observed median <b>'+x.transfer_median+' s/person</b> (n='+x.transfer_n+')'
         +' · sheet assumes <b>'+x.transfer_sheet_s.toFixed(2)+' s/person</b>');
      out+='<div class="obs mono">'+tl+'</div>'
        +'<div class=mut style="font-size:11px">transfer is PROVISIONAL — depends on the transit counts (ch29 80% precision on n=66, re-validating under 11m); firm once ch29 goes live.</div>';
    }
    return out;
  }).join('<hr style="border:none;border-top:1px solid #eee;margin:8px 0">');
  // THE VALIDATED LINE. The only close-travel measurement on this page that survived validation is
  // hand timing off video the engine never saw. It is a THIRD instrument and sits apart from both
  // engine lines. Assumption beside observation, no verdict — the panel's own rule.
  var handline='<hr style="border:none;border-top:1px solid #eee;margin:8px 0">'
    +'<div class="obs mono">'
    +'<span class="pill ok" style="font-size:10px;margin-right:6px">VALIDATED · hand-timed</span>'
    +'hand-timed ground truth (2026-08-05): close travel median <b>~2.0–2.2s</b>, n=15, both measured '
    +'lifts · sheet assumes <b>2.00s</b> · Bank C cliff <b>2.31s</b> NOT exceeded by the median'
    +'</div>'
    +'<div class=mut style="font-size:11px">extended-close tail under observation. Frame-anchored '
    +'endpoints on clean, continuous video; this is the instrument the engines were graded against, '
    +'not an engine output. It covers two cameras on one day and is not a continuous series — the '
    +'weekly hand-timed sample is what turns it into one.</div>';
  var note=(d.boundaries&&d.boundaries.doorwatch_retired)?('<div class=mut style="font-size:11px;margin-top:6px">Pi-watch (gw_event) and GPU engine (gw_door_event) are DIFFERENT INSTRUMENTS, split at '+esc(d.boundaries.doorwatch_retired.slice(0,10))+' (Pi door-watch retired). Their close-travel numbers are shown separately and are NOT comparable.</div>'):'';
  note=handline+note;
  // FIXED SCOPE, LABELLED. This panel is driven by DOOR_SPECS, which contains ch29 only, so it does
  // NOT follow the camera tab above it — clicking ch27 leaves this showing ch29 and, unlabelled,
  // that reads as ch27's compliance (reported 2026-08-05). Naming the camera is the fix; making it
  // follow the tab would need a spec per camera, which is a data question, not a view one.
  var specCams=[];
  (d.headline||[]).forEach(function(x){if(specCams.indexOf(x.cam)<0)specCams.push(x.cam)});
  var scope=specCams.length?(' · '+esc(specCams.join(', '))+' (reference camera'+(specCams.length>1?'s':'')+')'):'';
  document.getElementById('headline').innerHTML='<div class=headline><h3 class=mut style="margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase">compliance — assumption beside observation, no verdict'+scope+'</h3>'
    +'<div class=mut style="font-size:11px;margin:-2px 0 6px">this panel does not follow the camera tabs — it shows the cameras with a compliance spec on file</div>'
    +h+note+'</div>';
}

function unavail(d){
  var u=d.unavailable;
  if(!u){document.getElementById('unavail').innerHTML='';return;}
  document.getElementById('unavail').innerHTML=
    '<div class=card style="border-color:#b06a00;background:#fffaf0"><h3 style="color:#b06a00">not available — '+esc(u.reason)+'</h3>'
    +'<div class=mut style="font-size:12px;margin-bottom:4px">'+esc(u.detail)+' · Tier-1 ceiling. These need per-floor data:</div>'
    +'<ul style="margin:2px 0 4px 18px;font-size:13px">'+u.blocks.map(function(b){return '<li>'+esc(b)+'</li>'}).join('')+'</ul>'
    +'<div class=mut style="font-size:12px">unlock: '+esc(u.unlock)+'</div></div>';
}

function tabs(d){
  if(!cur){ cur = (d.cameras[0]&&d.cameras[0].cam)||''; }
  if(!trCam){ trCam = cur; }                 // default once, through the same value as the tab
  if(cur && trCam && cur !== trCam){ trCam = cur; }   // belt and braces: they can never diverge
  document.getElementById('tabs').innerHTML=d.cameras.map(function(c){
    var live=c.snap&&!c.snap.stale, dot=live?'#127a3d':(c.snap?'#b06a00':'#ccc');
    return '<div class="tab'+(c.cam===cur?' on':'')+'" onclick="pick(\''+c.cam+'\')">'
      +'<span class=dot style="background:'+dot+'"></span>'+esc(c.cam)+(c.label?' '+esc(c.label):'')+'</div>';
  }).join('');
}
// THE ONE SELECTOR. There used to be two — `cur` for the Dash tab and `trCam` for Trends — both
// seeded from ?cam= and then free to drift, because pick() wrote `cur` and the trends tab strip
// wrote `trCam`, and neither wrote the other or the URL. A single page could therefore show ch32 in
// the URL, ch27 highlighted, and ch16's per-floor panel, which is not a cosmetic problem: floors
// belong to one shaft, so a mismatched panel is a chart of a different building column under the
// wrong heading. Seeding both from the URL (the earlier partial fix) only made them agree at load.
//
// Everything that selects a camera goes through here, and nothing else assigns `cur` or `trCam`.
function selectCam(cam, opts){
  opts = opts || {};
  if(!cam) return;
  cur = cam; trCam = cam;
  // Do NOT carry the previous camera's RTT into this one's card, even for the instant before the
  // fetch lands — that is the stale-payload defect from the trends view, one panel over.
  try{history.replaceState(null,'','/dash?cam='+encodeURIComponent(cam)
      +(mode==='trends'?'&view=trends':''));}catch(e){}
  render();
  // Trends holds its own fetched payload, so it must be re-fetched for the new camera — but only
  // when it is the visible view or already loaded, so switching camera on the Dash tab does not
  // fire a trends request nobody asked for.
  if(!opts.noTrends && (mode==='trends' || TR)) loadTrends();
}
function pick(cam){ selectCam(cam); }

function bars(door){
  if(!door||!door.n){return '';}
  var h=door.hist, e=door.hist_edges, mx=Math.max.apply(null,h.concat([1]));
  var labs=[]; for(var i=0;i<=e.length;i++){labs.push(i<e.length?('<'+e[i]):('≥'+e[e.length-1]));}
  var b=h.map(function(c,i){var ht=Math.round(4+36*c/mx);
    return '<div><span>'+c+'</span><div class=bar style="height:'+ht+'px"></div><span>'+labs[i]+'</span></div>';}).join('');
  return '<div class=bars>'+b+'</div>';
}

function panel(d){
  var c=d.cameras.filter(function(x){return x.cam===cur})[0];
  if(!c){document.getElementById('panel').innerHTML='';return;}
  var snap=c.snap
    ? '<img class=snap src="/snap/'+GW+'/'+c.cam+'.jpg?t='+Date.now()+'"><div class="mut" style="font-size:12px;margin-top:2px">frame <span class="'+staleCls(c.snap.age_s,20)+'">'+age(c.snap.age_s)+'</span></div>'
    : '<div class=snap style="display:flex;align-items:center;justify-content:center;color:#666">no snapshot</div>';

  // DOOR — split by instrument. Pi (gw_event) is retired history; GPU (gw_door_event) is live.
  // ch16 has no Pi history but does have GPU cycles, so this is where its count+median become
  // visible numbers (condition b) — or an honest "reads but no cycles" if the pairs are missing.
  var g=(d.door_gpu||{})[c.cam];
  var piDoor=c.door&&c.door.total? (
    '<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">Pi-watch · retired</div>'
    +kv('cycles today / total',esc(c.door.today)+' / '+esc(c.door.total))
    +kv('close median / p85',(c.door.median==null?'—':c.door.median+'s')+' / '+(c.door.p85==null?'—':c.door.p85+'s')+'  (n='+c.door.n+')')
    +kv('last cycle',esc((c.door.last_open||'').slice(11,19)||'—'))
    +bars(c.door)
  ) : '';
  var gpuDoor;
  if(!g||g.era==null){ gpuDoor='<div class=blank>GPU door engine: no reads in any era</div>'; }
  else if(g.n_cycles===0){ gpuDoor='<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">GPU engine · era '+esc((g.era||'').slice(0,8))+' · LIVE</div>'
    +kv('floor-read rows',esc(g.n_rows))
    +'<div class=blank style="color:#b06a00">'+esc(g.reason||'no completed open→close cycle')+'</div>'; }
  else {
    // THE CAVEAT TRAVELS WITH THE NUMBER. The compliance panel marks h2-era travel SUPERSEDED; this
    // card printed the same figure as "LIVE" with no caveat, because the rule lived in a loop that
    // skipped every camera without a DOOR_SPECS entry — ch16 showed "1.47s · LIVE" from the very
    // instrument the panel above declares invalidated. Reported 2026-08-13.
    var sup=g.superseded;
    gpuDoor='<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">GPU engine · era '+esc((g.era||'').slice(0,8))+(sup?' · <span class=warn>SUPERSEDED</span>':' · LIVE')+'</div>'
    +kv('close cycles',esc(g.n_cycles))
    +kv('close median / p85',(g.median==null?'—':g.median+'s')+' / '+(g.p85==null?'—':g.p85+'s')+'  (n='+g.n+')'
        +(sup?' <span class=warn>· not a current measurement</span>':''))
    +kv('range',(g.min==null?'—':g.min+'–'+g.max+'s'))
    +(sup?('<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)">'
           +esc(g.superseded_note||'')+'.</div>'):'')
    // The cycle count is an ASSERTION built on state transitions, and the dwell threshold that would
    // make it a safe one has not been graded against hand truth yet. Say so where it is read, not
    // only in a release note — the same discipline the h2 travel figures get.
    +(g.cycles_provisional?('<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)">'
           +esc(g.cycle_caveat||'')+'.</div>'):'')
    +bars(g); }
  var door=(piDoor||gpuDoor)?(piDoor+(piDoor&&gpuDoor?'<hr style="border:none;border-top:1px solid var(--b);margin:8px 0">':'')+gpuDoor)
    : '<div class=blank>no door cycles recorded</div>';

  // TRANSIT
  var t=c.transit;
  var trans=t? (
    kv('boarded / alighted today','<span class=ok>'+esc(t.boarded_today)+'</span> / '+esc(t.alighted_today))
    +kv('total','+'+esc(t.boarded_total)+' / -'+esc(t.alighted_total))
    +kv('last transit',t.last_ts?age(d.t-t.last_ts):'—')
    +kv('source',esc(t.source||'—'))
  ) : '<div class=blank>no transit counts</div>';

  // PEAK CAR OCCUPANCY — a FLOOR, and the card says so in the first line, not in a footnote. Every
  // figure here is "at least this many": the detector misses people, bodies occlude each other, and
  // the foot anchor drops anyone whose feet leave the cabin polygon. Printed without that, a peak of
  // 3 in a 13-person car reads as a quarter-full lift when the frame showed 5-7 people.
  var oc=c.occupancy, occCard;
  if(!oc||!oc.n){
    occCard='<div class=blank>no episodes with occupancy coverage in this window'
      +((oc&&oc.n_episodes)?(' — '+oc.n_episodes+' episode(s) present, none carrying occupancy_frames &gt; 0'
        +(oc.n_off_era?(', '+oc.n_off_era+' counted under a different version'):'')):'')+'</div>';
  } else {
    occCard='<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">'
      +'measured minimum · floor, not a count</div>'
      +kv('peak today',(oc.today_peak==null?'—':'<b>'+oc.today_peak+'</b> people')
          +' <span class=mut>(n='+oc.today_n+' episodes)</span>')
      +kv('peak in window','<b>'+oc.peak+'</b> people <span class=mut>(n='+oc.n+')</span>')
      +kv('p95 / median',(oc.p95==null?'—':oc.p95)+' / '+(oc.median==null?'—':oc.median))
      +(oc.n_no_evidence?kv('no coverage','<span class=warn>'+oc.n_no_evidence+'</span> of '
          +oc.n_episodes+' episodes had no analysed frames — excluded'):'')
      +(oc.n_degraded?kv('degraded','<span class=warn>'+oc.n_degraded+'</span> of '+oc.n
          +' episodes measured while dropping &gt;20% of segments'):'')
      +(oc.n_off_era?kv('other era',oc.n_off_era+' episode(s) counted under a different '
          +'counting version — excluded, not pooled'):'')
      +'<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)">'
      +'<b>'+esc(oc.peak)+' is a MEASURED MINIMUM</b> — '+esc(oc.calibration)+'. '
      +esc(oc.anchor_note)+'.</div>';
  }

  // ROUND TRIP TIME — the coefficient the whole MEP-02 sheet resolves to.
  // Every absence names itself, and the caveat travels with the number rather than living in a
  // release note: RTT runs at dwell=0 because every threshold tested destroyed real closes, so
  // sub-second chatter at the home floor can FRAGMENT a trip and bias the median LOW.
  // FETCHED LAZILY, for the selected camera only. RTT walks door rows and /data serves seven
  // cameras; computing it there cost the whole endpoint its budget. RTT_BY[cam] is filled by
  // loadRTT() and the card renders "measuring" until it arrives — an honest wait, not a blank.
  var rt=RTT_BY[c.cam], rttCard;
  if(rt===undefined){ rttCard='<div class=blank>measuring round trips…</div>'; loadRTT(c.cam); }
  else if(rt===null){ rttCard='<div class=blank>RTT request failed — reload to retry</div>'; }
  else if(rt.state==='pending'){
    // Named, not blank: this is the precompute not having reached this era/camera yet, which no
    // reload fixes and which must never be read as "this lift made no round trips".
    rttCard='<div style="padding:10px;border:1px dashed #667;border-radius:6px">'
      +'<b>RTT NOT YET COMPUTED</b><div class=mut style="font-size:11px;margin-top:4px">'
      +esc(rt.note||'')+'</div></div>';
  }
  else if(rt.state==='too_many_rows'){
    rttCard='<div style="padding:10px;border:1px dashed #b06a00;border-radius:6px">'
      +'<b>RANGE TOO WIDE TO WALK</b><div class=mut style="font-size:11px;margin-top:4px">'
      +esc(rt.note||'')+'</div></div>';
  }
  else if(rt.state==='no_era'){ rttCard='<div class=blank>'+esc(rt.note||'no door-engine era')+'</div>'; }
  else if(rt.state==='no_rows'){ rttCard='<div class=blank>no door rows in this window (era '+esc((rt.era||'').slice(0,12))+')</div>'; }
  else if(rt.state==='era_ambiguous'){ rttCard='<div style="padding:10px;border:1px dashed #b06a00;border-radius:6px">'
      +'<b>ERA AMBIGUOUS</b><div class=mut style="font-size:11px;margin-top:4px">'+esc(rt.note||'')+'</div></div>'; }
  else if(rt.state==='no_floor'){
    rttCard='<div style="padding:10px;border:1px dashed #b06a00;border-radius:6px">'
      +'<b>RTT UNAVAILABLE \u2014 no floor attribution</b>'
      +'<div class=mut style="font-size:11px;margin-top:4px">'+esc(rt.note||'')
      +'. A round trip is defined by the home floor, so without a floor read there is no trip to '
      +'measure \u2014 this is not a lift that made no journeys.</div></div>';
  } else {
    var ar=rt.anomaly_rate;
    rttCard='<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">'
      +'door closed at '+esc(rt.home)+' &rarr; next door open at '+esc(rt.home)+'</div>'
      +kv('median / p85',(rt.all_day.median==null?'\u2014':rt.all_day.median+'s')+' / '
          +(rt.all_day.p85==null?'\u2014':rt.all_day.p85+'s')+' <span class=mut>(n='+rt.all_day.n+')</span>')
      +kv('AM / PM peak',(rt.windows['AM peak'].median==null?'\u2014':rt.windows['AM peak'].median+'s')
          +' / '+(rt.windows['PM peak'].median==null?'\u2014':rt.windows['PM peak'].median+'s'))
      +kv('anomaly rate',(ar==null?'\u2014':ar+'%')+' <span class=mut>('+rt.n_anomalies+' of '+rt.n_trips
          +' trips outside 30\u2013600s)</span>',(ar!=null&&ar>25?'bad':''))
      +kv('stops per trip',(rt.stops.median==null?'\u2014':rt.stops.median)+' <span class=mut>(p85 '
          +(rt.stops.p85==null?'\u2014':rt.stops.p85)+')</span>')
      +(ar!=null&&ar>25?('<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b00;background:rgba(176,0,0,.07)">'
        +'<b>anomaly rate above 25%</b> \u2014 this is a statement about FLOOR ATTRIBUTION, not about the '
        +'lift. Treat the median as provisional.</div>'):'')
      +(rt.stops.median!=null&&rt.stops.median<=2?('<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)">'
        +'<b>stops per trip is low</b> \u2014 a real round trip may be arriving as two. A plausible RTT '
        +'with implausible stops is a fragmented trip.</div>'):'')
      +'<div class=mut style="font-size:11px;margin-top:6px;padding:4px 6px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)">'
      +esc(rt.caveat||'')+'.</div>'
      +'<div class=mut style="font-size:11px;margin-top:4px">'+esc(rt.travel_gap||'')+'.</div>';
  }

  // STATE
  var a=c.analyzer, v=c.validation, state;
  if(!a){state='<div class=blank>not analysed — relay only</div>';}
  else{
    var vs=v?(v.state==='live'?'<span class=ok>live</span>':'<span class=warn>'+esc(v.state)+'</span>'):'—';
    state=kv('analyser',(a.up?'<span class=ok>up</span>':'<span class=bad>down</span>')+' ('+age(a.age_s)+')')
      +kv('mode / state',esc(a.mode||'—')+' · '+vs)
      +(v&&v.precision!=null?kv('precision',v.precision+'% on n='+v.n_reviewed):'')
      +kv('counting',esc((a.counting_version||(v&&v.counting_version))||'—'));
  }
  // Setup wizard entry points, beside validate: this is where an operator looks when a NEW camera
  // needs calibrating, so the flow has to be discoverable from here rather than from a runbook.
  // Deep views as BUTTONS beside the snapshot, not small print in a footer. This panel is where an
  // operator stands when they want to look closer at ONE camera, so every next place they might go
  // has to be visible from here.
  var link='<div class=camlinks>'
    +'<a class=cbtn href="/floorcheck/'+GW+'/'+c.cam+'">Floorcheck</a>'
    +'<a class=cbtn href="/validate?cam='+c.cam+'">Validate</a>'
    +'<a class=cbtn href="/ops/'+GW+'">Ops</a>'
    +'<a class=cbtn href="/calibrate?cam='+c.cam+'">Calibrate</a>'
    +'</div>'
    +'<div class=camlinks style="margin-top:2px">'
    +'<span class=mut style="font-size:11px;align-self:center">setup</span>'
    +'<a class=cbtn href="/calib-roi/'+GW+'/'+c.cam+'">ROIs</a>'
    +'<a class=cbtn href="/calib-label/'+GW+'/'+c.cam+'">label</a>'
    +'<a class=cbtn href="/calib-cells/'+GW+'/'+c.cam+'">cells</a>'
    +'</div>';

  document.getElementById('panel').innerHTML=
    '<div class=panelwrap><div>'+snap+link+'</div>'
    +'<div class=detail>'
    +'<div class=card><h3>Door</h3>'+door+'</div>'
    +'<div class=card><h3>Transit</h3>'+trans+'</div>'
    +'<div class=card><h3>Car occupancy <span class=mut style="font-weight:400">peak per door-open</span></h3>'+occCard+'</div>'
    +'<div class=card><h3>Round trip time <span class=mut style="font-weight:400">RTT &rarr; interval &rarr; compliance</span></h3>'+rttCard+'</div>'
    +'<div class=card><h3>State</h3>'+state+'</div>'
    +'<div class=card><h3>GPU analysis</h3>'+gpuToggle(d,c.cam)+'</div>'
    +'<div class=card><h3>Camera</h3>'+kv('channel',esc(c.channel))+kv('label',esc(c.label||'—'))
      +kv('snapshot',c.snap?('<span class="'+staleCls(c.snap.age_s,20)+'">'+age(c.snap.age_s)+'</span>'):'—')+'</div>'
    +'</div></div>'
    + tier2card((d.tier2||{})[c.cam], c.cam);
}

// ── ERA SELECTOR ─────────────────────────────────────────────────────────────────────────
// Auto-newest is right for operations and wrong for archaeology: the h2 rollover hid a week of
// rows on every camera and read as data loss. ERA[cam] holds a deliberate selection; '' = auto.
// The selection rides every data/trends/CSV fetch, so the panel, heatmap and exports always
// describe the same rows.
var ERA={};
function eraQuery(){
  var parts=Object.keys(ERA).filter(function(c){return ERA[c]}).map(function(c){return c+'='+ERA[c]});
  return parts.length?('era='+encodeURIComponent(parts.join(','))):'';
}
function setEra(cam,v){ERA[cam]=v;load();if(TR)loadTrends();}
function eraSelector(t,cam){
  var es=t.eras||[];
  if(es.length<2)return '';                     // one era = nothing to select
  function d(x){return x?new Date(x*1000).toLocaleDateString():'?'}
  var opts='<option value="">auto — newest ('+esc(t.eras[0].era)+')</option>'
    +es.map(function(e){
      return '<option value="'+esc(e.era)+'"'+((ERA[cam]||'')===e.era?' selected':'')+'>'
        +esc(e.era)+' · '+e.rows+' rows · '+d(e.first)+'–'+d(e.last)+'</option>';}).join('');
  return '<span style="margin-left:8px">era: <select onchange="setEra(\''+esc(cam)+'\',this.value)" '
    +'style="font:inherit;font-size:11px">'+opts+'</select></span>'
    +((ERA[cam])?' <b class=warn>viewing a selected era, not the live one</b>':'');
}

// ── GPU camera registry (wizard piece 5) ─────────────────────────────────────────────────
// Enabling a camera here is the whole deployment step: the fleet supervisor polls the registry and
// starts a worker. No env edit, no SSH, no restart. The button says what will happen and roughly
// when, because "nothing visibly happened" is the failure mode of an async toggle.
function gpuToggle(d,cam){
  var r=(d.registry||{})[cam];
  var on=r&&r.enabled;
  var body=kv('state',on?'<span class=ok>ENABLED</span>':'<span class=mut>disabled</span>')
    +(r?kv('stride / analyze_fps',esc(r.stride)+' / '+esc(r.analyze_fps||0)):'')
    +'<div style="margin-top:6px"><button class=gpubtn data-cam="'+esc(cam)+'" data-on="'+(on?'0':'1')+'">'
    +(on?'Disable analysis':'Enable analysis')+'</button>'
    +'<span class=mut id=gpumsg-'+esc(cam)+' style="font-size:11px;margin-left:8px"></span></div>';
  if(!r){body+='<div class=mut style="font-size:11px;margin-top:4px">not in the registry yet — enabling adds it</div>';}
  return body;
}
document.addEventListener('click',function(e){
  var b=e.target; if(!b.classList||!b.classList.contains('gpubtn'))return;
  var cam=b.getAttribute('data-cam'), on=b.getAttribute('data-on')==='1';
  b.disabled=true;
  var msg=document.getElementById('gpumsg-'+cam);
  if(msg)msg.textContent='saving…';
  fetch('/api/gw/'+GW+'/cameras/'+cam,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:on})}).then(function(r){
      return r.json().then(function(j){
        if(!r.ok){if(msg){msg.textContent=j.detail||'failed';}b.disabled=false;return;}
        if(msg)msg.textContent=j.note||'saved';
        load();                       // refresh so the panel reflects the registry, not the click
      });
    }).catch(function(){if(msg)msg.textContent='failed';b.disabled=false;});
});

// ── TIER-2 (from the GPU door engine's gw_door_event stream) ──────────────────────────────
// Every figure states its n. The era filter is printed at the top of the card, not buried in a
// tooltip: these numbers come from ONE template set, and pooling them with another era would be
// mixing instruments. The per-reason census is shown so an empty table reads as "the filter
// excluded everything" rather than "the lift made no stops".
function t2num(v,unit){return v==null?'—':(v+(unit||''))}
function tier2card(t,cam){
  if(!t){return '';}
  var s=t.stops||{}, su=t.speed_up||{}, sd=t.speed_down||{};
  var cen=Object.keys(t.reason_census||{}).sort().map(function(k){
    return esc(k)+' '+t.reason_census[k];}).join(' · ');
  var rowsHtml=(t.per_floor||[]).map(function(f){
    return '<tr><td><b>'+esc(f.floor)+'</b></td><td>'+f.stops+'</td><td>'+f.up_stops+'</td>'
      +'<td>'+f.down_stops+'</td><td class=ok>'+f.boarded+'</td><td>'+f.alighted+'</td></tr>';}).join('');
  var table=rowsHtml
    ? '<table class=t2><thead><tr><th>floor</th><th>stops</th><th>↑</th><th>↓</th><th>boarded</th><th>alighted</th></tr></thead><tbody>'+rowsHtml+'</tbody></table>'
    : '<div class=blank>no attributed stops in this era</div>';
  var exc=t.speed_excluded||{}, excTxt=Object.keys(exc).filter(function(k){return exc[k]>0})
    .map(function(k){return esc(k)+'='+exc[k];}).join(', ');
  return '<div class=card style="margin-top:12px">'
    +'<h3>Tier-2 · stops, direction &amp; speed <span class=mut style="font-weight:400;font-size:11px">from gw_door_event</span></h3>'
    +(t.stale_templates?('<div class=warnrow><b>STALE TEMPLATES</b> — '+esc(t.stale_templates)+'</div>'):'')
    +'<div class=mut style="font-size:11px;margin-bottom:6px">era: <b>'+esc(t.era_filter)+'</b>'
    +(cam?eraSelector(t,cam):'')
    +(t.expected_era?(' · last build produced <b>'+esc(t.expected_era)+'</b>'+(t.stale_templates?' <span class=bad>(MISMATCH)</span>':' ✓')):'')
    +'<br>'
    +'rows in era '+t.rows_in_era+' → confident reads <b>'+t.confident_reads+'</b> · reasons seen: '+(cen||'—')
    +(t.floor_order_declared?'':' · <b>no DASH_FLOOR_ORDER declared</b> — non-numeric floors are excluded from speed')
    +(t.floor_whitelist?(' · floor alphabet ['+esc(t.floor_alphabet_source)+']: <b>'+t.floor_whitelist.join(' ')+'</b>'+(t.off_alphabet_rejected?(' · <b class=bad>'+t.off_alphabet_rejected+' off-alphabet reads rejected</b>'):'')):' · <b>no floor alphabet</b> — not enough evidence derived yet')
    +(function(){var dt=t.floor_alphabet_detail; if(!dt||!dt.length)return '';
      var rej=dt.filter(function(d){return !d.admitted;});
      var adm=dt.filter(function(d){return d.admitted;});
      function when(x){return new Date(x*1000).toLocaleDateString();}
      var rows=adm.map(function(d){return '<tr><td><b>'+esc(d.floor)+'</b></td><td>'+d.n+'</td><td>'+esc(d.via)+'</td><td>'+when(d.first)+'–'+when(d.last)+'</td></tr>';}).join('')
        +rej.map(function(d){return '<tr style="opacity:.6"><td>'+esc(d.floor)+'</td><td>'+d.n+'</td><td class=bad>'+esc(d.via)+'</td><td>'+when(d.first)+'–'+when(d.last)+'</td></tr>';}).join('');
      return '<details style="margin-top:4px"><summary class=mut style="font-size:11px;cursor:pointer">derived floor alphabet — '+adm.length+' admitted, '+rej.length+' rejected (sightings + evidence)</summary>'
        +'<table class=t2 style="margin-top:4px"><thead><tr><th>floor</th><th>sightings</th><th>via</th><th>first–last</th></tr></thead><tbody>'+rows+'</tbody></table></details>';})()
    +'</div>'
    +kv('C17/C18 stops (n='+s.n+')','↑ '+t2num(s.up)+' up · ↓ '+t2num(s.down)+' down'
        +(s.no_arrow?' · '+s.no_arrow+' no arrow':'')+(s.unattributed?' · <span class=warn>'+s.unattributed+' unattributed</span>':''))
    +kv('C21 speed up (n='+t2num(su.n)+')',su.n?(su.median+' floors/s median · '+su.min+'–'+su.max):'—')
    +kv('C22 speed down (n='+t2num(sd.n)+')',sd.n?(sd.median+' floors/s median · '+sd.min+'–'+sd.max):'—')
    +(excTxt?'<div class=mut style="font-size:11px">speed segments excluded: '+excTxt+'</div>':'')
    +kv('transits joined to stops',t.transits_matched+' of '+t.transits_total)
    +(function(){var c=t.transition_census; if(!c||!c.cycle_funnel)return '';
      var f=c.cycle_funnel, y=c.yield;
      return '<div class=warnrow style="background:#eef4fb;border-left-color:#2a6db0;color:#234"><b>door-cycle health</b> — funnel: '
        +'opening '+f['closed->opening']+' → open '+f['opening->open']+' → closing '+f['open->closing']+' → <b>cycle '+f['closing->closed (CYCLE)']+'</b>'
        +' · confirm-open '+(y.open_confirm_rate_pct==null?'—':y.open_confirm_rate_pct+'%')
        +' · complete-close '+(y.close_complete_rate_pct==null?'—':y.close_complete_rate_pct+'%')
        +'<br><b>'+esc(c.diagnosis)+'</b></div>';})()
    +(function(){var j=t.join_diagnostics; if(!j)return '';
      return '<div class=mut style="font-size:11px">join: '+(j.join_rate_pct==null?'—':j.join_rate_pct+'%')
        +' of '+j.n_transits_in_era+' in-era transits inside a window (dur median '+(j.window_dur_s.median==null?'—':j.window_dur_s.median+'s')+')'
        +(j.excluded_out_of_era?(' · '+j.excluded_out_of_era+' transits excluded as out-of-era'):'')
        +' · misses: '+j.miss_gap_s.n+', gap median '+(j.miss_gap_s.median==null?'—':j.miss_gap_s.median+'s')
        +' ('+j.miss_gap_s.within_2s+' within 2s, '+j.miss_gap_s.beyond_10s+' beyond 10s)'
        +'<br>'+esc(j.reading)+'</div>';})()
    +table
    +'</div>';
}

var mode='cams', trCam='', TR=null;
// One entry per camera: undefined = not asked, null = the request failed, object = the answer.
var RTT_BY={}, RTT_INFLIGHT={};
function loadRTT(cam){
  if(!cam || RTT_INFLIGHT[cam]) return;
  RTT_INFLIGHT[cam]=true;
  var eq=eraQuery();
  fetch('/dash/'+GW+'/rtt?cam='+encodeURIComponent(cam)+(eq?('&'+eq):''))
    .then(function(r){return r.json()})
    // PENDING IS NOT FAILURE AND NOT ZERO. `j.rtt||null` collapsed "the precompute has not reached
    // this camera yet" into the same null as a dead request, and the card said "reload to retry"
    // for a condition no reload can fix. The server's state travels with the value.
    .then(function(j){ RTT_BY[cam] = j.rtt || {state:'pending', note:(j.error||j.state||'')};
                       RTT_INFLIGHT[cam]=false;
                       if(cur===cam && mode==='cams') panel(DATA); })
    .catch(function(){ RTT_BY[cam]=null; RTT_INFLIGHT[cam]=false; });
}
// SEED FROM THE URL, same parameter the Dash tab reads. These were two independent selectors: ?cam=
// set `cur` (the Dash tab) and never touched trCam, so /dash?cam=ch27 could render the heatmap for
// whatever camera was last clicked here — reported 2026-08-05 as ch27 in the URL showing ch29's
// floor alphabet. Floors are per-shaft, so a mismatched heatmap is not a cosmetic problem: it is a
// chart of a different building column under the wrong heading.
// trCam is seeded from `cur` (which reads ?cam= above) by tabs(), and thereafter only ever written
// by selectCam. It is deliberately NOT parsed from the URL a second time: two independent readers of
// one parameter is how they drifted in the first place.
function nav(){
  document.getElementById('nav').innerHTML=
    '<div class="tab'+(mode==='cams'?' on':'')+'" onclick="setMode(\'cams\')">Cameras</div>'
   +'<div class="tab'+(mode==='trends'?' on':'')+'" onclick="setMode(\'trends\')">Trends</div>';
}
function setMode(m){mode=m;
  document.getElementById('camview').style.display=(m==='cams')?'':'none';
  document.getElementById('trendview').style.display=(m==='trends')?'':'none';
  nav(); if(m==='trends')loadTrends();
}

// ---- inline SVG charts (CSP-safe, no libs) ----
// A "nice" axis maximum, so ticks read 0/25/50/75/100 rather than 0/23.7/47.4/71.1.
function niceMax(mx){
  if(!(mx>0))return 1;
  var e=Math.pow(10,Math.floor(Math.log(mx)/Math.LN10)), f=mx/e;
  return (f<=1?1:f<=2?2:f<=2.5?2.5:f<=5?5:10)*e;
}
function fmtN(v){return (Math.abs(v)>=100||v===Math.round(v))?String(Math.round(v)):v.toFixed(v<1?2:1)}
function pad2(h){return (h<10?'0':'')+h}
// y grid + tick labels. Callers scale their marks against the SAME nmx, or the axis lies.
function yAxis(nmx,W,H,pad,bot,top,unit){
  var g='',n=4;
  for(var i=0;i<=n;i++){
    var v=nmx*i/n, y=H-bot-(H-top-bot)*i/n;
    g+='<line x1="'+pad+'" x2="'+(W-8)+'" y1="'+y+'" y2="'+y+'" stroke="#808080" stroke-opacity="'+(i?0.18:0.45)+'"></line>'
     +'<text x="'+(pad-4)+'" y="'+(y+3)+'" font-size="8" fill="#999" text-anchor="end">'+fmtN(v)+'</text>';
  }
  if(unit){g+='<text x="'+pad+'" y="'+(top-4)+'" font-size="8" fill="#aaa">'+esc(unit)+'</text>';}
  return g;
}
// Full-height transparent columns: a generous tap target, so a phone user does not have to hit a
// 2px dot. This is why the charts are usable on site and not only at a desk.
function hitRects(hours,vals,W,H,pad,bot,top,bw,fmt){
  return vals.map(function(v,i){
    return '<rect x="'+(pad+i*bw)+'" y="'+top+'" width="'+bw+'" height="'+(H-bot-top)+'" fill="transparent" data-tip="'+esc(fmt(hours[i],v))+'"></rect>';
  }).join('');
}
function svgBars(title,intent,hours,vals,color,ref,refLab,unit){
  var W=560,H=160,pad=34,bot=18,top=22;
  var mx=Math.max.apply(null,vals.map(function(v){return v||0}).concat([1])), nmx=niceMax(mx);
  var bw=(W-pad-8)/vals.length;
  var bars=vals.map(function(v,i){var bh=(H-top-bot)*(v||0)/nmx;
    return '<rect x="'+(pad+i*bw+0.5)+'" y="'+(H-bot-bh)+'" width="'+(bw-1)+'" height="'+bh+'" fill="'+color+'"></rect>';}).join('');
  var labs=hours.map(function(h,i){return (h%3===0)?'<text x="'+(pad+i*bw+bw/2)+'" y="'+(H-5)+'" font-size="8" fill="#999" text-anchor="middle">'+h+'</text>':''}).join('');
  var rl=''; if(ref!=null){var y=H-bot-(H-top-bot)*ref/nmx;
    rl='<line x1="'+pad+'" x2="'+(W-8)+'" y1="'+y+'" y2="'+y+'" stroke="#c0392b" stroke-dasharray="4 3"></line>'
      +'<text x="'+(W-8)+'" y="'+(y-2)+'" font-size="9" fill="#c0392b" text-anchor="end">'+esc(refLab)+'</text>';}
  var hits=hitRects(hours,vals,W,H,pad,bot,top,bw,function(h,v){
    return pad2(h)+':00 — '+(v==null?'no data':fmtN(v)+(unit?' '+unit:''));});
  return '<div class=intent>'+esc(intent)+'</div>'
    +'<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;touch-action:manipulation">'
    +'<text x="'+pad+'" y="12" font-size="11" fill="#555">'+esc(title)+'</text>'
    +yAxis(nmx,W,H,pad,bot,top,unit)+rl+bars+labs+hits+'</svg>';
}
function svgLine(title,intent,hours,vals,color,ref,refLab,unit){
  var W=560,H=170,pad=34,bot=18,top=22;
  var real=vals.filter(function(v){return v!=null});
  var mx=Math.max.apply(null,real.concat([ref||1,1])), nmx=niceMax(mx);
  var bw=(W-pad-8)/vals.length;
  function xy(v,i){return [pad+i*bw+bw/2, H-bot-(H-top-bot)*v/nmx];}
  var pts=vals.map(function(v,i){return v==null?null:xy(v,i).join(',')}).filter(Boolean).join(' ');
  var dots=vals.map(function(v,i){if(v==null)return '';var c=xy(v,i);
    return '<circle cx="'+c[0]+'" cy="'+c[1]+'" r="2.5" fill="'+color+'"></circle>';}).join('');
  var rl=''; if(ref!=null){var y=H-bot-(H-top-bot)*ref/nmx;
    rl='<line x1="'+pad+'" x2="'+(W-8)+'" y1="'+y+'" y2="'+y+'" stroke="#c0392b" stroke-dasharray="4 3"></line>'
      +'<text x="'+(W-8)+'" y="'+(y-2)+'" font-size="9" fill="#c0392b" text-anchor="end">'+esc(refLab)+'</text>';}
  var labs=hours.map(function(h,i){return (h%3===0)?'<text x="'+(pad+i*bw+bw/2)+'" y="'+(H-5)+'" font-size="8" fill="#999" text-anchor="middle">'+h+'</text>':''}).join('');
  var hits=hitRects(hours,vals,W,H,pad,bot,top,bw,function(h,v){
    return pad2(h)+':00 — '+(v==null?'no cycles this hour':fmtN(v)+(unit?' '+unit:''));});
  return '<div class=intent>'+esc(intent)+'</div>'
    +'<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;touch-action:manipulation">'
    +'<text x="'+pad+'" y="12" font-size="11" fill="#555">'+esc(title)+'</text>'
    +yAxis(nmx,W,H,pad,bot,top,unit)+rl
    +'<polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="1.5"></polyline>'
    +dots+labs+hits+'</svg>';
}
// ---- floor x hour heatmap ----
// A heatmap and not a 3D surface on purpose: the question is "which floors, when". A flat grid
// answers it at a glance with nothing hidden behind anything else, and every cell is readable at
// the same weight. A surface rotates prettily and occludes exactly the bars you want to compare.
function heatColor(t,mode){
  if(!(t>0))return 'rgba(128,128,128,0.10)';
  var a=0.14+0.86*Math.pow(t,0.65);          // gamma so mid values stay legible, not washed out
  return (mode==='riders')?('rgba(42,109,176,'+a.toFixed(3)+')'):('rgba(18,122,61,'+a.toFixed(3)+')');
}
function svgHeat(floors,mode){
  var key=(mode==='riders')?'riders_by_hour':'stops_by_hour';
  var rows=(floors||[]).slice().sort(function(a,b){        // highest floor on top, like the shaft
    var ai=a.floor_idx,bi=b.floor_idx;
    if(ai==null&&bi==null)return String(a.floor)<String(b.floor)?-1:1;
    if(ai==null)return 1; if(bi==null)return -1; return bi-ai;});
  var mx=0,tot=0;
  rows.forEach(function(f){(f[key]||[]).forEach(function(v){tot+=v||0; if(v>mx)mx=v;})});
  if(!mx){
    return '<div class=blank>no '+(mode==='riders'?'riders':'stops')+' attributed to a floor yet in this era</div>';
  }
  var cw=20,ch=15,padL=44,padT=20,W=padL+24*cw+8,H=padT+rows.length*ch+18;
  var cells='',ylab='';
  rows.forEach(function(f,r){
    ylab+='<text x="'+(padL-5)+'" y="'+(padT+r*ch+11)+'" font-size="9" fill="#777" text-anchor="end">'+esc(f.floor)+'</text>';
    for(var h=0;h<24;h++){
      var v=(f[key]||[])[h]||0;
      cells+='<rect x="'+(padL+h*cw)+'" y="'+(padT+r*ch)+'" width="'+(cw-1)+'" height="'+(ch-1)+'" fill="'+heatColor(v/mx,mode)+'"'
        +' data-tip="floor '+esc(f.floor)+' · '+pad2(h)+':00 — '+v+' '+(mode==='riders'?'riders':'stops')+'"></rect>';
    }
  });
  var xlab='';
  for(var h2=0;h2<24;h2+=3){xlab+='<text x="'+(padL+h2*cw+cw/2)+'" y="'+(H-5)+'" font-size="8" fill="#999" text-anchor="middle">'+h2+'</text>';}
  var leg='';
  for(var i=0;i<5;i++){leg+='<rect x="'+(padL+i*13)+'" y="'+(padT-13)+'" width="12" height="7" fill="'+heatColor(i/4,mode)+'"></rect>';}
  leg+='<text x="'+(padL+5*13+5)+'" y="'+(padT-7)+'" font-size="8" fill="#999">0 → '+mx+' per floor-hour</text>';
  return '<div class=hmwrap><svg viewBox="0 0 '+W+' '+H+'" style="width:100%;min-width:'+W+'px;height:auto;touch-action:manipulation">'
    +leg+cells+ylab+xlab+'</svg></div>'
    +'<div class=mut style="font-size:11px">'+tot+' '+(mode==='riders'?'riders':'stops')+' placed on a floor · hours are IST</div>';
}
// ONE delegated tooltip for every chart — hover for a mouse, pointerdown for a tap.
function tipOn(e){
  var el=document.getElementById('tip'); if(!el)return;
  var t=(e.target&&e.target.getAttribute)?e.target.getAttribute('data-tip'):null;
  if(!t){el.style.display='none';return;}
  el.textContent=t; el.style.display='block';
  var x=e.clientX+12,y=e.clientY-10;
  if(x+236>window.innerWidth)x=Math.max(4,window.innerWidth-236);
  if(y<4)y=4;
  el.style.left=x+'px'; el.style.top=y+'px';
}
document.addEventListener('pointerover',tipOn);
document.addEventListener('pointerdown',tipOn);
window.addEventListener('scroll',function(){var e=document.getElementById('tip');if(e)e.style.display='none';},true);

function winCard(name,w,travelUnmeasured,uncalibrated){
  if(!w)return '';
  var ratio=(w.demand_ratio_vs_allday!=null)?('  <b class="'+(w.demand_ratio_vs_allday>=1.3?'bad':'')+'">'+w.demand_ratio_vs_allday+'× all-day</b>'):'';
  // A BARE DASH IS NOT A REASON. These boxes printed "—/— (n=0)" while the chart six inches below
  // explained that h3 does not measure travel at all. The reader who stops at the summary boxes —
  // which is most readers — saw a missing number and no cause, which reads as broken data rather
  // than as a deliberate design. The explanation belongs where the dash is, not only where the
  // chart is.
  // UNCALIBRATED beats every other explanation: without a door engine there are no cycles to have,
  // so "cycles/hr 0" is not a small number, it is a category error. n/a says which kind of absence.
  var cycTxt=uncalibrated
    ? '<span class=mut>n/a — uncalibrated</span>'
    : (w.cycles_per_hr+ratio);
  var closeTxt;
  if(uncalibrated){
    closeTxt='<span class=mut>n/a — no door calibration on this camera</span>';
  } else if(w.close.median==null && travelUnmeasured){
    closeTxt='<span class=mut>not measured — h3 state-only engine, travel is NULL by design</span>';
  } else if(w.close.median==null){
    closeTxt='<span class=mut>— no completed close in this window</span>';
  } else {
    closeTxt=w.close.median+'s / '+(w.close.p85==null?'—':w.close.p85+'s')+' (n='+w.close.n+')';
  }
  // The transfer metric carries its INSTRUMENT. Without it, a lift with Pi history showed a July
  // figure beside GPU-era demand while a lift without one showed "(n=0)" — the same label over two
  // different kinds of statement, silently switching eras between tabs.
  var xf=w.transfer, xfTxt;
  if(xf.applies_to_range===false){
    xfTxt='<span class=mut>not measured in this range — '+esc(xf.instrument_note||'')+'</span>';
  } else if(xf.median==null){
    xfTxt='<span class=mut>— no Pi-era cycles for this lift ('+esc(xf.instrument_note||'')+')</span>';
  } else {
    xfTxt=xf.median+' s/pp (n='+xf.n+') * <span class=mut>· '+esc(xf.instrument_note||'')+'</span>';
  }
  return '<div class=card><h3>'+esc(name)+' <span class=mut>'+w.from+':00–'+w.to+':00</span></h3>'
    +kv('cycles/hr',cycTxt)
    +kv('close med / p85',closeTxt)
    +kv('transfer',xfTxt)
    +kv('riders/hr',w.riders_per_hr)
    // Peak occupancy is NOT divided by hours — a max per hour is not a quantity. The label carries
    // "min" on the number itself so the figure cannot be lifted out of this card and read as a count.
    +kv('peak occupancy',(!w.occupancy||w.occupancy.peak==null)?'—'
        :('&ge;<b>'+w.occupancy.peak+'</b> <span class=mut>(p95 '+(w.occupancy.p95==null?'—':w.occupancy.p95)
          +', n='+w.occupancy.n+' episodes, measured minimum)</span>'))
    +kv('transits/cycle',w.transits_per_cycle==null?'—':w.transits_per_cycle)+'</div>';
}
function trCams(){
  var cams=(DATA&&DATA.cameras)?DATA.cameras.map(function(c){return c.cam}):[];
  return '<div class=tabs style="margin-bottom:6px">'
    +['',].concat(cams).map(function(c){var lbl=c||'fleet';
       return '<div class="tab'+(trCam===c?' on':'')+'" onclick="selectCam(\''+c+'\')">'+esc(lbl)+'</div>';}).join('')+'</div>';
}
// ---- period picker + explicit date range + table view + CSV (operator batch) ----
// A picked date pair OVERRIDES the period buttons (the server prefers from_d/to_d too); picking a
// period clears the dates so the two can never silently disagree about what the screen shows.
var trPeriod='all', trFrom='', trTo='', trTable=false;
function setPeriod(p){trPeriod=p;trFrom='';trTo='';loadTrends();}
function setDates(){
  trFrom=(document.getElementById('dfrom')||{}).value||'';
  trTo=(document.getElementById('dto')||{}).value||'';
  if(trFrom||trTo)trPeriod='';
  loadTrends();
}
function clearDates(){trFrom='';trTo='';trPeriod='all';loadTrends();}
// ONE query builder for the trends fetch AND every CSV link — the chart and its export can never
// describe different rows. from_d/to_d are YYYY-MM-DD (IST calendar days, inclusive both ends).
function trQuery(){
  var eq=eraQuery();
  return 'period='+encodeURIComponent(trPeriod||'all')
    +(trFrom?('&from_d='+encodeURIComponent(trFrom)):'')
    +(trTo?('&to_d='+encodeURIComponent(trTo)):'')
    +(trCam?('&cam='+encodeURIComponent(trCam)):'')
    +(eq?('&'+eq):'');
}
function toggleTable(){trTable=!trTable;renderTrends();}
function periodBar(){
  var opts=[['day','Today'],['week','7 days'],['month','30 days'],['all','All']];
  var dstyle='font:inherit;font-size:11px;padding:2px 4px;border:1px solid #bbb;border-radius:4px;background:transparent;color:inherit';
  return '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:2px 0 8px">'
    +opts.map(function(o){return '<button class="tog'+(trPeriod===o[0]?' on':'')+'" onclick="setPeriod(\''+o[0]+'\')">'+o[1]+'</button>';}).join('')
    +'<span style="width:10px"></span>'
    +'<input type=date id=dfrom value="'+trFrom+'" onchange="setDates()" style="'+dstyle+'">'
    +'<span class=mut style="font-size:11px">to</span>'
    +'<input type=date id=dto value="'+trTo+'" onchange="setDates()" style="'+dstyle+'">'
    +((trFrom||trTo)?'<button class=tog onclick="clearDates()">✕ dates</button>':'')
    +'<span style="width:10px"></span>'
    +'<button class="tog'+(trTable?' on':'')+'" onclick="toggleTable()">'+(trTable?'charts':'table')+'</button>'
    +'<span class=mut id=rangelab style="font-size:11px;margin-left:6px"></span></div>';
}
// Every download carries the SAME cam/period/date-range the screen is showing, so a spreadsheet
// and the chart above it cannot disagree about which rows they describe.
function dl(ds,label){
  return '<a class=dlbtn href="/dash/'+GW+'/export.csv?dataset='+ds+'&'+trQuery()+'">⤓ '+label+'</a>';
}
function exportBar(){
  // floor events ALL ERAS: no era filter, an `era` label column per row — full history in one
  // file, so Excel gets everything at once and the era discipline travels as data, not as a URL.
  var allEras='<a class=dlbtn href="/dash/'+GW+'/export.csv?dataset=floor_events&era=all'
    +'&period='+encodeURIComponent(trPeriod||'all')
    +(trFrom?('&from_d='+encodeURIComponent(trFrom)):'')+(trTo?('&to_d='+encodeURIComponent(trTo)):'')
    +(trCam?('&cam='+encodeURIComponent(trCam)):'')+'">⤓ floor events — ALL eras, labelled</a>';
  return '<div class=dlbar>'+dl('door_cycles','door cycles')+dl('transits','transits')
    +dl('floor_events','floor events')+dl('per_floor','per-floor')
    +dl('episodes','episodes + occupancy')+dl('rtt','round trips')+allEras
    +'<span class=mut style="font-size:11px">CSV — the rows behind these charts, same range'+(trCam?'':' (fleet)')+'</span></div>';
}
// A BARE DASH DOES NOT SAY WHICH ABSENCE IT IS. Peak occupancy only began being recorded when the
// field was deployed; every hour before that has no measurement and never will. Printing "—" there,
// in the same glyph used for "this hour had no episodes", invites the reader to average across a
// boundary that does not exist — and a dash in an occupancy column reads as an empty car.
function occCell(p){
  if(p.occ_peak!=null) return p.occ_peak;
  var note=(TR&&TR.occupancy_note)||{};
  if(!note.first_ts) return '<span class=mut title="peak car occupancy has never been recorded for '
    +'this selection">no coverage</span>';
  if(!note.n_episodes) return '<span class=mut>no coverage in range</span>';
  return '<span class=mut>no episodes</span>';       // measured elsewhere, just not in this hour
}
function trTableHtml(prof,W){
  var head='<tr><th>hour</th><th>cycles</th><th>boarded</th><th>alighted</th><th>riders</th>'
    +'<th>close med (s)</th><th>close p85</th><th>n</th><th>peak occ ≥</th><th>occ n</th></tr>';
  var body=prof.map(function(p){
    return '<tr><td>'+pad2(p.hour)+':00</td><td>'+p.cycles+'</td><td>'+p.boarded+'</td><td>'+p.alighted
      +'</td><td>'+(p.boarded+p.alighted)+'</td><td>'+(p.close_median==null?'—':p.close_median)
      +'</td><td>'+(p.close_p85==null?'—':p.close_p85)+'</td><td>'+(p.close_n||0)
      +'</td><td>'+occCell(p)+'</td><td>'+(p.occ_n||0)+'</td></tr>';}).join('');
  var tot=prof.reduce(function(a,p){a.c+=p.cycles;a.b+=p.boarded;a.a+=p.alighted;
    if(p.occ_peak!=null&&p.occ_peak>a.o)a.o=p.occ_peak; a.on+=(p.occ_n||0); return a;},
    {c:0,b:0,a:0,o:null,on:0});
  // The occupancy total column is a MAX, not a sum — summing per-hour peaks would invent a number
  // no car ever held. The header says ≥ for the same reason the panel does.
  var foot='<tr class=tot><td>total</td><td>'+tot.c+'</td><td>'+tot.b+'</td><td>'+tot.a+'</td><td>'
    +(tot.b+tot.a)+'</td><td colspan=3></td><td>'+(tot.o==null?'—':'max '+tot.o)+'</td><td>'+tot.on+'</td></tr>';
  return '<div class=card><h3>hour-of-day table <span class=mut style="font-weight:400">same aggregates as the charts</span></h3>'
    +'<div class=hmwrap><table class=t2>'+head+body+foot+'</table></div></div>';
}
function renderTrends(){
  if(!TR){document.getElementById('trendview').innerHTML=trCams()+'<div class=mut>loading…</div>';return;}
  var prof=TR.profile, hours=prof.map(function(p){return p.hour}), W=TR.windows;
  var bd=TR.boundaries.close_travel_max.iso.slice(0,10);
  var gaps=(TR.data_gaps||[]);
  var gapbanner=gaps.length?('<div class=mut style="font-size:12px;margin:2px 0 6px;padding:4px 8px;border-left:3px solid #b00;background:rgba(176,0,0,.06)"><b>DATA GAP</b> — '+gaps.map(function(g){return esc(g.note)}).join(' · ')+'. Hour buckets overlapping this window are undercounted (samples MISSING, not low demand).</div>'):'';
  // ERA BOUNDARY: a range spanning >1 counting version pools transits counted by different logic.
  // Labeled every time, never silent — the whole reason the picker can be trusted for the study.
  var eras=((TR.counting_eras||{}).in_range)||[];
  // h3 STATE-ONLY: cycles exist, travel does not. Drives the close-travel card below, which must
  // say so rather than draw an empty axis — an empty chart beside a populated demand curve reads
  // as "the doors stopped closing", which is the same misreading as the 0-cycles bug it sits next to.
  var TRAVEL_UNMEASURED_CAMS=((TR.range||{}).travel_unmeasured_cams)||[];
  var TRAVEL_UNMEASURED=TRAVEL_UNMEASURED_CAMS.length>0;
  // UNCALIBRATED: no door engine has ever posted for this camera. Its door-derived charts have no
  // axis to draw, and drawing one anyway — an empty grid with the 2.31s Bank C line ruled across
  // nothing — states a compliance comparison about a lift that was never measured.
  var UNCAL_CAMS=((TR.range||{}).uncalibrated_cams)||[];
  var UNCALIBRATED=UNCAL_CAMS.length>0 && (!trCam || UNCAL_CAMS.indexOf(trCam)>=0);
  function absent(title,intent,head,body){
    return '<div class=card><div class=h>'+title+'</div>'
      +'<div class=mut style="font-size:11px">'+intent+'</div>'
      +'<div style="padding:18px 10px;border:1px dashed #b06a00;border-radius:6px;margin-top:8px">'
      +'<b>'+head+'</b><div class=mut style="font-size:11px;margin-top:4px">'+body+'</div></div></div>';
  }
  var erabanner=(TR.counting_eras&&TR.counting_eras.crossing)?('<div class=mut style="font-size:12px;margin:2px 0 6px;padding:4px 8px;border-left:3px solid #b06a00;background:rgba(176,106,0,.07)"><b>ERA BOUNDARY IN RANGE</b> — pools transits counted under '+eras.length+' different counting versions: '
    +eras.map(function(e){return '<b>'+esc(e.version)+'</b> ('+String(e.first_seen||'').slice(0,10)+' → '+String(e.last_seen||'').slice(0,10)+', '+e.n_episodes+' validated eps)'}).join(' · ')
    +'. Hourly totals mix counting logics; validated precision applies per era, never to the pool. Narrow the dates to one era for comparable numbers.</div>'):'';
  var h=trCams()+periodBar()
    +'<div class=mut style="font-size:12px;margin:2px 0 6px">'+esc(TR.cam)+' · '+TR.n_days+' day(s) with data · '
    +((TR.range&&TR.range.label)?esc(TR.range.label)+' · ':'')
    +((TR.range?TR.range.cycles:0))+' cycles, '+((TR.range?TR.range.transits:0))+' transits in range · '
    +'close-travel uses the post-'+bd+' regime only (CLOSE_TRAVEL_MAX comparability boundary)</div>'
    +exportBar()
    +gapbanner
    +erabanner
    +'<div class=strip>'+winCard('all-day',W.all_day,TRAVEL_UNMEASURED,UNCALIBRATED)
        +winCard('AM peak',W.am_peak,TRAVEL_UNMEASURED,UNCALIBRATED)
        +winCard('PM peak',W.pm_peak,TRAVEL_UNMEASURED,UNCALIBRATED)+'</div>'
    +'<div class=mut style="font-size:11px;margin:2px 0 8px">* transfer PROVISIONAL (transit precision, re-validating). <b>THE PEAK TRAP</b>: the sheet coefficients describe a PEAK design condition, not an all-day average — peak &amp; all-day are shown SEPARATELY; the ratio is itself a finding.</div>'
    +(trTable?trTableHtml(prof,W):(''
    +(UNCALIBRATED
      ? absent('cycles / hour-of-day — the demand curve',
               'how often this lift’s doors operate — the work rate',
               'no door calibration on '+esc(UNCAL_CAMS.join(', '))+' — door cycles UNAVAILABLE',
               'This camera has never had a door engine run against it, so there are no cycles to '
               +'count. An empty chart here with “cycles/hr 0” beside it would say the lift stood '
               +'still; it says nothing of the kind. Unavailable until the camera is calibrated '
               +'(site visit scheduled). Occupancy and transit counts on this camera are '
               +'unaffected — they come from the counting path, not the door path.')
      : '<div class=card>'+svgBars('cycles / hour-of-day — the demand curve',
        'how often this lift’s doors operate — the work rate',
        hours,prof.map(function(p){return p.cycles}),'#127a3d',null,'','cycles')+'</div>')
    +'<div class=card>'+svgBars('riders (boardings+alightings) / hour-of-day',
        'boardings + alightings counted at this door — usage volume, not unique people',
        hours,prof.map(function(p){return p.boarded+p.alighted}),'#2a6db0',null,'','riders')+'</div>'
    // ROUND TRIP TIME by hour-of-day. Same chart family as cycles/hr, and the label carries n,
    // the anomaly rate and the dwell caveat — the three things that decide whether the curve
    // means anything. A designed-absence panel where the camera has no floor attribution: RTT is
    // defined by the home floor, so no floor is UNAVAILABLE and not zero.
    +((function(){
      var R=TR.rtt;
      if(!trCam) return '<div class=card><div class=h>round trip time / hour-of-day</div>'
        +'<div class=blank>pick a lift \u2014 a round trip is a property of one shaft, so a fleet '
        +'figure would pool trips from different buildings columns</div></div>';
      if(!R||R.state==='no_floor'||R.state==='no_era'||R.state==='no_rows'){
        return absent('round trip time / hour-of-day (s)',
          'door closed at the home floor \u2192 next door open at the home floor',
          'RTT UNAVAILABLE for '+esc(trCam),
          esc((R&&R.note)|| (R&&R.state==='no_rows'
                ? ('no door rows in this window for era '+String(R.era||'').slice(0,12))
                : 'no door-engine reads in any era'))
          +'. RTT is defined by the home floor; without a floor read there is no trip to measure. '
          +'This is not a lift that made no journeys.');
      }
      var vals=R.by_hour.map(function(h){return h.median});
      return '<div class=card>'+svgBars('round trip time / hour-of-day (s) \u2014 median',
        'door closed at '+esc(R.home)+' \u2192 next door open at '+esc(R.home)+'; both ends a STOP, not a pass',
        hours,vals,'#7a1f5c',null,'','s')
        +'<div class=mut style="font-size:11px;margin-top:4px">n='+R.all_day.n+' plausible trips'
        +' \u00b7 anomaly rate <b'+(R.anomaly_rate>25?' class=bad':'')+'>'+R.anomaly_rate+'%</b>'
        +' ('+R.n_anomalies+' of '+R.n_trips+' outside 30\u2013600s)'
        +' \u00b7 stops/trip median '+R.stops.median
        +'<br><b>'+esc(R.caveat||'')+'</b>'
        +'<br>'+esc(R.travel_gap||'')+'</div></div>';
    })())
    // PEAK CAR OCCUPANCY. Same chart family as riders/hr, deliberately: it is read in the same
    // glance and must not look like a more precise instrument than it is. An hour with episodes but
    // no people is a real 0; an hour with no episodes draws nothing, and the n row below says which
    // is which — the empty-chart-reads-as-no-activity trap this file has now hit twice.
    +'<div class=card>'+svgBars('peak car occupancy / hour-of-day — MEASURED MINIMUM',
        'the most people seen inside the cabin at once during any door-open episode in this hour — '
        +'a floor, not a count',
        hours,prof.map(function(p){return p.occ_peak}),'#6a3fa0',null,'','people (min)')
      +'<div class=mut style="font-size:11px;margin-top:4px">'
      +((TR.occupancy_note&&TR.occupancy_note.n_episodes)
        ? (TR.occupancy_note.n_episodes+' episode(s) with coverage in range · <b>'
           +esc(TR.occupancy_note.calibration)+'</b> · '+esc(TR.occupancy_note.anchor)
           +((TR.occupancy_note.first_ist)
             ? ' · first recorded '+esc(String(TR.occupancy_note.first_ist).slice(0,16))
               +' — anything earlier has NO coverage, which is not the same as an empty car'
             : ''))
        : ((TR.occupancy_note&&TR.occupancy_note.first_ist)
           ? ('no episodes carry occupancy coverage in this range — bars are absent, NOT zero. '
              +'The measurement begins '+esc(String(TR.occupancy_note.first_ist).slice(0,16))+'.')
           : 'peak car occupancy has never been recorded for this selection — no coverage, '
             +'which is not a measurement of zero'))
      +'</div></div>'
    +(UNCALIBRATED
      ? absent('close-travel median / hour-of-day (s)',
               'median seconds for the door to close, per hour',
               'no door calibration — close-travel UNAVAILABLE',
               'The 2.31s Bank C compliance line is deliberately NOT drawn: ruling a threshold '
               +'across an empty axis states a compliance comparison about a lift that was never '
               +'measured. Unavailable until this camera is calibrated.')
      : '<div class=card>'+(TRAVEL_UNMEASURED
        ? ('<div class=h>close-travel median / hour-of-day (s)</div>'
           +'<div class=mut style="font-size:11px">median seconds for the door to close, per hour</div>'
           +'<div style="padding:18px 10px;border:1px dashed #b06a00;border-radius:6px;margin-top:8px">'
           +'<b>no travel data (h3: travel unmeasured by design)</b>'
           +'<div class=mut style="font-size:11px;margin-top:4px">'
           +esc(TRAVEL_UNMEASURED_CAMS.join(', '))+' run the state-only engine: cycles ARE detected '
           +'(see the demand curve above) but the door is not timed. An empty chart here would read '
           +'as no activity; there is activity, and no travel measurement. Travel comes from the '
           +'weekly hand-timed sample.</div></div>')
        : svgLine('close-travel median / hour-of-day (s)',
        'median seconds for the door to close, per hour — the 2.31s line is Bank C’s compliance cliff',
        hours,prof.map(function(p){return p.close_median}),'#b06a00',2.31,'2.31 Bank C','s'))+'</div>')))
    +heatCard();
  document.getElementById('trendview').innerHTML=h;
}

// ---- floor x hour intensity ----
// Defaults to STOPS because stops are attributed today; riders depend on the transit join and the
// toggle says so rather than silently drawing an empty grid.
var heatMode='stops';
function setHeat(m){heatMode=m;renderTrends();}
function heatCard(){
  // Range-scoped tier2 from /trends once it has loaded for the shown camera — INCLUDING when the
  // range is empty (an empty range must show empty, not quietly fall back to all-history). The
  // /data (all-history) copy only bridges the moment between switching cameras and the fetch.
  var trReady=(TR&&trCam&&TR.cam===trCam);
  var t2=trReady?TR.tier2_range:((DATA&&DATA.tier2)?DATA.tier2[trCam]:null);
  // The camera goes IN the heading. The tab strip above shows which is active, but a chart whose
  // title does not name its subject is one glance away from being read as another camera's.
  var head='<div class=card><h3>riders &amp; stops per floor, per hour — '
    +'<span style="font-weight:600">'+esc(trCam||'pick a lift')+'</span> '
    +'<button class="tog'+(heatMode==='stops'?' on':'')+'" onclick="setHeat(\'stops\')">stops</button>'
    +'<button class="tog'+(heatMode==='riders'?' on':'')+'" onclick="setHeat(\'riders\')">riders</button></h3>'
    +'<div class=intent>'+(heatMode==='riders'
      ? 'RIDERS: transits whose timestamp falls inside a door-open window, placed on that stop\'s floor. '
        +'One row per floor, one column per hour.'
      : 'STOPS: door CYCLES — the door opened and closed at that floor. NOT floor readings: a lift '
        +'passing a floor is not counted here.')+'</div>';
  if(!trCam){
    return head+'<div class=blank>pick a lift above — floors belong to one shaft, so a fleet total would mix them</div></div>';
  }
  if(cur && trCam !== cur){
    head+='<div class=intent style="color:#8a6100">showing <b>'+esc(trCam)+'</b>, but the dashboard '
      +'above is on <b>'+esc(cur)+'</b> — these are different lifts with different floors. '
      +'<a href="#" onclick="selectCam(\''+esc(cur)+'\');return false">show '+esc(cur)+' instead</a></div>';
  }
  if(!t2){
    return head+'<div class=blank>no door-engine reads for '+esc(trCam)+' in the current era'
      +(trReady&&TR.range&&TR.range.label?' within '+esc(TR.range.label):'')+'</div></div>';
  }
  // THE CAPTION THAT STOPS THE WRONG CONCLUSION. The two charts count different things and the
  // rider side is sparse by nature: only ~15-20% of stops have a transit inside their window. A
  // reader who does not know that sees dense green against near-empty blue and concludes the data
  // is broken. It is not — but a chart that needs a briefing to read is a chart with a missing label.
  var totS=0, totR=0, nF=0, zeroF=0;
  (t2.per_floor||[]).forEach(function(f){
    var s=0,r=0;
    (f.stops_by_hour||[]).forEach(function(v){s+=v||0});
    (f.riders_by_hour||[]).forEach(function(v){r+=v||0});
    totS+=s; totR+=r; if(s>0){nF++; if(r===0)zeroF++;}
  });
  var rate=totS?Math.round(1000*totR/totS)/10:0;
  var note='<div class=mut style="font-size:11px;margin-top:4px">'
    +'<b>'+totS+'</b> stops · <b>'+totR+'</b> riders placed &rarr; <b>'+rate+'%</b> of stops have a '
    +'rider inside their door window. The two charts are NOT comparable cell-for-cell: green counts '
    +'door cycles, blue counts people, and most cycles carry nobody the counter saw.'
    +(zeroF?(' <b>'+zeroF+'</b> of '+nF+' floors with stops show zero riders — at this rate that is '
      +'expected for low-traffic floors, not evidence of missing data.'):'')
    +'</div>';
  if(heatMode==='riders'){
    var m=t2.transits_matched||0, j=t2.transits_joinable;
    note+='<div class=mut style="font-size:11px;margin-top:2px">transits joined: <b>'
      +m+'</b> of <b>'+(j==null?'?':j)+'</b> joinable in this era'
      +((t2.transits_total!=null&&j!=null&&t2.transits_total>j)?(' (' +t2.transits_total+' lifetime, most predating this era)'):'')
      +'. Unjoined transits are not on any floor and are absent here.</div>';
  }
  var unattr=(t2.stops&&t2.stops.unattributed)||0;
  if(unattr){
    var placed=(t2.stops&&t2.stops.n)||0;
    note+='<div class=mut style="font-size:11px;margin-top:2px">'
      +'<b>'+unattr+'</b> door cycles are on NO floor at all (no confident indicator read within '
      +(t2.attr_window_s||10)+'s) and appear in NEITHER chart — '
      +Math.round(1000*unattr/Math.max(unattr+placed,1))/10+'% of all cycles on this lift.</div>';
  }
  return head+svgHeat(t2.per_floor,heatMode)+note
    +'<div class=mut style="font-size:11px">era: '+esc(t2.era_filter||'')
    +(trReady&&TR.range&&TR.range.label?' · range: '+esc(TR.range.label):' · all history (range loading)')+'</div></div>';
}
function loadTrends(){
  // DISCARD A PAYLOAD THAT DESCRIBES ANOTHER CAMERA, BEFORE DRAWING ANYTHING.
  //
  // This rendered immediately "to show the selector" while TR still held the PREVIOUS camera's
  // fetch, so between the click and the response the page drew ch27's summary line and range under
  // ch16's heading, with ch16's per-floor panel beneath it (heatCard guards on TR.cam===trCam and
  // falls back, the summary line did not guard at all). Reported 2026-08-13 on
  // /dash?cam=ch16&view=trends. The selector variables were already unified by d8429b3 — they were
  // never the disagreement; a cached payload was.
  //
  // An empty view that says "loading…" is honest. A populated view describing a different lift is
  // not, and floors belong to one shaft, so it is not a cosmetic difference.
  var want = trCam || 'fleet';
  if (TR && (TR.cam || 'fleet') !== want) TR = null;
  renderTrends();
  var forCam = trCam;                                  // what THIS request is for
  fetch('/dash/'+GW+'/trends?'+trQuery()).then(function(r){return r.json()}).then(function(t){
    // A response that arrives after the selection moved on must be dropped, not drawn: two clicks
    // in quick succession can resolve out of order, and the loser would overwrite the winner.
    if (forCam !== trCam) return;
    TR = t; renderTrends();
  }).catch(function(){});
}

// tabs() seeds trCam from cur, and it only runs in the Cameras view — so a page that OPENS on
// Trends (?view=trends) never seeded it at all. Seed it here instead, on every render, so the two
// views cannot start out describing different lifts.
function render(){ if(!DATA)return;
  if(!cur){ cur=(DATA.cameras[0]&&DATA.cameras[0].cam)||''; }
  if(cur && trCam !== cur){ trCam = cur; }
  nav(); healthbar(DATA); strip(DATA); headline(DATA); unavail(DATA);
  if(mode==='cams'){tabs(DATA); panel(DATA);} }

/* ── DATA REFRESH: one in flight, bounded failures, backoff ────────────────────
   This was `load(); setInterval(load, 15000);` — a fixed timer with NO in-flight guard and no
   failure cap. Because /dash/{gw}/data could not finish, a single tab left open produced 46
   requests in 11.7h with ~25 simultaneously Pending, each stranding a query the gateway could
   never complete. That is what drove the two OOM kills: closing the tab alone took /ops from
   17.9s to 0.005s and load from 5.38 to 0.74.

   Three rules, and the first is the one that matters:
     1. NEVER start a request while one is outstanding. `inflight` is the guard; the timer is only
        ever re-armed from a settled request, so overlap is structurally impossible rather than
        merely unlikely.
     2. Stop after MAX_FAILS consecutive failures and hand the operator an explicit retry. An
        auto-refreshing page that retries forever is a load generator pointed at a sick server.
     3. Back off between retries (15s -> 30s -> 60s) instead of a fixed cadence.
   Also: a non-2xx is now an ERROR, not data. The old code fed the response straight to .json()
   and would happily render the 503 timeout body as if it were a dashboard. */
var REFRESH_MS = 15000;
var MAX_FAILS  = 3;
var inflight   = null;      // AbortController (or sentinel) while a request is outstanding
var fails      = 0;
var timer      = null;
var stopped    = false;

function scheduleLoad(ms){
  if(timer){clearTimeout(timer); timer=null;}
  if(stopped) return;
  timer = setTimeout(load, ms);
}
function backoffMs(){ return REFRESH_MS * Math.pow(2, Math.min(fails,3)); }

function dataUnavailable(msg){
  stopped = true;
  if(timer){clearTimeout(timer); timer=null;}
  var el = document.getElementById('stamp');
  el.innerHTML = '· <b style="color:#c33">data unavailable</b> · '+esc(String(msg||'').slice(0,160))
    +' <button id=retrybtn style="margin-left:6px;padding:2px 8px;cursor:pointer">retry</button>';
  var b = document.getElementById('retrybtn');
  if(b) b.onclick = function(){ fails=0; stopped=false; el.textContent='· retrying…'; load(); };
}

function load(){
  if(inflight) return;                       // rule 1: never overlap
  var eq = eraQuery();
  var ac = (window.AbortController ? new AbortController() : null);
  inflight = ac || {};
  fetch('/dash/'+GW+'/data'+(eq?('?'+eq):''), ac?{signal:ac.signal}:undefined)
    .then(function(r){
      if(!r.ok){                             // 503 timeout body is an error, never data
        return r.json().catch(function(){return {};}).then(function(b){
          var e=new Error((b&&b.detail)||('HTTP '+r.status)); e.status=r.status; throw e; });
      }
      return r.json();
    })
    .then(function(d){
      inflight=null; fails=0; DATA=d;
      /* State the RANGE these numbers describe. Windowing changed what the close-travel medians,
         stop counts and per-floor totals mean — they are now a window, not all history — so the
         page must say so rather than let an operator read a 7-day median as a lifetime one. */
      var w=d.window||{}, wl=w.label?(' · '+w.label+(w.expensive?' ⚠ expensive':'')):'';
      document.getElementById('stamp').textContent='· '+d.ist_today+wl+' · updated '+new Date().toLocaleTimeString();
      render();
      // Apply ?view=trends ONCE, after the first payload: setMode needs the camera list, and render()
      // above has just seeded trCam from cur, so the trends fetch goes out for the camera in the URL
      // rather than for whatever was selected last.
      if(WANT_VIEW==='trends' && mode!=='trends'){ WANT_VIEW='cams'; setMode('trends'); }
      scheduleLoad(REFRESH_MS);
    })
    .catch(function(err){
      inflight=null; fails++;
      if(fails>=MAX_FAILS){ dataUnavailable((err&&err.message)||'request failed'); return; }
      var wait=backoffMs();
      document.getElementById('stamp').textContent='· fetch failed ('+fails+'/'+MAX_FAILS
        +') · retrying in '+Math.round(wait/1000)+'s';
      scheduleLoad(wait);
    });
}
load();
</script>"""


# ============================================================ /calibrate — draw ROIs on a live frame
@dash_router.get("/calibrate", response_class=HTMLResponse)
def calibrate_page(cam: str = "ch29", fw: int = 0, fh: int = 0):
    """Click-and-drag the door_roi + two panel ROIs (rects) and the cabin/landing zones (polygons) on
    a LIVE frame; live-outputs the config (DOOR_ROI_FRAME / PANEL_ROIS + a camera_zones.json snippet) to
    paste — no more guessing coords in a terminal. The canvas works in the GPU's FRAME space (the /snap
    jpg is downscaled), so emitted coords are correct; both sizes are printed to verify. (Save-to-zones
    lands with the detector, once the zones-distribution target is settled.)"""
    w, h = FRAME_SIZES.get(cam, FRAME_SIZE_DEFAULT)
    if fw > 0 and fh > 0:
        w, h = fw, fh
    gw = os.environ.get("DASH_GW", "site-A")
    nav = nc.header("", gw, cam) + nc.cam_bar(gw, cam, "")
    page = (_CALIB_PAGE.replace("__GW__", gw).replace("__CAM__", cam)
            .replace("__FW__", str(w)).replace("__FH__", str(h))
            .replace("__NAV__", nav)
            .replace("</style>", nc.NAV_CSS + "</style>", 1))
    return page + nc.switcher_js(gw, cam, "/calibrate?cam=__C__")


_CALIB_PAGE = r"""<!doctype html><meta charset=utf-8><title>liftlab · calibrate</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--mono:ui-monospace,Consolas,monospace}
body{background:#111;color:#eee;font:14px system-ui;margin:0;padding:12px}
h1{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:#aaa;margin:0 0 8px}
.tool{display:inline-block;padding:5px 10px;margin:2px;border:2px solid #444;border-radius:6px;cursor:pointer;font-size:13px}
.tool.on{border-color:#fff;font-weight:600}
button{background:#333;color:#eee;border:1px solid #555;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:13px}
button.on{border-color:#fff;background:#2a4}
#wrap{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
canvas{border:1px solid #444;max-width:100%;image-rendering:pixelated;cursor:crosshair}
#side{min-width:320px;flex:1}
label{font-size:12px;color:#aaa;margin-right:6px}
input[type=number]{width:56px;background:#222;color:#eee;border:1px solid #555;border-radius:4px;padding:2px 4px;font-family:var(--mono)}
textarea{width:100%;height:180px;background:#0a0a0a;color:#7fdca0;border:1px solid #444;border-radius:6px;
         font-family:var(--mono);font-size:12px;padding:8px;box-sizing:border-box}
.hint{color:#888;font-size:12px;margin:6px 0}
#sizebar{font-family:var(--mono);font-size:12px;margin:4px 0;padding:4px 8px;border-radius:5px;background:#1a1a1a}

</style>
__NAV__
<h1>liftlab · calibrate <span id=cam style=color:#888></span></h1>
<div id=sizebar>frame …</div>
<div id=tools></div>
<div class=hint>rect tools: <b>drag</b> to draw · drag the box to move · drag the bottom-right handle to resize · or type numbers.
&nbsp; polygon tools: <b>click</b> to add points · drag a point to move.</div>
<div class=hint>frame: <button onclick=refresh()>↻ live</button>
  <button onclick="pinN('A')">pin A (e.g. shut)</button> <button onclick="pinN('B')">pin B (e.g. open)</button>
  &nbsp; view: <span id=views></span>
  &nbsp; A/B blend shows BOTH leaf edges at once — drag door_roi to span them = the leaf-travel band.</div>
<div id=wrap>
  <canvas id=cv></canvas>
  <div id=side>
    <div id=nums class=hint></div>
    <div style=margin:6px_0><button onclick=undo()>undo point</button> <button onclick=clr()>clear active</button></div>
    <textarea id=out readonly></textarea>
    <div style=margin-top:6px><button onclick=copyout()>copy config</button>
      <span id=copied style="color:#7fdca0;font-size:12px"></span></div>
    <div class=hint>Coords are in the GPU FRAME space (printed above), not snapshot px. Paste
      DOOR_ROI_FRAME / PANEL_ROIS into door_calib / GPU_DOOR, polygons into camera_zones.json.</div>
  </div>
</div>
<script>
var GW="__GW__", CAM="__CAM__", FW=__FW__, FH=__FH__;   // FW/FH = the GPU's PROCESSING frame size
document.getElementById('cam').textContent="· "+CAM;
var cv=document.getElementById('cv'), ctx=cv.getContext('2d'), img=new Image();
cv.width=FW; cv.height=FH;                               // canvas INTRINSIC = frame space -> coords correct
var natW=0, natH=0, bgA=null, bgB=null, view='live';
var S={door_roi:null,panel0:null,panel1:null,cabin:[],landing:[]};
var COLORS={door_roi:'#4aa3ff',panel0:'#2ec27e',panel1:'#12b5b5',cabin:'#e0a800',landing:'#b07be0'};
var RECTS=['door_roi','panel0','panel1'], POLYS=['cabin','landing'];
var tool='door_roi', mode=null, off=[0,0], vidx=-1;

function tools(){
  document.getElementById('tools').innerHTML=RECTS.concat(POLYS).map(function(t){
    return '<span class="tool'+(t===tool?' on':'')+'" style="color:'+COLORS[t]+'" onclick="pick(\''+t+'\')">'+t+'</span>';
  }).join('')+' &nbsp;<span class=tool style="border-color:#666" onclick="pick(\'\')">pan/none</span>';
}
function views(){
  var opts=[['live','live']]; if(bgA)opts.push(['A','A']); if(bgB)opts.push(['B','B']); if(bgA&&bgB)opts.push(['blend','A|B blend']);
  document.getElementById('views').innerHTML=opts.map(function(o){
    return '<button class="'+(view===o[0]?'on':'')+'" onclick="setView(\''+o[0]+'\')">'+o[1]+'</button>';}).join(' ');
}
function setView(v){view=v;views();render();}
function pick(t){tool=t;tools();syncNums();}
function refresh(){ view='live'; img.src='/snap/'+GW+'/'+CAM+'.jpg?t='+Date.now(); }
function snap(){var c=document.createElement('canvas');c.width=FW;c.height=FH;c.getContext('2d').drawImage(img,0,0,FW,FH);return c;}
function pinN(which){ if(!natW){return;} if(which==='A')bgA=snap(); else bgB=snap(); view=which; views(); render(); }

img.onload=function(){ natW=img.naturalWidth; natH=img.naturalHeight;
  var sc=(FW/natW).toFixed(2);
  var warn=(natW!==FW)?('<span style="color:#e0a800"> — snapshot is downscaled; coords are UPSCALED ×'+sc+' to the frame</span>'):' (snapshot == frame)';
  document.getElementById('sizebar').innerHTML='snapshot '+natW+'×'+natH+' &nbsp;→&nbsp; GPU frame <b>'+FW+'×'+FH+'</b> (emitting coords in frame space)'+warn;
  render(); output(); };
img.onerror=function(){ ctx.fillStyle='#400';ctx.fillRect(0,0,FW,FH);
  ctx.fillStyle='#fff';ctx.fillText('no /snap/'+GW+'/'+CAM+'.jpg — is the snapshot running?',10,20); };

function toSrc(e){var r=cv.getBoundingClientRect();
  return [Math.round((e.clientX-r.left)*FW/r.width), Math.round((e.clientY-r.top)*FH/r.height)];}   // -> FRAME px
function nearVtx(poly,p){for(var i=0;i<poly.length;i++){if(Math.abs(poly[i][0]-p[0])<8&&Math.abs(poly[i][1]-p[1])<8)return i;}return -1;}

cv.addEventListener('mousedown',function(e){
  if(!tool)return; var p=toSrc(e);
  if(RECTS.indexOf(tool)>=0){
    var r=S[tool];
    if(r){ if(Math.abs(p[0]-(r.x+r.w))<8&&Math.abs(p[1]-(r.y+r.h))<8){mode='resize';return;}
      if(p[0]>=r.x&&p[0]<=r.x+r.w&&p[1]>=r.y&&p[1]<=r.y+r.h){mode='move';off=[p[0]-r.x,p[1]-r.y];return;} }
    mode='draw'; S[tool]={x:p[0],y:p[1],w:1,h:1};
  } else { var poly=S[tool], vi=nearVtx(poly,p);
    if(vi>=0){mode='vtx';vidx=vi;} else {poly.push([p[0],p[1]]);render();output();} }
});
cv.addEventListener('mousemove',function(e){
  if(!mode)return; var p=toSrc(e);
  if(mode==='draw'||mode==='resize'){var r=S[tool];r.w=Math.max(1,p[0]-r.x);r.h=Math.max(1,p[1]-r.y);}
  else if(mode==='move'){var r=S[tool];r.x=p[0]-off[0];r.y=p[1]-off[1];}
  else if(mode==='vtx'){S[tool][vidx]=[p[0],p[1]];}
  render();output();
});
window.addEventListener('mouseup',function(){if(mode){mode=null;syncNums();output();}});

function undo(){ if(POLYS.indexOf(tool)>=0){S[tool].pop();render();output();} }
function clr(){ if(RECTS.indexOf(tool)>=0)S[tool]=null; else S[tool]=[]; render();output();syncNums(); }

function drawBg(){
  ctx.clearRect(0,0,FW,FH);
  if(view==='A'&&bgA){ctx.drawImage(bgA,0,0);}
  else if(view==='B'&&bgB){ctx.drawImage(bgB,0,0);}
  else if(view==='blend'&&bgA&&bgB){ctx.globalAlpha=1;ctx.drawImage(bgA,0,0);ctx.globalAlpha=0.5;ctx.drawImage(bgB,0,0);ctx.globalAlpha=1;}
  else if(natW){ctx.drawImage(img,0,0,FW,FH);}          // live snapshot stretched to frame space
}
function render(){
  drawBg();
  RECTS.forEach(function(t){var r=S[t]; if(!r)return;
    ctx.strokeStyle=COLORS[t]; ctx.lineWidth=(t===tool?2:1);
    ctx.strokeRect(r.x+0.5,r.y+0.5,r.w,r.h);
    ctx.fillStyle=COLORS[t]; ctx.font='10px monospace'; ctx.fillText(t,r.x+1,r.y-2>10?r.y-2:r.y+10);
    if(t===tool){ctx.fillRect(r.x+r.w-3,r.y+r.h-3,6,6);}
  });
  POLYS.forEach(function(t){var pl=S[t]; if(!pl.length)return;
    ctx.strokeStyle=COLORS[t]; ctx.lineWidth=(t===tool?2:1); ctx.beginPath();
    pl.forEach(function(pt,i){ i?ctx.lineTo(pt[0],pt[1]):ctx.moveTo(pt[0],pt[1]); });
    if(pl.length>2)ctx.closePath(); ctx.stroke();
    ctx.fillStyle=COLORS[t]; pl.forEach(function(pt){ctx.fillRect(pt[0]-2,pt[1]-2,4,4);});
    ctx.font='10px monospace'; ctx.fillText(t,pl[0][0]+2,pl[0][1]-2);
  });
}
function syncNums(){
  if(RECTS.indexOf(tool)<0){document.getElementById('nums').innerHTML='';return;}
  var r=S[tool]||{x:0,y:0,w:0,h:0};
  document.getElementById('nums').innerHTML=tool+' (frame px):&nbsp; '
    +['x','y','w','h'].map(function(k){return '<label>'+k+'<input type=number id=n_'+k+' value="'+r[k]+'"></label>';}).join('');
  ['x','y','w','h'].forEach(function(k){var el=document.getElementById('n_'+k);
    el.oninput=function(){ if(!S[tool])S[tool]={x:0,y:0,w:1,h:1}; S[tool][k]=parseInt(el.value||0,10); render();output(); };});
}
function output(){
  function rc(r){return r?(r.x+','+r.y+','+r.w+','+r.h):'—';}
  function poly(pl){return '['+pl.map(function(p){return '['+p[0]+','+p[1]+']';}).join(',')+']';}
  var key=CAM.replace(/^ch/,'');
  var pr=[S.panel0,S.panel1].filter(Boolean).map(rc).join(';');
  var t=''
    +'# frame '+FW+'x'+FH+' (GPU processing size)\n'
    +'DOOR_ROI_FRAME="'+rc(S.door_roi)+'"\n'
    +'PANEL_ROIS="'+pr+'"\n\n'
    +'# camera_zones.json  ("'+key+'", frame '+FW+'x'+FH+'):\n'
    +'"'+key+'": {\n'
    +'  "frame_wh": ['+FW+','+FH+'],\n'
    +'  "door_roi_frame": ['+(S.door_roi?[S.door_roi.x,S.door_roi.y,S.door_roi.w,S.door_roi.h].join(','):'')+'],\n'
    +'  "zone_cabin": '+poly(S.cabin)+',\n'
    +'  "zone_landing": '+poly(S.landing)+'\n}';
  document.getElementById('out').value=t;
}
function copyout(){var o=document.getElementById('out');o.select();document.execCommand('copy');
  var c=document.getElementById('copied');c.textContent='copied';setTimeout(function(){c.textContent='';},1500);}

tools(); views(); syncNums(); refresh();
</script>"""
