"""Workbook rendering (openpyxl). All figures arrive pre-aggregated per era
from model.py; this file NEVER computes a cross-era number — if a cell would
need one it prints 'n/a — spans eras'.

Formatting and chart configuration live in style.py and charts.py so that a
number can never land in a cell as a bare float and a chart can never ship
without readable axes.

Sheet builders record where they put things in an `anchors` dict. READ THIS
FIRST is built LAST, from those anchors, and then moved to the front — that is
what lets every "Evidence:" line on it name a cell range that really holds the
number it is quoting.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from openpyxl.utils import get_column_letter

from . import charts, demand_log, eras, model, narrative, stats, style
from .charts import ChartData  # re-exported: cli builds one and passes it in
from .eras import GPU_ENGINE, PI_WATCH
from .reader import precision_str
from .style import (BODY, BODY_BOLD, BODY_ITALIC, F_INT, F_PCT, F_RATE, F_SEC,
                    F_SEC_PLAIN, F_TS, FILL_ERA, FILL_NA, FILL_WARN, H1, H2,
                    banner, caption, cell, freeze_below, header_row, section,
                    title)

NA_SPANS = "n/a — spans eras"


def _fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, eras.IST).strftime("%Y-%m-%d %H:%M:%S%z")


def _dt(epoch: float | None):
    """A real datetime so the cell can carry a date number format."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, eras.IST).replace(tzinfo=None)


def _ci_str(lo, hi) -> str:
    if lo is None or hi is None:
        return "n too small"
    return f"[{lo:.2f}, {hi:.2f}]"


def _rng(first_row, last_row, n_cols) -> str:
    return f"A{first_row}:{get_column_letter(n_cols)}{last_row}"


def _lbl(ws, cam: str) -> str:
    """The building's name for a lift, with the camera channel appended on this
    SHEET's first mention — so a reader can tie 'lift 1' to 'ch16' once, then
    read clean names afterwards."""
    seen = getattr(ws, "_liftlab_named", None)
    if seen is None:
        seen = set()
        ws._liftlab_named = seen
    if cam in seen:
        return eras.lift_label(cam)
    seen.add(cam)
    return eras.lift_label_with_channel(cam)


def _era_banner(ws, row, boundaries) -> int:
    if not boundaries:
        return row
    msg = ("⚠ RANGE CROSSES ERA BOUNDARIES — every aggregate on this sheet is "
           "PER ERA; pi_watch and gpu_engine figures are different instruments "
           "and are never pooled. See COVERAGE & ERAS.")
    row = banner(ws, row, msg, fill=FILL_ERA, font=style.ERA_FONT)
    return row + 1


def _floor_banner(ws, row, ctx) -> int:
    """Amber banner naming every pool that lost more than the warn share to the
    one-frame floor — the reader must not meet a moved median unexplained."""
    warned = [(k, a) for k, a in sorted(ctx["aggs"].items())
              if a["close"]["floor_reject_warn"]]
    if not warned:
        return row
    bits = []
    for (cam, _inst, era_id), a in warned:
        cl = a["close"]
        bits.append(f"{eras.lift_label(cam)}/{era_id} "
                    f"{100 * cl['floor_reject_frac']:.0f}% "
                    f"({cl['n_floor_rejected']} of {cl['n_prefilter']})")
    msg = (f"⚠ ONE-FRAME QUANTIZATION FLOOR — closes shorter than "
           f"{ctx['min_close_s']:.2f}s are door-state flicker, not closes, and "
           f"are rejected from every close-travel figure. Pools losing more "
           f"than {100 * eras.FLOOR_REJECT_WARN_FRAC:.0f}% to the floor: "
           + "; ".join(bits) + ". Pre- and post-filter figures are both shown "
           "on PER-LIFT.")
    return banner(ws, row, msg, height=44) + 1


def _suppression_banner(ws, row, ctx) -> int:
    sup = [k for k, a in sorted(ctx["aggs"].items())
           if a["open_travel"]["suppressed"] or a["close"]["suppressed"]]
    if not sup:
        return row
    q = ctx.get("fleet_quantum_s")
    q_txt = f"{q:.2f}s" if q else "the detected frame quantum"
    msg = (f"⚠ NOT MEASURABLE — {len(sup)} lift-build pool(s) have more than "
           f"{100 * eras.QUANTUM_SUPPRESS_FRAC:.0f}% of their values pinned at "
           f"the sampling-resolution floor ({q_txt}, one video frame). No "
           f"verdict is issued for those measurements; the reason is printed "
           f"in place of the verdict on VS THE SHEET.")
    return banner(ws, row, msg, fill=FILL_NA, height=44) + 1


# ── READ THIS FIRST ──────────────────────────────────────────────────────────

def sheet_read_this_first(wb, ctx, anchors):
    """Built LAST (so anchors are populated), moved to the front by the caller."""
    ws = wb.create_sheet("READ THIS FIRST")
    ws.sheet_view.showGridLines = False
    row = title(ws, "LIFTLAB — what we measured, what we found, and what we "
                    "cannot say yet")
    row += 1
    cell(ws, row, 1, f"Covering {ctx['from_iso'][:16]} to {ctx['to_iso'][:16]} "
                     f"(building local time). Generated {ctx['generated_at']}.",
         font=BODY_ITALIC)
    row += 2

    # ── why the study exists (the programme, before any measurement)
    row = section(ws, row, "WHY THIS STUDY EXISTS")
    for para in narrative.why_this_study_exists(ctx):
        cell(ws, row, 1, para, wrap=True)
        ws.row_dimensions[row].height = 44
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        row += 1
    row += 1

    # ── what this measures
    row = section(ws, row, "WHAT THIS MEASURES")
    for para in narrative.what_this_measures(ctx):
        c = cell(ws, row, 1, para, wrap=True)
        ws.row_dimensions[row].height = 44
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        row += 1
    row += 1

    # ── the whole scope of what is under test, and how much this export reaches
    row = section(ws, row, "WHICH COEFFICIENTS ARE UNDER TEST")
    scope = narrative.coefficient_scope(ctx)
    n_done = sum(1 for c in scope if c["status"] == "MEASURED")
    n_part = sum(1 for c in scope if c["status"] == "PARTLY MEASURED")
    cell(ws, row, 1,
         f"The design sheet rests on {len(scope)} assumed coefficients. This "
         f"export fully measures {n_done} and partly measures {n_part}; the "
         f"rest are listed with what is blocking them. This table is what the "
         f"measured part is a fraction OF.", font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 30
    row += 1
    hdr = row
    row = header_row(ws, row, ["coefficient", "what it assumes",
                               "what measuring it needs",
                               "status in this export", "why"])
    _STATUS_FILL = {"MEASURED": style.FILL_GOOD,
                    "PARTLY MEASURED": style.FILL_WARN,
                    "NOT MEASURED": FILL_NA, "BLOCKED": FILL_NA}
    for c in scope + [narrative.demand_assumption_row(ctx)]:
        cell(ws, row, 1, f"{c['id']} — {c['name']}", font=BODY_BOLD, wrap=True)
        cell(ws, row, 2, c["assumes"], wrap=True)
        cell(ws, row, 3, c["needs"], wrap=True)
        cell(ws, row, 4, c["status"], font=BODY_BOLD,
             fill=_STATUS_FILL.get(c["status"]))
        cell(ws, row, 5, c["status_why"], wrap=True)
        ws.row_dimensions[row].height = 42
        row += 1
    for col, w in ((1, 30), (2, 34), (3, 34), (4, 20), (5, 46)):
        style.wrap_column(ws, col, hdr + 1, row - 1, width=w)
    row += 1

    # ── how to read the workbook
    row = section(ws, row, "HOW TO READ THIS WORKBOOK")
    hdr = row
    row = header_row(ws, row, ["sheet", "the question it answers"])
    for name, question in narrative.sheet_guide():
        cell(ws, row, 1, name, font=BODY_BOLD)
        cell(ws, row, 2, question, wrap=True)
        row += 1
    style.wrap_column(ws, 2, hdr + 1, row - 1, width=90)
    row += 1

    # ── colour convention
    row = section(ws, row, "COLOUR CONVENTION USED THROUGHOUT")
    for name, meaning, tag in narrative.COLOUR_LEGEND:
        fill = {"green": style.FILL_GOOD, "red": style.FILL_BAD,
                "amber": style.FILL_WARN, "grey": style.FILL_NA}[name]
        cell(ws, row, 1, tag, font=BODY_BOLD, fill=fill)
        cell(ws, row, 2, meaning, wrap=True)
        row += 1
    row += 1

    # ── findings
    row = section(ws, row, "WHAT WE FOUND")
    cell(ws, row, 1, "Ordered by how much each matters to the design decision, "
                     "not by where it appears in the workbook. Every figure "
                     "below can be checked at the sheet and cells named against "
                     "it.", font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    row += 2
    findings = narrative.build_findings(ctx, anchors)
    if not findings:
        row = banner(ws, row, "No findings — this range produced no measurable "
                              "door or transit data. See COVERAGE & ERAS.")
    for f in findings:
        # the headline first, so a reader scanning the column sees WHAT each
        # finding is about (and that an open question IS one) before the prose
        cell(ws, row, 1, f"{f['number']}.", font=BODY_BOLD)
        cell(ws, row, 2, f["headline"], font=H2, wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
        row += 1
        c = cell(ws, row, 2, f["sentence"], wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
        ws.row_dimensions[row].height = 44
        row += 1
        ev = f"Evidence: sheet {f['sheet']}, cells {f['range']}"
        if f.get("chart"):
            ev += f"; chart: {f['chart']}"
        cell(ws, row, 2, ev, font=BODY_ITALIC)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
        row += 1
        cell(ws, row, 2, "Confidence:", font=BODY_BOLD)
        cell(ws, row, 3, f["confidence"], font=BODY_BOLD,
             fill=style.confidence_fill(f["confidence"]))
        cell(ws, row, 4, f["why"], wrap=True)
        ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=9)
        ws.row_dimensions[row].height = 28
        row += 1
        for extra in f.get("extra") or []:
            cell(ws, row, 2, extra, font=BODY_ITALIC, wrap=True)
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=9)
            row += 1
        row += 1

    # ── what we cannot say
    row = section(ws, row, "WHAT WE CANNOT SAY YET — AND WHAT IT WOULD TAKE")
    hdr = row
    row = header_row(ws, row, ["what we cannot say", "why not",
                               "what would make it sayable"])
    for item in narrative.cannot_say_yet(ctx):
        cell(ws, row, 1, item["what"], font=BODY_BOLD, wrap=True)
        cell(ws, row, 2, item["why"], wrap=True)
        cell(ws, row, 3, item["to_fix"], wrap=True)
        ws.row_dimensions[row].height = 60
        row += 1
    for col in (1, 2, 3):
        style.wrap_column(ws, col, hdr + 1, row - 1, width=48)
    row += 1

    # ── how much data
    row = section(ws, row, "HOW MUCH DATA THIS IS")
    for s in narrative.data_scale_sentences(ctx):
        cell(ws, row, 1, s, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        ws.row_dimensions[row].height = 28
        row += 1
    row += 1

    # ── glossary
    row = section(ws, row, "GLOSSARY")
    hdr = row
    row = header_row(ws, row, ["term", "what it means"])
    for term, meaning in narrative.GLOSSARY:
        cell(ws, row, 1, term, font=BODY_BOLD)
        cell(ws, row, 2, meaning, wrap=True)
        ws.row_dimensions[row].height = 42
        row += 1
    style.wrap_column(ws, 2, hdr + 1, row - 1, width=95)

    style.set_widths(ws, {1: 26, 2: 40, 3: 22, 4: 40})
    style.print_setup(ws, landscape=False)
    return ws


# ── SUMMARY ──────────────────────────────────────────────────────────────────

def sheet_summary(wb, ctx, cd, anchors):
    ws = wb.active
    ws.title = "SUMMARY"
    row = title(ws, f"LIFTLAB report — {ctx['from_iso']} → {ctx['to_iso']}")
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    row = _floor_banner(ws, row, ctx)
    row = _suppression_banner(ws, row, ctx)

    meta = [
        ("Generated at", ctx["generated_at"]),
        ("Gateway", ctx["gw"]),
        ("DB", ctx["db_path"] + "  (opened read-only: mode=ro + query_only)"),
        ("Range (IST)", f"{_fmt_ts(ctx['t0'])} → {_fmt_ts(ctx['t1'])}"),
        ("Total raw rows exported", ctx["n_raw_rows"]),
        ("Eras present", ", ".join(sorted({f"{k[1]}/{k[2]}" for k in ctx["aggs"]}))
         or "none (no door cycles in range)"),
        ("Sampling resolution detected",
         (f"{ctx['fleet_quantum_s']:.2f}s per frame — the shortest duration the "
          f"cameras can express" if ctx.get("fleet_quantum_s")
          else "not determinable from this range")),
        ("Implausibly-short close filter",
         f"closes below {ctx['min_close_s']:.2f}s rejected (--min-close)"),
        ("Gap windows excluded", len([g for g in eras.DATA_GAPS
                                      if g["start_epoch"] < ctx["t1"]
                                      and g["end_epoch"] > ctx["t0"]])),
        ("Suspected undeclared gaps flagged", len(ctx.get("suspected_gaps") or [])),
    ]
    for k, v in meta:
        cell(ws, row, 1, k, font=BODY_BOLD)
        cell(ws, row, 2, v, fmt=F_INT if isinstance(v, int) else None)
        row += 1
    row += 1

    row = section(ws, row, "C27 DOOR OPERATING TIME — observed close travel vs "
                           "the assumed value, PER INSTRUMENT ERA (never "
                           "pooled)")
    cell(ws, row, 1,
         f"C27 is one of the {len(eras.COEFFICIENTS)} assumed coefficients "
         f"under test, and the most leveraged: the design sheet multiplies it "
         f"by the number of stops a car makes. It is not the purpose of the "
         f"study — see WHICH COEFFICIENTS ARE UNDER TEST on READ THIS FIRST "
         f"for the full scope and what this export reaches. Verdicts below are "
         f"mechanical statements about the measurement, not recommendations.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=13)
    ws.row_dimensions[row].height = 44
    row += 1
    headers = ["lift", "instrument", "era", "n (clean closes)", "median s",
               "median 95% CI lo s", "median 95% CI hi s",
               "p85 s", "sheet assumption s", "% > cliff", "cliff s",
               "95% CI of % > cliff", "verdict vs cliff (on median CI)"]
    hdr_row = row
    row = header_row(ws, row, headers)
    first_data = row
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl = a["close"]
        spec = eras.DOOR_SPECS.get(cam, {})
        mc = cl["median_ci"]
        verdict = (cl["suppression_reason"] if cl["suppressed"]
                   else stats.verdict_vs_threshold(mc, cl["cliff_s"]))
        cell(ws, row, 1, _lbl(ws, cam))
        cell(ws, row, 2, instrument)
        cell(ws, row, 3, era_id)
        cell(ws, row, 4, cl["n"], F_INT)
        cell(ws, row, 5, mc["median"], F_SEC)
        cell(ws, row, 6, mc["lo"], F_SEC)
        cell(ws, row, 7, mc["hi"], F_SEC)
        cell(ws, row, 8, cl["p85"], F_SEC)
        cell(ws, row, 9, spec.get("sheet_close_s", eras.SHEET_CLOSE_S), F_SEC)
        cell(ws, row, 10, (cl["over_cliff"]["pct"] / 100.0
                           if cl["over_cliff"]["pct"] is not None else None),
             F_PCT)
        cell(ws, row, 11, cl["cliff_s"], F_SEC)
        cell(ws, row, 12, _ci_str(cl["over_cliff"]["lo"], cl["over_cliff"]["hi"]))
        cell(ws, row, 13, verdict, wrap=True,
             fill=style.verdict_fill(verdict, cl["suppressed"]))
        row += 1
    if not ctx["aggs"]:
        cell(ws, row, 1, "No door cycles in range — see COVERAGE & ERAS.",
             fill=FILL_WARN)
        row += 1
    # The widest disagreement between two lifts in this ONE tower, stated as a
    # figure rather than left as arithmetic for the reader — it is a headline
    # finding, and READ THIS FIRST cites this range for it.
    pools = [(cam, a["close"], a["close"]["median_ci"])
             for (cam, _i, _e), a in sorted(ctx["aggs"].items())
             if a["close"]["n"] >= narrative.N_MEDIUM
             and not a["close"]["suppressed"]
             and a["close"]["median_ci"]["median"] is not None
             and a["close"]["median_ci"]["lo"] is not None]
    if len(pools) >= 2:
        lo_p = min(pools, key=lambda p: p[2]["median"])
        hi_p = max(pools, key=lambda p: p[2]["median"])
        gap = hi_p[2]["median"] - lo_p[2]["median"]
        disjoint = hi_p[2]["lo"] > lo_p[2]["hi"]
        cell(ws, row, 1, "Widest disagreement between two lifts", font=BODY_BOLD)
        cell(ws, row, 2, f"{_lbl(ws, hi_p[0])} vs {_lbl(ws, lo_p[0])}")
        cell(ws, row, 5, gap, F_SEC, font=BODY_BOLD,
             fill=FILL_WARN if disjoint and gap >= narrative.DIVERGENCE_S
             else None)
        cell(ws, row, 8, ("intervals do NOT overlap — not measurement noise; "
                          "cause UNRESOLVED, see READ THIS FIRST"
                          if disjoint else
                          "intervals overlap — could be measurement noise"),
             wrap=True)
        row += 1
    last_data = row - 1
    anchors["summary_headline"] = ("SUMMARY", _rng(first_data, max(first_data,
                                                                  last_data),
                                                   len(headers)))
    style.verdict_conditional_formatting(ws, 13, first_data, last_data)
    style.autofit(ws, max_row=last_data, wrap_cols=(13,))
    style.set_widths(ws, {3: 22, 13: 46})
    freeze_below(ws, hdr_row)
    style.print_setup(ws, repeat_row=hdr_row)
    row += 2

    # ── fleet comparison chart — the design reviewer's chart
    rows = []
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl, mc = a["close"], a["close"]["median_ci"]
        if cl["n"] == 0 or mc["median"] is None or cl["suppressed"]:
            continue
        lo = mc["lo"] if mc["lo"] is not None else mc["median"]
        hi = mc["hi"] if mc["hi"] is not None else mc["median"]
        rows.append({"label": f"{eras.lift_label_with_channel(cam)} {era_id}",
                     "median": mc["median"],
                     "err_lo": max(0.0, mc["median"] - lo),
                     "err_hi": max(0.0, hi - mc["median"]),
                     "n": cl["n"], "over": mc["median"] > cl["cliff_s"]})
    if rows:
        cliff = sorted({a["close"]["cliff_s"] for a in ctx["aggs"].values()})[0]
        n_over = sum(1 for r in rows if r["over"])
        assumed = eras.SHEET_CLOSE_S
        n_slower = sum(1 for r in rows if r["median"] > assumed)
        row = caption(ws, row, (
            f"Take-away: the sheet assumes {assumed:.2f}s; {n_slower} of "
            f"{len(rows)} measurable lift-builds close slower than that — the "
            f"gap between assumed and observed is what this study is measuring. "
            f"The red line marks {cliff:.2f}s, the point at which the "
            f"tightest-margin bank's design case flips; {n_over} sit above it. "
            f"Whiskers show the 95% range — where a whisker crosses a line, the "
            f"measurement cannot yet say which side that lift is on."))
        ch = charts.fleet_comparison(cd, rows, cliff)
        if ch is not None:
            ws.add_chart(ch, f"A{row}")
            anchors["fleet_compare_chart"] = (
                "SUMMARY", f"'Typical door-close travel by lift' at A{row}")
            row += charts.rows_for(10.0)

    # ── stopping rule for the BEST-SAMPLED pool
    best = None
    for key, a in ctx["aggs"].items():
        if a["close"]["n"] >= 2 and not a["close"]["suppressed"]:
            if best is None or a["close"]["n"] > best[1]["close"]["n"]:
                best = (key, a)
    if best:
        (cam, instrument, era_id), a = best
        cl = a["close"]
        row = caption(ws, row, (
            f"Take-away: this is the best-sampled pool ({eras.lift_label(cam)}, "
            f"n={cl['n']}). The study can stop for this lift when the dashed "
            f"95% band sits wholly below the red line and stays there; while "
            f"the band still touches the line, more closes are needed. "
            f"Per-lift versions of this chart are on PER-LIFT."))
        ch = charts.stopping_rule(
            cd, f"{eras.lift_label(cam)} — is the measurement settled yet? "
                f"({instrument}/{era_id}, n={cl['n']})",
            cl["values"], cl["cliff_s"])
        ws.add_chart(ch, f"A{row}")
        anchors["stopping_rule_chart"] = (
            "SUMMARY", f"'is the measurement settled yet' at A{row}")
        row += charts.rows_for()
    return ws


# ── VS THE SHEET ─────────────────────────────────────────────────────────────

def sheet_vs_sheet(wb, ctx, cd, anchors):
    from .model import vs_sheet_rows
    ws = wb.create_sheet("VS THE SHEET")
    row = title(ws, f"Observed coefficients vs {eras.SHEET_NAME} assumptions")
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    row = _suppression_banner(ws, row, ctx)
    cell(ws, row, 1, "Verdicts are mechanical: the confidence interval against "
                     "the decision threshold. No further interpretation is "
                     "offered. Where a measurement is bound by the camera's "
                     "sampling resolution, the reason is printed in place of a "
                     "verdict — a resolution-bound number is not a result.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
    ws.row_dimensions[row].height = 30
    row += 2

    headers = ["coefficient", "instrument / era", "sheet assumption",
               "observed value", "n", "95% CI", "decision threshold", "verdict"]
    first_hdr = None
    for cam in ctx["cams"]:
        if cam not in {k[0] for k in ctx["aggs"]} and cam not in eras.DOOR_SPECS:
            continue
        cell(ws, row, 1, f"{_lbl(ws, cam)}"
             f" — bank {ctx['banks'].get(cam) or 'UNKNOWN'}", font=H2)
        row += 1
        hdr_row = row
        first_hdr = first_hdr or hdr_row
        row = header_row(ws, row, headers)
        cam_first = row
        for r in vs_sheet_rows(ctx["aggs"], cam, ctx.get("coefficient_blockers")):
            cell(ws, row, 1, r["coefficient"])
            cell(ws, row, 2, r["era"])
            if isinstance(r["assumption"], str):
                cell(ws, row, 3, r["assumption"], wrap=True)
            else:
                cell(ws, row, 3, r["assumption"], F_SEC)
            cell(ws, row, 4, r["observed"], F_SEC)
            cell(ws, row, 5, r["n"], F_INT)
            cell(ws, row, 6, _ci_str(*r["ci"]))
            cell(ws, row, 7, r["threshold"], F_SEC)
            cell(ws, row, 8, r["verdict"], wrap=True,
                 fill=style.verdict_fill(r["verdict"], r.get("suppressed")))
            if "open travel" in r["coefficient"] and r.get("suppressed"):
                anchors.setdefault("vs_sheet_open",
                                   ("VS THE SHEET", f"A{row}:H{row}"))
            row += 1
        anchors[("vs_sheet", cam)] = ("VS THE SHEET", _rng(cam_first, row - 1,
                                                           len(headers)))
        style.verdict_conditional_formatting(ws, 8, cam_first, row - 1)
        row += 1
    anchors.setdefault("vs_sheet_open", ("VS THE SHEET", "—"))
    style.autofit(ws, wrap_cols=(1, 3, 8))
    style.set_widths(ws, {1: 42, 2: 26, 8: 60})
    if first_hdr:
        freeze_below(ws, first_hdr)
        style.print_setup(ws, repeat_row=first_hdr)
    return ws


# ── PER-LIFT ─────────────────────────────────────────────────────────────────

def sheet_per_lift(wb, ctx, cd, anchors):
    ws = wb.create_sheet("PER-LIFT")
    row = title(ws, "Per-lift analysis (one block per channel; every count has "
                    "its precision beside it)")
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    row = _floor_banner(ws, row, ctx)
    first_hdr = None

    for cam in ctx["cams"]:
        v = ctx["validation"].get(cam, {})
        cur_ver = (ctx["analyzer_versions"].get(cam)
                   or v.get("counting_version") or "unknown")
        prec = precision_str(ctx["validation"], cam, cur_ver)
        cell(ws, row, 1, _lbl(ws, cam), font=H1)
        cell(ws, row, 3, f"bank: {ctx['banks'].get(cam) or 'UNKNOWN'}")
        cell(ws, row, 4, f"counting_version: {cur_ver}")
        cell(ws, row, 5, f"precision: {prec}"
             + (f", validated {_fmt_ts(v['confirmed_at'])}"
                if v.get("confirmed_at") else ""))
        cell(ws, row, 7, f"coverage after gap exclusion: "
                         f"{ctx['coverage_pct'].get(cam, 0.0):.1f}%")
        row += 2

        cam_aggs = {k: a for k, a in ctx["aggs"].items() if k[0] == cam}
        if not cam_aggs:
            row = banner(ws, row, "No door cycles in this range for this "
                                  "channel — see COVERAGE & ERAS.", height=18)
            row += 1

        headers = ["instrument", "era", "cycles n",
                   "clean closes n (before floor filter)",
                   "close median s (before)", "close p85 s (before)",
                   "floor applied s",
                   "clean closes n (after floor filter)",
                   "close median s (after)", "close median 95% CI lo s",
                   "close median 95% CI hi s", "close p85 s (after)",
                   "compliance cliff s", "% of closes over the cliff",
                   "rejected at floor", "% of pool rejected",
                   "close min s", "close max s",
                   "excluded (flap/reopen/implausible/gap)", "dwell n",
                   "dwell median s", "dwell p85 s", "cycles/hr (gap-excl)"]
        hdr_row = row
        first_hdr = first_hdr or hdr_row
        row = header_row(ws, row, headers)
        block_first = row
        for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
            cl = a["close"]
            f = ctx["funnels"].get((cam, era_id), {})
            excl = (f"{f.get('n_flap', 0)}/{f.get('n_reopened', 0)}/"
                    f"{f.get('n_implausible', 0)}/{a['n_in_gap_excluded']}"
                    if instrument == GPU_ENGINE else
                    f"withheld={a['n_closes_excluded']}, gap={a['n_in_gap_excluded']}")
            cell(ws, row, 1, instrument)
            cell(ws, row, 2, era_id)
            cell(ws, row, 3, a["n_cycles"], F_INT)
            cell(ws, row, 4, cl["n_prefilter"], F_INT)
            cell(ws, row, 5, cl["median_prefilter"], F_SEC)
            cell(ws, row, 6, cl["p85_prefilter"], F_SEC)
            cell(ws, row, 7, cl["floor_s"], F_SEC)
            cell(ws, row, 8, cl["n"], F_INT)
            cell(ws, row, 9, cl["median_ci"]["median"], F_SEC)
            cell(ws, row, 10, cl["median_ci"]["lo"], F_SEC)
            cell(ws, row, 11, cl["median_ci"]["hi"], F_SEC)
            cell(ws, row, 12, cl["p85"], F_SEC)
            cell(ws, row, 13, cl["cliff_s"], F_SEC)
            cell(ws, row, 14, (cl["over_cliff"]["pct"] / 100.0
                               if cl["over_cliff"]["pct"] is not None else None),
                 F_PCT)
            cell(ws, row, 15, cl["n_floor_rejected"], F_INT)
            cell(ws, row, 16, cl["floor_reject_frac"], F_PCT,
                 fill=FILL_WARN if cl["floor_reject_warn"] else None)
            cell(ws, row, 17, cl["min"], F_SEC)
            cell(ws, row, 18, cl["max"], F_SEC)
            cell(ws, row, 19, excl)
            cell(ws, row, 20, a["dwell"]["n"], F_INT)
            cell(ws, row, 21, a["dwell"]["median_ci"]["median"], F_SEC)
            cell(ws, row, 22, a["dwell"]["p85"], F_SEC)
            cell(ws, row, 23, a["cycles_per_hr"], F_RATE)
            anchors[("per_lift_close", cam, instrument, era_id)] = (
                "PER-LIFT", f"A{row}:{get_column_letter(len(headers))}{row}")
            row += 1
        row += 1

        # open travel: state the suppression rather than print a false figure
        ot_hdr = row
        row = header_row(ws, row, ["instrument", "era", "open-travel n",
                                   "open-travel median s",
                                   "at the one-frame floor",
                                   "status against the sheet's open assumption"])
        for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
            ot = a["open_travel"]
            cell(ws, row, 1, instrument)
            cell(ws, row, 2, era_id)
            cell(ws, row, 3, ot["n"], F_INT)
            if ot["suppressed"]:
                cell(ws, row, 4, "not measurable", fill=FILL_NA)
            else:
                cell(ws, row, 4, ot["median_ci"]["median"], F_SEC)
            cell(ws, row, 5, ot["at_quantum"]["frac"], F_PCT,
                 fill=FILL_NA if ot["suppressed"] else None)
            cell(ws, row, 6, ot["suppression_reason"] or
                 stats.verdict_vs_threshold(ot["median_ci"], eras.SHEET_OPEN_S),
                 wrap=True,
                 fill=style.verdict_fill(None, ot["suppressed"]))
            row += 1
        style.wrap_column(ws, 6, ot_hdr + 1, row - 1, width=60)
        row += 1

        # transits — precision ADJACENT to every count
        row = header_row(ws, row, [
            "counting era", "boarded", "precision (boarded)", "alighted",
            "precision (alighted)", "riders/hr (gap-excl)", "gap-excluded n",
            "first", "last"])
        t_aggs = {k: d for k, d in ctx["transit_aggs"].items() if k[0] == cam}
        pi_counted = [(k, a) for k, a in cam_aggs.items()
                      if k[1] == PI_WATCH and a["n_counted_cycles"]]
        for (c_, ver), d in sorted(t_aggs.items()):
            p = precision_str(ctx["validation"], cam, ver)
            cell(ws, row, 1, ver)
            cell(ws, row, 2, d["boarded"], F_INT)
            cell(ws, row, 3, p)
            cell(ws, row, 4, d["alighted"], F_INT)
            cell(ws, row, 5, p)
            cell(ws, row, 6, d["per_hr"], F_RATE)
            cell(ws, row, 7, d["n_in_gap_excluded"], F_INT)
            cell(ws, row, 8, _dt(d["first_ts"]), F_TS)
            cell(ws, row, 9, _dt(d["last_ts"]), F_TS)
            row += 1
        for (c_, instrument, era_id), a in pi_counted:
            cell(ws, row, 1, f"{era_id} (Pi on-device counter)")
            cell(ws, row, 2, a["boarded"], F_INT)
            cell(ws, row, 3, "unvalidated (Pi counter was never precision-scored)")
            cell(ws, row, 4, a["alighted"], F_INT)
            cell(ws, row, 5, "unvalidated (Pi counter was never precision-scored)")
            row += 1
        if not t_aggs and not pi_counted:
            cell(ws, row, 1, "no transits in range")
            row += 1
        row += 2

        # charts per era with data
        for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
            cl = a["close"]
            if cl["n"] == 0:
                continue
            spec = eras.DOOR_SPECS.get(cam, {})
            assumption = spec.get("sheet_close_s", eras.SHEET_CLOSE_S)
            pct_over = cl["over_cliff"]["pct"] or 0.0
            vals = cl["values"]
            pct_over_assumed = (100.0 * sum(1 for v in vals if v > assumption)
                                / len(vals)) if vals else 0.0
            row = caption(ws, row, (
                f"Take-away: {pct_over_assumed:.0f}% of this lift's closes take "
                f"longer than the {assumption:.2f}s the design sheet assumes "
                f"(amber and red bars); the green bars are the closes at or "
                f"under it. Of the whole pool, {pct_over:.0f}% go beyond "
                f"{cl['cliff_s']:.2f}s, where the tightest-margin bank's design "
                f"case flips. n={cl['n']}."))
            ch = charts.close_histogram(
                cd, f"{eras.lift_label(cam)} close travel — {instrument}/{era_id} "
                    f"(n={cl['n']})", cl["values"], stats.CLOSE_HIST_EDGES,
                assumption, cl["cliff_s"])
            ws.add_chart(ch, f"A{row}")
            anchors[("close_hist", cam, instrument, era_id)] = (
                "PER-LIFT", f"'{eras.lift_label(cam)} close travel' at A{row}")

            if a["dwell"]["n"]:
                caption(ws, row - 1, (
                    f"Take-away: how long the doors stand open on "
                    f"{eras.lift_label(cam)} — the loading time, separate from "
                    f"the closing time. n={a['dwell']['n']}."), col=11)
                wsd, d0, d1, nr2 = cd.block([
                    ["dwell band (s)"] + stats.hist_labels(stats.DWELL_HIST_EDGES),
                    [f"{cam} {instrument}/{era_id} dwell (n={a['dwell']['n']})"]
                    + a["dwell"]["hist"]])
                ch2 = charts.bar_chart(
                    cd, f"{eras.lift_label(cam)} dwell distribution — "
                        f"{instrument}/{era_id} (n={a['dwell']['n']})",
                    cd.ref(d0, d0, 2, nr2), cd.ref(d1, d1, 1, nr2),
                    n_series=1, n_cats=nr2 - 1,
                    x_title="dwell (s)", y_title="number of cycles")
                ws.add_chart(ch2, f"K{row}")
            row += charts.rows_for()

            # stopping rule for every pool with enough data to judge
            if cl["n"] >= narrative.N_MEDIUM:
                row = caption(ws, row, (
                    f"Take-away: whether {eras.lift_label(cam)}'s close-travel "
                    f"measurement has settled. When the dashed 95% band sits "
                    f"wholly clear of the red compliance line, collecting more "
                    f"closes for this lift stops changing the answer."))
                ch3 = charts.stopping_rule(
                    cd, f"{eras.lift_label(cam)} — is the measurement settled "
                        f"yet? ({instrument}/{era_id}, n={cl['n']})",
                    cl["values"], cl["cliff_s"])
                ws.add_chart(ch3, f"A{row}")
                row += charts.rows_for()
        row += 2

    style.set_widths(ws, {1: 14, 2: 16, 3: 10, 4: 16, 5: 14, 6: 14, 7: 12,
                          8: 16, 9: 14, 10: 14, 11: 14, 12: 14, 13: 12, 14: 14,
                          15: 12, 16: 14, 19: 30, 23: 14})
    if first_hdr:
        freeze_below(ws, first_hdr)
        style.autofilter(ws, first_hdr, 23, first_hdr + 40)
        style.print_setup(ws, repeat_row=first_hdr)
    return ws


# ── FLEET ────────────────────────────────────────────────────────────────────

def sheet_fleet(wb, ctx, cd, anchors):
    ws = wb.create_sheet("FLEET")
    row = title(ws, "Fleet roll-up — UNWEIGHTED SUMS")
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    precs = [v["precision_pct"] for v in ctx["validation"].values()
             if v.get("precision_pct") is not None]
    prec_note = (f"precision {min(precs):.0f}%–{max(precs):.0f}% across "
                 f"contributing lifts — totals are NOT precision-weighted"
                 if precs else "no validated precision on any contributing lift")
    row = banner(ws, row, f"Fleet totals are unweighted sums; {prec_note}.",
                 height=18)
    banks_present = {b for b in ctx["banks"].values() if b}
    if not banks_present:
        row = banner(ws, row, "⚠ bank column is UNPOPULATED (all UNKNOWN) — "
                              "fleet figures below pool across banks and may "
                              "mix design cases. Populate lift_banks.json.",
                     height=30)
    row += 1

    groups: dict[str, list[str]] = {}
    for cam in ctx["cams"]:
        groups.setdefault(ctx["banks"].get(cam) or "UNKNOWN", []).append(cam)
    first_hdr = None
    for bank, cams in sorted(groups.items()):
        cell(ws, row, 1, f"Bank: {bank} ({', '.join(cams)})", font=H2)
        row += 1
        hdr_row = row
        first_hdr = first_hdr or hdr_row
        row = header_row(ws, row, [
            "counting era", "boarded (sum)", "alighted (sum)",
            "lifts contributing", "precision range", "riders/hr (sum, gap-excl)",
            "close-travel"])
        by_ver: dict[str, dict] = {}
        for (cam, ver), d in ctx["transit_aggs"].items():
            if cam not in cams:
                continue
            g = by_ver.setdefault(ver, {"b": 0, "a": 0, "cams": [], "rate": 0.0})
            g["b"] += d["boarded"]
            g["a"] += d["alighted"]
            g["cams"].append(cam)
            g["rate"] += d["per_hr"] or 0.0
        for ver, g in sorted(by_ver.items()):
            ps = [ctx["validation"][c]["precision_pct"] for c in g["cams"]
                  if ctx["validation"].get(c, {}).get("precision_pct") is not None]
            cell(ws, row, 1, ver)
            cell(ws, row, 2, g["b"], F_INT)
            cell(ws, row, 3, g["a"], F_INT)
            cell(ws, row, 4, ", ".join(sorted(g["cams"])))
            cell(ws, row, 5, (f"{min(ps):.0f}%–{max(ps):.0f}%" if ps
                              else "unvalidated"))
            cell(ws, row, 6, g["rate"], F_RATE)
            cell(ws, row, 7, NA_SPANS + " (per-camera door instruments)",
                 fill=FILL_NA)
            row += 1
        pi_b = sum(a["boarded"] for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        pi_a = sum(a["alighted"] for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        pi_n = sum(1 for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        if pi_n:
            cell(ws, row, 1, "pi_watch on-device counter (pre-2026-07-21)")
            cell(ws, row, 2, pi_b, F_INT)
            cell(ws, row, 3, pi_a, F_INT)
            cell(ws, row, 4, f"{pi_n} lift-eras")
            cell(ws, row, 5, "unvalidated")
            cell(ws, row, 7, NA_SPANS, fill=FILL_NA)
            row += 1
        if not by_ver and not pi_n:
            cell(ws, row, 1, "no transits in range")
            row += 1
        row += 1

    # hourly profile chart
    hours = list(range(24))
    cols = [["IST hour"] + hours]
    fleet_b = [0] * 24
    fleet_a = [0] * 24
    for cam in ctx["cams"]:
        p = ctx["profile"].get(cam, {})
        cols.append([f"{cam} boarded"] + [p.get(h, {}).get("boarded", 0)
                                          for h in hours])
        for h in hours:
            fleet_b[h] += p.get(h, {}).get("boarded", 0)
            fleet_a[h] += p.get(h, {}).get("alighted", 0)
    cols.append(["fleet boarded (unweighted sum)"] + fleet_b)
    cols.append(["fleet alighted (unweighted sum)"] + fleet_a)
    wsd, c0, c1, nr = cd.block(cols)
    # This chart plots RAW TOTALS pooled across counting builds — a different
    # definition from the canonical busiest hour. It therefore names its own
    # definition and defers to the canonical figure rather than stating a
    # competing "busiest hour" of its own.
    canon = (ctx.get("canonical") or {}).get("fleet_busiest_hour")
    if canon and canon.get("hour") is not None:
        row = caption(ws, row, (
            f"Take-away: when the building actually uses its lifts. NOTE THE "
            f"DEFINITION — this chart plots RAW TOTAL boardings per hour summed "
            f"over every day and pooled across counting builds, which is not "
            f"the workbook's headline busiest-hour figure. The canonical "
            f"busiest hour is {canon['hour']:02d}:00–{canon['hour'] + 1:02d}:00 "
            f"({canon['value']:.0f} boardings), defined as {canon['definition']} "
            f"— see DEMAND BY LIFT AND HOUR. Raw totals favour whichever hours "
            f"had most days observed, which is why the two differ."))
    else:
        row = caption(ws, row, "Take-away: no boardings were recorded in this "
                               "range.")
    ch = charts.line_chart(
        cd, "Hourly boarding profile — RAW TOTALS, pooled across counting "
            "builds (not the canonical busiest-hour figure)",
        cd.ref(c0, c0, 2, nr), cd.ref(c0 + 1, c1, 1, nr),
        n_series=len(cols) - 1, x_title="hour of the day",
        y_title="people", y_fmt="#,##0")
    ws.add_chart(ch, f"A{row}")
    anchors["profile_chart"] = ("FLEET", f"'Hourly boarding profile' at A{row}")
    style.autofit(ws, max_row=row - 2, wrap_cols=(7,))
    style.set_widths(ws, {1: 34, 7: 44})
    if first_hdr:
        freeze_below(ws, first_hdr)
        style.print_setup(ws, repeat_row=first_hdr)
    return ws


# ── DEMAND BY LIFT AND HOUR ──────────────────────────────────────────────────

DARK = "—"          # observed nothing. NOT zero. A dark lift is not an idle lift.


def sheet_demand(wb, ctx, cd, anchors):
    """Per-lift, per-hour demand — the thing floor attribution does NOT block.

    One block per counting era: counts made by different counting builds are
    different measurements, and a tidier single matrix would be one that pools
    them."""
    from .model import peak_5min_by_cam
    ws = wb.create_sheet("DEMAND BY LIFT AND HOUR")
    row = title(ws, "Demand by lift and hour — who uses which lift, when")
    row += 1
    cell(ws, row, 1,
         "All lifts here serve ONE tower, so the fleet column describes a "
         "single population and is meaningful. Every cell is the mean over the "
         f"days that lift had data in that hour, outages excluded. '{DARK}' "
         "means the lift was not observed in that hour — it is NOT a zero, and "
         "must not be read as 'this lift carried nobody'.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
    ws.row_dimensions[row].height = 44
    row += 2

    demand = ctx["demand"]
    cams = ctx["cams"]
    hours = demand["hours"]
    pop = ctx.get("population")

    # The canonical busiest hour, stated ONCE, at the top, with its definition —
    # every other statement of it in this workbook resolves to this value.
    canon = (ctx.get("canonical") or {}).get("fleet_busiest_hour")
    row = section(ws, row, "THE CANONICAL BUSIEST HOUR")
    if canon and canon.get("hour") is not None:
        cell(ws, row, 1, "busiest hour (fleet)", font=BODY_BOLD)
        cell(ws, row, 2, f"{canon['hour']:02d}:00–{canon['hour'] + 1:02d}:00")
        cell(ws, row, 4, "mean boardings in it", font=BODY_BOLD)
        cell(ws, row, 5, canon["value"], "0.0")
        row += 1
        cell(ws, row, 1, "typical hour, all day", font=BODY_BOLD)
        cell(ws, row, 2, canon["all_day_mean"], "0.0")
        cell(ws, row, 4, "peak : typical", font=BODY_BOLD)
        cell(ws, row, 5, canon["ratio"], "0.00")
        row += 1
        cell(ws, row, 1, "definition", font=BODY_BOLD)
        cell(ws, row, 2, canon["definition"], wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=10)
        ws.row_dimensions[row].height = 32
        row += 1
        cell(ws, row, 1,
             "This is the figure this workbook quotes wherever a fleet busiest "
             "hour is stated. Any sheet showing a different hour is using a "
             "different definition and says so where it does.",
             font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
        ws.row_dimensions[row].height = 28
        row += 1
    else:
        cell(ws, row, 1, "No boardings in range — no busiest hour to state.")
        row += 1
    anchors["canonical_busiest_hour"] = ("DEMAND BY LIFT AND HOUR",
                                         _rng(row - 4, row - 1, 10))
    row += 1
    p5 = peak_5min_by_cam(ctx["transits"], ctx["all_cycles"], cams,
                          ctx["t0"], ctx["t1"])

    first_matrix_range = None
    for ver in demand["versions"]:
        e = demand["by_era"][ver]
        row = section(ws, row, f"COUNTING ERA: {ver}")
        cell(ws, row, 1, f"{e['total_boarded']:,} boardings and "
                         f"{e['total_alighted']:,} alightings attributed to this "
                         f"counting build. Counts from different builds are "
                         f"different measurements and are never pooled.",
             font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
        row += 2

        # ── per-lift column headers: version, precision + n, coverage
        def _lift_headers(rw, heading):
            cell(ws, rw, 1, heading, font=BODY_BOLD, fill=style.FILL_HEADER,
                 wrap=True)
            ws.cell(row=rw, column=1).font = style.HEADER_FONT
            for i, cam in enumerate(cams, start=2):
                c = cell(ws, rw, i, _lbl(ws, cam), font=style.HEADER_FONT,
                         fill=style.FILL_HEADER, wrap=True)
                c.alignment = style.WRAP_CENTRE
            c = cell(ws, rw, len(cams) + 2, "FLEET TOTAL\n(unweighted sum of "
                                            "per-lift means)",
                     font=style.HEADER_FONT, fill=style.FILL_HEADER, wrap=True)
            c.alignment = style.WRAP_CENTRE
            ws.row_dimensions[rw].height = 32
            rw += 1
            # provenance strip: what each column's numbers rest on
            for label, fn in (
                ("counting_version", lambda cam: ver),
                ("precision (count accuracy)",
                 lambda cam: precision_str(ctx["validation"], cam, ver)),
                ("coverage % (gap-adjusted)",
                 lambda cam: f"{ctx['coverage_pct'].get(cam, 0.0):.0f}%")):
                cell(ws, rw, 1, label, font=BODY_ITALIC)
                for i, cam in enumerate(cams, start=2):
                    v = fn(cam)
                    unval = isinstance(v, str) and "unvalidated" in v
                    cell(ws, rw, i, v, font=BODY_ITALIC, wrap=True,
                         fill=FILL_NA if unval else None)
                cell(ws, rw, len(cams) + 2, "—", font=BODY_ITALIC)
                ws.row_dimensions[rw].height = 26
                rw += 1
            return rw

        def _matrix(rw, heading, per_cam, fleet, fmt, note=None, heat=True):
            """One hour x lift matrix. Dark hours read '—' in every matrix."""
            rw = section(ws, rw, heading)
            if note:
                cell(ws, rw, 1, note, font=BODY_ITALIC, wrap=True)
                ws.merge_cells(start_row=rw, start_column=1, end_row=rw,
                               end_column=len(cams) + 2)
                ws.row_dimensions[rw].height = 30
                rw += 1
            rw = _lift_headers(rw, "hour (IST)")
            first = rw
            for h in hours:
                cell(ws, rw, 1, f"{h:02d}:00", font=BODY_BOLD)
                for i, cam in enumerate(cams, start=2):
                    v = per_cam[cam][h]
                    if v is None:
                        cell(ws, rw, i, DARK, fill=FILL_NA)
                    else:
                        cell(ws, rw, i, v, fmt)
                fv = fleet[h]
                if fv is None:
                    cell(ws, rw, len(cams) + 2, DARK, fill=FILL_NA)
                else:
                    cell(ws, rw, len(cams) + 2, fv, fmt, font=BODY_BOLD)
                rw += 1
            if heat:
                style.heatmap(ws, 2, len(cams) + 1, first, rw - 1)
            return rw, first, rw - 1

        covs = [ctx["coverage_pct"].get(c, 0.0) for c in cams]
        totals_note = (
            f"Whole people, as counted. These totals are NOT comparable "
            f"between lifts: coverage ranges "
            f"{min(covs):.0f}%–{max(covs):.0f}% across these lifts, so a lift "
            f"watched for longer will show a bigger total whether or not it "
            f"carried more. Compare lifts on the MEAN matrix above; use these "
            f"totals to see the raw volume behind each mean."
            if covs else "Whole people, as counted.")
        mean_note = ("Averaged over the days each lift was actually observed in "
                     "that hour, which is what makes these figures comparable "
                     "between lifts of different coverage. A fraction of a "
                     "person is an average across days, not a part-person.")

        # ── BOARDINGS: mean, then the totals behind it
        row, b_first, b_last = _matrix(
            row, "MEAN BOARDINGS PER OBSERVED DAY, BY HOUR",
            e["boarded"], e["fleet_boarded"], "0.0", note=mean_note)
        if first_matrix_range is None:
            first_matrix_range = _rng_cols(b_first, b_last, 1, len(cams) + 2)
        row += 1
        row, _f, _l = _matrix(
            row, "TOTAL OBSERVED BOARDINGS, BY HOUR",
            e["total_boarded_hr"], e["fleet_total_boarded_hr"], F_INT,
            note=totals_note, heat=False)
        row += 1

        # ── ALIGHTINGS: mean, then the totals behind it
        row, _f, _l = _matrix(
            row, "MEAN ALIGHTINGS PER OBSERVED DAY, BY HOUR",
            e["alighted"], e["fleet_alighted"], "0.0", note=mean_note)
        row += 1
        row, _f, _l = _matrix(
            row, "TOTAL OBSERVED ALIGHTINGS, BY HOUR",
            e["total_alighted_hr"], e["fleet_total_alighted_hr"], F_INT,
            note=totals_note, heat=False)
        row += 1

        # ── OBSERVED DAYS
        row = section(ws, row, "OBSERVED DAYS BEHIND EACH CELL ABOVE")
        cell(ws, row, 1, f"How many days contributed to each mean. 0 means the "
                         f"lift was never observed in that hour, which is why "
                         f"the cell above reads '{DARK}'. Out of "
                         f"{demand['n_days_in_range']} days in range.",
             font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row,
                       end_column=10)
        ws.row_dimensions[row].height = 26
        row += 1
        row = _lift_headers(row, "hour (IST)")
        for h in hours:
            cell(ws, row, 1, f"{h:02d}:00", font=BODY_BOLD)
            for i, cam in enumerate(cams, start=2):
                n = e["observed_days"][cam][h]
                cell(ws, row, i, n, F_INT, fill=FILL_NA if n == 0 else None)
            cell(ws, row, len(cams) + 2, e["fleet_lifts_contributing"][h], F_INT)
            row += 1
        row += 1

        # ── PEAK ROW
        row = section(ws, row, "BUSIEST HOUR PER LIFT")
        is_canonical_era = bool(canon) and canon.get("era") == ver
        cell(ws, row, 1,
             ("The FLEET row here is the canonical busiest hour stated at the "
              "top of this sheet — same definition, same value."
              if is_canonical_era else
              f"NOTE — this block covers counting era {ver}, which is NOT the "
              f"era the canonical busiest hour is drawn from (that is "
              f"{canon['era'] if canon else 'n/a'}, the era carrying the most "
              f"boardings). The FLEET row below is this era's busiest hour on "
              f"the same definition, and will differ."),
             font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        ws.row_dimensions[row].height = 30
        row += 1
        hdr = row
        row = header_row(ws, row, [
            "lift", "busiest hour (IST)", "mean boardings in it",
            "all-day mean per observed hour", "peak : mean",
            "observed hours", "precision (count accuracy)"])
        for cam in list(cams) + ["__fleet__"]:
            pk = e["peaks"][cam]
            is_fleet = cam == "__fleet__"
            cell(ws, row, 1, "FLEET (all lifts, one tower)" if is_fleet
                 else _lbl(ws, cam), font=BODY_BOLD if is_fleet else BODY)
            cell(ws, row, 2, f"{pk['hour']:02d}:00" if pk["hour"] is not None
                 else DARK)
            cell(ws, row, 3, pk["value"], "0.0")
            cell(ws, row, 4, pk["mean"], "0.0")
            cell(ws, row, 5, pk["ratio"], "0.00")
            cell(ws, row, 6, pk["n_hours"], F_INT)
            if is_fleet:
                cell(ws, row, 7, "see per-lift rows — precision is per lift")
            else:
                p = precision_str(ctx["validation"], cam, ver)
                cell(ws, row, 7, p, wrap=True,
                     fill=FILL_NA if "unvalidated" in p else None)
            row += 1
        anchors.setdefault("demand_peaks", ("DEMAND BY LIFT AND HOUR",
                                            _rng(hdr + 1, row - 1, 7)))
        row += 1

        # ── PEAK DEMAND vs the handling-capacity assumption
        row = section(ws, row, "PEAK 5-MINUTE DEMAND vs THE MEP-02 HANDLING-"
                               "CAPACITY ASSUMPTION")
        if not pop:
            row = banner(ws, row, (
                "BLOCKED — no population figure was supplied to this export, so "
                "peak demand cannot be expressed as a % of the building's "
                "population and cannot be compared to the "
                f"{eras.HC_PEAK_DESIGN_PCT:.0f}% handling-capacity assumption. "
                "Raw 5-minute counts are given below instead. Re-run with "
                "--population N to unblock. The population is NEVER guessed."),
                fill=FILL_NA, height=44)
        hdr = row
        cols = ["lift", "busiest 5 minutes (boardings)", "when"]
        if pop:
            cols += [f"as % of population ({pop:,})",
                     f"vs {eras.HC_PEAK_DESIGN_PCT:.0f}% design assumption",
                     "verdict"]
        row = header_row(ws, row, cols)
        for cam in list(cams) + ["__fleet__"]:
            best = p5[cam]
            is_fleet = cam == "__fleet__"
            cell(ws, row, 1, "FLEET (all lifts, one tower)" if is_fleet
                 else _lbl(ws, cam), font=BODY_BOLD if is_fleet else BODY)
            cell(ws, row, 2, best["boardings"], F_INT)
            cell(ws, row, 3, _fmt_ts(best["w0"])[:16] if best["w0"] else DARK)
            if pop:
                frac = best["boardings"] / pop
                over = 100.0 * frac > eras.HC_PEAK_DESIGN_PCT
                cell(ws, row, 4, frac, F_PCT)
                cell(ws, row, 5, eras.HC_PEAK_DESIGN_PCT / 100.0, F_PCT)
                cell(ws, row, 6, "over design" if over else "within design",
                     fill=style.FILL_BAD if over else style.FILL_GOOD)
            row += 1
        row += 1

        # ── LOAD BALANCE
        row = section(ws, row, "LOAD BALANCE ACROSS LIFTS, BY HOUR")
        cell(ws, row, 1,
             "Coefficient of variation of boardings across the lifts observed "
             "in each hour: 0 means every observed lift carried the same load, "
             "higher means the load sat on some lifts more than others. READ "
             "WITH CARE — uneven COVERAGE can masquerade as uneven LOAD. This "
             "figure is only interpretable across lifts of similar coverage %, "
             "and the coverage strip under each column header is where to check "
             "that. Hours with fewer than two observed lifts are blank: a "
             "spread across one lift is not a spread.",
             font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
        ws.row_dimensions[row].height = 58
        row += 1
        covs = [ctx["coverage_pct"].get(c, 0.0) for c in cams]
        if covs and (max(covs) - min(covs)) > 20:
            row = banner(ws, row, (
                f"⚠ Coverage across these lifts ranges {min(covs):.0f}%–"
                f"{max(covs):.0f}% — a {max(covs) - min(covs):.0f}-point spread. "
                f"The load-balance figures below are NOT safely comparable "
                f"across lifts at this spread."), height=32)
        if not model.load_balance_reportable(e["cv"]):
            usable = sum(1 for c in e["cv"] if c is not None)
            row = banner(ws, row, (
                f"NOT REPORTED — a load-balance figure needs at least "
                f"{model.LOAD_BALANCE_MIN_LIFTS} lifts observed in the same "
                f"hour, and only {usable} of 24 hours meet that in this "
                f"counting era. Below that, the measure is degenerate: with "
                f"two lifts where one carried nobody it returns 1.41 whatever "
                f"the numbers are, which reads as a dramatic imbalance and "
                f"means nothing. The per-hour table is therefore withheld."),
                fill=FILL_NA, height=58)
            row += 1
        else:
            hdr = row
            row = header_row(ws, row, [
                "hour (IST)", "lifts observed", "coefficient of variation",
                "reading"])
            for h in hours:
                cv = e["cv"][h]
                if cv is None:
                    continue            # below the minimum: no row at all
                n_live = sum(1 for c in cams if e["boarded"][c][h] is not None)
                cell(ws, row, 1, f"{h:02d}:00", font=BODY_BOLD)
                cell(ws, row, 2, n_live, F_INT)
                cell(ws, row, 3, cv, "0.00")
                cell(ws, row, 4, ("shared fairly evenly" if cv < 0.25
                                  else "moderately uneven" if cv < 0.6
                                  else "concentrated on some lifts"))
                row += 1
            cell(ws, row, 1, f"Hours with fewer than "
                             f"{model.LOAD_BALANCE_MIN_LIFTS} lifts observed "
                             f"together are omitted rather than shown as a "
                             f"number — the measure is not meaningful there.",
                 font=BODY_ITALIC, wrap=True)
            ws.merge_cells(start_row=row, start_column=1, end_row=row,
                           end_column=8)
            row += 1
        row += 2

        # ── charts for this era
        by_lift = {eras.lift_label_with_channel(c): e["boarded"][c] for c in cams}
        row = caption(ws, row, (
            "Take-away: the shape of the day — which hours carry the load, and "
            "whether the lifts rise and fall together. Gaps in a lift's bars "
            "are hours it was not observed, not hours it was idle."))
        ws.add_chart(charts.demand_by_hour_grouped(
            cd, [f"{h:02d}:00" for h in hours], by_lift,
            f"Mean boardings by hour and lift — {ver}"), f"A{row}")
        anchors[("demand_chart", ver)] = (
            "DEMAND BY LIFT AND HOUR",
            f"'Mean boardings by hour and lift' at A{row}")
        row += charts.rows_for(10.0)

        pk = e["peaks"]["__fleet__"]
        if pk["hour"] is None:
            cap = "Take-away: no hour in this counting era carried boardings."
        elif is_canonical_era:
            cap = (f"Take-away: the busiest hour is "
                   f"{pk['hour']:02d}:00–{pk['hour'] + 1:02d}:00, carrying "
                   f"{pk['value']:.0f} boardings against an all-day mean of "
                   f"{pk['mean']:.0f} — {pk['ratio']:.1f}x the typical hour. "
                   f"This is the canonical figure stated at the top of this "
                   f"sheet.")
        else:
            # Deliberately NOT phrased as "the busiest hour is": this era taken
            # alone is a narrower view, and only the canonical figure gets to
            # make the unqualified claim.
            cap = (f"Take-away: counting era {ver} TAKEN ALONE peaks at "
                   f"{pk['hour']:02d}:00–{pk['hour'] + 1:02d}:00 with "
                   f"{pk['value']:.0f} boardings ({pk['ratio']:.1f}x its own "
                   f"all-day mean). This era is not the one the canonical "
                   f"busiest hour is drawn from — it carries less data — so "
                   f"this is a view of one build, not the workbook's "
                   f"busiest-hour figure.")
        row = caption(ws, row, cap)
        ws.add_chart(charts.fleet_demand_line(
            cd, [f"{h:02d}:00" for h in hours], e["fleet_boarded"],
            pk["hour"], f"Fleet boardings by hour — {ver}"), f"A{row}")
        row += charts.rows_for(9.0)
        row += 2

    # ── what this sheet still cannot show
    row = section(ws, row, "WHAT THIS SHEET STILL CANNOT SHOW")
    cell(ws, row, 1,
         "Demand PER FLOOR — where people get on and off — is not here because "
         "floor attribution produced no confident reads at all in this range "
         "(see TIER-2 EVIDENCE). That blocks origin/destination demand, the "
         "C21/C22 speed factors and the C17/C18 probable-stops coefficients. "
         "Per-lift and per-hour demand, shown above, is not blocked by it.",
         font=BODY_BOLD, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
    ws.row_dimensions[row].height = 46
    row += 2

    # ── reconciliation against PEAK ANALYSIS
    row = section(ws, row, "RECONCILIATION WITH PEAK ANALYSIS")
    total_here = sum(e["total_boarded"] for e in demand["by_era"].values())
    total_peak_sheet = sum(p["day_boardings"] for p in ctx["peaks"])
    cell(ws, row, 1, "boardings counted on this sheet (all counting eras)",
         font=BODY_BOLD)
    cell(ws, row, 2, total_here, F_INT)
    row += 1
    cell(ws, row, 1, "boardings counted on PEAK ANALYSIS (sum of day boardings)",
         font=BODY_BOLD)
    cell(ws, row, 2, total_peak_sheet, F_INT)
    row += 1
    agree = total_here == total_peak_sheet
    cell(ws, row, 1, "agree?", font=BODY_BOLD)
    cell(ws, row, 2, "YES — same gap-excluded boarding stream" if agree
         else f"NO — differ by {abs(total_here - total_peak_sheet):,}",
         fill=style.FILL_GOOD if agree else style.FILL_BAD)
    row += 1
    cell(ws, row, 1,
         "The peak RATIOS on the two sheets are not the same number and are not "
         "meant to be: PEAK ANALYSIS compares the busiest 5 minutes of a day to "
         "that day's average 5 minutes, while this sheet compares the busiest "
         "HOUR to the average hour. Both rest on the identical boarding stream, "
         "reconciled above.", font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
    ws.row_dimensions[row].height = 44

    anchors["demand_matrix"] = ("DEMAND BY LIFT AND HOUR",
                                first_matrix_range or "—")
    style.set_widths(ws, {1: 30})
    for i in range(2, len(cams) + 3):
        style.set_widths(ws, {i: 15})
    style.print_setup(ws)
    return ws


def _rng_cols(first_row, last_row, first_col, last_col) -> str:
    return (f"{get_column_letter(first_col)}{first_row}:"
            f"{get_column_letter(last_col)}{last_row}")


# ── PEAK ANALYSIS ────────────────────────────────────────────────────────────


def sheet_demand_log(wb, ctx, anchors):
    """DEMAND LOG — the flat, per-hour table. The same rows the CSV carries.

    This is deliberately the least interpreted sheet in the workbook: no means, no modelling, no
    charts. One row per hour per lift plus a FLEET row per hour, so a reader can check any headline
    figure elsewhere in this workbook against the counts it came from."""
    log = ctx.get("demand_log")
    if not log:
        return
    ws = wb.create_sheet("DEMAND LOG")
    row = title(ws, "Demand log — hourly counts, exactly as recorded")
    row += 1
    for line in ctx.get("demand_log_notes", []):
        cell(ws, row, 1, line, font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
        ws.row_dimensions[row].height = 30
        row += 1
    row += 1

    row = section(ws, row, "HOURLY — one row per lift per hour, plus FLEET")
    hdr = [h for _, h in demand_log.HOURLY_COLS]
    header_row(ws, row, hdr)
    first = row + 1
    row += 1
    # DARK is a string, so it never needs a number format; the counts always do — this workbook
    # forbids a bare number landing in a General-format cell.
    for r in log["hourly"]:
        for i, (k, _) in enumerate(demand_log.HOURLY_COLS, start=1):
            v = r.get(k)
            cell(ws, row, i, "" if v is None else v,
                 fmt=(F_INT if isinstance(v, (int, float)) else None),
                 font=BODY_BOLD if r["camera"] == demand_log.FLEET else BODY,
                 fill=FILL_NA if v == demand_log.DARK else None)
        row += 1
    anchors["demand_log_hourly"] = (ws.title, _rng(first, row - 1, len(hdr)))
    freeze_below(ws, first)
    row += 2

    row = section(ws, row, "DAILY ROLLUP")
    hdr2 = [h for _, h in demand_log.DAILY_COLS]
    header_row(ws, row, hdr2)
    first2 = row + 1
    row += 1
    for r in log["daily"]:
        for i, (k, _) in enumerate(demand_log.DAILY_COLS, start=1):
            v = r.get(k)
            if k == "coverage_pct":
                # F_PCT supplies the '%' and expects a FRACTION. The CSV column is literally
                # 'coverage %' and carries 0-100, so it is scaled here and ONLY here.
                cell(ws, row, i, "" if v is None else v / 100.0,
                     fmt=(F_PCT if v is not None else None))
            else:
                cell(ws, row, i, "" if v is None else v,
                     fmt=(F_INT if isinstance(v, (int, float)) else None))
        row += 1
    anchors["demand_log_daily"] = (ws.title, _rng(first2, row - 1, len(hdr2)))

    for i, w in enumerate((18, 10, 22, 10, 10, 30, 34, 10), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return row

def sheet_peak(wb, ctx, cd, anchors):
    from .model import aggregate_cycles
    ws = wb.create_sheet("PEAK ANALYSIS")
    row = title(ws, "Peak analysis — worst 5-min boarding window per day"
                + (f" (fixed window {ctx['peak_window_spec']})"
                   if ctx["peak_window_spec"] not in (None, "", "auto") else ""))
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    if ctx.get("suspected_gaps"):
        row = banner(ws, row, (
            f"⚠ {len(ctx['suspected_gaps'])} suspected undeclared quiet "
            f"window(s) are NOT excluded from the averages below. A quiet "
            f"period counted as live time deflates the average and inflates "
            f"every peak:average ratio on this sheet. See COVERAGE & ERAS."),
            height=44)
        row += 1
    pop = ctx.get("population")
    headers = ["day (IST)", "peak window", "boardings in window", "day boardings",
               "avg 5-min boardings (gap-excl)", "peak : average",
               "peak demand % of population"
               + ("" if pop else " (population not supplied)"),
               f"vs {eras.HC_PEAK_DESIGN_PCT:.0f}% HC design"]
    hdr_row = row
    row = header_row(ws, row, headers)
    first_data = row
    for p in ctx["peaks"]:
        pk = p["peak"]
        cell(ws, row, 1, p["day"])
        cell(ws, row, 2, (f"{_fmt_ts(pk['w0'])[11:16]}–{_fmt_ts(pk['w1'])[11:16]}"
                          if pk else "no boardings"))
        cell(ws, row, 3, pk["boardings"] if pk else 0, F_INT)
        cell(ws, row, 4, p["day_boardings"], F_INT)
        cell(ws, row, 5, p["avg_5min"], F_RATE)
        cell(ws, row, 6, p["peak_to_avg"], F_RATE)
        if pop and pk:
            frac = pk["boardings"] / pop
            over = 100.0 * frac > eras.HC_PEAK_DESIGN_PCT
            cell(ws, row, 7, frac, F_PCT)
            cell(ws, row, 8, "over design" if over else "within design",
                 fill=style.FILL_BAD if over else style.FILL_GOOD)
        else:
            cell(ws, row, 7, None)
            cell(ws, row, 8, "population not supplied — % left blank" if pk else "",
                 fill=FILL_NA if pk else None)
        row += 1
    anchors["peak_table"] = ("PEAK ANALYSIS",
                             _rng(first_data, max(first_data, row - 1),
                                  len(headers)))
    freeze_below(ws, hdr_row)
    row += 1

    row = section(ws, row, "Coefficients INSIDE daily peak windows vs ALL-DAY, "
                           "per instrument era (peak-window cycles pooled "
                           "across days within one era only)")
    row = header_row(ws, row, [
        "lift", "instrument", "era", "scope", "clean closes n", "close median s",
        "dwell n", "dwell median s"])
    windows = [(p["peak"]["w0"], p["peak"]["w1"]) for p in ctx["peaks"] if p["peak"]]

    def _in_peak(ts):
        return any(w0 <= ts < w1 for w0, w1 in windows)

    peak_cycles = [c for c in ctx["all_cycles"] if _in_peak(c["ts"])]
    peak_aggs = aggregate_cycles(peak_cycles, ctx["t0"], ctx["t1"],
                                 min_close_s=ctx["min_close_s"])
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        pa = peak_aggs.get((cam, instrument, era_id))
        for scope, src in (("peak windows", pa), ("all-day", a)):
            if src is None:
                continue
            cell(ws, row, 1, _lbl(ws, cam))
            cell(ws, row, 2, instrument)
            cell(ws, row, 3, era_id)
            cell(ws, row, 4, scope)
            cell(ws, row, 5, src["close"]["n"], F_INT)
            cell(ws, row, 6, src["close"]["median_ci"]["median"], F_SEC)
            cell(ws, row, 7, src["dwell"]["n"], F_INT)
            cell(ws, row, 8, src["dwell"]["median_ci"]["median"], F_SEC)
            row += 1
    style.autofit(ws, wrap_cols=(8,))
    style.set_widths(ws, {1: 16, 2: 18, 3: 22, 7: 26, 8: 26})
    style.print_setup(ws, repeat_row=hdr_row)
    return ws


# ── RAW ──────────────────────────────────────────────────────────────────────

def sheet_raw(wb, ctx, anchors):
    ws = wb.create_sheet("RAW")
    row = title(ws, "Raw era-tagged export — the audit trail. No aggregation.")
    row += 1
    row = _era_banner(ws, row, ctx["boundaries"])
    headers = ["ts (IST)", "ts_epoch", "channel", "lift_label", "bank",
               "instrument", "counting_version", "era_id", "event_type",
               "door_open_ts", "door_close_ts", "close_travel_s",
               "cycle_class/quality", "boarded", "alighted", "floor",
               "floor_source", "precision_at_time", "in_declared_gap",
               "below_min_close_floor"]
    hdr_row = row
    row = header_row(ws, row, headers)
    first_data = row
    keys = ("ts_ist", "ts_epoch", "channel", "lift_label", "bank", "instrument",
            "counting_version", "era_id", "event_type", "door_open_ts",
            "door_close_ts", "close_travel_s", "cycle_class", "boarded",
            "alighted", "floor", "floor_source", "precision_at_time", "in_gap",
            "below_floor")
    fmts = {"close_travel_s": F_SEC, "ts_epoch": "0.000",
            "boarded": F_INT, "alighted": F_INT}
    for r in ctx["raw_rows"]:
        for i, key in enumerate(keys, start=1):
            cell(ws, row, i, r.get(key), fmts.get(key))
        row += 1
    last = row - 1
    anchors["raw_table"] = ("RAW", _rng(first_data, max(first_data, last),
                                        len(headers)))
    style.set_widths(ws, {i: 18 for i in range(1, len(headers) + 1)})
    style.set_widths(ws, {1: 22, 7: 30, 18: 34, 10: 26, 11: 26})
    freeze_below(ws, hdr_row, col=4)
    style.autofilter(ws, hdr_row, len(headers), last)
    style.print_setup(ws, repeat_row=hdr_row)
    return ws


# ── COVERAGE & ERAS ──────────────────────────────────────────────────────────

def sheet_coverage(wb, ctx, cd, anchors):
    ws = wb.create_sheet("COVERAGE & ERAS")
    row = title(ws, "Coverage, era boundaries, and excluded gap windows")
    row += 1

    row = section(ws, row, "ERA BOUNDARIES CROSSED BY THIS RANGE")
    if ctx["boundaries"]:
        row = header_row(ws, row, ["boundary (ts)", "what changed",
                                   "rows before", "rows after"])
        for b in ctx["boundaries"]:
            cell(ws, row, 1, b["boundary"])
            cell(ws, row, 2, b["kind"], wrap=True)
            cell(ws, row, 3, b["rows_before"], F_INT)
            cell(ws, row, 4, b["rows_after"], F_INT)
            ws.row_dimensions[row].height = 40
            row += 1
    else:
        cell(ws, row, 1, "none — the whole range lies inside single eras")
        row += 1
    row += 1

    row = section(ws, row, "DECLARED DATA GAPS OVERLAPPING THIS RANGE "
                           "(excluded from all rate/duration statistics)")
    row = header_row(ws, row, ["start", "end", "channels affected", "reason"])
    n_gaps = 0
    for g in eras.DATA_GAPS:
        if g["start_epoch"] < ctx["t1"] and g["end_epoch"] > ctx["t0"]:
            cell(ws, row, 1, g["start"])
            cell(ws, row, 2, g["end"])
            cell(ws, row, 3, ", ".join(g["cams"]) if g["cams"] else "ALL")
            cell(ws, row, 4, g["reason"], wrap=True)
            ws.row_dimensions[row].height = 40
            row += 1
            n_gaps += 1
    if not n_gaps:
        cell(ws, row, 1, "none")
        row += 1
    row += 1

    # ── auto-detected suspects: flagged, never auto-excluded
    row = section(ws, row, "AUTO-DETECTED SUSPECTED GAPS — FLAGGED ONLY, "
                           "NOT EXCLUDED")
    row = banner(ws, row, (
        "These windows were found automatically: a channel went silent while it "
        "was producing data on both sides. They are NOT excluded from any "
        "statistic. Declaring a gap stays a human decision — check the "
        "operations record, then append confirmed outages to "
        "eras.py::DATA_GAPS and re-export."), height=44)
    susp = ctx.get("suspected_gaps") or []
    if susp:
        row = header_row(ws, row, ["channel", "kind", "start", "end",
                                   "hours silent", "hours already declared",
                                   "hours unexplained", "detail"])
        for g in susp:
            cell(ws, row, 1, g["cam"])
            cell(ws, row, 2, g["kind"])
            cell(ws, row, 3, _dt(g["start"]), F_TS)
            cell(ws, row, 4, _dt(g["end"]), F_TS)
            cell(ws, row, 5, g["span_s"] / 3600.0, F_RATE)
            cell(ws, row, 6, g["declared_s"] / 3600.0, F_RATE)
            cell(ws, row, 7, g["undeclared_s"] / 3600.0, F_RATE,
                 fill=FILL_WARN if g["undeclared_s"] > 3600 else None)
            cell(ws, row, 8, g["detail"], wrap=True)
            row += 1
    else:
        cell(ws, row, 1, "none detected in this range")
        row += 1
    row += 1

    # ── floor rejection per channel/era
    row = section(ws, row, "CYCLES REJECTED AT THE ONE-FRAME QUANTIZATION FLOOR")
    cell(ws, row, 1, (f"Closes shorter than {ctx['min_close_s']:.2f}s are "
                      f"rejected from every close-travel statistic: a lift door "
                      f"cannot close in one video frame, so a value that short "
                      f"is the door state flickering between frames. The "
                      f"sampling resolution is detected from the data on every "
                      f"run, never assumed."), font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 30
    row += 1
    hdr = row
    row = header_row(ws, row, [
        "channel", "instrument", "era", "detected frame quantum s",
        "quantum source", "clean closes before filter", "rejected at floor",
        "% of pool rejected", "close values at the quantum",
        "open values at the quantum"])
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl, ot = a["close"], a["open_travel"]
        cell(ws, row, 1, cam)
        cell(ws, row, 2, instrument)
        cell(ws, row, 3, era_id)
        cell(ws, row, 4, a["quantum_s"], F_SEC_PLAIN)
        cell(ws, row, 5, a["quantum_source"], wrap=True)
        cell(ws, row, 6, cl["n_prefilter"], F_INT)
        cell(ws, row, 7, cl["n_floor_rejected"], F_INT)
        cell(ws, row, 8, cl["floor_reject_frac"], F_PCT,
             fill=FILL_WARN if cl["floor_reject_warn"] else None)
        cell(ws, row, 9, cl["at_quantum"]["frac"], F_PCT,
             fill=FILL_NA if cl["suppressed"] else None)
        cell(ws, row, 10, ot["at_quantum"]["frac"], F_PCT,
             fill=FILL_NA if ot["suppressed"] else None)
        row += 1
    style.wrap_column(ws, 5, hdr + 1, row - 1, width=32)
    row += 1

    # ── how each lift got (or did not get) a bank
    row = section(ws, row, "BANK ASSIGNMENT — DERIVATION AND EVIDENCE")
    cell(ws, row, 1,
         "MEP-02 treats each bank as a separate design case, so which lifts "
         "share a bank changes what may be compared with what. A bank is "
         "derived ONLY from camera_registry.floor_range (an operator-entered "
         "fact): lifts serving the same floor range are one bank. Where that "
         "field is empty the lift stays UNKNOWN — a bank is never inferred "
         "from channel number or from observed floors.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 46
    row += 1
    hdr = row
    row = header_row(ws, row, ["channel", "bank", "floor_range (the evidence)",
                               "how it was assigned", "shares this range with"])
    ev_all = ctx.get("bank_evidence") or {}
    n_derived = 0
    for cam in ctx["cams"]:
        ev = ev_all.get(cam, {})
        bank = ctx["banks"].get(cam) or ""
        if bank:
            n_derived += 1
        cell(ws, row, 1, cam)
        cell(ws, row, 2, bank or "UNKNOWN",
             fill=None if bank else FILL_WARN)
        cell(ws, row, 3, ev.get("floor_range") or "(empty)",
             fill=None if ev.get("floor_range") else FILL_NA)
        cell(ws, row, 4, ev.get("source", ""), wrap=True)
        cell(ws, row, 5, ", ".join(ev.get("shared_with") or []) or "—")
        row += 1
    style.wrap_column(ws, 4, hdr + 1, row - 1, width=52)
    if n_derived == 0:
        row = banner(ws, row, (
            f"⚠ NO lift could be assigned a bank: camera_registry.floor_range "
            f"is empty for all {len(ctx['cams'])} channels, and "
            f"lift_banks.json carries no explicit assignment either. Every "
            f"fleet figure in this workbook therefore pools across whatever "
            f"banks exist and may mix design cases. To fix: enter each lift's "
            f"served floor range in the camera registry, or fill in "
            f"lift_banks.json, and re-export."), height=46)
    row += 1

    row = section(ws, row, "PER-CHANNEL COVERAGE (15-min buckets with ≥1 row, "
                           "over non-gap time) AND ROW COUNTS BY ERA")
    cov_hdr = row
    headers = ["channel", "coverage % (gap-adjusted)", "instrument", "era",
               "door cycles", "clean closes", "transits", "first row",
               "last row", "counting_version attribution"]
    row = header_row(ws, row, headers)
    cov_first = row
    for cam in ctx["cams"]:
        cam_aggs = sorted((k, a) for k, a in ctx["aggs"].items() if k[0] == cam)
        t_by_ver = sorted((k, d) for k, d in ctx["transit_aggs"].items()
                          if k[0] == cam)
        cov = ctx["coverage_pct"].get(cam, 0.0) / 100.0
        attribution = ctx["cv_attribution"].get(cam, "declared epochs")
        if not cam_aggs and not t_by_ver:
            cell(ws, row, 1, cam)
            cell(ws, row, 2, cov, F_PCT)
            cell(ws, row, 3, "—")
            cell(ws, row, 4, "ZERO ROWS in this range", fill=FILL_WARN)
            cell(ws, row, 10, attribution)
            row += 1
            continue
        for (c_, instrument, era_id), a in cam_aggs:
            cell(ws, row, 1, cam)
            cell(ws, row, 2, cov, F_PCT)
            cell(ws, row, 3, instrument)
            cell(ws, row, 4, era_id)
            cell(ws, row, 5, a["n_cycles"], F_INT)
            cell(ws, row, 6, a["close"]["n"], F_INT)
            cell(ws, row, 8, _dt(a["first_ts"]), F_TS)
            cell(ws, row, 9, _dt(a["last_ts"]), F_TS)
            cell(ws, row, 10, attribution)
            row += 1
        for (c_, ver), d in t_by_ver:
            cell(ws, row, 1, cam)
            cell(ws, row, 2, cov, F_PCT)
            cell(ws, row, 3, "gpu_engine (counting)")
            cell(ws, row, 4, f"counting: {ver}")
            cell(ws, row, 7, d["n"], F_INT)
            cell(ws, row, 8, _dt(d["first_ts"]), F_TS)
            cell(ws, row, 9, _dt(d["last_ts"]), F_TS)
            cell(ws, row, 10, attribution)
            row += 1
    anchors["coverage_table"] = ("COVERAGE & ERAS",
                                 _rng(cov_first, max(cov_first, row - 1),
                                      len(headers)))
    row += 1
    if ctx["version_mismatches"]:
        row = banner(ws, row, "⚠ COUNTING-VERSION CROSS-CHECK MISMATCHES "
                              "(declared epoch vs live analyzer_status / "
                              "camera_validation):", height=18)
        for m in ctx["version_mismatches"]:
            cell(ws, row, 1, m, wrap=True)
            ws.row_dimensions[row].height = 40
            row += 1
        row += 1

    # ── coverage timeline, with declared outages shaded distinctly
    days = ctx["coverage_daily"]["days"]
    if days:
        gap_share = []
        for d_label in days:
            d0 = datetime.fromisoformat(d_label).replace(tzinfo=eras.IST)
            s = max(d0.timestamp(), ctx["t0"])
            e = min((d0 + timedelta(days=1)).timestamp(), ctx["t1"])
            span = max(1.0, e - s)
            # fleet-wide declared outage share of the day
            share = min(1.0, eras.gap_overlap_s(ctx["cams"][0], s, e) / span)
            gap_share.append(round(share, 4))
        row = caption(ws, row, (
            "Take-away: where a line dips, that channel stopped producing data. "
            "Grey bars mark time already DECLARED as an outage — a dip over a "
            "grey bar is explained; a dip with no grey bar under it is not, and "
            "should be checked against the suspected-gaps table above."))
        ch = charts.coverage_timeline(cd, days,
                                      ctx["coverage_daily"]["by_cam"], gap_share)
        ws.add_chart(ch, f"A{row}")
        anchors["coverage_chart"] = ("COVERAGE & ERAS",
                                     f"'Daily coverage by channel' at A{row}")
    style.autofit(ws, max_row=cov_hdr, wrap_cols=(2, 4, 5))
    style.set_widths(ws, {1: 14, 2: 24, 3: 22, 4: 30, 5: 30, 8: 20, 9: 20,
                          10: 30})
    style.print_setup(ws)
    return ws


# ── TIER-2 EVIDENCE ───────────────────────────────────────────────────────────

def sheet_tier2(wb, ctx, anchors):
    """TIER-2 EVIDENCE — what floor attribution actually produced, and what each floor-dependent
    coefficient is really blocked on.

    This sheet used to be titled TIER-2 BLOCKED and reported ZERO confident reads on every camera,
    because the confident-read test excluded reason='single_panel' — which is what a
    single-panel-calibrated camera emits for every good read. ch27/ch29/ch30 held roughly 150k
    confident reads while the sheet said none. The verdict (C17/C18/C21/C22 not measurable) has NOT
    changed; the evidence and the reasoning have, and each coefficient now names its own blocker
    instead of sharing one that was wrong for all four."""
    ws = wb.create_sheet("TIER-2 EVIDENCE")
    row = title(ws, "Floor attribution — what was actually read, and what each "
                    "floor-dependent coefficient is blocked on")
    row += 1
    cell(ws, row, 1,
         "'Confident' means a non-null floor with reason in "
         + "/".join(r or "(blank)" for r in eras.FLOOR_OK_REASONS)
         + ". single_panel is a read from the ONE calibrated panel that passed the same per-panel "
           "bar as each half of an 'ok' — it lacks the second panel's cross-check, not quality. "
           "Counting it as non-confident reported cameras holding tens of thousands of reads as "
           "floor-blind. The per-reason census sits beside the count so the weaker assurance stays "
           "visible: a single panel cannot catch a SYSTEMATIC misread, which is what the derived "
           "floor alphabet defends against.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 58
    row += 2

    row = section(ws, row, "READ QUALITY, PER CAMERA PER ERA")
    cell(ws, row, 1, "Split by era on purpose: a template rebuild is a different instrument, and a "
                     "per-camera total would hide a camera that lost floor reading at a rebuild "
                     "behind its own earlier history.", font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 30
    row += 1
    hdr_row = row
    row = header_row(ws, row, ["channel", "era", "door rows", "confident reads", "confident %",
                               "no_read", "ambiguous", "invalid_label", "other"])
    first = row
    ev = ctx.get("tier2_evidence") or {}
    for (cam, era), d in sorted(ev.items()):
        if not d["rows"]:
            continue
        cell(ws, row, 1, cam)
        cell(ws, row, 2, era)
        cell(ws, row, 3, d["rows"], F_INT)
        cell(ws, row, 4, d["confident"], F_INT,
             fill=FILL_NA if not d["confident"] else None)
        cell(ws, row, 5, (d["confident"] / d["rows"]) if d["rows"] else 0, F_PCT)
        cell(ws, row, 6, d["no_read"], F_INT)
        cell(ws, row, 7, d["ambiguous"], F_INT)
        cell(ws, row, 8, d["invalid"], F_INT)
        cell(ws, row, 9, d["other"], F_INT)
        row += 1
    anchors["tier2_table"] = ("TIER-2 EVIDENCE", _rng(first, max(first, row - 1), 9))
    row += 2

    row = section(ws, row, "TRAVEL BETWEEN STOPS — FLOORS PER SECOND (not a speed factor)")
    cell(ws, row, 1,
         "Measured from consecutive confident reads that changed floor: |change in floor| / change "
         "in time, with direction taken from the floor INDEX change — NOT the arrow glyph, which "
         "is unreliable (see below). This is floors per SECOND. It is NOT C21/C22, which are a "
         "share of RATED speed: that conversion needs the inter-floor distance and the car's rated "
         "speed, and neither is held in this database. Same class of gap as C19's rated capacity.",
         font=BODY_ITALIC, wrap=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
    ws.row_dimensions[row].height = 48
    row += 1
    row = header_row(ws, row, ["channel", "era", "up segments n", "up median (fl/s)",
                               "down segments n", "down median (fl/s)", "unorderable floors",
                               "implausible rejected", ""])
    sp_first = row
    speed = ctx.get("floor_speed") or {}
    for (cam, era), v in sorted(speed.items()):
        if not (v["up"] or v["down"]):
            continue
        up, dn = sorted(v["up"]), sorted(v["down"])
        cell(ws, row, 1, cam)
        cell(ws, row, 2, era)
        cell(ws, row, 3, len(up), F_INT)
        cell(ws, row, 4, (up[len(up) // 2] if up else 0), F_SEC_PLAIN)
        cell(ws, row, 5, len(dn), F_INT)
        cell(ws, row, 6, (dn[len(dn) // 2] if dn else 0), F_SEC_PLAIN)
        cell(ws, row, 7, v["skipped_unmappable"], F_INT)
        cell(ws, row, 8, v["skipped_implausible"], F_INT)
        row += 1
    anchors["tier2_speed"] = ("TIER-2 EVIDENCE", _rng(sp_first, max(sp_first, row - 1), 8))
    row += 2

    row = section(ws, row, "ARROW DIRECTION ON CONFIDENT READS — why the up/down split is unsound")
    row = header_row(ws, row, ["channel", "era", "up", "down", "no arrow", "verdict", "", "", ""])
    ar_first = row
    for (cam, era), d in sorted(ev.items()):
        if not d["confident"]:
            continue
        tot = sum(d["arrow"].values()) or 1
        up = d["arrow"].get("up", 0)
        dn = d["arrow"].get("down", 0)
        na = tot - up - dn
        bad = bool((up or dn) and (not up or not dn or max(up, dn) > 0.95 * (up + dn)))
        cell(ws, row, 1, cam)
        cell(ws, row, 2, era)
        cell(ws, row, 3, up / tot, F_PCT)
        cell(ws, row, 4, dn / tot, F_PCT)
        cell(ws, row, 5, na / tot, F_PCT)
        cell(ws, row, 6,
             ("ONE-WAY ONLY — physically impossible; ROI or reader miscalibrated"
              if bad else "both directions seen"),
             fill=FILL_WARN if bad else None, wrap=True)
        row += 1
    anchors["tier2_arrow"] = ("TIER-2 EVIDENCE", _rng(ar_first, max(ar_first, row - 1), 6))
    row += 2

    row = section(ws, row, "VERDICT PER COEFFICIENT — unchanged, but now for the right reason")
    for cid in ("C17", "C18", "C21", "C22"):
        b = (ctx.get("coefficient_blockers") or {}).get(cid) or {}
        cell(ws, row, 1, cid, font=BODY_BOLD)
        cell(ws, row, 2, b.get("status", "BLOCKED"), font=BODY_BOLD, fill=FILL_NA)
        cell(ws, row, 3, b.get("blocker", ""), wrap=True)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=9)
        ws.row_dimensions[row].height = 30
        row += 1
        cell(ws, row, 3, b.get("evidence", ""), font=BODY_ITALIC, wrap=True)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=9)
        ws.row_dimensions[row].height = 66
        row += 1
        if b.get("measurable_as"):
            cell(ws, row, 3, "measurable instead as: " + b["measurable_as"], font=BODY_BOLD)
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=9)
            row += 1
        row += 1
    anchors["tier2_verdict"] = ("TIER-2 EVIDENCE", f"A{max(1, row - 1)}")

    style.set_widths(ws, {1: 12, 2: 22, 3: 16, 4: 16, 5: 14, 6: 16, 7: 18, 8: 18, 9: 14})
    freeze_below(ws, hdr_row)
    style.print_setup(ws, repeat_row=hdr_row)
    return ws
