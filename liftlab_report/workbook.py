"""Workbook rendering (openpyxl). All figures arrive pre-aggregated per era
from model.py; this file NEVER computes a cross-era number — if a cell would
need one it prints 'n/a — spans eras'.

Number formats are set explicitly; durations use 0.00 (never scientific).
Every figure row carries its n. Charts are native Excel charts fed from the
hidden DATA_CHARTS sheet.
"""

from __future__ import annotations

from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import eras, stats
from .eras import GPU_ENGINE, PI_WATCH
from .reader import precision_str

F_DUR = "0.00"
F_INT = "#,##0"
F_PCT = "0.0"
BOLD = Font(bold=True)
H1 = Font(bold=True, size=14)
WARN_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
ERA_FILL = PatternFill(start_color="FCE4EC", end_color="FCE4EC", fill_type="solid")
WRAP = Alignment(wrap_text=True, vertical="top")

NA_SPANS = "n/a — spans eras"


def _fmt_ts(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, eras.IST).strftime("%Y-%m-%d %H:%M:%S%z")


def _ci_str(lo, hi) -> str:
    if lo is None or hi is None:
        return "n too small"
    return f"[{lo:.2f}, {hi:.2f}]"


def _set(ws, row, col, value, fmt=None, font=None, fill=None):
    c = ws.cell(row=row, column=col, value=value)
    if fmt:
        c.number_format = fmt
    if font:
        c.font = font
    if fill:
        c.fill = fill
    return c


def _era_banner(ws, row, boundaries) -> int:
    if not boundaries:
        return row
    msg = ("⚠ RANGE CROSSES ERA BOUNDARIES — every aggregate on this sheet is "
           "PER ERA; pi_watch and gpu_engine figures are different instruments "
           "and are never pooled. See COVERAGE & ERAS.")
    c = _set(ws, row, 1, msg, font=Font(bold=True, color="9C0006"), fill=ERA_FILL)
    c.alignment = WRAP
    ws.row_dimensions[row].height = 30
    return row + 2


def _header_row(ws, row, headers):
    for i, h in enumerate(headers, start=1):
        _set(ws, row, i, h, font=BOLD)
    return row + 1


# ── DATA_CHARTS helpers ──────────────────────────────────────────────────────

class ChartData:
    """Sequential column blocks on a hidden sheet; returns openpyxl Reference
    ranges for chart series."""

    def __init__(self, wb):
        self.ws = wb.create_sheet("DATA_CHARTS")
        self.ws.sheet_state = "hidden"
        self.col = 1

    def block(self, title_rows: list[list]) -> tuple:
        """Write columns; title_rows[i] = [header, v1, v2, ...]. Returns
        (ws, first_col, last_col, n_rows)."""
        c0 = self.col
        nrows = 0
        for j, colvals in enumerate(title_rows):
            for i, v in enumerate(colvals):
                self.ws.cell(row=i + 1, column=c0 + j, value=v)
            nrows = max(nrows, len(colvals))
        self.col = c0 + len(title_rows) + 1
        return self.ws, c0, c0 + len(title_rows) - 1, nrows


def _bar_chart(title, cats_ref, data_ref, y_title="count"):
    ch = BarChart()
    ch.type = "col"
    ch.title = title
    ch.y_axis.title = y_title
    ch.x_axis.title = "bin (s)"
    ch.add_data(data_ref, titles_from_data=True)
    ch.set_categories(cats_ref)
    ch.height, ch.width = 8, 16
    return ch


def _line_chart(title, cats_ref, data_ref, y_title="", x_title=""):
    ch = LineChart()
    ch.title = title
    ch.y_axis.title = y_title
    ch.x_axis.title = x_title
    ch.add_data(data_ref, titles_from_data=True)
    ch.set_categories(cats_ref)
    ch.height, ch.width = 8, 20
    return ch


# ── sheet builders ───────────────────────────────────────────────────────────

def sheet_summary(wb, ctx, cd):
    ws = wb.active
    ws.title = "SUMMARY"
    _set(ws, 1, 1, f"LIFTLAB report — {ctx['from_iso']} → {ctx['to_iso']}", font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    meta = [
        ("Generated at", ctx["generated_at"]),
        ("Gateway", ctx["gw"]),
        ("DB", ctx["db_path"] + "  (opened read-only: mode=ro + query_only)"),
        ("Range (IST)", f"{_fmt_ts(ctx['t0'])} → {_fmt_ts(ctx['t1'])}"),
        ("Total raw rows exported", ctx["n_raw_rows"]),
        ("Eras present", ", ".join(sorted({f"{k[1]}/{k[2]}" for k in ctx["aggs"]}))
         or "none (no door cycles in range)"),
        ("Gap windows excluded", len([g for g in eras.DATA_GAPS
                                      if g["start_epoch"] < ctx["t1"]
                                      and g["end_epoch"] > ctx["t0"]])),
    ]
    for k, v in meta:
        _set(ws, row, 1, k, font=BOLD)
        _set(ws, row, 2, v)
        row += 1
    row += 1
    _set(ws, row, 1, "HEADLINE — observed door-close travel vs the sheet, "
                     "PER INSTRUMENT ERA (never pooled)", font=BOLD)
    row += 1
    row = _header_row(ws, row, [
        "lift", "instrument", "era", "n (clean closes)", "median s", "p85 s",
        f"sheet assumption s", "% > cliff", "cliff s", "95% CI of %>cliff",
        "verdict vs cliff (on median CI)"])
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        cl = a["close"]
        spec = eras.DOOR_SPECS.get(cam, {})
        mc = cl["median_ci"]
        _set(ws, row, 1, eras.lift_label(cam))
        _set(ws, row, 2, instrument)
        _set(ws, row, 3, era_id)
        _set(ws, row, 4, cl["n"], F_INT)
        _set(ws, row, 5, mc["median"], F_DUR)
        _set(ws, row, 6, cl["p85"], F_DUR)
        _set(ws, row, 7, spec.get("sheet_close_s", eras.SHEET_CLOSE_S), F_DUR)
        _set(ws, row, 8, cl["over_cliff"]["pct"], F_PCT)
        _set(ws, row, 9, cl["cliff_s"], F_DUR)
        _set(ws, row, 10, _ci_str(cl["over_cliff"]["lo"], cl["over_cliff"]["hi"]))
        _set(ws, row, 11, stats.verdict_vs_threshold(mc, cl["cliff_s"]))
        row += 1
    if not ctx["aggs"]:
        _set(ws, row, 1, "No door cycles in range — see COVERAGE & ERAS.")
        row += 1

    # Stopping-rule chart: running mean + 95% CI band vs the 2.31s line, for
    # each spec'd camera's largest clean-era pool.
    for cam in eras.DOOR_SPECS:
        best = None
        for key, a in ctx["aggs"].items():
            if key[0] == cam and a["close"]["n"] >= 2:
                if best is None or a["close"]["n"] > best[1]["close"]["n"]:
                    best = (key, a)
        if not best:
            continue
        (cam_, instrument, era_id), a = best
        vals = a["close"]["values"]
        run_mean, lo_b, hi_b, cliff_line, idx = [], [], [], [], []
        s = s2 = 0.0
        for i, v in enumerate(vals, start=1):
            s += v
            s2 += v * v
            m = s / i
            sd = (max(0.0, s2 - i * m * m) / (i - 1)) ** 0.5 if i > 1 else None
            half = (stats.Z95 * sd / (i ** 0.5)) if sd is not None else None
            run_mean.append(round(m, 3))
            lo_b.append(round(m - half, 3) if half is not None else None)
            hi_b.append(round(m + half, 3) if half is not None else None)
            cliff_line.append(a["close"]["cliff_s"])
            idx.append(i)
        wsd, c0, c1, nr = cd.block([
            ["n"] + idx, ["running mean"] + run_mean, ["CI lo"] + lo_b,
            ["CI hi"] + hi_b, [f"cliff {a['close']['cliff_s']}s"] + cliff_line])
        cats = Reference(wsd, min_col=c0, min_row=2, max_row=nr)
        data = Reference(wsd, min_col=c0 + 1, max_col=c1, min_row=1, max_row=nr)
        ch = _line_chart(
            f"{eras.lift_label(cam)} stopping rule — running mean close-travel "
            f"± 95% CI vs {a['close']['cliff_s']}s ({instrument}/{era_id}, "
            f"n={a['close']['n']})", cats, data,
            y_title="close travel s", x_title="cycles (chronological)")
        ws.add_chart(ch, f"A{row + 2}")
        row += 20
    return ws


def sheet_vs_sheet(wb, ctx, cd):
    ws = wb.create_sheet("VS THE SHEET")
    _set(ws, 1, 1, f"Observed coefficients vs {eras.SHEET_NAME} assumptions", font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    _set(ws, row, 1, "Verdicts are mechanical: CI vs decision threshold. "
                     "No further interpretation is offered.", font=BOLD)
    row += 2
    for cam in ctx["cams"]:
        if cam not in {k[0] for k in ctx["aggs"]} and cam not in eras.DOOR_SPECS:
            continue
        _set(ws, row, 1, f"{eras.lift_label(cam)} ({cam})"
             + (f" — bank {ctx['banks'].get(cam) or 'UNKNOWN'}"), font=BOLD)
        row += 1
        row = _header_row(ws, row, [
            "coefficient", "instrument / era", "sheet assumption",
            "observed value", "n", "95% CI", "decision threshold", "verdict"])
        from .model import vs_sheet_rows
        for r in vs_sheet_rows(ctx["aggs"], cam):
            _set(ws, row, 1, r["coefficient"])
            _set(ws, row, 2, r["era"])
            _set(ws, row, 3, r["assumption"] if isinstance(r["assumption"], str)
                 else r["assumption"], F_DUR if not isinstance(r["assumption"], str) else None)
            _set(ws, row, 4, r["observed"], F_DUR)
            _set(ws, row, 5, r["n"], F_INT)
            _set(ws, row, 6, _ci_str(*r["ci"]))
            _set(ws, row, 7, r["threshold"], F_DUR)
            _set(ws, row, 8, r["verdict"])
            row += 1
        row += 1
    return ws


def sheet_per_lift(wb, ctx, cd):
    ws = wb.create_sheet("PER-LIFT")
    _set(ws, 1, 1, "Per-lift analysis (one block per channel; every count has "
                   "its precision beside it)", font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    hist_labels = stats.hist_labels(stats.CLOSE_HIST_EDGES)
    dwell_labels = stats.hist_labels(stats.DWELL_HIST_EDGES)
    for cam in ctx["cams"]:
        v = ctx["validation"].get(cam, {})
        cur_ver = (ctx["analyzer_versions"].get(cam)
                   or v.get("counting_version") or "unknown")
        prec = precision_str(ctx["validation"], cam, cur_ver)
        _set(ws, row, 1, eras.lift_label(cam), font=H1)
        _set(ws, row, 3, f"bank: {ctx['banks'].get(cam) or 'UNKNOWN'}")
        _set(ws, row, 4, f"counting_version: {cur_ver}")
        _set(ws, row, 5, f"precision: {prec}"
             + (f", validated {_fmt_ts(v['confirmed_at'])}" if v.get("confirmed_at") else ""))
        _set(ws, row, 7, f"coverage after gap exclusion: "
             f"{ctx['coverage_pct'].get(cam, 0.0):.1f}%")
        row += 2

        cam_aggs = {k: a for k, a in ctx["aggs"].items() if k[0] == cam}
        if not cam_aggs:
            _set(ws, row, 1, "No door cycles in this range for this channel — "
                             "see COVERAGE & ERAS.", fill=WARN_FILL)
            row += 2
        row = _header_row(ws, row, [
            "instrument", "era", "cycles n", "clean closes n", "close median s",
            "close p85 s", "close min s", "close max s",
            "excluded (flap/reopen/implausible/gap)", "dwell n",
            "dwell median s", "dwell p85 s", "cycles/hr (gap-excl)"])
        for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
            f = ctx["funnels"].get((cam, era_id), {})
            excl = (f"{f.get('n_flap', 0)}/{f.get('n_reopened', 0)}/"
                    f"{f.get('n_implausible', 0)}/{a['n_in_gap_excluded']}"
                    if instrument == GPU_ENGINE else
                    f"withheld={a['n_closes_excluded']}, gap={a['n_in_gap_excluded']}")
            _set(ws, row, 1, instrument)
            _set(ws, row, 2, era_id)
            _set(ws, row, 3, a["n_cycles"], F_INT)
            _set(ws, row, 4, a["close"]["n"], F_INT)
            _set(ws, row, 5, a["close"]["median_ci"]["median"], F_DUR)
            _set(ws, row, 6, a["close"]["p85"], F_DUR)
            _set(ws, row, 7, a["close"]["min"], F_DUR)
            _set(ws, row, 8, a["close"]["max"], F_DUR)
            _set(ws, row, 9, excl)
            _set(ws, row, 10, a["dwell"]["n"], F_INT)
            _set(ws, row, 11, a["dwell"]["median_ci"]["median"], F_DUR)
            _set(ws, row, 12, a["dwell"]["p85"], F_DUR)
            _set(ws, row, 13, a["cycles_per_hr"], F_DUR)
            row += 1
        row += 1
        # transits — precision ADJACENT to every count
        row = _header_row(ws, row, [
            "counting era", "boarded", "precision (boarded)", "alighted",
            "precision (alighted)", "riders/hr (gap-excl)", "gap-excluded n",
            "first", "last"])
        t_aggs = {k: d for k, d in ctx["transit_aggs"].items() if k[0] == cam}
        pi_counted = [(k, a) for k, a in cam_aggs.items()
                      if k[1] == PI_WATCH and a["n_counted_cycles"]]
        for (c_, ver), d in sorted(t_aggs.items()):
            p = precision_str(ctx["validation"], cam, ver)
            _set(ws, row, 1, ver)
            _set(ws, row, 2, d["boarded"], F_INT)
            _set(ws, row, 3, p)
            _set(ws, row, 4, d["alighted"], F_INT)
            _set(ws, row, 5, p)
            _set(ws, row, 6, d["per_hr"], F_DUR)
            _set(ws, row, 7, d["n_in_gap_excluded"], F_INT)
            _set(ws, row, 8, _fmt_ts(d["first_ts"]))
            _set(ws, row, 9, _fmt_ts(d["last_ts"]))
            row += 1
        for (c_, instrument, era_id), a in pi_counted:
            _set(ws, row, 1, f"{era_id} (Pi on-device counter)")
            _set(ws, row, 2, a["boarded"], F_INT)
            _set(ws, row, 3, "unvalidated (Pi counter was never precision-scored)")
            _set(ws, row, 4, a["alighted"], F_INT)
            _set(ws, row, 5, "unvalidated (Pi counter was never precision-scored)")
            row += 1
        if not t_aggs and not pi_counted:
            _set(ws, row, 1, "no transits in range")
            row += 1
        row += 1
        # histograms + dwell distribution charts per era with data
        for (c_, instrument, era_id), a in sorted(cam_aggs.items()):
            if a["close"]["n"] == 0:
                continue
            wsd, c0, c1, nr = cd.block([
                ["bin"] + hist_labels,
                [f"{cam} {instrument}/{era_id} closes (n={a['close']['n']})"]
                + a["close"]["hist"]])
            cats = Reference(wsd, min_col=c0, min_row=2, max_row=nr)
            data = Reference(wsd, min_col=c1, min_row=1, max_row=nr)
            ch = _bar_chart(
                f"{eras.lift_label(cam)} close-travel — {instrument}/{era_id} "
                f"(n={a['close']['n']}; 2.00 assumption and "
                f"{a['close']['cliff_s']} cliff are bin edges)", cats, data)
            ws.add_chart(ch, f"A{row}")
            if a["dwell"]["n"]:
                wsd2, d0, d1, nr2 = cd.block([
                    ["bin"] + dwell_labels,
                    [f"{cam} {instrument}/{era_id} dwell (n={a['dwell']['n']})"]
                    + a["dwell"]["hist"]])
                cats2 = Reference(wsd2, min_col=d0, min_row=2, max_row=nr2)
                data2 = Reference(wsd2, min_col=d1, min_row=1, max_row=nr2)
                ch2 = _bar_chart(
                    f"{eras.lift_label(cam)} dwell distribution — "
                    f"{instrument}/{era_id} (n={a['dwell']['n']})", cats2, data2)
                ws.add_chart(ch2, f"J{row}")
            row += 17
        row += 2
    return ws


def sheet_fleet(wb, ctx, cd):
    ws = wb.create_sheet("FLEET")
    _set(ws, 1, 1, "Fleet roll-up — UNWEIGHTED SUMS", font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    precs = [v["precision_pct"] for v in ctx["validation"].values()
             if v.get("precision_pct") is not None]
    prec_note = (f"precision {min(precs):.0f}%–{max(precs):.0f}% across "
                 f"contributing lifts — totals are NOT precision-weighted"
                 if precs else "no validated precision on any contributing lift")
    _set(ws, row, 1, f"Fleet totals are unweighted sums; {prec_note}.",
         font=BOLD, fill=WARN_FILL)
    row += 1
    banks_present = {b for b in ctx["banks"].values() if b}
    if not banks_present:
        _set(ws, row, 1, "⚠ bank column is UNPOPULATED (all UNKNOWN) — fleet "
                         "figures below pool across banks and may mix design "
                         "cases. Populate lift_banks.json.", fill=WARN_FILL)
        row += 1
    row += 1
    groups: dict[str, list[str]] = {}
    for cam in ctx["cams"]:
        groups.setdefault(ctx["banks"].get(cam) or "UNKNOWN", []).append(cam)
    for bank, cams in sorted(groups.items()):
        _set(ws, row, 1, f"Bank: {bank} ({', '.join(cams)})", font=BOLD)
        row += 1
        row = _header_row(ws, row, [
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
            _set(ws, row, 1, ver)
            _set(ws, row, 2, g["b"], F_INT)
            _set(ws, row, 3, g["a"], F_INT)
            _set(ws, row, 4, ", ".join(sorted(g["cams"])))
            _set(ws, row, 5, (f"{min(ps):.0f}%–{max(ps):.0f}%" if ps
                              else "unvalidated"))
            _set(ws, row, 6, g["rate"], F_DUR)
            _set(ws, row, 7, NA_SPANS + " (per-camera door instruments)")
            row += 1
        pi_b = sum(a["boarded"] for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        pi_a = sum(a["alighted"] for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        pi_n = sum(1 for k, a in ctx["aggs"].items()
                   if k[0] in cams and k[1] == PI_WATCH and a["n_counted_cycles"])
        if pi_n:
            _set(ws, row, 1, "pi_watch on-device counter (pre-2026-07-21)")
            _set(ws, row, 2, pi_b, F_INT)
            _set(ws, row, 3, pi_a, F_INT)
            _set(ws, row, 4, f"{pi_n} lift-eras")
            _set(ws, row, 5, "unvalidated")
            _set(ws, row, 7, NA_SPANS)
            row += 1
        if not by_ver and not pi_n:
            _set(ws, row, 1, "no transits in range for this bank")
            row += 1
        row += 1
    # hourly profile chart — fleet + per-lift boardings by IST hour
    hours = list(range(24))
    cols = [["IST hour"] + hours]
    fleet_b = [0] * 24
    fleet_a = [0] * 24
    for cam in ctx["cams"]:
        p = ctx["profile"].get(cam, {})
        col = [f"{cam} boarded"] + [p.get(h, {}).get("boarded", 0) for h in hours]
        cols.append(col)
        for h in hours:
            fleet_b[h] += p.get(h, {}).get("boarded", 0)
            fleet_a[h] += p.get(h, {}).get("alighted", 0)
    cols.append(["fleet boarded (unweighted sum)"] + fleet_b)
    cols.append(["fleet alighted (unweighted sum)"] + fleet_a)
    wsd, c0, c1, nr = cd.block(cols)
    cats = Reference(wsd, min_col=c0, min_row=2, max_row=nr)
    data = Reference(wsd, min_col=c0 + 1, max_col=c1, min_row=1, max_row=nr)
    ch = _line_chart("Hourly boarding/alighting profile (IST, gap-excluded; "
                     "pi_watch counts + gpu transits shown as people counts)",
                     cats, data, y_title="people", x_title="IST hour")
    ws.add_chart(ch, f"A{row + 1}")
    return ws


def sheet_peak(wb, ctx, cd):
    ws = wb.create_sheet("PEAK ANALYSIS")
    _set(ws, 1, 1, "Peak analysis — worst 5-min boarding window per day"
         + (f" (fixed window {ctx['peak_window_spec']})"
            if ctx["peak_window_spec"] not in (None, "", "auto") else ""), font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    pop = ctx.get("population")
    row = _header_row(ws, row, [
        "day (IST)", "peak window", "boardings in window", "day boardings",
        "avg 5-min boardings (gap-excl)", "peak : average",
        "peak demand % of population" + ("" if pop else " (population not supplied)"),
        f"vs {eras.HC_PEAK_DESIGN_PCT}% HC design"])
    for p in ctx["peaks"]:
        pk = p["peak"]
        _set(ws, row, 1, p["day"])
        _set(ws, row, 2, (f"{_fmt_ts(pk['w0'])[11:16]}–{_fmt_ts(pk['w1'])[11:16]}"
                          if pk else "no boardings"))
        _set(ws, row, 3, pk["boardings"] if pk else 0, F_INT)
        _set(ws, row, 4, p["day_boardings"], F_INT)
        _set(ws, row, 5, p["avg_5min"], F_DUR)
        _set(ws, row, 6, p["peak_to_avg"], F_DUR)
        if pop and pk:
            pct = 100.0 * pk["boardings"] / pop
            _set(ws, row, 7, pct, F_PCT)
            _set(ws, row, 8, "over design" if pct > eras.HC_PEAK_DESIGN_PCT
                 else "within design")
        else:
            _set(ws, row, 7, "" if pk else "")
            _set(ws, row, 8, "population not supplied — % left blank" if pk else "")
        row += 1
    row += 1
    _set(ws, row, 1, "Coefficients INSIDE daily peak windows vs ALL-DAY, per "
                     "instrument era (peak-window cycles pooled across days "
                     "within one era only):", font=BOLD)
    row += 1
    row = _header_row(ws, row, [
        "lift", "instrument", "era", "scope", "clean closes n", "close median s",
        "dwell n", "dwell median s"])
    windows = [(p["peak"]["w0"], p["peak"]["w1"]) for p in ctx["peaks"] if p["peak"]]

    def _in_peak(ts):
        return any(w0 <= ts < w1 for w0, w1 in windows)

    from .model import aggregate_cycles
    peak_cycles = [c for c in ctx["all_cycles"] if _in_peak(c["ts"])]
    peak_aggs = aggregate_cycles(peak_cycles, ctx["t0"], ctx["t1"])
    for (cam, instrument, era_id), a in sorted(ctx["aggs"].items()):
        pa = peak_aggs.get((cam, instrument, era_id))
        for scope, src in (("peak windows", pa), ("all-day", a)):
            if src is None:
                continue
            _set(ws, row, 1, eras.lift_label(cam))
            _set(ws, row, 2, instrument)
            _set(ws, row, 3, era_id)
            _set(ws, row, 4, scope)
            _set(ws, row, 5, src["close"]["n"], F_INT)
            _set(ws, row, 6, src["close"]["median_ci"]["median"], F_DUR)
            _set(ws, row, 7, src["dwell"]["n"], F_INT)
            _set(ws, row, 8, src["dwell"]["median_ci"]["median"], F_DUR)
            row += 1
    return ws


def sheet_raw(wb, ctx):
    ws = wb.create_sheet("RAW")
    _set(ws, 1, 1, "Raw era-tagged export — the audit trail. No aggregation.", font=H1)
    row = 3
    row = _era_banner(ws, row, ctx["boundaries"])
    headers = ["ts (IST)", "ts_epoch", "channel", "lift_label", "bank",
               "instrument", "counting_version", "era_id", "event_type",
               "door_open_ts", "door_close_ts", "close_travel_s",
               "cycle_class/quality", "boarded", "alighted", "floor",
               "floor_source", "precision_at_time", "in_declared_gap"]
    row = _header_row(ws, row, headers)
    for r in ctx["raw_rows"]:
        for i, key in enumerate(("ts_ist", "ts_epoch", "channel", "lift_label",
                                 "bank", "instrument", "counting_version",
                                 "era_id", "event_type", "door_open_ts",
                                 "door_close_ts", "close_travel_s",
                                 "cycle_class", "boarded", "alighted", "floor",
                                 "floor_source", "precision_at_time",
                                 "in_gap"), start=1):
            fmt = F_DUR if key in ("close_travel_s",) else None
            _set(ws, row, i, r.get(key), fmt)
        row += 1
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 16
    return ws


def sheet_coverage(wb, ctx, cd):
    ws = wb.create_sheet("COVERAGE & ERAS")
    _set(ws, 1, 1, "Coverage, era boundaries, and excluded gap windows", font=H1)
    row = 3
    _set(ws, row, 1, "ERA BOUNDARIES CROSSED BY THIS RANGE", font=BOLD)
    row += 1
    if ctx["boundaries"]:
        row = _header_row(ws, row, ["boundary (ts)", "what changed",
                                    "rows before", "rows after"])
        for b in ctx["boundaries"]:
            _set(ws, row, 1, b["boundary"])
            c = _set(ws, row, 2, b["kind"])
            c.alignment = WRAP
            _set(ws, row, 3, b["rows_before"], F_INT)
            _set(ws, row, 4, b["rows_after"], F_INT)
            row += 1
    else:
        _set(ws, row, 1, "none — the whole range lies inside single eras")
        row += 1
    row += 1
    _set(ws, row, 1, "DECLARED DATA GAPS OVERLAPPING THIS RANGE (excluded from "
                     "all rate/duration statistics)", font=BOLD)
    row += 1
    row = _header_row(ws, row, ["start", "end", "channels affected", "reason"])
    n_gaps = 0
    for g in eras.DATA_GAPS:
        if g["start_epoch"] < ctx["t1"] and g["end_epoch"] > ctx["t0"]:
            _set(ws, row, 1, g["start"])
            _set(ws, row, 2, g["end"])
            _set(ws, row, 3, ", ".join(g["cams"]) if g["cams"] else "ALL")
            c = _set(ws, row, 4, g["reason"])
            c.alignment = WRAP
            row += 1
            n_gaps += 1
    if not n_gaps:
        _set(ws, row, 1, "none")
        row += 1
    row += 1
    _set(ws, row, 1, "PER-CHANNEL COVERAGE (15-min buckets with ≥1 row, over "
                     "non-gap time) AND ROW COUNTS BY ERA", font=BOLD)
    row += 1
    row = _header_row(ws, row, [
        "channel", "coverage % (gap-adjusted)", "instrument", "era",
        "door cycles", "clean closes", "transits", "first row", "last row",
        "counting_version attribution"])
    for cam in ctx["cams"]:
        cam_aggs = sorted((k, a) for k, a in ctx["aggs"].items() if k[0] == cam)
        t_by_ver = sorted((k, d) for k, d in ctx["transit_aggs"].items()
                          if k[0] == cam)
        cov = ctx["coverage_pct"].get(cam, 0.0)
        attribution = ctx["cv_attribution"].get(cam, "declared epochs")
        if not cam_aggs and not t_by_ver:
            _set(ws, row, 1, cam)
            _set(ws, row, 2, cov, F_PCT)
            _set(ws, row, 3, "—")
            _set(ws, row, 4, "ZERO ROWS in this range", fill=WARN_FILL)
            _set(ws, row, 10, attribution)
            row += 1
            continue
        for (c_, instrument, era_id), a in cam_aggs:
            _set(ws, row, 1, cam)
            _set(ws, row, 2, cov, F_PCT)
            _set(ws, row, 3, instrument)
            _set(ws, row, 4, era_id)
            _set(ws, row, 5, a["n_cycles"], F_INT)
            _set(ws, row, 6, a["close"]["n"], F_INT)
            _set(ws, row, 8, _fmt_ts(a["first_ts"]))
            _set(ws, row, 9, _fmt_ts(a["last_ts"]))
            _set(ws, row, 10, attribution)
            row += 1
        for (c_, ver), d in t_by_ver:
            _set(ws, row, 1, cam)
            _set(ws, row, 2, cov, F_PCT)
            _set(ws, row, 3, "gpu_engine (counting)")
            _set(ws, row, 4, f"counting: {ver}")
            _set(ws, row, 7, d["n"], F_INT)
            _set(ws, row, 8, _fmt_ts(d["first_ts"]))
            _set(ws, row, 9, _fmt_ts(d["last_ts"]))
            _set(ws, row, 10, attribution)
            row += 1
    row += 1
    if ctx["version_mismatches"]:
        _set(ws, row, 1, "⚠ COUNTING-VERSION CROSS-CHECK MISMATCHES (declared "
                         "epoch vs live analyzer_status/camera_validation):",
             font=BOLD, fill=WARN_FILL)
        row += 1
        for m in ctx["version_mismatches"]:
            _set(ws, row, 1, m)
            row += 1
        row += 1
    # coverage timeline chart: daily coverage % per channel
    days = ctx["coverage_daily"]["days"]
    cols = [["day"] + days]
    for cam in ctx["cams"]:
        cols.append([cam] + ctx["coverage_daily"]["by_cam"].get(cam, [0] * len(days)))
    if days:
        wsd, c0, c1, nr = cd.block(cols)
        cats = Reference(wsd, min_col=c0, min_row=2, max_row=nr)
        data = Reference(wsd, min_col=c0 + 1, max_col=c1, min_row=1, max_row=nr)
        ch = _line_chart("Coverage timeline — daily % of 15-min buckets with "
                         "data, per channel (gaps show as dips; era boundaries "
                         "listed above)", cats, data,
                         y_title="% buckets with data", x_title="day (IST)")
        ws.add_chart(ch, f"A{row + 1}")
    return ws


def sheet_tier2(wb, ctx):
    ws = wb.create_sheet("TIER-2 BLOCKED")
    _set(ws, 1, 1, "Floor attribution status — why C21/C22 (and C17/C18 trip "
                   "segmentation) are unavailable", font=H1)
    row = 3
    row = _header_row(ws, row, [
        "channel", "door rows in range", "confident reads", "no_read",
        "ambiguous", "invalid_label", "other", "glyphs seen (alphabet state)"])
    for cam in ctx["cams"]:
        d = ctx["floor_status"].get(cam)
        _set(ws, row, 1, cam)
        if not d:
            _set(ws, row, 2, 0, F_INT)
            _set(ws, row, 8, "no gw_door_event rows in range")
            row += 1
            continue
        _set(ws, row, 2, d["rows"], F_INT)
        _set(ws, row, 3, d["confident"], F_INT)
        _set(ws, row, 4, d["no_read"], F_INT)
        _set(ws, row, 5, d["ambiguous"], F_INT)
        _set(ws, row, 6, d["invalid"], F_INT)
        _set(ws, row, 7, d["other"], F_INT)
        c = _set(ws, row, 8, ", ".join(d["glyphs"][:40])
                 + (" …" if len(d["glyphs"]) > 40 else ""))
        c.alignment = WRAP
        row += 1
    row += 1
    _set(ws, row, 1, "C21/C22 speed factors need per-stop floor attribution "
                     "(confident reads on consecutive stops). C17/C18 need "
                     "trip segmentation on top of that. Until the confident-"
                     "read rate supports it, these stay blocked — the gap is "
                     "documented here rather than absent.", font=BOLD)
    ws.cell(row=row, column=1).alignment = WRAP
    return ws
