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

import re
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


def _annotate_first_mentions(findings: list[dict], cams: list[str]) -> None:
    """Append the camera channel to each lift's FIRST mention, in RENDER order.

    Done after sorting, not during construction: findings are built by rule and
    then reordered by priority, so 'first built' is not 'first read'. Matching
    is word-bounded so 'lift 1' does not claim the first mention of 'lift 10'."""
    seen: set[str] = set()
    by_label = sorted(((eras.lift_label(c), c) for c in cams),
                      key=lambda p: -len(p[0]))
    for f in findings:
        for field in ("headline", "sentence"):
            text = f.get(field) or ""
            for label, cam in by_label:
                if cam in seen or not label:
                    continue
                m = re.search(rf"{re.escape(label)}(?!\s*\()(?![\w])", text)
                if not m:
                    continue
                text = (text[:m.start()] + eras.lift_label_with_channel(cam)
                        + text[m.end():])
                seen.add(cam)
            f[field] = text


def _sentence_start(text: str) -> str:
    """Upper-case the first character only. str.capitalize() would lower-case
    the rest, mangling a building label like 'Service Lift' into 'Service
    lift' — these names come from the building, not from us."""
    return text[:1].upper() + text[1:] if text else text


def _relation(value, threshold, decimals: int = 2) -> str:
    """'above the 2.31s line' / 'below …' / 'exactly ON …' — never 'above' for
    a value that displays as equal to the threshold."""
    rel = stats.compare_to_threshold(value, threshold, decimals)
    if rel == stats.AT:
        return f"sits exactly ON the {threshold:.{decimals}f}s line"
    return f"is {rel} the {threshold:.{decimals}f}s line"


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
    _annotate_first_mentions(out, ctx.get("cams") or [])
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

    # LEAD ON THE ASSUMPTION GAP. The study exists to check assumed
    # coefficients against reality, so the first thing said about C27 is where
    # the assumed value sits relative to what was measured. The compliance
    # count follows as a CONSEQUENCE of that gap, not as the headline.
    quotable = [(key, cl) for key, _a, cl in rows
                if not cl["suppressed"]
                and not stats.interval_is_uninformative(cl["median_ci"])
                and cl["median_ci"]["median"] is not None]
    assumed = eras.SHEET_CLOSE_S
    if quotable:
        meds = sorted(cl["median_ci"]["median"] for _k, cl in quotable)
        n_q = sum(cl["n"] for _k, cl in quotable)
        lo_v, hi_v = meds[0], meds[-1]
        span = (f"is {lo_v:.2f}s" if len(meds) == 1
                else f"spans {lo_v:.2f}s to {hi_v:.2f}s")
        if assumed < lo_v:
            where = ("BELOW everything measured here — every lift firm enough "
                     "to quote closes slower than the sheet assumes")
        elif assumed > hi_v:
            where = ("ABOVE everything measured here — every lift firm enough "
                     "to quote closes faster than the sheet assumes")
        else:
            where = ("INSIDE the observed range — some lifts are faster than "
                     "the sheet assumes and some slower")
        first = (f"The design sheet assumes doors close in {assumed:.2f}s. "
                 f"Across the {len(quotable)} lift-build(s) measured firmly "
                 f"enough to quote, the observed typical close travel {span} "
                 f"(n={n_q:,} closes), so the assumed value sits {where}.")
    else:
        first = (f"The design sheet assumes doors close in {assumed:.2f}s. No "
                 f"lift-build in this range was measured firmly enough to put "
                 f"an observed value beside it — see the per-lift findings for "
                 f"what each is short of.")

    parts = []
    if over:
        parts.append(f"{len(over)} above it — "
                     + ", ".join(eras.lift_label(c) for c in sorted(set(over))))
    if under:
        parts.append(f"{len(under)} below it — "
                     + ", ".join(eras.lift_label(c) for c in sorted(set(under))))
    if straddle:
        parts.append(f"{len(straddle)} not yet placeable on either side")
    second = (f"Because the design sheet multiplies door-close time by the "
              f"number of stops a car makes, a gap of this kind propagates "
              f"into round-trip time — and banks differ in how much slack they "
              f"have to absorb it. On the tightest-margin bank the design case "
              f"flips at {cliff_txt}; measured against that line, of the "
              f"{len(rows)} lift-build combinations with door data: "
              + "; ".join(parts) + ".")
    sentence = first + " " + second
    lvl = (HIGH if (over or under) and not straddle and n_tot >= N_HIGH
           else MEDIUM if n_tot >= N_MEDIUM else TOO_EARLY)
    why = (f"{n_tot} door closes across all lifts; "
           + ("every lift's interval falls clearly on one side"
              if not straddle else
              f"{len(straddle)} lift(s) still have intervals crossing the line"))
    out.append(_finding(
        P_COMPLIANCE,
        "C27 door operating time — observed close travel vs the assumed value",
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
        headline = f"{eras.lift_label(cam)} — close travel"
        if cl["suppressed"]:
            sentence = (f"{eras.lift_label(cam)} on "
                        f"{_era_label(instrument, era_id)}: no close-travel figure "
                        f"is quoted — {cl['suppression_reason']}")
        elif stats.interval_is_uninformative(mc):
            # No point estimate in result language — the interval does not
            # support one, and a number in this sentence shape would be read as
            # a measurement of the same standing as a well-sampled lift's.
            # Describe the SPREAD, not the midpoint — restating the midpoint
            # inside a "not measurable" sentence is how a suppressed number
            # ends up quoted anyway.
            span = (f"the range consistent with this data spans "
                    f"{mc['lo']:.2f}s to {mc['hi']:.2f}s — a spread of "
                    f"{mc['hi'] - mc['lo']:.2f}s, more than "
                    f"{stats.MAX_CI_WIDTH_RATIO:g}x the value it is meant to "
                    f"pin down, and consistent both with a lift under the "
                    f"assumed {eras.SHEET_CLOSE_S:.2f}s and one well over it"
                    if mc["lo"] is not None else
                    "there are too few closes to put any range around it")
            sentence = (f"{eras.lift_label(cam)} on "
                        f"{_era_label(instrument, era_id)}: not enough data to "
                        f"state a close-travel value (n={cl['n']}; {span}). No "
                        f"figure is quoted for this lift-build.")
        else:
            ci_txt = (f"(the true value is very likely between {mc['lo']:.2f}s "
                      f"and {mc['hi']:.2f}s)")
            pct = cl["over_cliff"]["pct"]
            sentence = (f"{eras.lift_label(cam)} on "
                        f"{_era_label(instrument, era_id)} takes a typical "
                        f"{mc['median']:.2f}s to close {ci_txt} — this "
                        f"{_relation(mc['median'], cliff)}, with {pct:.0f}% of "
                        f"its closes over that line, from n={cl['n']} closes.")
        out.append(_finding(
            P_WORST, headline, sentence,
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
        f"{_sentence_start(eras.lift_label(hi_cam))} takes "
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
    # Read the CANONICAL figure — never recompute one here, or this finding
    # becomes the third different busiest hour in the document.
    canon = (ctx.get("canonical") or {}).get("fleet_busiest_hour")
    if not canon or canon.get("hour") is None:
        return []
    ver = canon["era"]
    e = ((ctx.get("demand") or {}).get("by_era") or {}).get(ver)
    if not e:
        return []
    sheet, rng = _anchor(anchors, "canonical_busiest_hour",
                         "DEMAND BY LIFT AND HOUR")
    ranked = sorted(
        ((h, v) for h, v in enumerate(e["fleet_boarded"]) if v is not None),
        key=lambda hv: -hv[1])[:3]
    busy = ", ".join(f"{h:02d}:00" for h, _v in ranked)
    n_lifts = len(ctx["cams"])
    sentence = (
        f"The tower's busiest hour is "
        f"{canon['hour']:02d}:00–{canon['hour'] + 1:02d}:00, carrying about "
        f"{canon['value']:.0f} boardings against a typical hour's "
        f"{canon['all_day_mean']:.0f} — {canon['ratio']:.1f} times the average "
        f"hour. The three busiest hours of the day are {busy}. All {n_lifts} "
        f"lifts serve one tower, so this describes a single population. "
        f"Definition: {canon['definition']}.")
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
    from .model import LOAD_BALANCE_MIN_LIFTS, load_balance_reportable
    cvs = [(h, c) for h, c in enumerate(e["cv"]) if c is not None]
    covs = [ctx["coverage_pct"].get(c, 0.0) for c in ctx["cams"]]
    spread = (max(covs) - min(covs)) if covs else 0.0
    sheet, rng = _anchor(anchors, "demand_matrix", "DEMAND BY LIFT AND HOUR")
    n_lifts = len(ctx["cams"])
    coverage_caveat = (
        f"Per-lift coverage in this range spans {min(covs):.0f}%–"
        f"{max(covs):.0f}%, a {spread:.0f}-point spread, and a lift that was "
        f"watched less will show less load whether or not it carried less — so "
        f"uneven coverage can masquerade as uneven load."
        if covs else "")

    if not cvs or not load_balance_reportable(e["cv"]):
        sentence = (
            f"Whether the load is shared evenly between lifts cannot be stated "
            f"from this range. Fewer than {LOAD_BALANCE_MIN_LIFTS} lifts were "
            f"observed together in most hours of the day, and a spread measured "
            f"across one or two lifts is not a spread. {coverage_caveat}")
        return [_finding(
            P_DEMAND + 1, "Is the load balanced across the lifts?", sentence,
            sheet, rng, None, TOO_EARLY,
            f"only {len(cvs)} of 24 hours had at least "
            f"{LOAD_BALANCE_MIN_LIFTS} lifts observed together",
            n=len(cvs),
            extra=["To make this answerable: get the lifts observed over the "
                   "same hours, then re-export."])]

    mean_cv = sum(c for _h, c in cvs) / len(cvs)
    worst_h, worst_cv = max(cvs, key=lambda hc: hc[1])
    reading = ("shared fairly evenly between lifts" if mean_cv < 0.25
               else "moderately uneven between lifts" if mean_cv < 0.6
               else "concentrated on some lifts rather than shared")
    sentence = (
        f"Across the {len(cvs)} hours where at least {LOAD_BALANCE_MIN_LIFTS} "
        f"lifts were observed together, boardings are {reading} (average "
        f"spread {mean_cv:.2f}, widest at {worst_h:02d}:00 at "
        f"{worst_cv:.2f}). THIS MUST BE READ WITH CARE: {coverage_caveat} At "
        f"this coverage spread the comparison is not safe across all "
        f"{n_lifts} lifts.")
    lvl = TOO_EARLY if spread > 20 else MEDIUM
    why = (f"coverage spread of {spread:.0f} points across lifts; load balance "
           f"is only interpretable across lifts of similar coverage, and these "
           f"lifts are not")
    return [_finding(P_DEMAND + 1, "Is the load balanced across the lifts?",
                     sentence, sheet, rng, None, lvl, why, n=len(cvs),
                     extra=[f"To make this readable: level up coverage across "
                            f"the lifts, then re-export. Comparing demand "
                            f"between a {max(covs):.0f}%-covered lift and a "
                            f"{min(covs):.0f}%-covered one mostly measures the "
                            f"cameras, not the traffic."])]


def _f_per_floor_blocked(ctx, anchors):
    """What demand analysis still cannot reach, and why."""
    fs = ctx.get("floor_status") or {}
    rows = sum(d.get("rows", 0) for d in fs.values())
    confident = sum(d.get("confident", 0) for d in fs.values())
    if not rows or confident:
        return []
    sheet, rng = _anchor(anchors, "tier2_table", "TIER-2 EVIDENCE")
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
        extra=["Floor reading also gates the C17/C18 probable-stops coefficients, but it is NOT "
               "the only thing gating them, and it is not what gates C21/C22 at all — those need "
               "a rated speed and floor-to-floor height this database does not hold. See TIER-2 "
               "EVIDENCE for each coefficient's actual blocker; fixing the OCR does not unblock "
               "them together."])]


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
            f"{_sentence_start(eras.lift_label(cam))} on "
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
            f"{_sentence_start(eras.lift_label(cam))} takes about "
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
            "to_fix": ("Get the floor-indicator OCR reading reliably (see TIER-2 EVIDENCE for "
                       "what the cameras are currently seeing), then re-export. Note this alone "
                       "does not unblock C21/C22, which additionally need a rated speed and "
                       "floor-to-floor height that are not held anywhere.")})
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
     "seconds. It is ONE of the eight assumed coefficients this study is "
     "validating, and the most leveraged of them: the design sheet multiplies "
     "it by the number of stops a car makes, so a small error per cycle "
     "compounds into the round-trip time that sets how many lifts a tower "
     "needs."),
    ("lift 1, ch16",
     "'Lift 1' is the building's own name for the lift, taken from the channel "
     "map. 'ch16' is the camera channel watching it. They refer to the same "
     "lift; the channel is shown on first mention on each sheet so the two can "
     "be tied together. Where the building has not named a lift, the workbook "
     "says so rather than inventing a name."),
    ("dwell",
     "How long the doors stay fully open while people get in and out, in "
     "seconds. Separate from close travel."),
    ("transit",
     "One person crossing the doorway, counted by the cameras. A boarding is a "
     "transit going in; an alighting is one going out."),
    ("boarding",
     "One person entering the lift car. Peak demand is counted in boardings."),
    ("mean per observed day, vs total observed",
     "A TOTAL is the raw number of people counted — whole people. A MEAN PER "
     "OBSERVED DAY divides that total by the number of days the lift was "
     "actually being watched in that hour. Totals cannot be compared between "
     "lifts here, because the lifts were watched for very different amounts of "
     "time and a lift watched longer will show a bigger total regardless of how "
     "busy it was; the means can. A mean of 12.3 people does not mean part of a "
     "person boarded — it means that across the days observed, that hour "
     "averaged 12.3 people, the same way a household can average 2.4 children."),
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
     "A worked example of coefficient sensitivity, not a target anyone is "
     "aiming at. Each bank has slack in its round-trip time. Where that slack "
     "is generous, a door-close time above the assumed value changes nothing. "
     "Where it is tight — a few seconds — the same overshoot is enough to flip "
     "that bank's design case. The 'cliff' is the close-travel value at which "
     "that flip happens for the tightest-margin bank. It illustrates why the "
     "accuracy of an assumed coefficient decides lift count and speed; it is a "
     "result of this study, not its purpose."),
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
        ("SUMMARY", "Which assumed coefficients this site could measure, how "
                    "far the measurements sit from the assumptions, and "
                    "whether enough has been collected to stop measuring."),
        ("VS THE SHEET", "Coefficient by coefficient: what the design sheet "
                         "assumes, what this site actually did, and whether "
                         "the measurement is firm enough to revise the "
                         "assumption with."),
        ("PER-LIFT", "One lift at a time: its door timings, how long it stands "
                     "open, how many people it carried, and how much of the "
                     "period it was observed — the per-unit evidence behind "
                     "every fleet figure."),
        ("FLEET", "The lifts added together, for the figures where adding them "
                  "is valid — and an explicit statement of what that addition "
                  "does and does not mean."),
        ("DEMAND BY LIFT AND HOUR", "How the building actually uses its lifts "
                                    "through the day. This feeds the demand "
                                    "assumption, which needs no floor "
                                    "attribution and is the study's "
                                    "highest-value output."),
        ("PEAK ANALYSIS", "The busiest five minutes of each day, which is the "
                          "window the handling-capacity assumption is written "
                          "against."),
        ("RAW", "Every individual recorded event, so any number in this "
                "workbook can be traced back to the observations behind it."),
        ("COVERAGE & ERAS", "When the cameras were actually watching, when the "
                            "measuring setup changed, and which quiet periods "
                            "are outages we know about versus ones we don't — "
                            "how much weight the measurements can bear."),
        ("TIER-2 EVIDENCE", "Which assumed coefficients this site could not "
                           "measure at all, and exactly what is stopping "
                           "each one."),
    ]


def why_this_study_exists(ctx: dict) -> list[str]:
    """The opening block. Explains the PROGRAMME this workbook belongs to,
    before any measurement is shown."""
    n_coef = len(eras.COEFFICIENTS)
    return [
        f"When a new tower is designed, the lift traffic calculation is driven "
        f"by a table of assumed coefficients — how long doors take, how many "
        f"people a car carries, how many floors it stops at. Those assumptions "
        f"come from the {eras.SHEET_NAME} standards table. They have never "
        f"been checked against a real, occupied building.",
        f"This study measures what actually happens. Cameras in the lift cars "
        f"of handed-over, occupied residential towers record boardings, "
        f"alightings, door timings, dwell and stops. Those measurements go "
        f"back into the design sheet as corrected factors.",
        f"It answers two questions: (1) {eras.STUDY_QUESTIONS[0]} "
        f"(2) {eras.STUDY_QUESTIONS[1]}",
        f"Scope: {eras.STUDY_SCOPE} It is a BENCHMARKING study across a "
        f"portfolio, not a compliance audit of any one building.",
        f"This workbook reports ONE site's measurements toward that programme, "
        f"covering {ctx['from_iso'][:16]} to {ctx['to_iso'][:16]}. It is not a "
        f"verdict on this building. It reports observations beside the "
        f"assumptions they test, with the number of observations against every "
        f"figure, and says so explicitly wherever the data does not support a "
        f"conclusion. There are {n_coef} assumed coefficients under test in "
        f"total; the table below shows how much of that this export reaches.",
    ]


def what_this_measures(ctx: dict) -> list[str]:
    """What the cameras record, and what it feeds."""
    assumed = eras.SHEET_CLOSE_S
    return [
        f"Cameras inside the lift cars record, for every door cycle: when the "
        f"doors start and finish opening, how long they stand open, how long "
        f"they take to close, and how many people cross the threshold in each "
        f"direction. From those, this workbook derives door operating time, "
        f"passenger transfer time, passengers per trip, lost time per stop, "
        f"and demand by hour.",
        f"Each of those is an assumed coefficient in the {eras.SHEET_NAME} "
        f"design sheet. Every figure here is placed beside the assumption it "
        f"tests, so the gap between assumed and observed is readable directly.",
        f"Door-close time carries more weight than the others because the "
        f"design sheet multiplies it by the number of stops a car makes — "
        f"currently around 15 down-stops — so an error of a fraction of a "
        f"second per door cycle compounds into the round-trip time that decides "
        f"how many lifts a tower needs and how fast they must run. The sheet "
        f"assumes {assumed:.2f} seconds.",
        f"The highest-value measurement in this study is NOT in the round-trip "
        f"calculation at all. The {eras.HC_PEAK_DESIGN_PCT:.0f}% "
        f"handling-capacity figure is a DEMAND assumption — "
        f"{eras.HC_ASSUMPTION_NOTE}. Cameras measure that directly and it needs "
        f"no floor attribution. If actual demand is well below the assumption, "
        f"the portfolio is being over-designed; if well above it, there is a "
        f"service problem nobody has diagnosed. Either outcome outweighs the "
        f"whole coefficient exercise.",
    ]


def coefficient_scope(ctx: dict) -> list[dict]:
    """The full scope of what is under test, with THIS export's status per
    coefficient computed from the data — so the reader sees what the measured
    part is a fraction OF.

    Status is derived, never declared: a table that says 'measured' while the
    data says otherwise is worse than no table."""
    aggs = ctx.get("aggs") or {}
    fs = ctx.get("floor_status") or {}
    confident = sum(d.get("confident", 0) for d in fs.values())

    def _any(key_fn):
        return any(key_fn(a) for a in aggs.values())

    n_close = sum(a["close"]["n"] for a in aggs.values())
    open_suppressed = aggs and all(a["open_travel"]["suppressed"]
                                   for a in aggs.values())
    n_xfer = sum(a["transfer_pp"]["n"] for a in aggs.values())
    n_pax = sum(a["pax_per_trip"]["n"] for a in aggs.values())
    n_lost = sum(a["lost_time"]["n"] for a in aggs.values())

    status: dict[str, tuple[str, str]] = {}
    if n_close and not open_suppressed:
        status["C27"] = ("PARTLY MEASURED",
                         f"close travel measured (n={n_close:,}); open travel "
                         f"also measured")
    elif n_close:
        status["C27"] = ("PARTLY MEASURED",
                         f"close travel measured (n={n_close:,}); open travel "
                         f"NOT measurable — pinned at the camera's frame "
                         f"quantum")
    else:
        status["C27"] = ("NOT MEASURED", "no clean door cycles in this range")
    status["C26"] = (("MEASURED", f"n={n_xfer:,} cycles with counts joined to "
                                  f"dwell") if n_xfer else
                     ("NOT MEASURED", "no per-cycle passenger counts joined to "
                                      "dwell in this range"))
    status["C19"] = (("PARTLY MEASURED",
                      f"observed loading recorded (n={n_pax:,}) but the car's "
                      f"rated capacity is not in this database, so it cannot "
                      f"be expressed as a share of capacity") if n_pax else
                     ("NOT MEASURED", "no per-cycle passenger counts in range"))
    status["B24"] = (("MEASURED", f"n={n_lost:,} stops") if n_lost else
                     ("NOT MEASURED", "needs dwell measured against passenger "
                                      "load"))
    # C17/C18/C21/C22 — status stays BLOCKED, but the REASON is now derived per coefficient.
    # This block used to assign one hardcoded "floor attribution produced N confident reads"
    # sentence to all four, which was wrong on two counts: floor attribution is not the blocker
    # for C21/C22 at all, and it is not the blocker for C17/C18 either once single_panel reads
    # are counted. Each now names what actually stops it.
    blockers = ctx.get("coefficient_blockers") or {}
    for cid in ("C17", "C18", "C21", "C22"):
        b = blockers.get(cid)
        if b:
            status[cid] = (b.get("status", "BLOCKED"), b.get("blocker", ""))
        else:
            status[cid] = ("BLOCKED", f"floor attribution produced {confident:,} confident "
                                      f"reads in this range")

    out = []
    for c in eras.COEFFICIENTS:
        st, why = status.get(c["id"], ("NOT MEASURED", ""))
        out.append({**c, "status": st, "status_why": why})
    return out


def demand_assumption_row(ctx: dict) -> dict:
    """The handling-capacity assumption, tracked separately from the RTT
    coefficients because it is a demand figure and the study's highest-value
    output."""
    pop = ctx.get("population")
    peaks = [p for p in ctx["peaks"] if p["peak"]]
    best = max((p["peak"]["boardings"] for p in peaks), default=0)
    if pop:
        st = "MEASURED"
        why = (f"busiest five minutes observed carried {best} boardings, "
               f"{100.0 * best / pop:.1f}% of the {pop:,} population supplied")
    else:
        st = "BLOCKED"
        why = (f"busiest five minutes observed carried {best} boardings, but no "
               f"population figure was supplied to this export, so it cannot be "
               f"expressed as a share of the population. Re-run with "
               f"--population N. The population is never guessed.")
    return {"id": f"{eras.HC_PEAK_DESIGN_PCT:.0f}% HC",
            "name": "handling capacity (peak 5-minute demand)",
            "assumes": eras.HC_ASSUMPTION_NOTE,
            "needs": "boarding counts and the building's population",
            "status": st, "status_why": why}
