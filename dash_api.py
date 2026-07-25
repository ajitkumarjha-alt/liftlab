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

import json
import os
import re
import sqlite3
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


def _q(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
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
    if t0 is None:
        return True
    return ep is not None and t0 <= ep < t1


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


def _door_transition_census(db, gw, cam, era):
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
    rows = _q(db, "SELECT door_state FROM gw_door_event WHERE gateway_id=? AND cam=? "
                  "AND door_version LIKE ? AND door_state IS NOT NULL ORDER BY ts, id", (gw, cam, era + "%"))
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
        "era": era, "state_runs": len(seq), "transitions": trans,
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
        "pairing_suspect": (began_closing > opened or completed > confirmed_open),
        "diagnosis": (
            # closings/cycles cannot legitimately exceed openings/opens — that is the tracker pairing
            # edges across gaps or noise (the 0.08s / 4641s close_travel). Grade it RED, not normal.
            "PAIRING SUSPECT: more closings than openings (or cycles than opens) — the tracker is "
            "pairing edges across gaps/noise; close_travel is unreliable (time-guard fix pending deploy)"
            if (began_closing > opened or completed > confirmed_open)
            else "few opening->open: near_open threshold too high for this edge" if opened and _pct(confirmed_open, opened) is not None and _pct(confirmed_open, opened) < 50
            else "few closing->closed: close_th too low, doors never read fully shut" if began_closing and _pct(completed, began_closing) is not None and _pct(completed, began_closing) < 50
            else "cycles completing normally" if completed else "no completed cycles — see the funnel"),
    }


def _door_gpu_by_cam(db, gw, cams):
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
        rows = _q(db, "SELECT ts, close_travel_s ct FROM gw_door_event "
                      "WHERE gateway_id=? AND cam=? AND door_version LIKE ? ORDER BY ts",
                  (gw, cam, era + "%"))
        cts = sorted(float(r["ct"]) for r in rows if r["ct"] is not None and r["ct"] > 0)
        n = len(cts)
        # SUSPECT GUARD. The GPU tracker paired edges across gaps/noise, so stored close_travel can be
        # sub-frame (0.08s) or gap-spanning (thousands of s). Flag the line so nobody quotes 0.08s, and
        # compute a plausible-only median (0.3-30s) beside the raw one so a usable number survives.
        PLAUS_LO, PLAUS_HI = 0.3, 30.0
        impossible = [v for v in cts if v < PLAUS_LO or v > PLAUS_HI]
        plaus = [v for v in cts if PLAUS_LO <= v <= PLAUS_HI]
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
            "n_rows": len(rows), "n_cycles": n, "n": n,
            "median": round(_pctl(cts, 0.5), 2) if n else None,
            "p85": round(_pctl(cts, 0.85), 2) if n else None,
            "min": round(cts[0], 2) if n else None, "max": round(cts[-1], 2) if n else None,
            "hist": _hist(cts), "hist_edges": _HIST_EDGES, "spec": spec_out,
            "measurement_suspect": suspect, "n_impossible": len(impossible),
            "plausible_n": len(plaus),
            "plausible_median": (round(_pctl(plaus, 0.5), 2) if plaus else None),
            "plausible_p85": (round(_pctl(plaus, 0.85), 2) if plaus else None),
            "reason": (None if n else "era rows exist but NO completed open->close cycle "
                       "(floor reads without usable pairs) — the gap is real")}
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


def _era_for(db, gw, cam):
    """The templates-hash prefix to filter this camera's door rows by, and where it came from.

    Auto-resolution takes the newest row's door_version — a rebuild therefore moves the era on its
    own, which is correct: the new templates ARE a new instrument. The panel prints whichever era
    was used, so an auto-resolved era is never silent."""
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


def _derive_floor_alphabet(rows, gw_cam_labels, min_sightings=3, max_fps=None):
    """Derive the valid floor set from EVIDENCE, not a typed list (the ask). A floor string is admitted
    when it corroborates — never on mere occurrence, which is circular (a misread would whitelist
    itself). Admission = all glyphs human-verified AND one of:
      (a) it is a human-LABELED floor (labels.json) — direct evidence, covers non-numeric floors, or
      (b) it is numeric with >= min_sightings confident sightings AND transition support: at least once
          it sat next to a temporal-neighbour read reachable at <= max_fps floors/sec (i.e. it appears
          inside a sequential run, not as a teleport). 129 between two 19s is 110 floors in one read
          interval -> no support -> rejected; the phantom-hundreds rule falls straight out of this.
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
    # Admission. The glyph set is NOT a hard gate: a sparse label sample omits digits that real floors
    # use (labels 12,19,20 never show an 8, yet floor 18 is real), and the reader can only emit glyphs
    # it has templates for anyway. Corroboration — numeric run + sightings — is the stronger evidence.
    # So: labeled floors are trusted directly; numeric floors admit on corroboration; a non-numeric
    # floor the human never labeled has no corroboration path (letters can't be placed on the number
    # line) and is rejected — which is exactly what kills 7G while keeping a labeled G.
    admitted, detail = set(), {}
    for f, d in seen.items():
        is_labeled = f in labeled
        numeric = _floor_idx(f) is not None
        if is_labeled:
            via = "labeled"; admitted.add(f)
        elif numeric and d["n"] >= min_sightings and d["supported"]:
            via = "corroborated"; admitted.add(f)
        elif numeric and not d["supported"]:
            via = "reject:no_transition_support (teleport/phantom cell)"
        elif numeric and d["n"] < min_sightings:
            via = f"reject:only_{d['n']}_sightings (<{min_sightings})"
        else:
            via = "reject:non_numeric_unlabeled (no corroboration path)"
        detail[f] = {"n": d["n"], "first": d["first"], "last": d["last"],
                     "supported": d["supported"], "admitted": f in admitted, "via": via}
    return admitted, detail


def _tier2(db, gw, cam, transits, t0=None, t1=None):
    """Tier-2 for one camera, from the gw_door_event stream of ONE era.

    transits: [(ts, direction)] for this cam, ascending — joined to door-open windows for per-floor
    demand. Returns None when the era has no rows at all (nothing to say), otherwise a dict whose
    every metric carries its own n plus the era/quality filter that produced it.
    """
    era, era_src = _era_for(db, gw, cam)
    if not era:
        return None
    rows = _q(db, "SELECT ts, floor, direction, door_state, reason, read_conf FROM gw_door_event "
                  "WHERE gateway_id=? AND cam=? AND door_version LIKE ? ORDER BY ts",
              (gw, cam, era + "%"))
    # Optional DATE RANGE on top of the era. Both sides are filtered together: leaving transits
    # unfiltered while narrowing the door rows would join riders to windows that are no longer in
    # the result, and the per-floor totals would exceed the range they claim to describe.
    if t0 is not None:
        rows = [r for r in rows if _in_range(r["ts"], t0, t1)]
        transits = [t for t in transits if _in_range(t[0], t0, t1)]
    if not rows:
        return None

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
    # DERIVED by default, from this era's own evidence (glyphs + labeled floors + read corroboration);
    # DASH_FLOOR_ALPHABET is an optional manual override for the day the derivation is wrong.
    manual = _floor_alphabet(cam)
    derived, alpha_detail = _derive_floor_alphabet(rows, _labels_evidence(gw, cam))
    if manual is not None:
        alphabet, alpha_source = manual, "manual override (DASH_FLOOR_ALPHABET)"
    elif derived:
        alphabet, alpha_source = derived, "derived from evidence"
    else:
        alphabet, alpha_source = None, "none (not enough evidence yet — accepting all)"
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
            for nxt in rows[idx + 1:]:
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
        "expected_era": exp_era, "built_at": built_at, "stale_templates": stale_templates,
        "era_filter": era_note,
        "quality_reasons": list(DOOR_OK_REASONS),
        "floor_whitelist": (sorted(alphabet) if alphabet else None),
        "floor_alphabet_source": alpha_source,
        "floor_alphabet_detail": [
            {"floor": f, "n": d["n"], "first": round(d["first"], 0), "last": round(d["last"], 0),
             "admitted": d["admitted"], "via": d["via"]}
            for f, d in sorted(alpha_detail.items(), key=lambda kv: (-kv[1]["n"], kv[0]))],
        "off_alphabet_rejected": sum(v for k, v in census.items()
                                     if str(k).startswith(("off_alphabet", "not_in_derived_alphabet"))),
        "rows_in_era": len(rows),
        "confident_reads": len(conf),
        "reason_census": census,
        # C17/C18
        "stops": {"n": len(stops), "up": up_stops, "down": dn_stops, "no_arrow": no_arrow,
                  "unattributed": unattributed},
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
        "transition_census": _door_transition_census(db, gw, cam, era),
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
    today = _ist_today_epoch()
    rows = _q(db, "SELECT cam, direction, ts FROM transit_event WHERE gateway_id=?", (gw,))
    by = {}
    for r in rows:
        d = by.setdefault(r["cam"], {"bt": 0, "at": 0, "b": 0, "a": 0, "last": None})
        ins = r["direction"] == "in"
        d["b" if ins else "a"] += 1
        if r["ts"] and r["ts"] >= today:
            d["bt" if ins else "at"] += 1
        if r["ts"] and (d["last"] is None or r["ts"] > d["last"]):
            d["last"] = r["ts"]
    return {cam: {"boarded_today": d["bt"], "alighted_today": d["at"],
                  "boarded_total": d["b"], "alighted_total": d["a"], "last_ts": d["last"]}
            for cam, d in by.items()}


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


def _latest(db, table, gw):
    rows = _q(db, f"SELECT * FROM {table} WHERE gateway_id=? ORDER BY id DESC LIMIT 1", (gw,))
    return dict(rows[0]) if rows else None


@dash_router.get("/dash/{gw}/data")
def dash_data(gw: str):
    db = _db()
    now = time.time()
    cams = _cameras(db, gw)
    door = _door_by_cam(db, gw)                      # Pi-era (gw_event), RETIRED instrument
    door_gpu = _door_gpu_by_cam(db, gw, [c["cam"] for c in cams])   # GPU-era (gw_door_event), LIVE
    trans = _transit_by_cam(db, gw)
    xfer = _transfer_by_cam(db, gw)
    floor_cov = _floor_coverage(db, gw)
    registry = _registry(db, gw)
    tj = _transits_for_join(db, gw)
    tier2 = {c["cam"]: _tier2(db, gw, c["cam"], tj.get(c["cam"], [])) for c in cams}
    tier2 = {k: v for k, v in tier2.items() if v}
    ana = _analyzers(db, gw)
    val = _validations(db, gw)
    w = _latest(db, "watch_status", gw)
    r = _latest(db, "relay_status", gw)
    db.close()

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
        headline.append(dict(spec, cam=cam, median=g["median"], p85=g["p85"], n=g["n"],
                             instrument="GPU door engine (gw_door_event) — LIVE",
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
    if not tier2:
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
                         "pi": pi, "relay": relay, "gpu": gpu,
                         "cameras": out_cams, "headline": headline, "registry": registry,
                         "floor_coverage": floor_cov, "tier2": tier2, "unavailable": unavailable,
                         "door_gpu": door_gpu,
                         "boundaries": {"doorwatch_retired": DOORWATCH_RETIRED_BOUNDARY,
                                        "close_travel_max": CLOSE_TRAVEL_MAX_BOUNDARY}})


@dash_router.get("/dash/{gw}/trends")
def dash_trends(gw: str, cam: str = "", from_h: int = -1, to_h: int = -1,
                period: str = "all", from_d: str = "", to_d: str = ""):
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
    ev = _q(db, "SELECT e.door_open_start_ts os, e.door_open_full_ts of, e.door_close_start_ts cs, "
                "e.close_travel_s ct, e.quality q, e.boarded b, e.alighted a "
                "FROM gw_event e JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?" + cam_filter, args)
    tr = _q(db, "SELECT ts, direction FROM transit_event WHERE gateway_id=?" +
            (" AND cam=?" if cam else ""), ([gw, cam] if cam else [gw]))
    db.close()
    # RANGE FILTER. Door cycles carry a local ISO timestamp and transits an epoch, so both are
    # normalised to epoch before comparing — mixing the two representations is how a range quietly
    # drops one series and not the other.
    t0, t1, range_label = _range_bounds(period, from_d, to_d)
    if t0 is not None:
        ev = [r for r in ev if _in_range(_epoch(r["os"]), t0, t1)]
        tr = [r for r in tr if _in_range(r["ts"], t0, t1)]

    prof = {h: {"cycles": 0, "boarded": 0, "alighted": 0, "closes": [], "xfer": []} for h in range(24)}
    days = set()
    for r in ev:
        h = _local_hour(r["os"])
        if h is None:
            continue
        days.add((r["os"] or "")[:10])
        prof[h]["cycles"] += 1                                   # every opening = demand (flagged included)
        ep = _epoch(r["os"])
        clean = (r["q"] is None or r["q"] == "ok")
        if r["ct"] is not None and clean and ep is not None and ep >= _BOUNDARY_EPOCH:
            prof[h]["closes"].append(float(r["ct"]))             # comparable regime only
        load = (r["b"] or 0) + (r["a"] or 0)
        o, c = _epoch(r["of"]), _epoch(r["cs"])
        if load > 0 and o is not None and c is not None and c > o:
            prof[h]["xfer"].append((c - o) / load)
    for r in tr:
        try:
            h = datetime.fromtimestamp(r["ts"], IST).hour
        except Exception:
            continue
        prof[h]["boarded" if r["direction"] == "in" else "alighted"] += 1

    # Days that actually CONTRIBUTED, reported honestly; the max(1,..) is only the divisor guard.
    # An empty range must read "0 days" rather than silently averaging over a day that had nothing.
    n_days_real = len(days)
    ndays = max(1, n_days_real)
    profile = [{"hour": h, "cycles": prof[h]["cycles"],
                "boarded": prof[h]["boarded"], "alighted": prof[h]["alighted"],
                **{f"close_{k}": v for k, v in _stats(prof[h]["closes"]).items()}} for h in range(24)]

    def window(lo, hi):
        hrs = [h for h in range(24) if lo <= h < hi]
        cyc = sum(prof[h]["cycles"] for h in hrs)
        closes = [v for h in hrs for v in prof[h]["closes"]]
        xfer = [v for h in hrs for v in prof[h]["xfer"]]
        bo = sum(prof[h]["boarded"] for h in hrs); al = sum(prof[h]["alighted"] for h in hrs)
        span = max(1, len(hrs))
        return {"from": lo, "to": hi, "cycles": cyc, "cycles_per_hr": round(cyc / (span * ndays), 2),
                "boarded": bo, "alighted": al, "riders_per_hr": round((bo + al) / (span * ndays), 2),
                "close": _stats(closes), "transfer": {**_stats(xfer), "provisional": True},
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
                         "windows": windows,
                         "range": {"period": period, "from_d": from_d, "to_d": to_d,
                                   "label": range_label, "t0": t0, "t1": t1,
                                   "cycles": len(ev), "transits": len(tr)},
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
}


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
                period: str = "all", from_d: str = "", to_d: str = ""):
    """Download the underlying rows. dataset = door_cycles | transits | floor_events | per_floor."""
    if dataset not in _EXPORT:
        return JSONResponse({"error": f"unknown dataset {dataset!r}", "datasets": _EXPORT}, status_code=400)
    t0, t1, label = _range_bounds(period, from_d, to_d)
    db = _db()
    tag = f"{gw}_{cam or 'fleet'}_{dataset}_{(period or 'all')}"
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

    if dataset == "floor_events":
        # Per-camera era, exactly as the panel resolves it — a fleet export spans several cameras
        # with DIFFERENT eras, so one global LIKE would silently drop whole cameras from the file.
        cams = [cam] if cam else [c["cam"] for c in _cameras(db, gw)]
        rows = []
        for c in cams:
            era, _src = _era_for(db, gw, c)
            if not era:
                continue
            rows += _q(db, "SELECT cam, ts, floor, direction, door_state, read_conf, panels_agreed, "
                           "reason, close_travel_s, door_version FROM gw_door_event "
                           "WHERE gateway_id=? AND cam=? AND door_version LIKE ? ORDER BY ts",
                       (gw, c, era + "%"))
        db.close()
        out = [(r["cam"], r["ts"], _iso_ist(r["ts"]), r["floor"], r["direction"], r["door_state"],
                r["read_conf"], r["panels_agreed"], r["reason"], r["close_travel_s"], r["door_version"])
               for r in rows if _in_range(r["ts"], t0, t1)]
        return _csv(out, ["cam", "ts_epoch", "ts_ist", "floor", "direction", "door_state", "read_conf",
                          "panels_agreed", "reason", "close_travel_s", "door_version"], f"{tag}.csv")

    # per_floor — the aggregate as displayed, including the by-hour columns the heatmap draws
    tj = _transits_for_join(db, gw)
    cams = [cam] if cam else [c["cam"] for c in _cameras(db, gw)]
    out = []
    for c in cams:
        t2 = _tier2(db, gw, c, tj.get(c, []), t0, t1)
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
function esc(s){return s==null?'':(''+s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function age(s){return s==null?'—':(s<90?Math.round(s)+'s':Math.round(s/60)+'m')+' ago'}
function kv(k,v,c){return '<div class=kv><span class=mut>'+k+'</span><b class="'+(c||'')+'">'+v+'</b></div>'}
function staleCls(s,lim){return s==null?'stale':(s>lim?'stale':'ok')}

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
  document.getElementById('strip').innerHTML=h.join('');
}

function headline(d){
  if(!d.headline||!d.headline.length){document.getElementById('headline').innerHTML='';return;}
  var h=d.headline.map(function(x){
    var susp=x.measurement_suspect?'<span class="pill bad" style="font-size:10px;margin-right:6px">MEASUREMENT SUSPECT</span>':'';
    var tag='<span class="pill '+(x.live?'ok':'mut')+'" style="font-size:10px;margin-right:6px">'
      +(x.live?'LIVE · GPU · era '+esc((x.era||'').slice(0,8)):'RETIRED · Pi-watch')+'</span>';
    var dl;
    if(x.median==null){
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
    var out='<div class="obs mono">'+tag+susp+dl+'</div>';
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
  var note=(d.boundaries&&d.boundaries.doorwatch_retired)?('<div class=mut style="font-size:11px;margin-top:6px">Pi-watch (gw_event) and GPU engine (gw_door_event) are DIFFERENT INSTRUMENTS, split at '+esc(d.boundaries.doorwatch_retired.slice(0,10))+' (Pi door-watch retired). Their close-travel numbers are shown separately and are NOT comparable.</div>'):'';
  document.getElementById('headline').innerHTML='<div class=headline><h3 class=mut style="margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase">compliance — assumption beside observation, no verdict</h3>'+h+note+'</div>';
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
  cur=cur||(d.cameras[0]&&d.cameras[0].cam);
  document.getElementById('tabs').innerHTML=d.cameras.map(function(c){
    var live=c.snap&&!c.snap.stale, dot=live?'#127a3d':(c.snap?'#b06a00':'#ccc');
    return '<div class="tab'+(c.cam===cur?' on':'')+'" onclick="pick(\''+c.cam+'\')">'
      +'<span class=dot style="background:'+dot+'"></span>'+esc(c.cam)+(c.label?' '+esc(c.label):'')+'</div>';
  }).join('');
}
function pick(cam){
  cur=cam;
  try{history.replaceState(null,'', '/dash?cam='+encodeURIComponent(cam));}catch(e){}
  render();
}

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
  else { gpuDoor='<div class=mut style="font-size:10px;text-transform:uppercase;letter-spacing:.08em">GPU engine · era '+esc((g.era||'').slice(0,8))+' · LIVE</div>'
    +kv('close cycles',esc(g.n_cycles))
    +kv('close median / p85',(g.median==null?'—':g.median+'s')+' / '+(g.p85==null?'—':g.p85+'s')+'  (n='+g.n+')')
    +kv('range',(g.min==null?'—':g.min+'–'+g.max+'s'))
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
    +'<div class=card><h3>State</h3>'+state+'</div>'
    +'<div class=card><h3>GPU analysis</h3>'+gpuToggle(d,c.cam)+'</div>'
    +'<div class=card><h3>Camera</h3>'+kv('channel',esc(c.channel))+kv('label',esc(c.label||'—'))
      +kv('snapshot',c.snap?('<span class="'+staleCls(c.snap.age_s,20)+'">'+age(c.snap.age_s)+'</span>'):'—')+'</div>'
    +'</div></div>'
    + tier2card((d.tier2||{})[c.cam]);
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
function tier2card(t){
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

function winCard(name,w){
  if(!w)return '';
  var ratio=(w.demand_ratio_vs_allday!=null)?('  <b class="'+(w.demand_ratio_vs_allday>=1.3?'bad':'')+'">'+w.demand_ratio_vs_allday+'× all-day</b>'):'';
  return '<div class=card><h3>'+esc(name)+' <span class=mut>'+w.from+':00–'+w.to+':00</span></h3>'
    +kv('cycles/hr',w.cycles_per_hr+ratio)
    +kv('close med / p85',(w.close.median==null?'—':w.close.median+'s')+' / '+(w.close.p85==null?'—':w.close.p85+'s')+' (n='+w.close.n+')')
    +kv('transfer',(w.transfer.median==null?'—':w.transfer.median+' s/pp')+' (n='+w.transfer.n+') *')
    +kv('riders/hr',w.riders_per_hr)
    +kv('transits/cycle',w.transits_per_cycle==null?'—':w.transits_per_cycle)+'</div>';
}
function trCams(){
  var cams=(DATA&&DATA.cameras)?DATA.cameras.map(function(c){return c.cam}):[];
  return '<div class=tabs style="margin-bottom:6px">'
    +['',].concat(cams).map(function(c){var lbl=c||'fleet';
       return '<div class="tab'+(trCam===c?' on':'')+'" onclick="trCam=\''+c+'\';loadTrends()">'+esc(lbl)+'</div>';}).join('')+'</div>';
}
// ---- period picker + table view + CSV (operator batch) ----
var trPeriod='all', trTable=false;
function setPeriod(p){trPeriod=p;loadTrends();}
function toggleTable(){trTable=!trTable;renderTrends();}
function periodBar(){
  var opts=[['day','Today'],['week','7 days'],['month','30 days'],['all','All']];
  return '<div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:2px 0 8px">'
    +opts.map(function(o){return '<button class="tog'+(trPeriod===o[0]?' on':'')+'" onclick="setPeriod(\''+o[0]+'\')">'+o[1]+'</button>';}).join('')
    +'<span style="width:10px"></span>'
    +'<button class="tog'+(trTable?' on':'')+'" onclick="toggleTable()">'+(trTable?'charts':'table')+'</button>'
    +'<span class=mut id=rangelab style="font-size:11px;margin-left:6px"></span></div>';
}
// Every download carries the SAME cam/period the screen is showing, so a spreadsheet and the chart
// above it cannot disagree about which rows they describe.
function dl(ds,label){
  var q='?dataset='+ds+'&period='+encodeURIComponent(trPeriod)+(trCam?('&cam='+encodeURIComponent(trCam)):'');
  return '<a class=dlbtn href="/dash/'+GW+'/export.csv'+q+'">⤓ '+label+'</a>';
}
function exportBar(){
  return '<div class=dlbar>'+dl('door_cycles','door cycles')+dl('transits','transits')
    +dl('floor_events','floor events')+dl('per_floor','per-floor')
    +'<span class=mut style="font-size:11px">CSV — the rows behind these charts, same range'+(trCam?'':' (fleet)')+'</span></div>';
}
function trTableHtml(prof,W){
  var head='<tr><th>hour</th><th>cycles</th><th>boarded</th><th>alighted</th><th>riders</th>'
    +'<th>close med (s)</th><th>close p85</th><th>n</th></tr>';
  var body=prof.map(function(p){
    return '<tr><td>'+pad2(p.hour)+':00</td><td>'+p.cycles+'</td><td>'+p.boarded+'</td><td>'+p.alighted
      +'</td><td>'+(p.boarded+p.alighted)+'</td><td>'+(p.close_median==null?'—':p.close_median)
      +'</td><td>'+(p.close_p85==null?'—':p.close_p85)+'</td><td>'+(p.close_n||0)+'</td></tr>';}).join('');
  var tot=prof.reduce(function(a,p){a.c+=p.cycles;a.b+=p.boarded;a.a+=p.alighted;return a;},{c:0,b:0,a:0});
  var foot='<tr class=tot><td>total</td><td>'+tot.c+'</td><td>'+tot.b+'</td><td>'+tot.a+'</td><td>'
    +(tot.b+tot.a)+'</td><td colspan=3></td></tr>';
  return '<div class=card><h3>hour-of-day table <span class=mut style="font-weight:400">same aggregates as the charts</span></h3>'
    +'<div class=hmwrap><table class=t2>'+head+body+foot+'</table></div></div>';
}
function renderTrends(){
  if(!TR){document.getElementById('trendview').innerHTML=trCams()+'<div class=mut>loading…</div>';return;}
  var prof=TR.profile, hours=prof.map(function(p){return p.hour}), W=TR.windows;
  var bd=TR.boundaries.close_travel_max.iso.slice(0,10);
  var gaps=(TR.data_gaps||[]);
  var gapbanner=gaps.length?('<div class=mut style="font-size:12px;margin:2px 0 6px;padding:4px 8px;border-left:3px solid #b00;background:rgba(176,0,0,.06)"><b>DATA GAP</b> — '+gaps.map(function(g){return esc(g.note)}).join(' · ')+'. Hour buckets overlapping this window are undercounted (samples MISSING, not low demand).</div>'):'';
  var h=trCams()+periodBar()
    +'<div class=mut style="font-size:12px;margin:2px 0 6px">'+esc(TR.cam)+' · '+TR.n_days+' day(s) with data · '
    +((TR.range&&TR.range.label)?esc(TR.range.label)+' · ':'')
    +((TR.range?TR.range.cycles:0))+' cycles, '+((TR.range?TR.range.transits:0))+' transits in range · '
    +'close-travel uses the post-'+bd+' regime only (CLOSE_TRAVEL_MAX comparability boundary)</div>'
    +exportBar()
    +gapbanner
    +'<div class=strip>'+winCard('all-day',W.all_day)+winCard('AM peak',W.am_peak)+winCard('PM peak',W.pm_peak)+'</div>'
    +'<div class=mut style="font-size:11px;margin:2px 0 8px">* transfer PROVISIONAL (transit precision, re-validating). <b>THE PEAK TRAP</b>: the sheet coefficients describe a PEAK design condition, not an all-day average — peak &amp; all-day are shown SEPARATELY; the ratio is itself a finding.</div>'
    +(trTable?trTableHtml(prof,W):(''
    +'<div class=card>'+svgBars('cycles / hour-of-day — the demand curve',
        'how often this lift’s doors operate — the work rate',
        hours,prof.map(function(p){return p.cycles}),'#127a3d',null,'','cycles')+'</div>'
    +'<div class=card>'+svgBars('riders (boardings+alightings) / hour-of-day',
        'boardings + alightings counted at this door — usage volume, not unique people',
        hours,prof.map(function(p){return p.boarded+p.alighted}),'#2a6db0',null,'','riders')+'</div>'
    +'<div class=card>'+svgLine('close-travel median / hour-of-day (s)',
        'median seconds for the door to close, per hour — the 2.31s line is Bank C’s compliance cliff',
        hours,prof.map(function(p){return p.close_median}),'#b06a00',2.31,'2.31 Bank C','s')+'</div>'))
    +heatCard();
  document.getElementById('trendview').innerHTML=h;
}

// ---- floor x hour intensity ----
// Defaults to STOPS because stops are attributed today; riders depend on the transit join and the
// toggle says so rather than silently drawing an empty grid.
var heatMode='stops';
function setHeat(m){heatMode=m;renderTrends();}
function heatCard(){
  var t2=(DATA&&DATA.tier2)?DATA.tier2[trCam]:null;
  var head='<div class=card><h3>riders &amp; stops per floor, per hour '
    +'<button class="tog'+(heatMode==='stops'?' on':'')+'" onclick="setHeat(\'stops\')">stops</button>'
    +'<button class="tog'+(heatMode==='riders'?' on':'')+'" onclick="setHeat(\'riders\')">riders</button></h3>'
    +'<div class=intent>which floors are busy, and when — one row per floor, one column per hour</div>';
  if(!trCam){
    return head+'<div class=blank>pick a camera above — floors belong to one lift, so a fleet total would mix shafts</div></div>';
  }
  if(!t2){
    return head+'<div class=blank>no door-engine reads for '+esc(trCam)+' in the current era</div></div>';
  }
  var note='';
  if(heatMode==='riders'){
    var m=t2.transits_matched||0, j=t2.transits_joinable;
    note='<div class=mut style="font-size:11px;margin-top:4px">riders come from transits joined to a door-open window: <b>'
      +m+'</b> of <b>'+(j==null?'?':j)+'</b> joinable in this era'
      +((t2.transits_total!=null&&j!=null&&t2.transits_total>j)?(' (' +t2.transits_total+' lifetime, most predating this era)'):'')
      +'. Unjoined transits are not on any floor and are absent here.</div>';
  }
  return head+svgHeat(t2.per_floor,heatMode)+note
    +'<div class=mut style="font-size:11px">era: '+esc(t2.era_filter||'')+'</div></div>';
}
function loadTrends(){
  renderTrends();  // show selector immediately
  fetch('/dash/'+GW+'/trends?period='+encodeURIComponent(trPeriod)+(trCam?('&cam='+encodeURIComponent(trCam)):'')).then(function(r){return r.json()}).then(function(t){TR=t;renderTrends();}).catch(function(){});
}

function render(){ if(!DATA)return; nav(); strip(DATA); headline(DATA); unavail(DATA); if(mode==='cams'){tabs(DATA); panel(DATA);} }
function load(){
  fetch('/dash/'+GW+'/data').then(function(r){return r.json()}).then(function(d){
    DATA=d; document.getElementById('stamp').textContent='· '+d.ist_today+' · updated '+new Date().toLocaleTimeString();
    render();
  }).catch(function(){document.getElementById('stamp').textContent='· FETCH FAILED';});
}
load(); setInterval(load, 15000);
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
