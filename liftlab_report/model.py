"""Aggregation: rows → per-era figures. NO POOLING across era boundaries.

The unit of aggregation everywhere is (cam, instrument, era_id). A fleet
figure is an UNWEIGHTED SUM of per-lift counts and is labelled as such; a
fleet close-travel statistic is never emitted because per-camera door eras are
distinct instruments ("n/a — spans eras").
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import eras, stats
from .eras import GPU_ENGINE, PI_WATCH


def _era_key(c) -> tuple:
    return (c["cam"], c["instrument"], c["era_id"])


def aggregate_cycles(cycles: list[dict], t0: float, t1: float,
                     min_close_s: float | None = None) -> dict[tuple, dict]:
    """{(cam, instrument, era_id): agg}. Gap-time cycles are excluded from
    rate denominators upstream; here they are excluded from duration pools
    entirely (a close inside a declared gap window is suspect data).

    min_close_s is the one-frame quantization floor (eras.MIN_PLAUSIBLE_CLOSE_S
    by default): closes below it are door-state flip artifacts, not closes, and
    are rejected from every close-travel statistic. The pre-filter pool is kept
    alongside so the effect of the floor is auditable on PER-LIFT rather than
    invisible."""
    floor = (eras.MIN_PLAUSIBLE_CLOSE_S if min_close_s is None
             else float(min_close_s))
    pools: dict[tuple, dict] = {}
    for c in cycles:
        if eras.in_gap(c["cam"], c["ts"]):
            key = _era_key(c)
            p = pools.setdefault(key, _empty_pool())
            p["n_in_gap_excluded"] += 1
            continue
        p = pools.setdefault(_era_key(c), _empty_pool())
        p["n_cycles"] += 1
        p["first_ts"] = min(p["first_ts"], c["ts"]) if p["first_ts"] else c["ts"]
        p["last_ts"] = max(p["last_ts"], c["ts"]) if p["last_ts"] else c["ts"]
        # Every measured travel value, quotable or not — the quantum is a
        # property of the SAMPLING, so it is detected from the widest pool
        # available for the era, not from the post-filter survivors.
        if c.get("close_travel_s") is not None:
            p["all_travel"].append(float(c["close_travel_s"]))
        if c.get("open_travel_s") is not None and c["open_travel_s"] > 0:
            p["all_travel"].append(float(c["open_travel_s"]))
        if c.get("clean_close") and c.get("close_travel_s") is not None:
            v = float(c["close_travel_s"])
            p["closes_prefilter"].append(v)
            if v < floor:
                p["n_floor_rejected"] += 1
            else:
                p["closes"].append(v)
        else:
            p["n_closes_excluded"] += 1
        if c.get("dwell_s") is not None and c["dwell_s"] > 0:
            p["dwells"].append(float(c["dwell_s"]))
        if c.get("open_travel_s") is not None and c["open_travel_s"] > 0:
            p["opens"].append(float(c["open_travel_s"]))
        if c.get("boarded") is not None:
            p["boarded"] += int(c["boarded"])
            p["n_counted_cycles"] += 1
            p["pax_per_trip"].append(int(c["boarded"]))
        if c.get("alighted") is not None:
            p["alighted"] += int(c["alighted"])
        load = (c.get("boarded") or 0) + (c.get("alighted") or 0)
        if load > 0 and c.get("dwell_s") and c["dwell_s"] > 0:
            p["transfer_pp"].append(c["dwell_s"] / load)
            p["lost_time"].append(c["dwell_s"] - eras.SHEET_TRANSFER_S_PP * load)
    # The frame quantum is a property of the SAMPLING, not of one era's pool.
    # Detect per era first, then use the MEDIAN of the successful detections as
    # the fallback for eras whose own pool was inconclusive. Pooling the raw
    # values fleet-wide would let one camera whose travels drift off-grid mask
    # the quantum for every other camera — and an era with no quantum silently
    # dodges the suppression rule, which is exactly the failure this guards.
    per_era = {key: stats.detect_quantum(p["all_travel"])
               for key, p in pools.items()}
    found = sorted(q for q in per_era.values() if q is not None)
    fleet_q = found[len(found) // 2] if found else None
    out = {}
    for key, p in pools.items():
        out[key] = _finalise(key, p, t0, t1, floor, per_era[key], fleet_q)
    return out


def _empty_pool() -> dict:
    return {"n_cycles": 0, "closes": [], "closes_prefilter": [], "dwells": [],
            "opens": [], "all_travel": [],
            "transfer_pp": [], "lost_time": [], "pax_per_trip": [],
            "boarded": 0, "alighted": 0, "n_counted_cycles": 0,
            "n_closes_excluded": 0, "n_in_gap_excluded": 0,
            "n_floor_rejected": 0,
            "first_ts": None, "last_ts": None}


def _finalise(key, p, t0, t1, floor, own_q=None, fleet_q=None) -> dict:
    cam, instrument, era_id = key
    closes = sorted(p["closes"])
    closes_pre = sorted(p["closes_prefilter"])
    opens = sorted(p["opens"])
    n = len(closes)
    spec = eras.DOOR_SPECS.get(cam)
    cliff = (spec or {}).get("compliance_s", eras.COMPLIANCE_CLIFF_S)
    over = sum(1 for v in closes if v > cliff)
    covered_s = max(0.0, (t1 - t0) - eras.gap_overlap_s(cam, t0, t1))

    # Sampling resolution for this era, detected from every travel value it
    # produced (opens included — they are the same clock), falling back to the
    # fleet-wide detection. A per-era value within jitter of the fleet value is
    # snapped to it so ±1 ms noise does not present as two different quanta.
    quantum, quantum_source = own_q, "detected in this era"
    if quantum is None:
        quantum = fleet_q
        quantum_source = ("median of the other eras' detections "
                          "(this era's own pool was inconclusive)"
                          if fleet_q is not None else "undetermined")
    elif fleet_q is not None and abs(quantum - fleet_q) <= stats.grid_tol(fleet_q):
        quantum = fleet_q
    close_floor_share = stats.floor_share(closes_pre, quantum)
    open_floor_share = stats.floor_share(opens, quantum)
    n_pre = len(closes_pre)
    reject_frac = (p["n_floor_rejected"] / n_pre) if n_pre else None

    # Suppression: a pool with too much mass pinned at one quantum is
    # resolution-bound and gets a reason instead of a verdict.
    def _suppress(fs, kind, remedy):
        if fs["frac"] is not None and fs["frac"] > eras.QUANTUM_SUPPRESS_FRAC:
            return True, stats.floor_suppression_note(kind, fs, remedy)
        return False, None

    open_sup, open_why = _suppress(open_floor_share, "open-travel",
                                   stats.OPEN_TRAVEL_REMEDY)
    close_sup, close_why = _suppress(close_floor_share, "close-travel",
                                     stats.CLOSE_TRAVEL_REMEDY)
    return {
        "cam": cam, "instrument": instrument, "era_id": era_id,
        "first_ts": p["first_ts"], "last_ts": p["last_ts"],
        "n_cycles": p["n_cycles"],
        "n_in_gap_excluded": p["n_in_gap_excluded"],
        "n_closes_excluded": p["n_closes_excluded"],
        "quantum_s": quantum,
        "quantum_source": quantum_source,
        "close": {
            "n": n,
            "median_ci": stats.median_ci(closes),
            "p85": stats.pctl(closes, 0.85),
            "min": closes[0] if closes else None,
            "max": closes[-1] if closes else None,
            "mean_ci": stats.mean_ci(closes),
            "hist": stats.hist(closes, stats.CLOSE_HIST_EDGES),
            "over_cliff": stats.wilson_ci(over, n),
            "cliff_s": cliff,
            "values": closes,
            # pre-floor-filter twins, so the filter's effect is auditable
            "n_prefilter": n_pre,
            "median_prefilter": stats.pctl(closes_pre, 0.5),
            "p85_prefilter": stats.pctl(closes_pre, 0.85),
            "floor_s": floor,
            "n_floor_rejected": p["n_floor_rejected"],
            "floor_reject_frac": reject_frac,
            "floor_reject_warn": (reject_frac is not None
                                  and reject_frac > eras.FLOOR_REJECT_WARN_FRAC),
            "at_quantum": close_floor_share,
            "suppressed": close_sup,
            "suppression_reason": close_why,
        },
        "dwell": {
            "n": len(p["dwells"]),
            "median_ci": stats.median_ci(sorted(p["dwells"])),
            "p85": stats.pctl(sorted(p["dwells"]), 0.85),
            "hist": stats.hist(p["dwells"], stats.DWELL_HIST_EDGES),
        },
        "open_travel": {
            "n": len(opens),
            "median_ci": stats.median_ci(opens),
            "at_quantum": open_floor_share,
            "suppressed": open_sup,
            "suppression_reason": open_why,
        },
        "transfer_pp": stats.mean_ci(p["transfer_pp"]),
        "lost_time": stats.mean_ci(p["lost_time"]),
        "pax_per_trip": stats.mean_ci([float(x) for x in p["pax_per_trip"]]),
        "boarded": p["boarded"], "alighted": p["alighted"],
        "n_counted_cycles": p["n_counted_cycles"],
        "covered_s": covered_s,
        "cycles_per_hr": (p["n_cycles"] / (covered_s / 3600.0)) if covered_s > 0 else None,
    }


# ── transits ────────────────────────────────────────────────────────────────

def aggregate_transits(transits: list[dict], stamps: dict, t0: float, t1: float
                       ) -> dict[tuple, dict]:
    """{(cam, counting_version): {boarded, alighted, per_hr, first_ts,
    last_ts, n_in_gap_excluded}} — gap transits excluded from counts AND the
    rate denominator excludes gap time."""
    by: dict[tuple, dict] = {}
    for t in transits:
        ver = eras.counting_version_at(t["cam"], t["ts"], stamps)
        key = (t["cam"], ver)
        d = by.setdefault(key, {"boarded": 0, "alighted": 0, "n": 0,
                                "n_in_gap_excluded": 0,
                                "first_ts": None, "last_ts": None})
        if eras.in_gap(t["cam"], t["ts"]):
            d["n_in_gap_excluded"] += 1
            continue
        d["n"] += 1
        d["boarded" if t["direction"] == "in" else "alighted"] += 1
        d["first_ts"] = min(d["first_ts"], t["ts"]) if d["first_ts"] else t["ts"]
        d["last_ts"] = max(d["last_ts"], t["ts"]) if d["last_ts"] else t["ts"]
    for (cam, _ver), d in by.items():
        covered_s = max(0.0, (t1 - t0) - eras.gap_overlap_s(cam, t0, t1))
        d["per_hr"] = ((d["boarded"] + d["alighted"]) / (covered_s / 3600.0)
                       if covered_s > 0 else None)
    return by


def hourly_profile(transits: list[dict], pi_cycles: list[dict]) -> dict[str, dict]:
    """{cam: {hour: {'boarded': n, 'alighted': n}}} in IST hours, gap-excluded.
    Pi-era boarded/alighted come from gw_event cycle counts; GPU-era from
    transit_event. The two are kept in the same profile ONLY as counts of
    people (not durations) — the profile chart labels both sources."""
    prof: dict[str, dict] = {}
    for t in transits:
        if eras.in_gap(t["cam"], t["ts"]):
            continue
        h = datetime.fromtimestamp(t["ts"], eras.IST).hour
        d = prof.setdefault(t["cam"], {}).setdefault(h, {"boarded": 0, "alighted": 0})
        d["boarded" if t["direction"] == "in" else "alighted"] += 1
    for c in pi_cycles:
        if eras.in_gap(c["cam"], c["ts"]):
            continue
        h = datetime.fromtimestamp(c["ts"], eras.IST).hour
        d = prof.setdefault(c["cam"], {}).setdefault(h, {"boarded": 0, "alighted": 0})
        d["boarded"] += c.get("boarded") or 0
        d["alighted"] += c.get("alighted") or 0
    return prof


# ── peak analysis ────────────────────────────────────────────────────────────

def _boarding_events(transits, pi_cycles) -> list[tuple[float, int, str]]:
    """(ts, boardings, cam) — unified boarding stream, gap-excluded."""
    ev = [(t["ts"], 1, t["cam"]) for t in transits
          if t["direction"] == "in" and not eras.in_gap(t["cam"], t["ts"])]
    ev += [(c["ts"], int(c["boarded"]), c["cam"]) for c in pi_cycles
           if (c.get("boarded") or 0) > 0 and not eras.in_gap(c["cam"], c["ts"])]
    ev.sort()
    return ev


def peak_by_day(transits, pi_cycles, t0: float, t1: float,
                fixed_window: tuple[int, int] | None = None) -> list[dict]:
    """Per IST day in [t0,t1): worst 5-min window by boardings (sliding,
    60s step), or the fixed HH:MM-HH:MM window when supplied. Windows that
    overlap a fleet-wide declared gap are not eligible as 'worst'."""
    events = _boarding_events(transits, pi_cycles)
    out = []
    day = datetime.fromtimestamp(t0, eras.IST).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
    end = datetime.fromtimestamp(t1, eras.IST)
    while day < end:
        d0 = max(day.timestamp(), t0)
        d1 = min((day + timedelta(days=1)).timestamp(), t1)
        day_events = [(ts, n, cam) for ts, n, cam in events if d0 <= ts < d1]
        day_total = sum(n for _, n, _ in day_events)
        best = None
        if fixed_window is not None:
            w0 = day.replace(hour=fixed_window[0] // 60,
                             minute=fixed_window[0] % 60).timestamp()
            w1 = day.replace(hour=fixed_window[1] // 60,
                             minute=fixed_window[1] % 60).timestamp()
            n = sum(n_ for ts, n_, _ in day_events if w0 <= ts < w1)
            best = {"w0": w0, "w1": w1, "boardings": n}
        elif day_events:
            step, width = 60, 300
            lo = int(d0 // step) * step
            hi = int(d1 // step) * step
            j0 = 0
            for w0 in range(lo, hi, step):
                w1 = w0 + width
                n = 0
                for ts, n_, _ in day_events:
                    if w0 <= ts < w1:
                        n += n_
                    elif ts >= w1:
                        break
                mid = (w0 + w1) / 2
                fleet_gap = any(g["cams"] is None and
                                g["start_epoch"] <= mid < g["end_epoch"]
                                for g in eras.DATA_GAPS)
                if n > 0 and not fleet_gap:
                    if best is None or n > best["boardings"]:
                        best = {"w0": float(w0), "w1": float(w1), "boardings": n}
        # peak:average — peak 5-min boardings vs the day's mean 5-min boardings
        # over covered (non-gap) time.
        covered_s = (d1 - d0) - eras.gap_overlap_s("ch__fleet__", d0, d1)
        # fleet-wide gap only: use the union of all-cams gaps via any cam name;
        # per-cam residual coverage is on the COVERAGE sheet.
        n_5min_slots = covered_s / 300.0 if covered_s > 0 else None
        avg_5min = (day_total / n_5min_slots) if n_5min_slots else None
        out.append({
            "day": day.date().isoformat(),
            "day_boardings": day_total,
            "peak": best,
            "avg_5min": avg_5min,
            "peak_to_avg": (best["boardings"] / avg_5min
                            if best and avg_5min else None),
        })
        day += timedelta(days=1)
    return out


def parse_peak_window(spec: str) -> tuple[int, int] | None:
    """'HH:MM-HH:MM' → (start_min, end_min) since midnight, or None for auto."""
    if not spec or spec == "auto":
        return None
    a, b = spec.split("-", 1)
    h0, m0 = a.split(":")
    h1, m1 = b.split(":")
    w = (int(h0) * 60 + int(m0), int(h1) * 60 + int(m1))
    if not (0 <= w[0] < w[1] <= 24 * 60):
        raise ValueError(f"bad --peak-window {spec!r}")
    return w


# ── vs-the-sheet coefficient rows ────────────────────────────────────────────

def vs_sheet_rows(aggs: dict[tuple, dict], cam: str,
                  blockers: dict | None = None) -> list[dict]:
    """One row per MEP-02 coefficient per instrument-era for `cam`. Verdicts
    are mechanical (stats.verdict_vs_threshold); where a coefficient cannot be
    measured the row says so instead of vanishing."""
    spec = eras.DOOR_SPECS.get(cam, {})
    sheet_close = spec.get("sheet_close_s", eras.SHEET_CLOSE_S)
    sheet_open = spec.get("sheet_open_s", eras.SHEET_OPEN_S)
    sheet_xfer = spec.get("transfer_sheet_s", eras.SHEET_TRANSFER_S_PP)
    cliff = spec.get("compliance_s", eras.COMPLIANCE_CLIFF_S)
    rows = []
    cam_aggs = {k: v for k, v in aggs.items() if k[0] == cam}
    for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
        tag = f"{instrument} / {era_id}"
        mc = a["close"]["median_ci"]
        cl = a["close"]
        rows.append({
            "coefficient": "C27 door close travel (s)", "era": tag,
            "assumption": sheet_close, "threshold": cliff,
            "observed": mc["median"], "n": mc["n"],
            "ci": (mc["lo"], mc["hi"]),
            "verdict": (cl["suppression_reason"] if cl["suppressed"]
                        else stats.verdict_vs_threshold(mc, cliff)),
            "suppressed": cl["suppressed"],
        })
        ot = a["open_travel"]
        oc = ot["median_ci"]
        rows.append({
            "coefficient": "C27 door open travel (s)", "era": tag,
            "assumption": sheet_open, "threshold": sheet_open,
            "observed": oc["median"], "n": oc["n"],
            "ci": (oc["lo"], oc["hi"]),
            "verdict": (ot["suppression_reason"] if ot["suppressed"]
                        else stats.verdict_vs_threshold(oc, sheet_open)),
            "suppressed": ot["suppressed"],
        })
        xc = a["transfer_pp"]
        rows.append({
            "coefficient": "C26 transfer (s/person)", "era": tag,
            "assumption": sheet_xfer, "threshold": sheet_xfer,
            "observed": xc["mean"], "n": xc["n"],
            "ci": (xc["lo"], xc["hi"]),
            "verdict": (stats.verdict_vs_threshold(xc, sheet_xfer)
                        if xc["n"] else "not measurable in this era — needs "
                        "per-cycle boarded/alighted joined to dwell"),
        })
        pc = a["pax_per_trip"]
        rows.append({
            "coefficient": "C19 avg pax/trip", "era": tag,
            "assumption": "80% of capacity (capacity not in DB)",
            "threshold": None,
            "observed": pc["mean"], "n": pc["n"],
            "ci": (pc["lo"], pc["hi"]),
            "verdict": ("not comparable — car capacity not supplied; "
                        "observed value reported for the record" if pc["n"]
                        else "not measurable in this era — no per-cycle counts"),
        })
        lt = a["lost_time"]
        rows.append({
            "coefficient": "B24 lost time (s/stop, residual = dwell − 1.5×load)",
            "era": tag,
            "assumption": "sheet value not in DB", "threshold": None,
            "observed": lt["mean"], "n": lt["n"],
            "ci": (lt["lo"], lt["hi"]),
            "verdict": ("observed residual reported — no sheet value to compare"
                        if lt["n"] else "not measurable in this era"),
        })
    # C17/C18/C21/C22 — verdict unchanged (still not measurable), but the REASON is now derived
    # per coefficient from the data instead of the hardcoded "needs floor attribution", which was
    # wrong: floor attribution works on ch27/ch29/ch30.
    blockers = blockers or {}
    for cid, coeff in (("C17", "C17 probable stops (up)"),
                       ("C18", "C18 probable stops (down)"),
                       ("C21", "C21 speed factor (up)"),
                       ("C22", "C22 speed factor (down)")):
        b = blockers.get(cid) or {}
        rows.append({
            "coefficient": coeff, "era": "—", "assumption": "per sheet",
            "threshold": None, "observed": None, "n": 0, "ci": (None, None),
            "verdict": ("not measurable — "
                        + (b.get("blocker") or "needs floor attribution")
                        + "; see TIER-2 EVIDENCE"),
            "blocker_evidence": b.get("evidence"),
            "measurable_as": b.get("measurable_as"),
        })
    for r in rows:
        r.setdefault("suppressed", False)
    return rows


def direction_trustworthy(evidence: dict) -> dict[tuple, bool]:
    """{(cam, era): bool} — may an up/down split be emitted for this camera?

    Only when the reader could actually NAME both arrows. ch27's templates contain only 'down' and
    ch30's only 'up' (verified 2026-08-04 from the built .npz), so their direction field is the one
    value their classifier can return, not a reading. `arrow_unqualified` counts rows the engine
    itself suppressed; for older rows written before n_arrow_labels existed, a distribution that is
    entirely or almost entirely one direction is the same evidence after the fact."""
    out = {}
    for key, d in evidence.items():
        if not d.get("confident"):
            out[key] = False
            continue
        if d.get("arrow_unqualified"):
            out[key] = False            # the engine said so at source
            continue
        up, dn = d["arrow"].get("up", 0), d["arrow"].get("down", 0)
        both = up > 0 and dn > 0
        lopsided = (up + dn) > 0 and max(up, dn) > 0.95 * (up + dn)
        out[key] = bool(both and not lopsided)
    return out


def cycle_attribution(cycles: list[dict], transits: list[dict], cams: list,
                      t0: float, t1: float) -> dict[str, dict]:
    """Per camera: can a crossing be attributed to a floor at all?

    This is the evidence table behind "per-floor demand is not viable yet". It deliberately reports
    the funnel rather than a single rate, because the two candidate blockers are distinguishable and
    people keep reaching for the wrong one:

        transits                    every crossing counted in the range
        in_any_cycle                fell inside a DETECTED door cycle
        in_cycle_with_floor         ...and that cycle carried a confident floor  <- the real ceiling
        cycle_time_pct              how much of the range the doors were observably open

    If in_any_cycle is low, the blocker is CYCLE DETECTION — the door engine never saw the opening
    the passenger walked through. If in_any_cycle is high but in_cycle_with_floor is low, the
    blocker is floor OCR. Measured 2026-08-04 it is emphatically the former: cycles cover
    1.3-10.2% of wall time, so most crossings happen during openings that were never detected.
    """
    C: dict[str, list] = {}
    T: dict[str, list] = {}
    for c in cycles:
        if t0 <= c["ts"] < t1 and not eras.in_gap(c["cam"], c["ts"]):
            C.setdefault(c["cam"], []).append(c)
    for t in transits:
        if t0 <= t["ts"] < t1 and not eras.in_gap(t["cam"], t["ts"]):
            T.setdefault(t["cam"], []).append(t)

    out: dict[str, dict] = {}
    for cam in sorted(set(cams) | set(C) | set(T)):
        cs = sorted(C.get(cam, []), key=lambda c: c["ts"])
        ts = sorted(T.get(cam, []), key=lambda t: t["ts"])
        cover = sum(max(0.0, (c.get("close_ts") or c["ts"]) - c["ts"]) for c in cs)
        in_any = in_floor = 0
        i = 0
        for t in ts:
            while i < len(cs) and (cs[i].get("close_ts") or cs[i]["ts"]) < t["ts"]:
                i += 1
            j = i
            while j < len(cs) and cs[j]["ts"] <= t["ts"]:
                if (cs[j].get("close_ts") or cs[j]["ts"]) >= t["ts"]:
                    in_any += 1
                    if cs[j].get("floor") is not None:
                        in_floor += 1
                    break
                j += 1
        span = max(1.0, t1 - t0 - eras.gap_overlap_s(cam, t0, t1))
        n_t = len(ts)
        out[cam] = {
            "cycles": len(cs), "transits": n_t,
            "in_any_cycle": in_any, "in_cycle_with_floor": in_floor,
            "pct_in_any": (100.0 * in_any / n_t) if n_t else None,
            "pct_with_floor": (100.0 * in_floor / n_t) if n_t else None,
            "cycle_time_s": round(cover), "cycle_time_pct": 100.0 * cover / span,
            "blocker": ("no door calibration — zero cycles" if not cs else
                        "floor OCR" if (in_any and in_floor / max(1, in_any) < 0.5) else
                        "cycle detection"),
        }
    return out


# Idle gap that ends a cabin-active period. Occupancy is only meaningful WITHIN a run of activity;
# carrying a running total across a quiet night accumulates every counting error in between.
OCCUPANCY_IDLE_GAP_S = 600.0


def occupancy_periods(transits: list[dict], stamps: dict, validation: dict,
                      t0: float, t1: float, idle_gap_s: float | None = None) -> dict[tuple, list]:
    """ESTIMATED occupancy per cabin-active period. DERIVED FROM CROSSINGS — NOT A MEASUREMENT.

    The system counts door crossings, not people in a car. Cumulative boarded-minus-alighted is
    therefore an ESTIMATE whose error compounds with every cycle: at a per-camera precision of
    83-95%, roughly 1 crossing in 6 to 1 in 20 is wrong, and those errors accumulate in one
    direction as often as not. A running total across a whole day is meaningless.

    So it is bounded two ways:
      * RESET TO ZERO at every idle gap (default 10 min with no transit). A period is one run of
        cabin activity, and the estimate never carries across one.
      * Each period reports the camera's precision, the number of crossings accumulated, and an
        implied error bound, so the figure is never quotable without its uncertainty.

    NEGATIVE OCCUPANCY IS A BROKEN DERIVATION, NOT A SMALL NUMBER. More people left the car than
    entered it, which cannot happen — it means missed boardings, double-counted alightings, or a
    period boundary in the wrong place. The display clamps at zero, but `went_negative` and
    `min_raw` are recorded so the clamp can never hide it.

    -> {(cam, counting_version): [period, ...]}
    """
    gap = OCCUPANCY_IDLE_GAP_S if idle_gap_s is None else float(idle_gap_s)
    by: dict[tuple, list] = {}
    seq: dict[str, list] = {}
    for t in transits:
        if not (t0 <= t["ts"] < t1) or eras.in_gap(t["cam"], t["ts"]):
            continue
        seq.setdefault(t["cam"], []).append(t)
    for cam, ts_list in seq.items():
        ts_list.sort(key=lambda t: t["ts"])
        v = validation.get(cam) or {}
        prec = v.get("precision_pct")
        cur = None
        for t in ts_list:
            ver = eras.counting_version_at(cam, t["ts"], stamps)
            new_period = (cur is None or t["ts"] - cur["last_ts"] > gap
                          or ver != cur["counting_version"])   # era hygiene ends a period too
            if new_period:
                if cur:
                    by.setdefault((cam, cur["counting_version"]), []).append(cur)
                cur = {"cam": cam, "counting_version": ver, "start_ts": t["ts"], "last_ts": t["ts"],
                       "boarded": 0, "alighted": 0, "n_crossings": 0,
                       "raw": 0, "peak_raw": 0, "min_raw": 0, "went_negative": False,
                       "precision_pct": prec}
            cur["last_ts"] = t["ts"]
            cur["n_crossings"] += 1
            if t["direction"] == "in":
                cur["boarded"] += 1; cur["raw"] += 1
            else:
                cur["alighted"] += 1; cur["raw"] -= 1
            cur["peak_raw"] = max(cur["peak_raw"], cur["raw"])
            cur["min_raw"] = min(cur["min_raw"], cur["raw"])
            if cur["raw"] < 0:
                cur["went_negative"] = True
        if cur:
            by.setdefault((cam, cur["counting_version"]), []).append(cur)

    for periods in by.values():
        for p in periods:
            p["end_ts"] = p["last_ts"]
            p["duration_s"] = round(p["end_ts"] - p["start_ts"], 1)
            p["estimated_occupancy"] = max(0, p["raw"])       # clamped for DISPLAY only
            p["clamped"] = p["raw"] < 0
            # Implied error bound: each crossing carries (1 - precision) chance of being wrong, and
            # the estimate is a DIFFERENCE of two counts, so the errors do not cancel — they add.
            # Stated as a plain +/- on the accumulated crossings rather than a confidence interval,
            # because the per-crossing errors are not independent enough to justify one.
            if p["precision_pct"] is not None:
                err = (1.0 - p["precision_pct"] / 100.0) * p["n_crossings"]
                p["error_bound"] = round(err, 1)
                p["error_note"] = (f"+/-{err:.0f} people at {p['precision_pct']:.0f}% precision "
                                   f"over {p['n_crossings']} crossings")
            else:
                p["error_bound"] = None
                p["error_note"] = "unvalidated camera — no precision, so no error bound"
    return by


def measured_occupancy(episodes: list[dict], stamps: dict, t0: float, t1: float) -> dict[tuple, dict]:
    """PEAK CAR OCCUPANCY per (cam, counting_version) — MEASURED, not derived.

    A DIFFERENT INSTRUMENT from occupancy_periods() above, and the distinction is the whole point.
    That one accumulates boarded-minus-alighted across crossings and goes negative in most periods,
    which is why the workbook ships the finding and not the number. This one is a count of DISTINCT
    TRACKS SIMULTANEOUSLY INSIDE THE CABIN ZONE on a single frame — it never accumulates, so it
    cannot drift, and it cannot go negative. The two must never be added, averaged or shown as
    versions of one figure.

    IT IS STILL A FLOOR. Detection misses people, bodies occlude each other, and the foot anchor
    drops anyone whose feet leave the polygon. eras.OCC_CALIBRATION states the size of that gap and
    the single scene it was measured on. Every field below is "at least this many".

    THE EVIDENCE RULE: an episode counts only where occupancy_frames > 0. NULL means a worker that
    predates the feature; 0 means no analysed frames stood behind the peak. Both are counted in
    n_no_evidence and excluded from the statistics — never treated as an empty car.

    ERA SCOPING is by counting_version, because the cabin polygon and the membership anchor live in
    the counting logic. Peaks from two versions describe two different zones and are keyed apart.
    """
    by: dict[tuple, dict] = {}
    for e in episodes:
        if not (t0 <= e["ts"] < t1) or eras.in_gap(e["cam"], e["ts"]):
            continue
        ver = e.get("counting_version") or eras.counting_version_at(e["cam"], e["ts"], stamps)
        k = (e["cam"], ver)
        b = by.setdefault(k, {"cam": e["cam"], "counting_version": ver, "peaks": [], "hand": [],
                              "n_episodes": 0, "n_no_evidence": 0, "n_degraded": 0,
                              "frames": 0, "first_ts": None, "last_ts": None})
        b["n_episodes"] += 1
        b["first_ts"] = e["ts"] if b["first_ts"] is None else min(b["first_ts"], e["ts"])
        b["last_ts"] = e["ts"] if b["last_ts"] is None else max(b["last_ts"], e["ts"])
        if e.get("human_occupancy") is not None:
            # LEG 2: a hand count taken beside the machine's peak for the same episode. Kept
            # whether or not the machine had coverage — the human saw the car either way.
            b["hand"].append({"ts": e["ts"], "human": int(e["human_occupancy"]),
                              "machine": e.get("occupancy_max")})
        if not (e.get("occupancy_frames") or 0) > 0:
            b["n_no_evidence"] += 1
            continue
        b["peaks"].append(int(e.get("occupancy_max") or 0))
        b["frames"] += int(e["occupancy_frames"])
        if e.get("occupancy_degraded"):
            b["n_degraded"] += 1
    for b in by.values():
        pk = sorted(b["peaks"])
        b["n"] = len(pk)
        b["peak"] = pk[-1] if pk else None
        b["p95"] = stats.pctl(pk, 0.95) if pk else None
        b["median"] = stats.pctl(pk, 0.5) if pk else None
        b["mean"] = round(sum(pk) / len(pk), 2) if pk else None
        # Hand counts, paired with the machine peak for the SAME episode. The ratio is the only
        # honest calibration this system can build, and it is reported per pair rather than as a
        # single factor until there are enough pairs to justify one.
        pairs = [h for h in b["hand"] if h["machine"] is not None]
        b["n_hand"] = len(b["hand"])
        b["n_hand_pairs"] = len(pairs)
        b["hand_pairs"] = pairs
        b["hand_ratio"] = (round(sum(p["machine"] for p in pairs) / sum(p["human"] for p in pairs), 3)
                           if pairs and sum(p["human"] for p in pairs) else None)
    return by


def occupancy_summary(periods_by_key: dict) -> dict:
    """Fleet-level honesty check: how often did the derivation break?"""
    tot = neg = 0
    for periods in periods_by_key.values():
        for p in periods:
            tot += 1
            neg += 1 if p["went_negative"] else 0
    return {"n_periods": tot, "n_went_negative": neg,
            "pct_negative": (100.0 * neg / tot) if tot else None}


def per_floor_demand(cycles: list[dict], transits: list[dict], stamps: dict,
                     evidence: dict, t0: float, t1: float) -> dict:
    """Per-floor stops / boardings / alightings, by joining transits to the stop they happened in.

    THE JOIN RULE, stated because every number below depends on it:
      A transit at time T is attributed to the door cycle whose [open_ts, close_ts] contains T, on
      the SAME camera. The floor is that cycle's confident floor read. A transit matching no cycle,
      or matching a cycle with no floor, is UNATTRIBUTED and counted as such — never silently
      dropped and never assigned to a neighbouring floor.

    FAILURE MODES, all of which are reported rather than hidden:
      * no floor on the cycle — the doors opened, the panel was not read. Dominant on ch27/ch30.
      * transit outside every cycle — the counter saw a crossing while the doors were, as far as
        the door engine knows, shut. Clock skew between the two producers lands here: transit_event
        comes from the GPU counting worker and gw_door_event from the door engine, two processes
        with independent clocks, so a boundary-adjacent crossing can fall outside its own stop.
      * a cycle with no transits is a real stop with nobody crossing — counted as a stop, zero
        riders. That is NOT the same as an unattributed transit and is kept distinct.
      * DIRECTION is emitted only where the reader can name both arrows (see
        direction_trustworthy). Where it cannot, up/down stay None rather than inheriting the one
        value the classifier is capable of producing.

    Era hygiene: keyed by (cam, counting_version, era). Nothing is pooled across either.
    """
    dirs_ok = direction_trustworthy(evidence)
    by_cam_cycles: dict[str, list] = {}
    for c in cycles:
        if not (t0 <= c["ts"] < t1) or eras.in_gap(c["cam"], c["ts"]):
            continue
        by_cam_cycles.setdefault(c["cam"], []).append(c)
    for v in by_cam_cycles.values():
        v.sort(key=lambda c: c["ts"])

    by_cam_transits: dict[str, list] = {}
    for t in transits:
        if not (t0 <= t["ts"] < t1) or eras.in_gap(t["cam"], t["ts"]):
            continue
        by_cam_transits.setdefault(t["cam"], []).append(t)
    for v in by_cam_transits.values():
        v.sort(key=lambda t: t["ts"])

    out: dict[tuple, dict] = {}
    for cam, cyc in by_cam_cycles.items():
        tr = by_cam_transits.get(cam, [])
        ti = 0
        for c in cyc:
            ver = eras.counting_version_at(cam, c["ts"], stamps)
            era = c.get("era_id") or "unversioned"
            key = (cam, ver, era)
            d = out.setdefault(key, {"floors": {}, "n_stops": 0, "n_stops_no_floor": 0,
                                     "n_transits_attributed": 0, "n_transits_total": 0,
                                     "direction_ok": dirs_ok.get((cam, era), False)})
            d["n_stops"] += 1
            close = c.get("close_ts") or (c["ts"] + eras.DOOR_OPEN_MAX_S
                                          if hasattr(eras, "DOOR_OPEN_MAX_S") else c["ts"] + 60.0)
            # both lists ascending -> one forward pass, not a rescan per stop
            while ti < len(tr) and tr[ti]["ts"] < c["ts"]:
                ti += 1
            j, matched = ti, []
            while j < len(tr) and tr[j]["ts"] <= close:
                matched.append(tr[j]); j += 1
            floor = c.get("floor")
            if floor is None:
                d["n_stops_no_floor"] += 1
                continue                       # its transits stay unattributed, by construction
            f = d["floors"].setdefault(str(floor), {
                "stops": 0, "boarded": 0, "alighted": 0, "up_stops": None, "down_stops": None})
            f["stops"] += 1
            if d["direction_ok"]:
                if f["up_stops"] is None:
                    f["up_stops"] = f["down_stops"] = 0
                cd = c.get("direction")
                if cd == "up":
                    f["up_stops"] += 1
                elif cd == "down":
                    f["down_stops"] += 1
            for t in matched:
                f["boarded" if t["direction"] == "in" else "alighted"] += 1
                d["n_transits_attributed"] += 1
    for (cam, _v, _e), d in out.items():
        d["n_transits_total"] = len(by_cam_transits.get(cam, []))
        d["attribution_pct"] = (100.0 * d["n_transits_attributed"] / d["n_transits_total"]
                                if d["n_transits_total"] else None)
    return out


def coefficient_blockers(tier2_evidence: dict, speed: dict, cams: list) -> dict[str, dict]:
    """Per-coefficient blocker for C17/C18/C21/C22, DERIVED from the data.

    These four were previously hardcoded — model.py emitted "not measurable — needs floor
    attribution" and narrative.py set BLOCKED, both regardless of the data. The verdict was right
    by accident: floor attribution stopped being the blocker once single_panel reads were counted
    (ch27/ch29/ch30 hold ~150k confident reads), but the coefficients are still not reportable, for
    reasons the workbook was not stating. Each blocker below names what actually stops it.

    Returns {cid: {status, blocker, evidence, measurable_as}}."""
    live = {k: v for k, v in tier2_evidence.items() if v["rows"]}
    # cameras whose CURRENT era can attribute floors at all
    seeing = sorted({cam for (cam, _era), v in live.items() if v["confident"] > 0})
    blind = sorted({cam for cam in cams
                    if not any(v["confident"] for (c, _e), v in live.items() if c == cam)})

    # ── C21/C22: floors-per-second IS measured; the speed FACTOR is not derivable ──
    n_up = sum(len(v["up"]) for v in speed.values())
    n_dn = sum(len(v["down"]) for v in speed.values())
    per_cam = ", ".join(
        f"{cam} up n={len(v['up'])}/down n={len(v['down'])}"
        for (cam, _era), v in sorted(speed.items()) if v["up"] or v["down"])
    speed_blocker = {
        "status": "BLOCKED",
        "blocker": "rated speed and floor-to-floor height are not held in this database",
        "evidence": (f"floors-per-second IS measured: {n_up:,} up segments and {n_dn:,} down "
                     f"segments from consecutive confident reads ({per_cam or 'none'}). "
                     f"Floor attribution is NOT the blocker. Converting floors/s into 'share of "
                     f"rated speed' needs the inter-floor distance and the car's rated speed, "
                     f"neither of which is in this database — the same class of gap as C19's "
                     f"rated capacity."),
        "measurable_as": "floors per second (reported on TIER-2 EVIDENCE), not a speed factor",
    }

    # ── C17/C18: floor attribution is not the blocker; three others are ──
    arrow_lines, degenerate = [], []
    for (cam, era), v in sorted(live.items()):
        if not v["confident"]:
            continue
        tot = sum(v["arrow"].values()) or 1
        share = {k: 100.0 * n / tot for k, n in v["arrow"].items()}
        arrow_lines.append(f"{cam} " + "/".join(f"{k} {share[k]:.0f}%" for k in sorted(share)))
        directional = {k: s for k, s in share.items() if k in ("up", "down")}
        # A lift that only ever shows one arrow is physically impossible -> the arrow ROI or the
        # reader is miscalibrated, and any up/down SPLIT built on it is not trustworthy.
        if directional and (len(directional) == 1 or max(directional.values()) > 95.0):
            degenerate.append(cam)
    stops_blocker = {
        "status": "BLOCKED",
        "blocker": ("arrow direction is degenerate on some cameras, and trip segmentation is not "
                    "implemented"),
        "evidence": ("Floor attribution is NOT the blocker — "
                     + (f"{', '.join(seeing)} attribute floors. " if seeing else "no camera does. ")
                     + "Arrow distribution over confident reads: "
                     + ("; ".join(arrow_lines) if arrow_lines else "none") + ". "
                     + (f"Physically impossible one-way arrow on {', '.join(sorted(set(degenerate)))} "
                        f"— a lift does not travel in only one direction, so the ROI or the reader "
                        f"is miscalibrated and any up/down split built on it is unsound. "
                        if degenerate else "")
                     + "Trip segmentation (grouping stops into directional runs) is not implemented "
                       "in this report, and a large share of door cycles carry no attributable "
                       "floor, so stops-per-trip would undercount."),
        "measurable_as": None,
    }
    out = {"C21": dict(speed_blocker), "C22": dict(speed_blocker),
           "C17": dict(stops_blocker), "C18": dict(stops_blocker)}
    for cid in out:
        out[cid]["floor_blind_cams"] = blind
        out[cid]["attributing_cams"] = seeing
    return out


def floor_speed_segments(reads: list[dict]) -> dict[tuple, dict]:
    """{(cam, era): {up: [floors/s], down: [...], skipped_unmappable, skipped_implausible}}.

    A segment is the transition between two CONSECUTIVE confident reads that changed floor:
    |Δindex| / Δt. Direction comes from the floor INDEX change, not the arrow glyph — which matters,
    because the arrow is degenerate on some cameras while the index is not.

    This is what C21/C22 would be built on. It is floors per SECOND, NOT a speed factor: converting
    to "share of rated speed" needs floor-to-floor height and the car's rated speed, neither of
    which this database holds. See coefficient_blockers()."""
    out: dict[tuple, dict] = {}
    prev_key = prev = None
    for r in reads:
        key = (r["cam"], r["era"])
        d = out.setdefault(key, {"up": [], "down": [], "skipped_unmappable": 0,
                                 "skipped_implausible": 0})
        if key != prev_key:
            prev_key, prev = key, None
        i1 = eras.floor_index(r["floor"])
        if i1 is None:
            d["skipped_unmappable"] += 1
            prev = None            # cannot bridge a segment across an unorderable floor
            continue
        if prev is not None:
            i0, dt = prev[0], r["ts"] - prev[1]
            if dt > 0 and i1 != i0:
                fps = abs(i1 - i0) / dt
                if fps > eras.MAX_FLOORS_PER_S:
                    d["skipped_implausible"] += 1
                else:
                    (d["up"] if i1 > i0 else d["down"]).append(fps)
        prev = (i1, r["ts"])
    return out


# ── demand by lift and hour ──────────────────────────────────────────────────
# Floor attribution is blocked (no confident reads), so per-floor origin/
# destination demand is unavailable. Per-lift, per-hour demand is NOT blocked.
#
# The unit is (cam, counting_version, hour-of-day). counting_version is part of
# the key because counts made by different counting builds are different
# measurements — pooling them to get a tidier matrix would be the same mistake
# the instrument split guards against.

def demand_by_lift_hour(transits: list[dict], pi_cycles: list[dict],
                        cov_buckets: dict, bucket_s: int, stamps: dict,
                        cams: list[str], t0: float, t1: float) -> dict:
    """Mean boardings/alightings per hour-of-day, per lift, per counting era.

    A cell is the mean over the DAYS THAT LIFT HAD DATA IN THAT HOUR, so a lift
    that was dark does not drag its own average down. A cell with no observed
    days is None — rendered '—', never 0. A dark lift is not an idle lift, and
    a zero there would be read as 'this lift carried nobody'.

    Returns {'versions': [...], 'hours': [0..23], 'by_era': {version: {...}}}.
    """
    hours = list(range(24))
    day0 = datetime.fromtimestamp(t0, eras.IST).replace(
        hour=0, minute=0, second=0, microsecond=0)
    days = []
    d = day0
    while d.timestamp() < t1:
        days.append(d)
        d += timedelta(days=1)

    # (cam, version, day, hour) -> counts, and which of those windows were live
    counts: dict[tuple, dict] = {}
    observed: set[tuple] = set()
    versions: set[str] = set()

    for cam in cams:
        buckets = cov_buckets.get(cam) or set()
        for day in days:
            for h in hours:
                w0 = (day + timedelta(hours=h)).timestamp()
                w1 = w0 + 3600.0
                if w1 <= t0 or w0 >= t1:
                    continue
                lo, hi = max(w0, t0), min(w1, t1)
                # live = at least one coverage bucket with data, and the window
                # is not wholly inside a declared outage
                b0 = int((lo - t0) // bucket_s)
                b1 = int((hi - t0 - 1e-9) // bucket_s)
                has_rows = any(b in buckets for b in range(b0, b1 + 1))
                fully_gapped = eras.gap_overlap_s(cam, lo, hi) >= (hi - lo) - 1.0
                ver = eras.counting_version_at(cam, (lo + hi) / 2.0, stamps)
                versions.add(ver)
                if has_rows and not fully_gapped:
                    observed.add((cam, ver, day.date().isoformat(), h))

    def _bump(cam, ts, boarded, alighted):
        if eras.in_gap(cam, ts):
            return
        dt = datetime.fromtimestamp(ts, eras.IST)
        ver = eras.counting_version_at(cam, ts, stamps)
        key = (cam, ver, dt.date().isoformat(), dt.hour)
        c = counts.setdefault(key, {"boarded": 0, "alighted": 0})
        c["boarded"] += boarded
        c["alighted"] += alighted

    for t in transits:
        if not (t0 <= t["ts"] < t1):
            continue
        _bump(t["cam"], t["ts"], 1 if t["direction"] == "in" else 0,
              1 if t["direction"] != "in" else 0)
    for c in pi_cycles:
        if not (t0 <= c["ts"] < t1):
            continue
        _bump(c["cam"], c["ts"], int(c.get("boarded") or 0),
              int(c.get("alighted") or 0))

    by_era: dict[str, dict] = {}
    for ver in sorted(versions):
        boarded: dict[str, list] = {}
        alighted: dict[str, list] = {}
        obs_days: dict[str, list] = {}
        total_b: dict[str, list] = {}
        total_a: dict[str, list] = {}
        for cam in cams:
            b_row, a_row, d_row, tb_row, ta_row = [], [], [], [], []
            for h in hours:
                day_keys = [(cam, ver, day.date().isoformat(), h) for day in days]
                live = [k for k in day_keys if k in observed]
                d_row.append(len(live))
                if not live:
                    b_row.append(None)
                    a_row.append(None)
                    tb_row.append(None)
                    ta_row.append(None)
                    continue
                b = sum(counts.get(k, {}).get("boarded", 0) for k in live)
                a = sum(counts.get(k, {}).get("alighted", 0) for k in live)
                # Totals are the raw observed counts; means divide by the days
                # actually observed, which is what makes them comparable across
                # lifts with different coverage.
                tb_row.append(b)
                ta_row.append(a)
                b_row.append(b / len(live))
                a_row.append(a / len(live))
            boarded[cam] = b_row
            alighted[cam] = a_row
            obs_days[cam] = d_row
            total_b[cam] = tb_row
            total_a[cam] = ta_row

        # Fleet: all 7 lifts serve ONE tower, so a fleet demand aggregate is a
        # statement about one population and is meaningful. It is an UNWEIGHTED
        # SUM of the per-lift hourly means, labelled as such, over the lifts
        # that were actually observed in that hour.
        fleet_b, fleet_a, fleet_n = [], [], []
        for h in hours:
            live = [c for c in cams if boarded[c][h] is not None]
            fleet_n.append(len(live))
            fleet_b.append(sum(boarded[c][h] for c in live) if live else None)
            fleet_a.append(sum(alighted[c][h] for c in live) if live else None)

        fleet_tb, fleet_ta = [], []
        for h in hours:
            live = [c for c in cams if total_b[c][h] is not None]
            fleet_tb.append(sum(total_b[c][h] for c in live) if live else None)
            fleet_ta.append(sum(total_a[c][h] for c in live) if live else None)

        by_era[ver] = {
            "boarded": boarded, "alighted": alighted, "observed_days": obs_days,
            "total_boarded_hr": total_b, "total_alighted_hr": total_a,
            "fleet_total_boarded_hr": fleet_tb,
            "fleet_total_alighted_hr": fleet_ta,
            "fleet_boarded": fleet_b, "fleet_alighted": fleet_a,
            "fleet_lifts_contributing": fleet_n,
            "peaks": _demand_peaks(cams, boarded, fleet_b),
            "cv": _load_balance_cv(cams, boarded),
            "n_transits": sum(
                v["boarded"] + v["alighted"]
                for k, v in counts.items() if k[1] == ver),
            # raw totals — the reconciliation handle against PEAK ANALYSIS
            "total_boarded": sum(v["boarded"] for k, v in counts.items()
                                 if k[1] == ver),
            "total_alighted": sum(v["alighted"] for k, v in counts.items()
                                  if k[1] == ver),
        }
    return {"hours": hours, "versions": sorted(versions), "by_era": by_era,
            "n_days_in_range": len(days)}


# ── canonical figures ────────────────────────────────────────────────────────
# A figure stated on more than one sheet must come from ONE computation. The
# v3 workbook stated three different fleet busiest-hours — a raw pooled sum on
# FLEET, a per-era mean on READ THIS FIRST, and a different era's per-era mean
# on DEMAND — because three places each computed their own. A reader met the
# document contradicting itself on its most quotable number.
#
# canonical_figures() is now the single source. Anything rendering a canonical
# figure reads it from here. A sheet that genuinely needs a DIFFERENT
# definition must say so and say how it differs; `definition` is the sentence
# it has to print.

def canonical_figures(demand: dict, peaks: list, aggs: dict,
                      coverage_pct: dict) -> dict:
    """{name: {value, definition, ...}} — every figure quoted on more than one
    sheet, computed once."""
    out: dict = {}

    by_era = (demand or {}).get("by_era") or {}
    if by_era:
        # The counting era carrying the most boardings speaks for the building:
        # counts from different counting builds are different measurements, so
        # a fleet figure has to belong to ONE of them rather than pool them.
        ver = max(by_era, key=lambda v: by_era[v]["total_boarded"])
        e = by_era[ver]
        pk = e["peaks"]["__fleet__"]
        out["fleet_busiest_hour"] = {
            "era": ver,
            "hour": pk["hour"],
            "value": pk["value"],
            "all_day_mean": pk["mean"],
            "ratio": pk["ratio"],
            "definition": (
                "mean boardings per observed hour-of-day, summed across lifts, "
                f"within counting era {ver}; hours in which a lift was not "
                "observed are excluded rather than counted as zero"),
        }

    out["total_boardings"] = {
        "value": sum(e["total_boarded"] for e in by_era.values()),
        "definition": ("every boarding in range, outages excluded, summed "
                       "across all counting eras"),
    }
    out["n_clean_closes"] = {
        "value": sum(a["close"]["n"] for a in (aggs or {}).values()),
        "definition": ("door closes surviving flap/reopen/implausible "
                       "classification and the one-frame floor, summed across "
                       "lift-eras"),
    }
    live = {c: v for c, v in (coverage_pct or {}).items() if v > 0}
    out["coverage_range"] = {
        "lo": min(live.values()) if live else 0.0,
        "hi": max(live.values()) if live else 0.0,
        "definition": ("share of 15-minute blocks holding at least one row, "
                       "over non-outage time, per channel"),
    }
    return out





def peak_5min_by_cam(transits, pi_cycles, cams, t0, t1) -> dict:
    """Busiest 5 minutes per lift over the whole range, and for the fleet.

    Same sliding window and same gap-excluded boarding stream as
    peak_by_day(), so the two sheets cannot disagree about what a boarding is.
    {cam: {'boardings', 'w0'}} plus '__fleet__'."""
    ev = _boarding_events(transits, pi_cycles)
    out = {}
    for cam in list(cams) + ["__fleet__"]:
        sub = sorted((ts, n) for ts, n, c in ev
                     if (cam == "__fleet__" or c == cam) and t0 <= ts < t1)
        best = {"boardings": 0, "w0": None}
        if sub:
            j = 0
            run = 0
            for i, (ts, n) in enumerate(sub):
                run += n
                while sub[j][0] <= ts - 300.0:
                    run -= sub[j][1]
                    j += 1
                if run > best["boardings"]:
                    best = {"boardings": run, "w0": sub[j][0]}
        out[cam] = best
    return out


def demand_totals(demand: dict) -> dict:
    """{version: total boardings} — the figure that must reconcile against the
    day-boardings column on PEAK ANALYSIS, since both come from the same
    gap-excluded boarding stream."""
    return {ver: e["total_boarded"] for ver, e in demand["by_era"].items()}


def _demand_peaks(cams, boarded, fleet_b) -> dict:
    """Busiest hour, its value, the all-day mean hourly, and peak:mean — per
    lift and for the fleet. Means are over OBSERVED hours only."""
    out = {}

    def _one(vals):
        live = [(h, v) for h, v in enumerate(vals) if v is not None]
        if not live or all(v == 0 for _h, v in live):
            return {"hour": None, "value": None, "mean": None, "ratio": None,
                    "n_hours": len(live)}
        h_peak, v_peak = max(live, key=lambda hv: hv[1])
        mean = sum(v for _h, v in live) / len(live)
        return {"hour": h_peak, "value": v_peak, "mean": mean,
                "ratio": (v_peak / mean) if mean else None,
                "n_hours": len(live)}

    for cam in cams:
        out[cam] = _one(boarded[cam])
    out["__fleet__"] = _one(fleet_b)
    return out


# A spread needs something to spread across. With two lifts the coefficient of
# variation is degenerate — one lift at zero and any other value gives sqrt(2)
# = 1.414 regardless of the numbers, which reads as a dramatic imbalance and
# means nothing. Three observed lifts is the floor for saying anything.
LOAD_BALANCE_MIN_LIFTS = 3


def _load_balance_cv(cams, boarded) -> list:
    """Coefficient of variation of boardings ACROSS lifts, per hour.

    0 means every observed lift carried the same load that hour; higher means
    the load sat on some lifts more than others. None below
    LOAD_BALANCE_MIN_LIFTS observed lifts — see the constant."""
    out = []
    for h in range(24):
        vals = [boarded[c][h] for c in cams if boarded[c][h] is not None]
        if len(vals) < LOAD_BALANCE_MIN_LIFTS:
            out.append(None)
            continue
        m = sum(vals) / len(vals)
        if m <= 0:
            out.append(None)
            continue
        sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        out.append(sd / m)
    return out


def load_balance_reportable(cv: list) -> bool:
    """Whether the per-hour load-balance table is worth printing at all.

    If most hours could not be computed, the table is mostly blanks and the few
    rows that survive are not representative of the day — one line saying so is
    more honest than 24 rows of mostly nothing."""
    usable = sum(1 for c in cv if c is not None)
    return usable > len(cv) / 2


# ── suspected (undeclared) gap detection ─────────────────────────────────────

SUSPECT_WINDOW_S = 6 * 3600          # the shortest silence worth flagging


def detect_suspected_gaps(all_cycles: list[dict], transits: list[dict],
                          cams: list[str], t0: float, t1: float) -> list[dict]:
    """Windows a channel went silent while it was demonstrably alive on both
    sides — i.e. candidate gaps that nobody has DECLARED.

    Two detectors, both requiring rows before AND after the silence so a
    channel's first/last day is never mistaken for an outage:
      * whole IST days with zero rows on a channel that has rows on other days;
      * any silence of >= SUSPECT_WINDOW_S between consecutive rows.

    FLAGS ONLY. Nothing here is excluded from any statistic — declaring a gap
    stays a human decision, made by appending to eras.DATA_GAPS."""
    rows_by_cam: dict[str, list[float]] = {c: [] for c in cams}
    for r in list(all_cycles) + list(transits):
        if r["cam"] in rows_by_cam and t0 <= r["ts"] < t1:
            rows_by_cam[r["cam"]].append(r["ts"])

    day0 = datetime.fromtimestamp(t0, eras.IST).replace(
        hour=0, minute=0, second=0, microsecond=0)
    days = []
    d = day0
    while d.timestamp() < t1:
        days.append((d.date().isoformat(), max(d.timestamp(), t0),
                     min((d + timedelta(days=1)).timestamp(), t1)))
        d += timedelta(days=1)

    out = []
    for cam in cams:
        ts_list = sorted(rows_by_cam[cam])
        if not ts_list:
            continue                      # zero rows everywhere: already on COVERAGE
        first, last = ts_list[0], ts_list[-1]
        # (a) silent whole days, bracketed by live days
        silent_days = []
        for label, d0, d1 in days:
            if d1 <= first or d0 >= last:
                continue                  # outside the channel's own live span
            if any(d0 <= ts < d1 for ts in ts_list):
                continue
            silent_days.append((d0, d1))
            if _fully_declared(cam, d0, d1):
                continue
            out.append(_suspect(cam, d0, d1, "silent day",
                                f"{label}: zero rows, but this channel has "
                                f"rows before and after"))
        # (b) long silences inside the live span, not already told as a day
        for a, b in zip(ts_list, ts_list[1:]):
            if b - a < SUSPECT_WINDOW_S:
                continue
            if _fully_declared(cam, a, b):
                continue
            covered = sum(max(0.0, min(b, d1) - max(a, d0))
                          for d0, d1 in silent_days)
            if covered >= 0.5 * (b - a):
                continue                  # already reported as silent day(s)
            out.append(_suspect(cam, a, b, "long silence",
                                f"{(b - a) / 3600.0:.1f} h with no rows, between "
                                f"rows on both sides"))
    out.sort(key=lambda g: (g["start"], g["cam"]))
    return out


def _suspect(cam: str, t0: float, t1: float, kind: str, detail: str) -> dict:
    """A flagged window, with the share already covered by DECLARED gaps so the
    reader can see how much of it is genuinely unexplained."""
    declared = eras.gap_overlap_s(cam, t0, t1)
    span = max(0.0, t1 - t0)
    return {"cam": cam, "start": t0, "end": t1, "kind": kind, "detail": detail,
            "span_s": span, "declared_s": declared,
            "undeclared_s": max(0.0, span - declared)}


def _fully_declared(cam: str, t0: float, t1: float) -> bool:
    """True when [t0,t1) is already inside declared gap windows for cam."""
    if t1 <= t0:
        return True
    return eras.gap_overlap_s(cam, t0, t1) >= (t1 - t0) - 1.0


# ── era boundary detection for a range ───────────────────────────────────────

def boundaries_crossed(t0: float, t1: float, aggs: dict[tuple, dict],
                       transit_aggs: dict[tuple, dict]) -> list[dict]:
    """Every declared or observed boundary inside [t0,t1), with row counts on
    each side (counted from the era-attributed aggregates)."""
    out = []
    if t0 < eras.INSTRUMENT_SPLIT_EPOCH <= t1:
        pi_n = sum(a["n_cycles"] for k, a in aggs.items() if k[1] == PI_WATCH)
        gpu_n = sum(a["n_cycles"] for k, a in aggs.items() if k[1] == GPU_ENGINE)
        out.append({"boundary": eras.INSTRUMENT_SPLIT,
                    "kind": "INSTRUMENT SPLIT — Pi door-watch retired, GPU "
                            "DoorFloorEngine live. Figures on the two sides "
                            "are NOT comparable and are never pooled.",
                    "rows_before": pi_n, "rows_after": gpu_n})
    if t0 < eras.CLOSE_TRAVEL_MAX_EPOCH <= t1:
        pre = sum(a["n_cycles"] for k, a in aggs.items()
                  if k[2] == "pi_watch/pre-ctmax30")
        post = sum(a["n_cycles"] for k, a in aggs.items()
                   if k[2] == "pi_watch/ctmax30")
        out.append({"boundary": eras.CLOSE_TRAVEL_MAX_BOUNDARY,
                    "kind": "CLOSE_TRAVEL_MAX 10→30 (pi_watch sub-era; longer "
                            "real closes admitted after this point)",
                    "rows_before": pre, "rows_after": post})
    for _cam, ts_iso, ver in eras.COUNTING_EPOCHS:
        ep = datetime.fromisoformat(ts_iso).timestamp()
        if t0 < ep <= t1:
            before = sum(d["n"] for (c, v), d in transit_aggs.items() if v != ver)
            after = sum(d["n"] for (c, v), d in transit_aggs.items() if v == ver)
            out.append({"boundary": ts_iso,
                        "kind": f"counting_version → {ver}",
                        "rows_before": before, "rows_after": after})
    ge = eras.guard_epoch()
    if ge and t0 < ge <= t1:
        out.append({"boundary": datetime.fromtimestamp(ge, eras.IST).isoformat(),
                    "kind": "DoorTracker time-guard deploy (DASH_DOOR_GUARD_TS) "
                            "— pre-guard GPU cycles excluded from quotable pool",
                    "rows_before": None, "rows_after": None})
    return out
