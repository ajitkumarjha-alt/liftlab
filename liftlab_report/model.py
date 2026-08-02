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

def vs_sheet_rows(aggs: dict[tuple, dict], cam: str) -> list[dict]:
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
    for coeff, why in (
            ("C17 probable stops (up)", "needs trip segmentation from floor attribution"),
            ("C18 probable stops (down)", "needs trip segmentation from floor attribution"),
            ("C21 speed factor (up)", "needs floor attribution"),
            ("C22 speed factor (down)", "needs floor attribution")):
        rows.append({
            "coefficient": coeff, "era": "—", "assumption": "per sheet",
            "threshold": None, "observed": None, "n": 0, "ci": (None, None),
            "verdict": f"not measurable — {why}; see TIER-2 BLOCKED",
        })
    for r in rows:
        r.setdefault("suppressed", False)
    return rows


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
        for cam in cams:
            b_row, a_row, d_row = [], [], []
            for h in hours:
                day_keys = [(cam, ver, day.date().isoformat(), h) for day in days]
                live = [k for k in day_keys if k in observed]
                d_row.append(len(live))
                if not live:
                    b_row.append(None)
                    a_row.append(None)
                    continue
                b = sum(counts.get(k, {}).get("boarded", 0) for k in live)
                a = sum(counts.get(k, {}).get("alighted", 0) for k in live)
                b_row.append(b / len(live))
                a_row.append(a / len(live))
            boarded[cam] = b_row
            alighted[cam] = a_row
            obs_days[cam] = d_row

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

        by_era[ver] = {
            "boarded": boarded, "alighted": alighted, "observed_days": obs_days,
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


def _load_balance_cv(cams, boarded) -> list:
    """Coefficient of variation of boardings ACROSS lifts, per hour.

    0 means every observed lift carried the same load that hour; higher means
    the load sat on some lifts more than others. Computed only over lifts
    observed in that hour, and None below 2 such lifts — a spread across one
    lift is not a spread."""
    out = []
    for h in range(24):
        vals = [boarded[c][h] for c in cams if boarded[c][h] is not None]
        if len(vals) < 2:
            out.append(None)
            continue
        m = sum(vals) / len(vals)
        if m <= 0:
            out.append(None)
            continue
        sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
        out.append(sd / m)
    return out


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
