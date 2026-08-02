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


def aggregate_cycles(cycles: list[dict], t0: float, t1: float) -> dict[tuple, dict]:
    """{(cam, instrument, era_id): agg}. Gap-time cycles are excluded from
    rate denominators upstream; here they are excluded from duration pools
    entirely (a close inside a declared gap window is suspect data)."""
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
        if c.get("clean_close") and c.get("close_travel_s") is not None:
            p["closes"].append(float(c["close_travel_s"]))
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
    out = {}
    for key, p in pools.items():
        out[key] = _finalise(key, p, t0, t1)
    return out


def _empty_pool() -> dict:
    return {"n_cycles": 0, "closes": [], "dwells": [], "opens": [],
            "transfer_pp": [], "lost_time": [], "pax_per_trip": [],
            "boarded": 0, "alighted": 0, "n_counted_cycles": 0,
            "n_closes_excluded": 0, "n_in_gap_excluded": 0,
            "first_ts": None, "last_ts": None}


def _finalise(key, p, t0, t1) -> dict:
    cam, instrument, era_id = key
    closes = sorted(p["closes"])
    n = len(closes)
    spec = eras.DOOR_SPECS.get(cam)
    cliff = (spec or {}).get("compliance_s", eras.COMPLIANCE_CLIFF_S)
    over = sum(1 for v in closes if v > cliff)
    covered_s = max(0.0, (t1 - t0) - eras.gap_overlap_s(cam, t0, t1))
    return {
        "cam": cam, "instrument": instrument, "era_id": era_id,
        "first_ts": p["first_ts"], "last_ts": p["last_ts"],
        "n_cycles": p["n_cycles"],
        "n_in_gap_excluded": p["n_in_gap_excluded"],
        "n_closes_excluded": p["n_closes_excluded"],
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
        },
        "dwell": {
            "n": len(p["dwells"]),
            "median_ci": stats.median_ci(sorted(p["dwells"])),
            "p85": stats.pctl(sorted(p["dwells"]), 0.85),
            "hist": stats.hist(p["dwells"], stats.DWELL_HIST_EDGES),
        },
        "open_travel": {
            "n": len(p["opens"]),
            "median_ci": stats.median_ci(sorted(p["opens"])),
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
        rows.append({
            "coefficient": "C27 door close travel (s)", "era": tag,
            "assumption": sheet_close, "threshold": cliff,
            "observed": mc["median"], "n": mc["n"],
            "ci": (mc["lo"], mc["hi"]),
            "verdict": stats.verdict_vs_threshold(mc, cliff),
        })
        oc = a["open_travel"]["median_ci"]
        rows.append({
            "coefficient": "C27 door open travel (s)", "era": tag,
            "assumption": sheet_open, "threshold": sheet_open,
            "observed": oc["median"], "n": oc["n"],
            "ci": (oc["lo"], oc["hi"]),
            "verdict": stats.verdict_vs_threshold(oc, sheet_open),
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
    return rows


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
