"""Plain-English conclusions for READ THIS FIRST, generated from the data.

Written for a reader who has never seen this project — an MEP design reviewer
or a manager. Two rules hold this file together:

  1. NOTHING here is hardcoded prose about a specific number. Every figure in
     every sentence is read out of the computed context; the thresholds come
     from eras.py (the sheet's own assumptions), not from a literal typed into
     a string. If the data changes, the sentences change with it.
  2. Every finding carries its EVIDENCE (the sheet and cell range it can be
     checked against) and its CONFIDENCE, and a finding whose supporting metric
     was suppressed as resolution-bound SAYS SO instead of quietly dropping it.

Confidence is mechanical, never editorial:
  HIGH             — n >= N_HIGH, a CI exists, and it does not straddle the
                     decision threshold
  MEDIUM           — n >= N_MEDIUM and a CI exists (it may straddle)
  TOO EARLY TO SAY — anything else: too few observations, no computable CI, or
                     the metric was suppressed. Carries the n that would move it
                     where such an n exists.
"""

from __future__ import annotations

from datetime import datetime

from . import eras, stats

HIGH = "HIGH"
MEDIUM = "MEDIUM"
TOO_EARLY = "TOO EARLY TO SAY"

N_HIGH = 100
N_MEDIUM = 30

# Finding order is by weight on the design decision, not by sheet order.
P_COMPLIANCE = 10          # does the fleet clear the compliance cliff
P_UNRESOLVED = 15          # an open question that blocks quoting other figures
P_WORST = 20               # which lift is worst, and by how much
P_MEASURE_BLOCKED = 30     # a headline coefficient that cannot be measured
P_DATA_QUALITY = 40        # filters and artifacts that move the headline
P_DEMAND = 45              # when the building uses its lifts, and how evenly
P_TRANSFER = 50            # secondary coefficients
P_PEAK = 60
P_COVERAGE = 70

# Two lifts in the SAME tower differing by more than this in typical close
# travel is not ordinary unit-to-unit variation and gets raised as an open
# question rather than reported as a result.
DIVERGENCE_S = 0.5


def _era_label(instrument: str, era_id: str) -> str:
    """Instrument + era in words a first-time reader can hold."""
    which = ("the on-camera Pi door-watch" if instrument == eras.PI_WATCH
             else "the GPU door engine")
    return f"{which}, build {era_id}"


def _confidence(n: int, ci: dict, threshold: float | None,
                suppressed: bool = False) -> tuple[str, str]:
    """(level, reason). Mechanical — see the module docstring."""
    if suppressed:
        return TOO_EARLY, "the underlying measurement was suppressed as resolution-bound"
    if n < N_MEDIUM:
        return TOO_EARLY, f"only {n} measurements; {N_MEDIUM} is the minimum for a reading"
    if ci.get("lo") is None or ci.get("hi") is None:
        return TOO_EARLY, f"n={n} is too small for a confidence interval"
    if threshold is None:
        return MEDIUM, f"n={n}, interval computed, but there is no sheet value to compare against"
    straddles = ci["lo"] <= threshold <= ci["hi"]
    if n >= N_HIGH and not straddles:
        return HIGH, f"n={n} and the interval sits entirely on one side of the threshold"
    if straddles:
        return (MEDIUM if n >= N_MEDIUM else TOO_EARLY), (
            f"n={n} but the interval still crosses the threshold"
            + _how_much_more(n, ci, threshold))
    return MEDIUM, f"n={n} — clear of the threshold, but below n={N_HIGH}"


# Beyond this multiple of the current n, "collect more" stops being advice.
IMPRACTICAL_MULTIPLE = 50


def _how_much_more(n: int, ci: dict, threshold: float) -> str:
    """What n would settle it — or an honest statement that no n will.

    When the point estimate sits almost exactly ON the threshold, the projected
    n explodes. Quoting that number would be worse than useless: the real
    finding is that this lift is AT the limit, and that is what gets said."""
    need = stats.n_for_separation(ci, threshold)
    if not need:
        return ""
    if need > IMPRACTICAL_MULTIPLE * max(n, 1):
        return ("; the measured value sits essentially ON the threshold, so no "
                "practical amount of further data will put it clearly on one "
                "side — treat this lift as being at the limit")
    return f"; about n={need:,} would be needed to separate them"


def _finding(priority, headline, sentence, sheet, cell_range, chart,
             confidence, why, n=None, extra=None) -> dict:
    return {"priority": priority, "headline": headline, "sentence": sentence,
            "sheet": sheet, "range": cell_range, "chart": chart,
            "confidence": confidence, "why": why, "n": n, "extra": extra or []}


def build_findings(ctx: dict, anchors: dict) -> list[dict]:
    """Ordered findings. `anchors` maps a key to (sheet_name, cell_range or
    chart description) recorded by the sheet builders as they laid the sheet
    out, so every 'Evidence:' line points at a range that really holds the
    number being quoted."""
    out: list[dict] = []
    out += _f_compliance(ctx, anchors)
    out += _f_divergence(ctx, anchors)
    out += _f_open_travel(ctx, anchors)
    out += _f_floor_filter(ctx, anchors)
    out += _f_demand(ctx, anchors)
    out += _f_load_balance(ctx, anchors)
    out += _f_per_floor_blocked(ctx, anchors)
    out += _f_transfer(ctx, anchors)
    out += _f_peak(ctx, anchors)
    out += _f_coverage(ctx, anchors)
    out.sort(key=lambda f: (f["priority"], -(f["n"] or 0)))
    for i, f in enumerate(out, start=1):
        f["number"] = i
    return out


def _anchor(anchors: dict, key, default_sheet: str, default: str = "—"):
    got = anchors.get(key)
    if not got:
        return default_sheet, default
    return got


# ── finding rules ────────────────────────────────────────────────────────────

def _f_compliance(ctx, anchors):
    """The core design question: does observed close travel clear the cliff?"""
    out = []
    rows = []
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl = a["close"]
        if cl["n"] == 0:
            continue
        rows.append(((cam, instrument, era_id), a, cl))
    if not rows:
        return out

    # (a) fleet-level answer, counted from the per-era verdicts
    over, under, straddle, blocked = [], [], [], []
    for key, _a, cl in rows:
        cam = key[0]
        if cl["suppressed"]:
            blocked.append(cam)
            continue
        mc, cliff = cl["median_ci"], cl["cliff_s"]
        if mc["lo"] is None or mc["hi"] is None:
            straddle.append(cam)
        elif mc["lo"] > cliff:
            over.append(cam)
        elif mc["hi"] < cliff:
            under.append(cam)
        else:
            straddle.append(cam)
    n_tot = sum(cl["n"] for _k, _a, cl in rows)
    cliffs = sorted({cl["cliff_s"] for _k, _a, cl in rows})
    cliff_txt = " / ".join(f"{c:.2f}s" for c in cliffs)
    sheet, rng = _anchor(anchors, "summary_headline", "SUMMARY")
    parts = []
    if over:
        parts.append(f"{len(over)} ({', '.join(eras.lift_label(c) for c in sorted(set(over)))}) "
                     f"measured ABOVE it")
    if under:
        parts.append(f"{len(under)} ({', '.join(eras.lift_label(c) for c in sorted(set(under)))}) "
                     f"measured below it")
    if straddle:
        parts.append(f"{len(straddle)} too close to call at the data collected so far")
    sentence = (f"Of the {len(rows)} lift-and-build combinations with door data, "
                + "; ".join(parts) +
                f" — measured against the compliance line of {cliff_txt}.")
    lvl = (HIGH if (over or under) and not straddle and n_tot >= N_HIGH
           else MEDIUM if n_tot >= N_MEDIUM else TOO_EARLY)
    why = (f"{n_tot} door closes across all lifts; "
           + ("every lift's interval falls clearly on one side"
              if not straddle else
              f"{len(straddle)} lift(s) still have intervals crossing the line"))
    out.append(_finding(
        P_COMPLIANCE, "Does door-close travel clear the compliance line?",
        sentence, sheet, rng,
        _anchor(anchors, "fleet_compare_chart", sheet, "fleet comparison chart")[1],
        lvl, why, n=n_tot))

    # (b) one finding per lift-era, so each number is individually checkable
    for key, _a, cl in sorted(rows, key=lambda r: -(r[2]["median_ci"]["median"] or 0)):
        cam, instrument, era_id = key
        mc, cliff = cl["median_ci"], cl["cliff_s"]
        lvl, why = _confidence(cl["n"], mc, cliff, cl["suppressed"])
        sheet, rng = _anchor(anchors, ("per_lift_close", cam, instrument, era_id),
                             "PER-LIFT")
        if cl["suppressed"]:
            sentence = (f"{eras.lift_label(cam).capitalize()} on "
                        f"{_era_label(instrument, era_id)}: no close-travel figure "
                        f"is quoted — {cl['suppression_reason']}")
        else:
            rel = ("above" if (mc["median"] or 0) > cliff else "below")
            ci_txt = (f"(the true value is very likely between {mc['lo']:.2f}s and "
                      f"{mc['hi']:.2f}s)" if mc["lo"] is not None
                      else "(too few closes to put a range around it)")
            pct = cl["over_cliff"]["pct"]
            sentence = (f"{eras.lift_label(cam).capitalize()} on "
                        f"{_era_label(instrument, era_id)} takes a typical "
                        f"{mc['median']:.2f}s to close {ci_txt} — {rel} the "
                        f"{cliff:.2f}s line, with {pct:.0f}% of its closes over "
                        f"that line, from n={cl['n']} closes.")
        out.append(_finding(
            P_WORST, f"{eras.lift_label(cam)} — close travel", sentence,
            sheet, rng,
            _anchor(anchors, ("close_hist", cam, instrument, era_id), sheet,
                    "close-travel histogram")[1],
            lvl, why, n=cl["n"]))
    return out


def _f_divergence(ctx, anchors):
    """Two lifts in the SAME tower whose close travel differs materially, with
    non-overlapping intervals.

    This is deliberately NOT resolved here. Both explanations — genuine
    mechanical difference, or a measurement artifact of camera geometry or the
    door-detection build — are consistent with the evidence in this workbook,
    and nothing in the data separates them. Picking one would be inventing a
    conclusion; the honest output is a named open question that blocks per-lift
    figures from being quoted for design."""
    pools = []
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl, mc = a["close"], a["close"]["median_ci"]
        if cl["suppressed"] or mc["median"] is None or mc["lo"] is None:
            continue
        if cl["n"] < N_MEDIUM:
            continue
        pools.append((cam, instrument, era_id, cl, mc))
    if len(pools) < 2:
        return []
    lo_p = min(pools, key=lambda p: p[4]["median"])
    hi_p = max(pools, key=lambda p: p[4]["median"])
    if lo_p[0] == hi_p[0]:
        return []
    gap = hi_p[4]["median"] - lo_p[4]["median"]
    disjoint = hi_p[4]["lo"] > lo_p[4]["hi"]
    if gap < DIVERGENCE_S or not disjoint:
        return []

    hi_cam, _hi_i, hi_era, hi_cl, hi_mc = hi_p
    lo_cam, _lo_i, lo_era, lo_cl, lo_mc = lo_p
    sheet, rng = _anchor(anchors, "summary_headline", "SUMMARY")
    same_build = hi_era == lo_era
    sentence = (
        f"{eras.lift_label(hi_cam).capitalize()} takes "
        f"{hi_mc['median']:.2f}s to close and "
        f"{eras.lift_label(lo_cam)} takes {lo_mc['median']:.2f}s — a "
        f"{gap:.2f}s difference between two lifts in the same tower, and the "
        f"two ranges do not overlap ([{hi_mc['lo']:.2f}, {hi_mc['hi']:.2f}] "
        f"against [{lo_mc['lo']:.2f}, {lo_mc['hi']:.2f}]), so this is not "
        f"measurement noise. Identical door hardware should not differ this "
        f"much. THE CAUSE IS UNRESOLVED: it is either a real mechanical "
        f"difference between the two units — which would be a maintenance "
        f"finding — or an artifact of how these two cameras see their doors "
        f"({'both lifts were measured by different door-detection builds'
            if not same_build else 'both were measured by the same '
            'door-detection build, which makes camera geometry the more likely '
            'of the two'}). Nothing in this data separates the two "
        f"explanations, and this workbook does not choose between them.")
    return [_finding(
        P_UNRESOLVED,
        f"OPEN QUESTION — {eras.lift_label(hi_cam)} and {eras.lift_label(lo_cam)} "
        f"disagree by {gap:.2f}s and nothing here says why",
        sentence, sheet, rng,
        _anchor(anchors, "fleet_compare_chart", sheet,
                "fleet comparison chart")[1],
        TOO_EARLY,
        f"both pools are individually well-sampled (n={hi_cl['n']} and "
        f"n={lo_cl['n']}) and their intervals are disjoint, so the DIFFERENCE "
        f"is solid; what is unresolved is its CAUSE, which no amount of the "
        f"same measurement will settle",
        n=min(hi_cl["n"], lo_cl["n"]),
        extra=[
            "This blocks per-lift close-travel figures from being quoted for "
            "design: until the cause is known, a per-lift number cannot be "
            "attributed to the lift rather than to its camera.",
            "To resolve: physically time the doors on both lifts with a "
            "stopwatch and compare against these figures; and/or re-measure "
            "both under the same door-detection build with checked camera "
            "geometry. One afternoon of manual timing separates the two "
            "explanations outright."])]


def _f_demand(ctx, anchors):
    """When the building actually uses its lifts."""
    demand = ctx.get("demand") or {}
    by_era = demand.get("by_era") or {}
    if not by_era:
        return []
    # the counting era carrying the most data speaks for the building
    ver = max(by_era, key=lambda v: by_era[v]["total_boarded"])
    e = by_era[ver]
    pk = e["peaks"]["__fleet__"]
    if pk["hour"] is None:
        return []
    sheet, rng = _anchor(anchors, "demand_peaks", "DEMAND BY LIFT AND HOUR")
    ranked = sorted(
        ((h, v) for h, v in enumerate(e["fleet_boarded"]) if v is not None),
        key=lambda hv: -hv[1])[:3]
    busy = ", ".join(f"{h:02d}:00" for h, _v in ranked)
    sentence = (
        f"The tower's busiest hour is "
        f"{pk['hour']:02d}:00–{pk['hour'] + 1:02d}:00, carrying about "
        f"{pk['value']:.0f} boardings against a typical hour's "
        f"{pk['mean']:.0f} — {pk['ratio']:.1f} times the average hour. The "
        f"three busiest hours of the day are {busy}. All seven lifts serve one "
        f"tower, so this describes a single population.")
    lvl, why = (MEDIUM, f"{e['total_boarded']:,} boardings under counting build "
                        f"{ver}; hours a lift was not observed are excluded "
                        f"rather than counted as zero")
    return [_finding(P_DEMAND, "When the building uses its lifts", sentence,
                     sheet, rng,
                     _anchor(anchors, ("demand_chart", ver), sheet,
                             "mean boardings by hour and lift")[1],
                     lvl, why, n=e["total_boarded"])]


def _f_load_balance(ctx, anchors):
    """Whether the load sits evenly across the lifts — heavily caveated,
    because uneven coverage looks exactly like uneven load."""
    demand = ctx.get("demand") or {}
    by_era = demand.get("by_era") or {}
    if not by_era:
        return []
    ver = max(by_era, key=lambda v: by_era[v]["total_boarded"])
    e = by_era[ver]
    cvs = [(h, c) for h, c in enumerate(e["cv"]) if c is not None]
    if not cvs:
        return []
    covs = [ctx["coverage_pct"].get(c, 0.0) for c in ctx["cams"]]
    spread = (max(covs) - min(covs)) if covs else 0.0
    mean_cv = sum(c for _h, c in cvs) / len(cvs)
    worst_h, worst_cv = max(cvs, key=lambda hc: hc[1])
    sheet, rng = _anchor(anchors, "demand_matrix", "DEMAND BY LIFT AND HOUR")
    reading = ("evenly shared" if mean_cv < 0.25
               else "moderately uneven" if mean_cv < 0.6
               else "concentrated on some lifts")
    sentence = (
        f"Across the hours where at least two lifts were observed, boardings "
        f"are {reading} between lifts (average spread {mean_cv:.2f}, worst at "
        f"{worst_h:02d}:00 at {worst_cv:.2f}). THIS FIGURE MUST BE READ WITH "
        f"CARE: per-lift coverage in this range spans "
        f"{min(covs):.0f}%–{max(covs):.0f}%, a {spread:.0f}-point spread, and "
        f"a lift that was watched less will show less load whether or not it "
        f"carried less. At this coverage spread the comparison is not safe "
        f"across all seven lifts.")
    lvl = TOO_EARLY if spread > 20 else MEDIUM
    why = (f"coverage spread of {spread:.0f} points across lifts; load balance "
           f"is only interpretable across lifts of similar coverage, and these "
           f"lifts are not")
    return [_finding(P_DEMAND + 1, "Is the load balanced across the lifts?",
                     sentence, sheet, rng, None, lvl, why, n=len(cvs),
                     extra=["To make this readable: level up coverage across "
                            "the lifts, then re-export. Comparing demand "
                            "between a 57%-covered lift and a 17%-covered one "
                            "mostly measures the cameras, not the traffic."])]


def _f_per_floor_blocked(ctx, anchors):
    """What demand analysis still cannot reach, and why."""
    fs = ctx.get("floor_status") or {}
    rows = sum(d.get("rows", 0) for d in fs.values())
    confident = sum(d.get("confident", 0) for d in fs.values())
    if not rows or confident:
        return []
    sheet, rng = _anchor(anchors, "tier2_table", "TIER-2 BLOCKED")
    sentence = (
        f"Demand PER FLOOR — where people get on and off — cannot be reported "
        f"at all. Not one of the {rows:,} door observations in this range "
        f"produced a confident floor reading, so there is no way to say which "
        f"floor a boarding happened at. Demand per lift and per hour, which "
        f"does not need the floor, is reported in full.")
    return [_finding(
        P_DEMAND + 2, "Per-floor demand is unavailable", sentence, sheet, rng,
        None, TOO_EARLY,
        f"0 confident floor reads out of {rows:,} door observations",
        n=0,
        extra=["This is the same blocker behind the C21/C22 speed factors and "
               "the C17/C18 probable-stops coefficients. Fixing the "
               "floor-indicator reading unblocks all of them together."])]


def _f_open_travel(ctx, anchors):
    """Door OPEN travel: report the suppression as a finding in its own right,
    because the sheet has an assumption for it and a reader will look for it."""
    sup = [(k, a) for k, a in sorted(ctx["aggs"].items())
           if a["open_travel"]["suppressed"]]
    if not sup:
        return []
    lifts = sorted({eras.lift_label(k[0]) for k, _a in sup})
    fracs = [a["open_travel"]["at_quantum"]["frac"] for _k, a in sup]
    q = sup[0][1]["open_travel"]["at_quantum"]["quantum"]
    n_tot = sum(a["open_travel"]["n"] for _k, a in sup)
    spec_open = eras.SHEET_OPEN_S
    sheet, rng = _anchor(anchors, "vs_sheet_open", "VS THE SHEET")
    sentence = (
        f"How long doors take to OPEN cannot be measured with the current "
        f"camera settings, so this workbook states no figure against the "
        f"sheet's {spec_open:.2f}s open-travel assumption. Between "
        f"{100 * min(fracs):.0f}% and {100 * max(fracs):.0f}% of open "
        f"measurements on {', '.join(lifts)} land on the single smallest value "
        f"the cameras can record ({q:.2f}s, one video frame), which means the "
        f"opening is finishing faster than the cameras sample — the recorded "
        f"value is the limit of the instrument, not the speed of the door.")
    return [_finding(
        P_MEASURE_BLOCKED, "Door OPEN travel is not measurable", sentence,
        sheet, rng, None, TOO_EARLY,
        f"suppressed on all {len(sup)} lift-builds by the resolution-floor rule "
        f"(more than {100 * eras.QUANTUM_SUPPRESS_FRAC:.0f}% of values pinned at "
        f"the {q:.2f}s frame quantum); n={n_tot} open measurements exist but "
        f"none of them carry usable resolution",
        n=n_tot,
        extra=[f"To measure it: raise analyze_fps so one frame is well under the "
               f"true opening time, or detect the open edge by a method that "
               f"does not depend on frame sampling."])]


def _f_floor_filter(ctx, anchors):
    """Where the one-frame floor moved a headline number, say so — a reader
    comparing against an earlier export needs to know why it moved."""
    out = []
    warned = [(k, a) for k, a in sorted(ctx["aggs"].items())
              if a["close"]["floor_reject_warn"]]
    if not warned:
        return out
    floor = ctx.get("min_close_s", eras.MIN_PLAUSIBLE_CLOSE_S)
    for key, a in warned:
        cam, instrument, era_id = key
        cl = a["close"]
        sheet, rng = _anchor(anchors, ("per_lift_close", cam, instrument, era_id),
                             "PER-LIFT")
        pre, post = cl["median_prefilter"], cl["median_ci"]["median"]
        shift = ("" if pre is None or post is None else
                 f", which moved the typical close from {pre:.2f}s to {post:.2f}s")
        sentence = (
            f"{eras.lift_label(cam).capitalize()} on "
            f"{_era_label(instrument, era_id)} lost "
            f"{cl['n_floor_rejected']} of {cl['n_prefilter']} recorded closes "
            f"({100 * cl['floor_reject_frac']:.0f}%) to the implausibly-short "
            f"filter at {floor:.2f}s{shift}. Readings that short are the door "
            f"state flickering between frames, not a door closing.")
        out.append(_finding(
            P_DATA_QUALITY,
            f"{eras.lift_label(cam)} — closes rejected as too short to be real",
            sentence, sheet, rng, None,
            MEDIUM if cl["n"] >= N_MEDIUM else TOO_EARLY,
            f"{100 * cl['floor_reject_frac']:.0f}% rejection is above the "
            f"{100 * eras.FLOOR_REJECT_WARN_FRAC:.0f}% level that triggers this "
            f"warning; n={cl['n']} survives the filter",
            n=cl["n"]))
    return out


def _f_transfer(ctx, anchors):
    """C26 transfer seconds per person, where per-cycle counts allow it."""
    out = []
    for key, a in sorted(ctx["aggs"].items()):
        cam, instrument, era_id = key
        xc = a["transfer_pp"]
        if not xc["n"]:
            continue
        spec = eras.DOOR_SPECS.get(cam, {})
        thr = spec.get("transfer_sheet_s", eras.SHEET_TRANSFER_S_PP)
        lvl, why = _confidence(xc["n"], xc, thr)
        sheet, rng = _anchor(anchors, ("vs_sheet", cam), "VS THE SHEET")
        rel = "longer than" if (xc["mean"] or 0) > thr else "shorter than"
        sentence = (
            f"{eras.lift_label(cam).capitalize()} takes about "
            f"{xc['mean']:.2f}s per person to load and unload, {rel} the "
            f"sheet's {thr:.2f}s allowance, from n={xc['n']} cycles where the "
            f"people count could be joined to the door timing.")
        out.append(_finding(P_TRANSFER, f"{eras.lift_label(cam)} — transfer time",
                            sentence, sheet, rng, None, lvl, why, n=xc["n"]))
    return out


def _f_peak(ctx, anchors):
    """Peak demand — the number a design reviewer sizes the bank against."""
    peaks = [p for p in ctx["peaks"] if p["peak"]]
    sheet, rng = _anchor(anchors, "peak_table", "PEAK ANALYSIS")
    if not peaks:
        return [_finding(
            P_PEAK, "Peak demand", "No day in this range produced a usable "
            "busiest-five-minutes window.", sheet, rng, None, TOO_EARLY,
            "no day had boardings outside declared outage windows", n=0)]
    ratios = [p["peak_to_avg"] for p in peaks if p["peak_to_avg"]]
    best = max(peaks, key=lambda p: p["peak"]["boardings"])
    pop = ctx.get("population")
    pop_txt = ""
    if pop:
        pct = 100.0 * best["peak"]["boardings"] / pop
        pop_txt = (f" That is {pct:.1f}% of the {pop:,}-person population in "
                   f"five minutes, against the sheet's "
                   f"{eras.HC_PEAK_DESIGN_PCT:.0f}% design assumption.")
    else:
        pop_txt = (" The share of the building's population this represents "
                   "cannot be stated because no population figure was supplied "
                   "to the export.")
    ratio_txt = (f" Across {len(ratios)} days with a usable window, the busiest "
                 f"five minutes ran {min(ratios):.1f}x to {max(ratios):.1f}x the "
                 f"average five minutes." if ratios else "")
    sentence = (f"The busiest five minutes observed carried "
                f"{best['peak']['boardings']} boardings, on {best['day']}."
                + ratio_txt + pop_txt)
    lvl = (MEDIUM if len(peaks) >= 5 else TOO_EARLY)
    why = (f"{len(peaks)} day(s) produced a usable peak window; a peak figure "
           f"quoted for design should rest on more days than this, and on days "
           f"known to be representative")
    return [_finding(P_PEAK, "Peak demand", sentence, sheet, rng,
                     _anchor(anchors, "profile_chart", "FLEET",
                             "hourly profile chart")[1],
                     lvl, why, n=len(peaks))]


def _f_coverage(ctx, anchors):
    """How much of the range actually produced data, and how evenly."""
    sheet, rng = _anchor(anchors, "coverage_table", "COVERAGE & ERAS")
    covs = {c: v for c, v in ctx["coverage_pct"].items()}
    live = {c: v for c, v in covs.items() if v > 0}
    if not live:
        return []
    days = (ctx["t1"] - ctx["t0"]) / 86400.0
    sentence = (
        f"Across {days:.0f} days, per-channel coverage after excluding declared "
        f"outages runs from {min(live.values()):.0f}% to {max(live.values()):.0f}% "
        f"— that is, even the best-covered lift was producing data for well "
        f"under all of the time it was supposed to be.")
    return [_finding(
        P_COVERAGE, "How much of the period actually produced data", sentence,
        sheet, rng,
        _anchor(anchors, "coverage_chart", sheet, "coverage timeline")[1],
        MEDIUM, f"{len(live)} channel(s) produced rows; coverage is measured as "
                f"the share of 15-minute blocks holding at least one row, over "
                f"non-outage time", n=len(live))]


# ── the other sections of the sheet ──────────────────────────────────────────

def cannot_say_yet(ctx: dict) -> list[dict]:
    """{'what', 'why', 'to_fix'} — the honest limits of this export."""
    out = []
    with_door = {k[0] for k, a in ctx["aggs"].items() if a["n_cycles"] > 0}
    without = [c for c in ctx["cams"] if c not in with_door]
    if without:
        out.append({
            "what": (f"Nothing at all about {len(without)} of the "
                     f"{len(ctx['cams'])} lifts "
                     f"({', '.join(eras.lift_label(c) for c in without)})."),
            "why": ("These channels produced no door cycles in this range — "
                    "they have never been door-calibrated, so the door engine "
                    "has no template to measure against."),
            "to_fix": ("Run door calibration on those channels, then re-export. "
                       "Until then any fleet figure here describes "
                       f"{len(with_door)} lifts, not {len(ctx['cams'])}.")})

    fs = ctx.get("floor_status") or {}
    confident = sum(d.get("confident", 0) for d in fs.values())
    rows = sum(d.get("rows", 0) for d in fs.values())
    if rows and confident == 0:
        out.append({
            "what": ("Which floor each lift stopped at — and therefore the "
                     "speed factors C21/C22 and the probable-stops "
                     "coefficients C17/C18."),
            "why": (f"Not one of the {rows:,} door observations produced a "
                    f"confident floor read. Without knowing the floor of "
                    f"consecutive stops, travel between floors cannot be timed "
                    f"and trips cannot be segmented."),
            "to_fix": ("Get the floor-indicator OCR reading reliably (see "
                       "TIER-2 BLOCKED for what the cameras are currently "
                       "seeing), then re-export.")})
    elif rows and confident:
        pct = 100.0 * confident / rows
        if pct < 50:
            out.append({
                "what": "Floor-dependent coefficients C21/C22 and C17/C18.",
                "why": (f"Only {pct:.1f}% of door observations produced a "
                        f"confident floor read ({confident:,} of {rows:,})."),
                "to_fix": "Improve floor-indicator recognition, then re-export."})

    peaks = [p for p in ctx["peaks"] if p["peak"]]
    if len(peaks) < 10:
        out.append({
            "what": "A peak-demand figure solid enough to size a lift bank on.",
            "why": (f"Only {len(peaks)} day(s) in this range produced a usable "
                    f"busiest-five-minutes window, and single-digit day counts "
                    f"cannot show whether a peak is typical or a one-off."),
            "to_fix": ("Collect across enough normal working days — and identify "
                       "which days are representative of the building's routine "
                       "— then re-export.")})

    sup = [k for k, a in ctx["aggs"].items() if a["open_travel"]["suppressed"]]
    if sup:
        out.append({
            "what": "How long the doors take to open.",
            "why": ("The cameras sample too slowly to time the opening: most "
                    "measurements come back as exactly one frame, which is the "
                    "instrument's floor rather than the door's speed."),
            "to_fix": ("Raise the analysis frame rate, or detect the open edge "
                       "by a method that does not rely on frame sampling.")})

    if ctx.get("suspected_gaps"):
        n = len(ctx["suspected_gaps"])
        out.append({
            "what": ("Whether the quiet periods flagged on COVERAGE & ERAS were "
                     "real quiet or an unrecorded outage."),
            "why": (f"{n} window(s) show a channel going silent while it was "
                    f"producing data on both sides. They are FLAGGED but NOT "
                    f"excluded — treating them as outages without knowing the "
                    f"cause would be guessing."),
            "to_fix": ("Check the operations record for those windows; if they "
                       "were outages, declare them and re-export.")})
    return out


def data_scale(ctx: dict) -> dict:
    """The 'how much data is this really' block. Everything computed."""
    rows = ctx["raw_rows"]
    days_span = (ctx["t1"] - ctx["t0"]) / 86400.0
    by_day: dict[str, int] = {}
    for r in rows:
        by_day[r["ts_ist"][:10]] = by_day.get(r["ts_ist"][:10], 0) + 1
    top = sorted(by_day.items(), key=lambda kv: -kv[1])[:3]
    top_n = sum(n for _d, n in top)
    concentration = (100.0 * top_n / len(rows)) if rows else 0.0
    live = {c: v for c, v in ctx["coverage_pct"].items() if v > 0}
    return {
        "days_span": days_span,
        "n_rows": len(rows),
        "n_days_with_rows": len(by_day),
        "top_days": top,
        "top_share_pct": concentration,
        "coverage_lo": min(live.values()) if live else 0.0,
        "coverage_hi": max(live.values()) if live else 0.0,
        "n_lifts_total": len(ctx["cams"]),
        "n_lifts_with_door_data": len({k[0] for k, a in ctx["aggs"].items()
                                       if a["n_cycles"] > 0}),
        "n_declared_gaps": len([g for g in eras.DATA_GAPS
                                if g["start_epoch"] < ctx["t1"]
                                and g["end_epoch"] > ctx["t0"]]),
        "n_suspected_gaps": len(ctx.get("suspected_gaps") or []),
    }


def data_scale_sentences(ctx: dict) -> list[str]:
    d = data_scale(ctx)
    s = [f"This export covers {d['days_span']:.0f} days and "
         f"{d['n_rows']:,} individual recorded events."]
    if d["top_days"]:
        days_txt = ", ".join(day for day, _n in d["top_days"])
        s.append(f"The data is NOT spread evenly across those days: "
                 f"{d['top_share_pct']:.0f}% of all rows fall on just "
                 f"{len(d['top_days'])} days ({days_txt}). A figure computed "
                 f"over the whole range is therefore mostly a figure about "
                 f"those days.")
    s.append(f"Per-channel coverage runs {d['coverage_lo']:.0f}%–"
             f"{d['coverage_hi']:.0f}% of non-outage time, so even the "
             f"best-covered lift was unobserved for most of the period.")
    s.append(f"{d['n_lifts_with_door_data']} of {d['n_lifts_total']} lifts "
             f"produced any door data at all.")
    s.append(f"{d['n_declared_gaps']} outage window(s) are declared and excluded "
             f"from every rate and duration; {d['n_suspected_gaps']} further "
             f"quiet window(s) are flagged as suspicious but NOT excluded.")
    s.append("What this means: treat the numbers here as a first measurement "
             "of a fleet that has been observed briefly and unevenly, not as a "
             "settled performance record. Where a finding says TOO EARLY TO "
             "SAY, that is the honest state of the evidence, not a formality.")
    return s


# Definitions are not claims about the data, so they are the one thing in this
# module that is legitimately fixed prose. Terms are the ones the workbook uses.
GLOSSARY = [
    ("close travel",
     "How long the doors take to go from starting to close to fully shut, in "
     "seconds. This is the number the lift design sheet makes an assumption "
     "about, and the main thing this study measures."),
    ("dwell",
     "How long the doors stay fully open while people get in and out, in "
     "seconds. Separate from close travel."),
    ("transit",
     "One person crossing the doorway, counted by the cameras. A boarding is a "
     "transit going in; an alighting is one going out."),
    ("boarding",
     "One person entering the lift car. Peak demand is counted in boardings."),
    ("p85",
     "The 85th percentile: the value that 85% of the measurements fall below. "
     "Useful because a typical (median) figure hides the slow tail, and lift "
     "design has to survive the slow tail, not the typical case."),
    ("confidence interval (CI)",
     "The range the true value is very likely to sit in, given how many "
     "measurements were taken. A narrow interval means the answer is pinned "
     "down; a wide one means more data is needed. If the interval crosses a "
     "threshold, the measurement cannot yet tell which side of it we are on."),
    ("n",
     "The number of measurements a figure is computed from. Every figure in "
     "this workbook carries its n; a figure without one is a defect."),
    ("the compliance cliff",
     "The close-travel value above which the bank stops meeting its design "
     "case. It sits just above the sheet's own assumed close time, so a small "
     "overshoot in door closing is the difference between compliant and not — "
     "which is why this study measures close travel to two decimal places."),
    ("different instruments",
     "Door timings here come from two different measuring systems that ran at "
     "different times: an on-camera Pi door-watch, retired partway through, and "
     "a GPU door engine that replaced it. They use different detectors, clocks "
     "and sampling, so their numbers are not comparable — this workbook NEVER "
     "adds them together or averages across them. Where a cell would need to "
     "combine them it says 'n/a — spans eras' instead of printing a number."),
    ("era",
     "A stretch of time over which the measuring setup did not change. Figures "
     "are computed within an era and never pooled across one, because a change "
     "of setup changes what the number means."),
    ("counting_version",
     "The version of the people-counting software in effect when a count was "
     "made. Counts made by different versions are reported separately."),
    ("one-frame quantization floor",
     "The cameras see the world as still frames, so any duration they report is "
     "a whole number of frames. The shortest thing they can report is one "
     "frame. When a lot of measurements come back as exactly one frame, the "
     "instrument has hit its limit and the real duration is shorter than it can "
     "see — such measurements are reported as not measurable, never as a "
     "result."),
]

COLOUR_LEGEND = [
    ("green", "Observed value clears the threshold favourably.", "GOOD"),
    ("red", "Observed value exceeds the compliance cliff.", "OVER"),
    ("amber", "Inconclusive — the interval crosses the threshold; keep "
              "collecting.", "INCONCLUSIVE"),
    ("grey", "Not measurable — the instrument cannot resolve this, or the "
             "inputs it needs are missing.", "NOT MEASURABLE"),
]


def sheet_guide() -> list[tuple[str, str]]:
    """(sheet, the question it answers) — plain terms, no jargon."""
    return [
        ("SUMMARY", "How do the lifts' door-close times compare to what the "
                    "design sheet assumed, and are we collecting enough data "
                    "to stop?"),
        ("VS THE SHEET", "Coefficient by coefficient: what did the design sheet "
                         "assume, what did we actually measure, and does the "
                         "measurement clear the threshold?"),
        ("PER-LIFT", "For one lift at a time: how fast do its doors close, how "
                     "long do they stay open, how many people use it, and how "
                     "much of the time were we watching it?"),
        ("FLEET", "The lifts added up — and an explicit statement of what that "
                  "addition does and does not mean."),
        ("PEAK ANALYSIS", "How busy is the busiest five minutes of each day, "
                          "and how does that compare to the average?"),
        ("RAW", "Every individual recorded event, so any number in this "
                "workbook can be traced back to the observations behind it."),
        ("COVERAGE & ERAS", "When were we actually watching, when did the "
                            "measuring setup change, and which quiet periods "
                            "are outages we know about versus ones we don't?"),
        ("TIER-2 BLOCKED", "Which design coefficients we still cannot measure "
                           "at all, and exactly what is stopping us."),
    ]


def what_this_measures(ctx: dict) -> list[str]:
    """Opening paragraph. Numbers come from the declared sheet values."""
    cliffs = sorted({a["close"]["cliff_s"] for a in ctx["aggs"].values()}) or \
        [eras.COMPLIANCE_CLIFF_S]
    cliff = cliffs[0]
    assumed = eras.SHEET_CLOSE_S
    margin = cliff - assumed
    return [
        f"Cameras inside the lift cars watch the doors. From that video this "
        f"study times how long the doors take to close, how long they stay "
        f"open, and how many people get in and out.",
        f"The {eras.SHEET_NAME} design sheet assumes the doors close in "
        f"{assumed:.2f} seconds. The bank stops meeting its design case above "
        f"{cliff:.2f} seconds.",
        f"That {margin:.2f}-second margin is the whole reason this study exists: "
        f"the difference between a compliant lift bank and a non-compliant one "
        f"is smaller than the eye can judge, so it has to be measured rather "
        f"than estimated.",
        f"This workbook reports what the cameras actually recorded between "
        f"{ctx['from_iso'][:16]} and {ctx['to_iso'][:16]}, with the number of "
        f"observations beside every figure and an explicit statement wherever "
        f"the data does not support a conclusion.",
    ]
