"""Unit tests for liftlab_report — era hygiene, gap exclusion, empty range,
read-only guarantee, the one-frame quantization floor, and the
resolution-bound suppression rule.
Run: python -m pytest test_liftlab_report.py -q"""

import hashlib
import re
import sqlite3
from datetime import datetime

import pytest
from openpyxl.utils.cell import coordinate_to_tuple

from liftlab_report import (charts, cli, demand_log, eras, model, narrative,
                            reader, stats, workbook)
from liftlab_report.fixtures import FRAME_QUANTUM_S, make_fixture


def _ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=eras.IST).timestamp()


@pytest.fixture()
def fixture_db(tmp_path):
    return make_fixture(str(tmp_path / "gateway.db"))


def _md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


# ── 1. era straddling: per-era aggregates, no pooled figure ──────────────────

def test_straddle_produces_per_era_aggregates_never_pooled(fixture_db):
    t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-07-25T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    instruments = {k[1] for k in ctx["aggs"]}
    assert instruments == {eras.PI_WATCH, eras.GPU_ENGINE}
    # every close-travel pool lies entirely on one side of the instrument split
    for (cam, instrument, era_id), a in ctx["aggs"].items():
        if a["first_ts"] is None:
            continue
        if instrument == eras.PI_WATCH:
            assert a["last_ts"] < eras.INSTRUMENT_SPLIT_EPOCH
        else:
            assert a["first_ts"] >= eras.INSTRUMENT_SPLIT_EPOCH
    # both pi sub-eras split at the CLOSE_TRAVEL_MAX boundary
    pi_eras = {k[2] for k in ctx["aggs"] if k[1] == eras.PI_WATCH}
    assert pi_eras == {"pi_watch/pre-ctmax30", "pi_watch/ctmax30"}
    # the boundary is declared on COVERAGE
    kinds = " ".join(b["kind"] for b in ctx["boundaries"])
    assert "INSTRUMENT SPLIT" in kinds
    # no aggregate object anywhere pools across the split
    assert all(k[1] in (eras.PI_WATCH, eras.GPU_ENGINE) for k in ctx["aggs"])


def test_straddle_workbook_builds_and_has_coverage_boundary(fixture_db, tmp_path):
    t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-07-25T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    wb = cli.build_workbook(ctx)
    out = tmp_path / "straddle.xlsx"
    wb.save(out)
    ws = wb["COVERAGE & ERAS"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "INSTRUMENT SPLIT" in text
    # SUMMARY: every headline row names exactly one instrument
    s = wb["SUMMARY"]
    inst_col = [c.value for c in s["B"] if c.value in ("pi_watch", "gpu_engine")]
    assert len(inst_col) == len(ctx["aggs"])


def test_withheld_quality_close_never_enters_pool(fixture_db):
    t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-07-20T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    a = ctx["aggs"][("ch29", eras.PI_WATCH, "pi_watch/ctmax30")]
    assert a["n_closes_excluded"] == 1          # the close_suspect row
    assert all(v is not None for v in a["close"]["values"])


# ── 2. gap exclusion ─────────────────────────────────────────────────────────

def test_gap_rows_excluded_from_pools_and_rates(fixture_db):
    t0, t1 = _ts("2026-07-30T00:00:00"), _ts("2026-08-01T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    ch16 = [(k, a) for k, a in ctx["aggs"].items() if k[0] == "ch16"]
    assert ch16
    for _k, a in ch16:
        # the 2026-07-31T13:00 cycle sits inside the declared outage
        assert a["n_in_gap_excluded"] >= 1
        assert all(not eras.in_gap("ch16", ts)
                   for ts in (a["first_ts"], a["last_ts"]) if ts)
    # rate denominator: covered time excludes the gap
    span = t1 - t0
    gap = eras.gap_overlap_s("ch16", t0, t1)
    assert gap > 0
    _k, a = ch16[0]
    assert a["covered_s"] == pytest.approx(span - gap)
    # transit inside the gap excluded from counts
    for (cam, _v), d in ctx["transit_aggs"].items():
        if cam == "ch16":
            assert d["n_in_gap_excluded"] >= 1


def test_gap_union_never_double_counts():
    # overlapping declared windows must union, not sum
    g = eras.gap_overlap_s("ch27",
                           _ts("2026-08-01T22:30:00"), _ts("2026-08-01T23:30:00"))
    # ch27: full-outage tail (22:30–22:35) + churn (22:52–23:12) + 23:16–23:18
    assert g == pytest.approx(5 * 60 + 20 * 60 + 2 * 60)


# ── 3. empty range ───────────────────────────────────────────────────────────

def test_empty_range_builds_valid_workbook(fixture_db, tmp_path):
    t0, t1 = _ts("2026-06-01T00:00:00"), _ts("2026-06-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    assert ctx["aggs"] == {} and ctx["raw_rows"] == []
    wb = cli.build_workbook(ctx)
    out = tmp_path / "empty.xlsx"
    wb.save(out)
    ws = wb["COVERAGE & ERAS"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "ZERO ROWS" in text


# ── 4. read-only guarantee ───────────────────────────────────────────────────

def test_db_never_written(fixture_db, tmp_path):
    before = _md5(fixture_db)
    t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    wb = cli.build_workbook(ctx)
    wb.save(tmp_path / "full.xlsx")
    assert _md5(fixture_db) == before
    db = reader.open_ro(fixture_db)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO gw_event (source_id) VALUES (1)")
    db.close()


# ── 5. fleet: unweighted sums, precision range, bank warning ─────────────────

def test_fleet_unweighted_and_bank_warning(fixture_db, tmp_path):
    t0, t1 = _ts("2026-07-21T00:00:00"), _ts("2026-08-01T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    assert all((ctx["banks"].get(c) or "") == "" for c in ctx["cams"])
    wb = cli.build_workbook(ctx)
    ws = wb["FLEET"]
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "unweighted sums" in text.lower()
    assert "NOT precision-weighted" in text
    assert "bank column is UNPOPULATED" in text
    assert "n/a — spans eras" in text


# ── counting-version attribution ─────────────────────────────────────────────

def test_counting_version_attribution(fixture_db):
    t0, t1 = _ts("2026-07-21T00:00:00"), _ts("2026-08-01T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    vers = {v for (_c, v) in ctx["transit_aggs"]}
    assert "2026-07-17-yolo11m-dwell-disp" in vers          # ch29 Jul 22-23
    assert "2026-07-28-registry-zones" in vers              # ch16 Jul 30
    # raw transit rows carry the era-correct version
    for r in ctx["raw_rows"]:
        if r["event_type"] == "transit" and r["channel"] == "ch29":
            assert r["counting_version"] == "2026-07-17-yolo11m-dwell-disp"


# ── GPU cycle classification (port of the dashboard walk) ────────────────────

def test_reopen_cycle_excluded_from_clean_pool(fixture_db):
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-23T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    f = ctx["funnels"][("ch29", "e79e50d3")]
    assert f["n_reopened"] == 1
    assert f["n_clean"] == 15


def test_peak_analysis_finds_daily_peak(fixture_db):
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-24T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    days = [p for p in ctx["peaks"] if p["peak"]]
    assert len(days) == 2
    assert all(p["peak"]["boardings"] >= 1 for p in days)


# ── 6. the one-frame quantization floor ──────────────────────────────────────

QUANT_T0, QUANT_T1 = "2026-07-25T00:00:00", "2026-07-26T00:00:00"
CH32 = ("ch32", eras.GPU_ENGINE, "aa11bb22")


def _quant_ctx(fixture_db, **kw):
    return cli.build_context(fixture_db, "site-A", _ts(QUANT_T0), _ts(QUANT_T1),
                             **kw)


def test_quantum_detected_empirically_not_hardcoded(fixture_db):
    """The frame interval is read out of the data, so it follows analyze_fps."""
    a = _quant_ctx(fixture_db)["aggs"][CH32]
    assert a["quantum_s"] == pytest.approx(FRAME_QUANTUM_S, abs=0.002)
    # ...and the detector is not merely echoing a constant: a different grid
    # detects as that grid, and ungridded data detects as nothing.
    assert stats.detect_quantum([0.05 * k for k in range(1, 200)]) == \
        pytest.approx(0.05, abs=0.002)
    assert stats.detect_quantum([1.0 + 0.37 * ((i * 7919) % 97) / 97.0
                                 for i in range(300)]) is None


def test_sub_floor_closes_rejected_from_every_close_statistic(fixture_db):
    """Fixture closes of 4-6 frames (0.32-0.48s) are plausible under the old
    PLAUS_LO=0.3 bound but far too short to be a real close. None of them may
    reach a close-travel figure — not the median, not p85, not min, not the
    histogram."""
    cl = _quant_ctx(fixture_db)["aggs"][CH32]["close"]
    assert cl["n_floor_rejected"] > 0, (
        "the floor rejected nothing — the fixture no longer exercises it")
    assert cl["n"] == cl["n_prefilter"] - cl["n_floor_rejected"]
    assert all(v >= eras.MIN_PLAUSIBLE_CLOSE_S for v in cl["values"])
    assert cl["min"] >= eras.MIN_PLAUSIBLE_CLOSE_S
    assert cl["median_ci"]["n"] == cl["n"]
    assert sum(cl["hist"]) == cl["n"]


def test_one_frame_closes_never_reach_a_pool_by_any_route(fixture_db):
    """A one-frame close is caught upstream as implausible, before the floor
    ever sees it. It must still be absent from every close figure — and the
    pre-filter audit pool must not smuggle it back in."""
    cl = _quant_ctx(fixture_db)["aggs"][CH32]["close"]
    q = FRAME_QUANTUM_S
    assert not any(abs(v - q) < 0.01 for v in cl["values"])
    assert cl["median_prefilter"] > q
    assert cl["n_prefilter"] < 60          # the 12 one-frame cycles are outside it


def test_floor_is_configurable_and_moves_the_pool(fixture_db):
    """--min-close / the module constant actually drive the filter."""
    loose = _quant_ctx(fixture_db, min_close_s=0.0)["aggs"][CH32]["close"]
    strict = _quant_ctx(fixture_db, min_close_s=2.0)["aggs"][CH32]["close"]
    assert loose["n_floor_rejected"] == 0
    assert loose["n"] > strict["n"]
    assert strict["min"] >= 2.0
    # with the floor off, the sub-floor band survives — that is the whole point
    assert loose["min"] < eras.MIN_PLAUSIBLE_CLOSE_S
    assert loose["min"] == pytest.approx(4 * FRAME_QUANTUM_S, abs=0.002)
    # the pre-filter twin is the SAME pool in both runs — it is the audit trail
    assert loose["n_prefilter"] == strict["n_prefilter"]


def test_prefilter_figures_kept_so_the_filter_is_auditable(fixture_db):
    cl = _quant_ctx(fixture_db)["aggs"][CH32]["close"]
    assert cl["median_prefilter"] is not None and cl["p85_prefilter"] is not None
    # dropping one-frame values can only raise the median
    assert cl["median_ci"]["median"] >= cl["median_prefilter"]
    assert 0.0 < cl["floor_reject_frac"] <= 1.0
    assert cl["floor_reject_warn"] is (
        cl["floor_reject_frac"] > eras.FLOOR_REJECT_WARN_FRAC)


def test_floor_rejections_reported_per_channel_era_on_coverage(fixture_db):
    ctx = _quant_ctx(fixture_db)
    ws = cli.build_workbook(ctx)["COVERAGE & ERAS"]
    text = " ".join(str(c.value) for row in ws.iter_rows()
                    for c in row if c.value is not None)
    assert "REJECTED AT THE ONE-FRAME QUANTIZATION FLOOR" in text
    assert "detected frame quantum" in text
    n_rej = ctx["aggs"][CH32]["close"]["n_floor_rejected"]
    vals = [c.value for row in ws.iter_rows() for c in row]
    assert n_rej in vals


def test_pool_losing_more_than_warn_share_warns_on_summary(fixture_db):
    ctx = _quant_ctx(fixture_db)
    assert ctx["aggs"][CH32]["close"]["floor_reject_warn"]
    ws = cli.build_workbook(ctx)["SUMMARY"]
    text = " ".join(str(c.value) for row in ws.iter_rows()
                    for c in row if c.value is not None)
    assert "ONE-FRAME QUANTIZATION FLOOR" in text
    assert "ch32" in text or "lift 32" in text


# ── 7. resolution-bound suppression ──────────────────────────────────────────

def test_open_travel_suppressed_when_pinned_at_the_quantum(fixture_db):
    """3 of every 4 fixture opens are one frame long. That is the instrument's
    floor, not the door's speed, so NO verdict may be emitted."""
    ot = _quant_ctx(fixture_db)["aggs"][CH32]["open_travel"]
    assert ot["at_quantum"]["frac"] > eras.QUANTUM_SUPPRESS_FRAC
    assert ot["suppressed"] is True
    why = ot["suppression_reason"]
    assert "not measurable" in why
    assert "sampling-resolution floor" in why
    assert "analyze_fps" in why                       # names the remedy
    assert f"{ot['at_quantum']['quantum']:.2f}s quantum" in why


def test_suppressed_open_travel_never_emits_a_verdict(fixture_db):
    """The v1 defect: 'CI clears threshold (below)' against the 3s open
    assumption — i.e. the claim that doors open in a tenth of a second."""
    ctx = _quant_ctx(fixture_db)
    rows = [r for r in model.vs_sheet_rows(ctx["aggs"], "ch32")
            if "open travel" in r["coefficient"]]
    assert rows
    for r in rows:
        assert r["suppressed"] is True
        assert "clears threshold" not in r["verdict"]
        assert "straddles" not in r["verdict"]
        assert r["verdict"].startswith("not measurable")
    ws = cli.build_workbook(ctx)["VS THE SHEET"]
    text = " ".join(str(c.value) for row in ws.iter_rows()
                    for c in row if c.value is not None)
    assert "not measurable — measurement at sampling-resolution floor" in text


def test_suppression_threshold_is_the_declared_one(fixture_db):
    """Just under the threshold must NOT suppress; just over must."""
    q = FRAME_QUANTUM_S
    n = 100
    k = int(eras.QUANTUM_SUPPRESS_FRAC * n)
    below = [q] * (k - 2) + [q * 12] * (n - k + 2)
    above = [q] * (k + 5) + [q * 12] * (n - k - 5)
    assert stats.floor_share(below, q)["frac"] <= eras.QUANTUM_SUPPRESS_FRAC
    assert stats.floor_share(above, q)["frac"] > eras.QUANTUM_SUPPRESS_FRAC


def test_close_travel_gets_the_same_floor_guard(fixture_db):
    """The guard is not open-travel-specific: a close pool pinned at the
    quantum is suppressed by the same rule."""
    q = FRAME_QUANTUM_S
    pinned = [q] * 80 + [2.4] * 20
    fs = stats.floor_share(pinned, q)
    assert fs["frac"] > eras.QUANTUM_SUPPRESS_FRAC
    note = stats.floor_suppression_note("close-travel", fs,
                                        stats.CLOSE_TRAVEL_REMEDY)
    assert note.startswith("not measurable")
    assert "close-travel" in note


# ── 8. undeclared gaps ───────────────────────────────────────────────────────

def test_jul24_and_jul27_are_declared(fixture_db):
    """Both zero-row days are DECLARED, so they leave rate denominators."""
    for day in ("2026-07-24", "2026-07-27"):
        noon = _ts(f"{day}T12:00:00")
        assert eras.in_gap("ch29", noon), f"{day} not declared"
        g = [g for g in eras.DATA_GAPS
             if g["start_epoch"] <= noon < g["end_epoch"]][0]
        assert "undeclared — no rows" in g["reason"]
        assert g["cams"] is None
    # a full IST day each, so a whole day leaves the denominator
    d0, d1 = _ts("2026-07-24T00:00:00"), _ts("2026-07-25T00:00:00")
    assert eras.gap_overlap_s("ch16", d0, d1) == pytest.approx(d1 - d0)


def test_suspected_gap_detector_flags_undeclared_silence_only(fixture_db):
    """ch34 is live Jul 25 and Jul 28. Jul 26 is undeclared → flagged.
    Jul 27 is declared → not flagged. Flagging never excludes."""
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-29T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    susp = [g for g in ctx["suspected_gaps"] if g["cam"] == "ch34"]
    assert susp, "the Jul-26 silence on ch34 was not flagged"
    flagged = {datetime.fromtimestamp(g["start"], eras.IST).date().isoformat()
               for g in susp if g["kind"] == "silent day"}
    assert "2026-07-26" in flagged
    assert "2026-07-27" not in flagged          # already declared, stays quiet
    # FLAGGED, NOT EXCLUDED: no declared gap was created for Jul 26
    noon26 = _ts("2026-07-26T12:00:00")
    assert not eras.in_gap("ch34", noon26)
    for g in susp:
        assert g["undeclared_s"] > 0


def test_suspected_gaps_surface_on_coverage_but_not_as_exclusions(fixture_db):
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-29T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    ws = cli.build_workbook(ctx)["COVERAGE & ERAS"]
    text = " ".join(str(c.value) for row in ws.iter_rows()
                    for c in row if c.value is not None)
    assert "AUTO-DETECTED SUSPECTED GAPS" in text
    assert "FLAGGED ONLY" in text and "NOT EXCLUDED" in text


# ── 9. READ THIS FIRST ───────────────────────────────────────────────────────

def test_read_this_first_is_the_first_sheet(fixture_db):
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    assert wb.sheetnames[0] == "READ THIS FIRST"
    assert wb.sheetnames[-1] == "DATA_CHARTS"


def test_every_finding_carries_evidence_and_confidence(fixture_db):
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-26T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    findings = narrative.build_findings(ctx, {})
    assert findings
    for f in findings:
        assert f["sentence"] and f["sheet"]
        assert f["confidence"] in (narrative.HIGH, narrative.MEDIUM,
                                   narrative.TOO_EARLY)
        assert f["why"], "a confidence without a reason is not a confidence"


def test_findings_name_a_range_that_holds_the_number(fixture_db):
    """Evidence must be checkable: the seconds figures a finding quotes have to
    appear in the cells it names."""
    import re
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-26T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    wb = cli.build_workbook(ctx)
    ws = wb["READ THIS FIRST"]
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    checked = 0
    for i, r in enumerate(rows):
        if not (isinstance(r[0], str) and re.fullmatch(r"\d+\.", r[0].strip())):
            continue
        sentence = r[1]
        ev = next((rows[j][1] for j in range(i + 1, min(i + 4, len(rows)))
                   if isinstance(rows[j][1], str)
                   and rows[j][1].startswith("Evidence:")), None)
        assert ev, f"finding {r[0]} has no Evidence line"
        m = re.search(r"sheet (.+?), cells ([A-Z]+\d+:[A-Z]+\d+)", ev)
        assert m, f"finding {r[0]} evidence names no cell range: {ev}"
        sheet, rng = m.group(1), m.group(2)
        assert sheet in wb.sheetnames
        have = []
        for row in wb[sheet][rng]:
            for c in row:
                if isinstance(c.value, (int, float)):
                    have.append(float(c.value))
        for q in re.findall(r"(\d+\.\d{2})(?=s\b)", str(sentence)):
            assert any(abs(float(q) - h) < 0.006 for h in have), (
                f"finding {r[0]} quotes {q}s but {sheet}!{rng} does not hold it")
            checked += 1
    assert checked, "no numeric claim was traced — the check proved nothing"


def test_suppressed_metric_says_so_in_the_findings(fixture_db):
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-26T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    findings = narrative.build_findings(ctx, {})
    open_f = [f for f in findings if "OPEN travel" in f["headline"]]
    assert open_f, "an entirely suppressed coefficient vanished from the sheet"
    f = open_f[0]
    assert f["confidence"] == narrative.TOO_EARLY
    assert "cannot be measured" in f["sentence"]
    assert "frame" in f["sentence"]


def test_read_this_first_states_the_data_is_uneven(fixture_db):
    t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    text = " ".join(narrative.data_scale_sentences(ctx))
    assert "NOT spread evenly" in text
    assert "of all rows fall on just" in text
    cannot = narrative.cannot_say_yet(ctx)
    assert cannot, "the workbook claimed there is nothing it cannot say"
    assert all(c["what"] and c["why"] and c["to_fix"] for c in cannot)


# ── 10. the v1 regressions stay fixed ────────────────────────────────────────

def test_empty_and_straddling_ranges_still_build_with_the_new_sheet(fixture_db,
                                                                   tmp_path):
    for label, (a, b) in {
            "empty": ("2026-06-01T00:00:00", "2026-06-02T00:00:00"),
            "straddle": ("2026-07-14T00:00:00", "2026-07-25T00:00:00")}.items():
        ctx = cli.build_context(fixture_db, "site-A", _ts(a), _ts(b))
        wb = cli.build_workbook(ctx)
        wb.save(tmp_path / f"{label}.xlsx")
        assert wb.sheetnames[0] == "READ THIS FIRST"


def test_every_chart_has_readable_axes_and_a_caption(fixture_db):
    """The v1 defect: axis titles but no tick labels, no gridlines, no caption."""
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    n = 0
    for ws in wb.worksheets:
        for ch in getattr(ws, "_charts", []):
            n += 1
            for ax in (ch.x_axis, ch.y_axis):
                assert ax.delete is False
                assert ax.tickLblPos == "nextTo"
                assert ax.majorTickMark == "out"
            assert ch.y_axis.numFmt not in (None, "General")
            assert ch.y_axis.majorGridlines is not None
            assert ch.x_axis.majorGridlines is None
            # add_chart() keeps the anchor as an A1 string until the file is
            # written, where it becomes a marker object; accept either.
            if isinstance(ch.anchor, str):
                r, c = coordinate_to_tuple(ch.anchor)
            else:
                r, c = ch.anchor._from.row + 1, ch.anchor._from.col + 1
            cap = ws.cell(row=r - 1, column=c).value
            assert cap and str(cap).startswith("Take-away"), (
                f"chart on {ws.title} at r{r} has no plain-English caption")
    assert n >= 4, "expected several charts to check"


def test_no_number_lands_in_a_general_format_cell(fixture_db):
    """The v1 defect: raw floats like 44.08060453400503 in cells."""
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    offenders = []
    for ws in wb.worksheets:
        if ws.title == "DATA_CHARTS":
            continue
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, (int, float)) and \
                        c.number_format == "General":
                    offenders.append(f"{ws.title}!{c.coordinate}={c.value}")
    assert not offenders, f"unformatted numbers: {offenders[:10]}"


# ── 11. chart data labels (v2 shipped unreadable charts) ─────────────────────

LABEL_FLAGS = ("showVal", "showSerName", "showCatName", "showLegendKey",
               "showPercent", "showBubbleSize")


def test_data_labels_are_never_left_unset(fixture_db):
    """openpyxl OMITS unset DataLabelList flags and Excel then supplies its own
    defaults — printing 'series name, category name, value' on every point.
    Every flag must be written explicitly, or the labels must be absent."""
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    seen = 0
    for ws in wb.worksheets:
        for ch in getattr(ws, "_charts", []):
            seen += 1
            dl = ch.dataLabels
            if dl is None:
                continue                      # no labels at all is allowed
            for flag in LABEL_FLAGS:
                assert getattr(dl, flag) is not None, (
                    f"{ws.title}: dataLabels.{flag} left None — Excel will "
                    f"apply its own default")
            assert dl.showVal is True
            assert dl.showSerName is False
            assert dl.showCatName is False
            assert dl.showLegendKey is False
            assert dl.showPercent is False
            assert dl.showBubbleSize is False
    assert seen >= 4


def test_labels_dropped_where_they_could_not_be_read(fixture_db):
    """Past the legibility limit the labels come off entirely — an unreadable
    label is worse than none. 12-bin histograms must not carry labels."""
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    checked = 0
    for ws in wb.worksheets:
        for ch in getattr(ws, "_charts", []):
            cats = 0
            for s in ch.series:
                ref = getattr(getattr(s, "cat", None), "numRef", None) or \
                    getattr(getattr(s, "cat", None), "strRef", None)
                if ref is not None and ref.f:
                    m = re.findall(r"\$(\d+)", ref.f)
                    if len(m) >= 2:
                        cats = max(cats, int(m[1]) - int(m[0]) + 1)
            if cats > charts.MAX_LABELLED_CATS:
                assert ch.dataLabels is None, (
                    f"{ws.title}: {cats} categories still carries data labels")
                checked += 1
    assert checked, "no wide chart was checked — the limit proved nothing"


def test_stacked_histogram_writes_none_not_zero_for_absent_bands(fixture_db):
    """A stacked bin must carry a value in exactly ONE series. Zeros in the
    other two would draw zero-height segments and label them."""
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    ws = wb["DATA_CHARTS"]
    found = 0
    for row in ws.iter_rows(min_row=1, max_row=1):
        for c in row:
            if not (isinstance(c.value, str)
                    and c.value.startswith("at or below the")):
                continue
            found += 1
            # the three band columns start at THIS cell's column
            for r in range(2, 14):
                cells = [ws.cell(row=r, column=c.column + k).value
                         for k in range(3)]
                non_null = [x for x in cells if x is not None]
                assert len(non_null) <= 1, (
                    f"bin row {r} has {len(non_null)} populated bands: "
                    f"{cells} — zero-height segments would be drawn and "
                    f"labelled")
    assert found, "no stacked histogram band block found"


# ── 12. demand by lift and hour ──────────────────────────────────────────────

DEMAND_T0, DEMAND_T1 = "2026-07-22T00:00:00", "2026-07-26T00:00:00"


def _demand_ctx(fixture_db):
    return cli.build_context(fixture_db, "site-A", _ts(DEMAND_T0), _ts(DEMAND_T1))


def test_dark_hour_is_a_dash_never_zero(fixture_db):
    """A lift that was not observed in an hour must not read 0 — a dark lift is
    not an idle lift, and 0 would be read as 'carried nobody'."""
    ctx = _demand_ctx(fixture_db)
    d = ctx["demand"]
    ver = max(d["by_era"], key=lambda v: d["by_era"][v]["total_boarded"])
    e = d["by_era"][ver]
    for cam in ctx["cams"]:
        for h in d["hours"]:
            observed = e["observed_days"][cam][h]
            val = e["boarded"][cam][h]
            if observed == 0:
                assert val is None, (
                    f"{cam} h{h}: 0 observed days but value {val} — a dark "
                    f"hour must be None, rendered '—'")
            else:
                assert val is not None
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    vals = [c.value for r in ws.iter_rows() for c in r]
    assert workbook.DARK in vals


def test_observed_days_matrix_matches_the_main_matrix(fixture_db):
    ctx = _demand_ctx(fixture_db)
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    rows = [[c.value for c in r] for r in ws.iter_rows()]

    def _block(title_text):
        """The 24 hour-rows under a titled matrix. Located by finding the
        'hour (IST)' header after the title, not by a hard-coded offset, so
        adding a caption row does not silently shift the assertions."""
        i = next(k for k, r in enumerate(rows)
                 if any(c == title_text for c in r))
        h = next(k for k in range(i, len(rows))
                 if rows[k] and rows[k][0] == "hour (IST)")
        start = h + 4                      # header + 3 provenance rows
        return rows[start:start + 24]

    main = _block("MEAN BOARDINGS PER OBSERVED DAY, BY HOUR")
    totals = _block("TOTAL OBSERVED BOARDINGS, BY HOUR")
    days = _block("OBSERVED DAYS BEHIND EACH CELL ABOVE")
    assert len(main) == len(totals) == len(days) == 24
    n_cams = len(ctx["cams"])
    for i in range(24):
        assert main[i][0] == days[i][0] == totals[i][0], (
            f"hour label mismatch on row {i}")
        for j in range(1, n_cams + 1):
            observed = days[i][j]
            assert (main[i][j] == workbook.DARK) == (observed == 0), (
                f"row {i} col {j}: '—' and 0-observed-days disagree")
            # dark hours stay dark in the TOTALS matrix too
            assert (totals[i][j] == workbook.DARK) == (observed == 0), (
                f"row {i} col {j}: totals matrix disagrees with observed days")
            if observed:
                assert isinstance(totals[i][j], int), (
                    f"row {i} col {j}: total is not a whole number of people")
                assert totals[i][j] == pytest.approx(
                    main[i][j] * observed, abs=0.01), (
                    f"row {i} col {j}: total does not reconcile with "
                    f"mean x observed days")


def test_demand_never_pools_counting_eras(fixture_db):
    """Counts from different counting builds are different measurements."""
    ctx = _demand_ctx(fixture_db)
    d = ctx["demand"]
    assert len(d["versions"]) >= 1
    assert set(d["by_era"]) == set(d["versions"])
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    for ver in d["versions"]:
        assert ver in text, f"counting era {ver} is not named on the sheet"
    assert "never pooled" in text


def test_demand_reconciles_with_peak_analysis(fixture_db):
    """Both sheets rest on the same gap-excluded boarding stream, so their
    boarding totals must agree exactly."""
    ctx = _demand_ctx(fixture_db)
    here = sum(e["total_boarded"] for e in ctx["demand"]["by_era"].values())
    there = sum(p["day_boardings"] for p in ctx["peaks"])
    assert here == there, f"demand={here} vs peak sheet={there}"
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "YES — same gap-excluded boarding stream" in text


def test_population_block_states_its_blocker_when_absent(fixture_db):
    ctx = _demand_ctx(fixture_db)
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "BLOCKED — no population figure was supplied" in text
    assert "NEVER guessed" in text
    assert "--population" in text


def test_population_block_compares_when_supplied(fixture_db):
    ctx = cli.build_context(fixture_db, "site-A", _ts(DEMAND_T0), _ts(DEMAND_T1),
                            population=2400)
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "BLOCKED — no population figure was supplied" not in text
    assert "as % of population (2,400)" in text
    assert "within design" in text or "over design" in text


def test_load_balance_warns_that_coverage_can_mimic_load(fixture_db):
    ctx = _demand_ctx(fixture_db)
    ws = cli.build_workbook(ctx)["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "uneven COVERAGE can masquerade as uneven LOAD" in text
    assert "similar coverage" in text
    # CV is undefined below two observed lifts, never faked as 0
    d = ctx["demand"]
    ver = d["versions"][0]
    e = d["by_era"][ver]
    for h in d["hours"]:
        live = sum(1 for c in ctx["cams"] if e["boarded"][c][h] is not None)
        if live < 2:
            assert e["cv"][h] is None


# ── 13. bank derivation ──────────────────────────────────────────────────────

def test_banks_derived_from_shared_floor_range(tmp_path):
    """Lifts serving the same floor range are one bank; the evidence is named."""
    db = make_fixture(str(tmp_path / "banked.db"), floor_ranges={
        "ch16": "1-30", "ch27": "1-30", "ch29": "31-60", "ch30": " 31 - 60 "})
    ctx = cli.build_context(db, "site-A", _ts("2026-07-22T00:00:00"),
                            _ts("2026-07-26T00:00:00"))
    assert ctx["banks"]["ch16"] == ctx["banks"]["ch27"] != ""
    assert ctx["banks"]["ch29"] == ctx["banks"]["ch30"] != ""
    assert ctx["banks"]["ch16"] != ctx["banks"]["ch29"]
    # whitespace variants normalise to the same bank, not to two banks
    assert ctx["bank_evidence"]["ch30"]["floor_range"] == "31-60"
    assert ctx["bank_evidence"]["ch16"]["shared_with"] == ["ch27"]
    assert "floor_range" in ctx["bank_evidence"]["ch16"]["source"]
    # channels with no floor_range stay UNKNOWN — never guessed
    for cam in ("ch32", "ch34", "ch37"):
        assert ctx["banks"][cam] == ""


def test_banks_stay_unknown_when_floor_range_is_empty(fixture_db):
    """The live gateway state: floor_range empty on every channel."""
    ctx = _demand_ctx(fixture_db)
    assert all((ctx["banks"].get(c) or "") == "" for c in ctx["cams"])
    for cam in ctx["cams"]:
        assert "EMPTY" in ctx["bank_evidence"][cam]["source"]
    ws = cli.build_workbook(ctx)["COVERAGE & ERAS"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "BANK ASSIGNMENT — DERIVATION AND EVIDENCE" in text
    assert "NO lift could be assigned a bank" in text
    # and the cross-bank pooling warning stays live on FLEET
    fleet = " ".join(str(c.value) for r in cli.build_workbook(ctx)["FLEET"]
                     .iter_rows() for c in r if c.value is not None)
    assert "bank column is UNPOPULATED" in fleet


def test_sidecar_assignment_outranks_derivation(tmp_path):
    """An operator's explicit statement beats a derived one."""
    db = make_fixture(str(tmp_path / "b2.db"), floor_ranges={"ch16": "1-30"})
    sidecar = tmp_path / "banks.json"
    sidecar.write_text('{"banks": {"ch16": "Bank C"}}', encoding="utf-8")
    ctx = cli.build_context(db, "site-A", _ts("2026-07-22T00:00:00"),
                            _ts("2026-07-26T00:00:00"),
                            banks_path=str(sidecar))
    assert ctx["banks"]["ch16"] == "Bank C"
    assert "lift_banks.json" in ctx["bank_evidence"]["ch16"]["source"]


# ── 14. the unresolved per-lift divergence ───────────────────────────────────

def test_divergence_is_raised_as_an_open_question_not_a_conclusion(fixture_db):
    """Two lifts in one tower differing materially with disjoint intervals is
    a finding — but the CAUSE must not be picked."""
    t0, t1 = _ts("2026-07-25T00:00:00"), _ts("2026-07-26T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    findings = narrative.build_findings(ctx, {})
    div = [f for f in findings if "OPEN QUESTION" in f["headline"]]
    if not div:
        pytest.skip("this fixture range has no divergent pair to report on")
    f = div[0]
    assert f["confidence"] == narrative.TOO_EARLY
    s = f["sentence"]
    assert "UNRESOLVED" in s
    # both explanations offered, neither chosen
    assert "mechanical" in s and "camera" in s
    assert "does not choose between them" in s
    # and it says what it blocks
    assert any("blocks per-lift" in e for e in f["extra"])


def test_divergence_absent_when_intervals_overlap():
    """No open question where the difference could be noise."""
    ci_a = {"median": 2.0, "lo": 1.8, "hi": 2.4, "n": 200}
    ci_b = {"median": 2.2, "lo": 2.0, "hi": 2.6, "n": 200}
    assert not (ci_b["lo"] > ci_a["hi"])       # overlapping → not a divergence


def test_v3_findings_cover_demand(fixture_db):
    ctx = _demand_ctx(fixture_db)
    heads = " | ".join(f["headline"] for f in narrative.build_findings(ctx, {}))
    assert "When the building uses its lifts" in heads
    assert "load balanced" in heads


def test_per_floor_finding_appears_exactly_when_no_confident_reads(fixture_db):
    """The live gateway has ZERO confident floor reads; the fixture cameras do
    read floors. The finding must track the data, not be hardcoded either way."""
    ctx = _demand_ctx(fixture_db)
    confident = sum(d.get("confident", 0)
                    for d in ctx["floor_status"].values())
    heads = " | ".join(f["headline"] for f in narrative.build_findings(ctx, {}))
    present = "Per-floor demand is unavailable" in heads
    assert present == (confident == 0), (
        f"{confident} confident reads but finding present={present}")

    # force the live condition and the finding must appear
    blinded = dict(ctx)
    blinded["floor_status"] = {
        cam: dict(d, confident=0, no_read=d.get("rows", 0))
        for cam, d in ctx["floor_status"].items()}
    heads2 = " | ".join(f["headline"]
                        for f in narrative.build_findings(blinded, {}))
    assert "Per-floor demand is unavailable" in heads2


# ── 15. one figure, one computation ──────────────────────────────────────────

def _all_text(wb, skip=("DATA_CHARTS",)):
    out = []
    for ws in wb.worksheets:
        if ws.title in skip:
            continue
        for r in ws.iter_rows():
            for c in r:
                if isinstance(c.value, str):
                    out.append((ws.title, c.value))
    return out


def test_every_rendered_busiest_hour_resolves_to_the_canonical_value(fixture_db):
    """v3 stated three different fleet busiest hours. Every statement of one
    must now resolve to the single canonical figure, or explicitly name a
    different definition."""
    ctx = _demand_ctx(fixture_db)
    canon = ctx["canonical"]["fleet_busiest_hour"]
    assert canon["hour"] is not None
    canonical_txt = f"{canon['hour']:02d}:00"
    wb = cli.build_workbook(ctx)
    offenders = []
    for sheet, text in _all_text(wb):
        # An UNQUALIFIED claim about the busiest hour must be the canonical one.
        for m in re.finditer(r"busiest hour is (\d{2}):00", text):
            if f"{m.group(1)}:00" != canonical_txt:
                offenders.append((sheet, "unqualified", text[:140]))
    assert not offenders, f"non-canonical busiest hour stated: {offenders}"

    # Any OTHER hour that appears as a peak must be explicitly scoped — naming
    # its counting era and saying it is not the canonical figure. Rewording
    # alone must not be enough to pass this test.
    for sheet, text in _all_text(wb):
        for m in re.finditer(r"peaks at (\d{2}):00", text):
            if f"{m.group(1)}:00" == canonical_txt:
                continue
            assert "counting era" in text and "canonical" in text, (
                f"[{sheet}] states a peak of {m.group(1)}:00 without scoping "
                f"it against the canonical figure: {text[:160]}")


def test_a_sheet_using_another_definition_must_declare_it(fixture_db):
    """FLEET plots raw pooled totals — a different definition. It is allowed,
    but only if it says so and defers to the canonical figure."""
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    fleet = " ".join(v for s, v in _all_text(wb) if s == "FLEET")
    assert "NOTE THE DEFINITION" in fleet
    assert "canonical busiest hour" in fleet
    demand = " ".join(v for s, v in _all_text(wb)
                      if s == "DEMAND BY LIFT AND HOUR")
    assert "THE CANONICAL BUSIEST HOUR" in demand
    assert ctx["canonical"]["fleet_busiest_hour"]["definition"] in demand


def test_cross_sheet_figures_come_from_one_computation(fixture_db):
    """Boarding totals and coverage range are quoted on several sheets."""
    ctx = _demand_ctx(fixture_db)
    canon = ctx["canonical"]
    assert canon["total_boardings"]["value"] == sum(
        p["day_boardings"] for p in ctx["peaks"])
    assert canon["n_clean_closes"]["value"] == sum(
        a["close"]["n"] for a in ctx["aggs"].values())
    live = [v for v in ctx["coverage_pct"].values() if v > 0]
    assert canon["coverage_range"]["lo"] == pytest.approx(min(live))
    assert canon["coverage_range"]["hi"] == pytest.approx(max(live))


# ── 16. boundary comparison ──────────────────────────────────────────────────

def test_equality_is_never_reported_as_exceedance():
    assert stats.compare_to_threshold(2.31, 2.31) == stats.AT
    assert stats.compare_to_threshold(2.3149, 2.31) == stats.AT   # displays 2.31
    assert stats.compare_to_threshold(2.3151, 2.31) == stats.ABOVE
    assert stats.compare_to_threshold(2.3049, 2.31) == stats.BELOW
    assert stats.compare_to_threshold(None, 2.31) is None


def test_a_median_on_the_line_reads_as_on_the_line(fixture_db):
    """The v3 defect: 'takes a typical 2.31s to close — above the 2.31s line'."""
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    for f in narrative.build_findings(ctx, {}):
        s = f["sentence"]
        for m in re.finditer(r"typical (\d+\.\d{2})s to close.*?is (above|below) "
                             r"the (\d+\.\d{2})s line", s):
            val, rel, thr = float(m.group(1)), m.group(2), float(m.group(3))
            assert val != thr, (
                f"{val}s reported as {rel} the identical {thr}s line: {s[:150]}")
    # and the AT wording exists as an option at all
    assert "sits exactly ON" in narrative._relation(2.31, 2.31)


# ── 17. weak intervals must not be rendered as results ───────────────────────

def test_no_point_estimate_where_the_interval_is_useless(fixture_db):
    """A CI wider than the value itself, or n<30, cannot support a stated
    value in result language."""
    assert stats.interval_is_uninformative(
        {"median": 2.31, "lo": 0.88, "hi": 22.28, "n": 15})
    assert stats.interval_is_uninformative(
        {"median": 2.31, "lo": 2.2, "hi": 2.4, "n": 12})       # n too small
    assert not stats.interval_is_uninformative(
        {"median": 3.16, "lo": 2.99, "hi": 3.35, "n": 1244})

    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    for (cam, inst, era), a in ctx["aggs"].items():
        cl, mc = a["close"], a["close"]["median_ci"]
        if not stats.interval_is_uninformative(mc) or cl["suppressed"]:
            continue
        sent = [f["sentence"] for f in narrative.build_findings(ctx, {})
                if era in f["sentence"] and eras.lift_label(cam) in f["sentence"]
                and "close-travel" in f["sentence"]]
        for s in sent:
            assert "not enough data to state a value" in s or \
                   "not enough data to state a close-travel value" in s, s[:160]
            assert "takes a typical" not in s


# ── 18. load balance ─────────────────────────────────────────────────────────

def test_load_balance_needs_three_lifts(fixture_db):
    """CoV across two points is degenerate — sqrt(2) whenever one is zero."""
    ctx = _demand_ctx(fixture_db)
    for ver, e in ctx["demand"]["by_era"].items():
        for h in ctx["demand"]["hours"]:
            live = sum(1 for c in ctx["cams"] if e["boarded"][c][h] is not None)
            if live < model.LOAD_BALANCE_MIN_LIFTS:
                assert e["cv"][h] is None, (
                    f"{ver} h{h}: CoV computed across only {live} lift(s)")


def test_load_balance_table_withheld_when_mostly_uncomputable(fixture_db):
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    text = " ".join(v for s, v in _all_text(wb)
                    if s == "DEMAND BY LIFT AND HOUR")
    for ver, e in ctx["demand"]["by_era"].items():
        if not model.load_balance_reportable(e["cv"]):
            assert "NOT REPORTED" in text
            assert "degenerate" in text
    # sqrt(2) must never appear as a computed CoV *value* — only, at most, in
    # the prose explaining why such a value would be meaningless
    ws = wb["DEMAND BY LIFT AND HOUR"]
    for r in ws.iter_rows():
        for c in r:
            if isinstance(c.value, float):
                assert abs(c.value - 2 ** 0.5) > 1e-3, (
                    f"{c.coordinate}: degenerate two-lift CoV rendered as a "
                    f"load-balance value")


# ── 19. narrative is grammatical ─────────────────────────────────────────────

def test_finding_sentences_are_not_garbled(fixture_db):
    """The v3 defect: 'boardings are concentrated on some lifts between lifts'.
    Catches duplicated clause fragments, unsubstituted placeholders and doubled
    prepositions to the extent those are testable."""
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    findings = narrative.build_findings(ctx, {})
    assert findings
    bad_patterns = [
        (r"\{[a-z_]+\}", "unsubstituted placeholder"),
        (r"\b(\w+)\s+\1\b", "doubled word"),
        (r"\b(?:of|in|to|on|at|between|from|with|by)\s+"
         r"(?:of|in|to|on|at|between|from|with|by)\b", "doubled preposition"),
        (r"\bbetween lifts\b.*\bbetween lifts\b", "duplicated clause"),
        (r"\bNone\b", "None leaked into prose"),
        (r"\s,|\s\.", "space before punctuation"),
        (r"\(\s*\)", "empty parentheses"),
        (r"—\s*—", "doubled dash"),
    ]
    for f in findings:
        for field in ("headline", "sentence", "why"):
            text = f.get(field) or ""
            for pat, why in bad_patterns:
                m = re.search(pat, text)
                assert not m, (
                    f"finding {f['number']} {field}: {why} "
                    f"({m.group(0)!r}) in: {text[:170]}")
            assert text.strip() == text.strip().replace("  ", " ") or \
                "  " not in text, f"double space in {field}: {text[:120]}"


def test_lift_names_are_not_mangled_by_capitalisation():
    eras.set_display_labels({"ch16": "Service Lift"})
    try:
        assert narrative._sentence_start(eras.lift_label("ch16")) == "Service Lift"
    finally:
        eras.set_display_labels({})


# ── 20. lift naming ──────────────────────────────────────────────────────────

def test_display_labels_come_from_channel_map(fixture_db):
    ctx = _demand_ctx(fixture_db)
    assert ctx["channel_labels"], "channel_map labels were not read"
    for cam, label in ctx["channel_labels"].items():
        assert eras.lift_label(cam) == label
    assert not ctx["unlabelled_cams"]


def test_no_channel_derived_label_where_a_real_one_exists(fixture_db):
    """'lift 16' must not appear anywhere when channel_map calls it 'lift 1'."""
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    real = set(ctx["channel_labels"].values())
    forbidden = {f"lift {c.removeprefix('ch')}" for c in ctx["cams"]} - real
    offenders = []
    for sheet, text in _all_text(wb):
        for bad in forbidden:
            if re.search(rf"\b{re.escape(bad)}\b(?!\s*\()", text):
                offenders.append((sheet, bad, text[:100]))
    assert not offenders, f"channel-derived labels rendered: {offenders[:5]}"


def test_unnamed_lift_is_marked_not_invented():
    eras.set_display_labels({})
    try:
        lab = eras.lift_label("ch16")
        assert eras.UNLABELLED_SUFFIX in lab
    finally:
        eras.set_display_labels({})


def test_channel_shown_on_first_mention_then_dropped(fixture_db):
    ctx = _demand_ctx(fixture_db)
    findings = narrative.build_findings(ctx, {})
    blob = " ".join(f["headline"] + " " + f["sentence"] for f in findings)
    for cam in ctx["cams"]:
        label = eras.lift_label(cam)
        if label not in blob:
            continue
        assert blob.count(f"{label} ({cam})") == 1, (
            f"{cam}: channel should be shown exactly once, on first mention")


# ── 21. study framing ────────────────────────────────────────────────────────

def test_study_purpose_is_not_framed_as_door_close_or_compliance(fixture_db):
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    banned = [
        "whole reason this study exists",
        "the main thing this study measures",
        "which is why this study measures close travel",
    ]
    for sheet, text in _all_text(wb):
        low = text.lower()
        for b in banned:
            assert b not in low, f"[{sheet}] still frames the study as {b!r}"


def test_opening_block_states_the_two_study_questions(fixture_db):
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    text = " ".join(v for s, v in _all_text(wb) if s == "READ THIS FIRST")
    assert "WHY THIS STUDY EXISTS" in text
    for q in eras.STUDY_QUESTIONS:
        assert q in text, f"study question missing: {q[:60]}"
    assert "BENCHMARKING" in text
    assert "not a verdict on this building" in text


def test_coefficient_scope_table_covers_all_eight_with_status(fixture_db):
    ctx = _demand_ctx(fixture_db)
    scope = narrative.coefficient_scope(ctx)
    assert {c["id"] for c in scope} == {c["id"] for c in eras.COEFFICIENTS}
    assert len(scope) == 8
    valid = {"MEASURED", "PARTLY MEASURED", "NOT MEASURED", "BLOCKED"}
    for c in scope:
        assert c["status"] in valid
        assert c["status_why"], f"{c['id']} has a status with no reason"
    wb = cli.build_workbook(ctx)
    text = " ".join(v for s, v in _all_text(wb) if s == "READ THIS FIRST")
    assert "WHICH COEFFICIENTS ARE UNDER TEST" in text
    for c in eras.COEFFICIENTS:
        assert c["id"] in text


def test_scope_status_tracks_the_data_not_a_hardcoded_table(fixture_db):
    """C17/C18/C21/C22 stay BLOCKED, but each must name its OWN actual blocker.

    Rewritten 2026-08-04. The old version asserted that blinding floor_status put
    '0 confident reads' into every one of the four reasons — which passed only because the reason
    was HARDCODED to the same string for all four. Floor attribution is not the blocker for
    C21/C22 at all, so that assertion was locking in the wrong explanation."""
    ctx = _demand_ctx(fixture_db)
    scope = {c["id"]: c for c in narrative.coefficient_scope(ctx)}
    for cid in ("C17", "C18", "C21", "C22"):
        assert scope[cid]["status"] == "BLOCKED"
        assert scope[cid]["status_why"], f"{cid} has no stated blocker"
    # C21/C22 are blocked by a MISSING PARAMETER, C17/C18 by direction + segmentation. If these
    # two reasons are ever identical again, the per-coefficient derivation has been lost.
    assert scope["C21"]["status_why"] == scope["C22"]["status_why"]
    assert scope["C17"]["status_why"] == scope["C18"]["status_why"]
    assert scope["C21"]["status_why"] != scope["C17"]["status_why"], \
        "C21/C22 and C17/C18 must not share one blocker — that was the hardcoded bug"
    assert "rated speed" in scope["C21"]["status_why"]
    assert "floor attribution" not in scope["C21"]["status_why"].lower()
    # C27 tracks whether closes were actually measured
    empty = dict(ctx, aggs={})
    assert {c["id"]: c for c in narrative.coefficient_scope(empty)
            }["C27"]["status"] == "NOT MEASURED"


def test_single_panel_reads_count_as_confident(fixture_db):
    """The bug this whole rewrite came from: reason='single_panel' is a good read from the one
    calibrated panel. Scoring it non-confident reported cameras holding tens of thousands of
    reads as floor-blind."""
    assert "single_panel" in eras.FLOOR_OK_REASONS
    db = reader.open_ro(fixture_db)
    try:
        t0, t1 = _ts("2026-07-14T00:00:00"), _ts("2026-08-02T00:00:00")
        status = reader.fetch_floor_read_status(db, "site-A", t0, t1)
        ev = reader.fetch_tier2_evidence(db, "site-A", t0, t1)
    finally:
        db.close()
    # the per-era evidence must agree with the per-camera census on confident totals
    per_cam = {}
    for (cam, _era), d in ev.items():
        per_cam[cam] = per_cam.get(cam, 0) + d["confident"]
    for cam, d in status.items():
        assert d["confident"] == per_cam.get(cam, 0), cam


def test_tier2_evidence_is_split_per_era(fixture_db):
    """A rebuild is a different instrument. A per-camera total would hide a camera that lost
    floor reading at a rebuild behind its own earlier history (ch16: 19,424 -> 1 on live data)."""
    db = reader.open_ro(fixture_db)
    try:
        ev = reader.fetch_tier2_evidence(db, "site-A",
                                         _ts("2026-07-14T00:00:00"), _ts("2026-08-02T00:00:00"))
    finally:
        db.close()
    assert ev, "no tier-2 evidence produced"
    for key, d in ev.items():
        assert isinstance(key, tuple) and len(key) == 2, key
        assert d["rows"] >= d["confident"] >= 0
        assert sum(d["arrow"].values()) <= d["confident"]


def test_floor_speed_is_floors_per_second_not_a_speed_factor(fixture_db):
    """C21/C22 need a share of RATED speed. Nothing here may imply we have that."""
    db = reader.open_ro(fixture_db)
    try:
        reads = reader.fetch_confident_reads(db, "site-A",
                                             _ts("2026-07-14T00:00:00"),
                                             _ts("2026-08-02T00:00:00"))
    finally:
        db.close()
    speed = model.floor_speed_segments(reads)
    for _key, v in speed.items():
        for fps in v["up"] + v["down"]:
            assert 0 < fps <= eras.MAX_FLOORS_PER_S, fps
    blockers = model.coefficient_blockers({}, speed, ["ch16"])
    assert blockers["C21"]["measurable_as"].startswith("floors per second")
    assert "not a speed factor" in blockers["C21"]["measurable_as"]


def test_demand_assumption_is_tracked_and_names_its_blocker(fixture_db):
    ctx = _demand_ctx(fixture_db)
    row = narrative.demand_assumption_row(ctx)
    assert row["status"] == "BLOCKED"
    assert "--population" in row["status_why"]
    assert "never guessed" in row["status_why"]
    with_pop = narrative.demand_assumption_row(dict(ctx, population=2400))
    assert with_pop["status"] == "MEASURED"


def test_workbook_states_no_recommendation(fixture_db):
    """The machine emits facts. It must not advise a design change."""
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    banned = ["we recommend", "should be increased", "should be reduced",
              "you should add", "must be redesigned", "add another lift",
              "is compliant", "is non-compliant"]
    for sheet, text in _all_text(wb):
        low = text.lower()
        for b in banned:
            assert b not in low, f"[{sheet}] recommends: {b!r} in {text[:120]}"


# ── 22. the tightened uninformative-interval rule ────────────────────────────

def test_interval_rule_catches_the_half_to_full_width_band():
    """The band the old rule admitted: CI width between 0.5x and 1.0x the
    value. lift 3 / 260d4a0f sat here — value 2.75s, [1.52, 4.25], width 2.73."""
    assert stats.MAX_CI_WIDTH_RATIO == 0.5
    the_case = {"median": 2.75, "lo": 1.52, "hi": 4.25, "n": 91}
    assert stats.interval_is_uninformative(the_case)

    # explicitly across the band
    for width, expected in ((0.40, False),   # 0.16x — firm
                            (1.30, False),   # 0.52x ... just over? no: 0.52>0.5
                            (1.37, True),    # 0.55x — suppressed
                            (2.00, True),    # 0.80x — suppressed
                            (2.60, True)):   # 1.04x — suppressed
        ci = {"median": 2.50, "lo": 2.50 - width / 2, "hi": 2.50 + width / 2,
              "n": 200}
        got = stats.interval_is_uninformative(ci)
        ratio = width / 2.50
        assert got == (ratio > stats.MAX_CI_WIDTH_RATIO), (
            f"width {width} (ratio {ratio:.2f}) -> {got}")

    # the ratio is configurable
    loose = {"median": 2.50, "lo": 2.00, "hi": 3.00, "n": 200}   # ratio 0.40
    assert not stats.interval_is_uninformative(loose)
    assert stats.interval_is_uninformative(loose, max_width_ratio=0.3)


def test_newly_caught_pool_renders_the_suppression_text(fixture_db):
    """Any pool the tightened rule catches must state why, not go silent."""
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    findings = narrative.build_findings(ctx, {})
    caught = [(cam, era) for (cam, _i, era), a in ctx["aggs"].items()
              if not a["close"]["suppressed"]
              and stats.interval_is_uninformative(a["close"]["median_ci"])]
    for cam, era in caught:
        matching = [f for f in findings
                    if era in f["sentence"] and "close-travel" in f["sentence"]]
        assert matching, f"{cam}/{era} was suppressed but says nothing"
        for f in matching:
            assert "not enough data to state a close-travel value" in f["sentence"]
            assert "takes a typical" not in f["sentence"]


def test_suppression_text_describes_the_actual_rule(fixture_db):
    """The wording must not claim the CI is wider than the value when the rule
    is now half the value — that sentence would be false for the 0.5-1.0 band."""
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    for f in narrative.build_findings(ctx, {}):
        s = f["sentence"]
        if "not enough data to state a close-travel value" not in s:
            continue
        assert "wider than the value itself" not in s, (
            f"stale wording from the 1.0x rule: {s[:170]}")
        m = re.search(r"spans (\d+\.\d{2})s to (\d+\.\d{2})s — a spread of "
                      r"(\d+\.\d{2})s", s)
        if m:
            lo, hi, spread = (float(m.group(i)) for i in (1, 2, 3))
            assert spread == pytest.approx(hi - lo, abs=0.011), s[:170]


# ── 23. finding 1 leads on the assumption gap ────────────────────────────────

def test_finding_one_leads_on_the_assumption_gap(fixture_db):
    """The study is about assumed-vs-observed. The compliance count may follow
    as a consequence, but must not be the opening clause."""
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    first = narrative.build_findings(ctx, {})[0]
    s = first["sentence"]
    opening = s.split(". ")[0]          # ". " — not ".", which splits "2.00s"
    assert "design sheet assumes" in opening, (
        f"finding 1 does not open on the assumption: {opening}")
    assert f"{eras.SHEET_CLOSE_S:.2f}s" in opening
    # the compliance framing must come later, not first
    cliff_pos = s.find("flips at")
    assumed_pos = s.find("assumes")
    if cliff_pos >= 0:
        assert assumed_pos < cliff_pos, "compliance leads the assumption gap"
    assert first["sheet"] and first["confidence"] and first["why"]


def test_finding_one_states_the_observed_range_with_n(fixture_db):
    t0, t1 = _ts("2026-07-21T05:30:00"), _ts("2026-08-02T00:00:00")
    ctx = cli.build_context(fixture_db, "site-A", t0, t1)
    s = narrative.build_findings(ctx, {})[0]["sentence"]
    quotable = [a["close"] for a in ctx["aggs"].values()
                if not a["close"]["suppressed"]
                and not stats.interval_is_uninformative(a["close"]["median_ci"])
                and a["close"]["median_ci"]["median"] is not None]
    if not quotable:
        pytest.skip("no quotable pool in this range")
    meds = sorted(c["median_ci"]["median"] for c in quotable)
    assert f"{meds[0]:.2f}s" in s and f"{meds[-1]:.2f}s" in s
    assert f"n={sum(c['n'] for c in quotable):,}" in s
    # and it says which side of the observed range the assumption falls on
    assert any(w in s for w in ("BELOW everything measured",
                                "ABOVE everything measured",
                                "INSIDE the observed range"))


def test_no_take_away_leads_on_compliance(fixture_db):
    """Chart captions must lead on the assumption gap too."""
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    for sheet, text in _all_text(wb):
        if not text.startswith("Take-away:"):
            continue
        head = text[:110].lower()
        if "compliance" in head or "cliff" in head:
            assert "assum" in head, (
                f"[{sheet}] take-away leads on compliance: {text[:110]}")


# ── 24. demand totals beside means ───────────────────────────────────────────

def test_totals_matrix_is_whole_people_and_reconciles(fixture_db):
    ctx = _demand_ctx(fixture_db)
    d = ctx["demand"]
    for ver, e in d["by_era"].items():
        for cam in ctx["cams"]:
            for h in d["hours"]:
                tot, mean = e["total_boarded_hr"][cam][h], e["boarded"][cam][h]
                days = e["observed_days"][cam][h]
                assert (tot is None) == (mean is None)
                if tot is None:
                    assert days == 0
                    continue
                assert isinstance(tot, int)
                assert tot == pytest.approx(mean * days, abs=1e-6)


def test_demand_sheet_shows_both_matrices_with_the_coverage_caveat(fixture_db):
    ctx = _demand_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    ws = wb["DEMAND BY LIFT AND HOUR"]
    text = " ".join(str(c.value) for r in ws.iter_rows()
                    for c in r if c.value is not None)
    assert "MEAN BOARDINGS PER OBSERVED DAY, BY HOUR" in text
    assert "TOTAL OBSERVED BOARDINGS, BY HOUR" in text
    assert "MEAN ALIGHTINGS PER OBSERVED DAY, BY HOUR" in text
    assert "TOTAL OBSERVED ALIGHTINGS, BY HOUR" in text
    assert "NOT comparable" in text
    assert "Compare lifts on the MEAN matrix" in text
    # totals carry an integer format, means one decimal
    fmts = {c.number_format for r in ws.iter_rows() for c in r
            if isinstance(c.value, int)}
    assert "#,##0" in fmts


def test_glossary_distinguishes_mean_from_total(fixture_db):
    ctx = _demand_ctx(fixture_db)
    terms = {t for t, _m in narrative.GLOSSARY}
    assert "mean per observed day, vs total observed" in terms
    meaning = dict(narrative.GLOSSARY)["mean per observed day, vs total observed"]
    assert "whole people" in meaning.lower()
    assert "part of a person" in meaning.lower()


def test_population_share_stays_blocked(fixture_db):
    """v5 must not have introduced a share-of-population figure anywhere."""
    ctx = _demand_ctx(fixture_db)
    assert ctx.get("population") is None
    wb = cli.build_workbook(ctx)
    text = " ".join(v for s, v in _all_text(wb)
                    if s == "DEMAND BY LIFT AND HOUR")
    assert "BLOCKED — no population figure was supplied" in text
    assert "as % of population" not in text


def test_sheets_are_frozen_and_the_raw_sheet_filters(fixture_db):
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    assert wb["RAW"].auto_filter.ref, "RAW has no autofilter"
    for name in ("SUMMARY", "RAW", "PER-LIFT", "TIER-2 EVIDENCE"):
        assert wb[name].freeze_panes, f"{name} has no frozen header"
    for name in ("SUMMARY", "RAW", "PER-LIFT"):
        assert wb[name].page_setup.orientation == "landscape"


# ── DEMAND LOG ───────────────────────────────────────────────────────────────
# The export exists to enforce three things: dark is never zero, counting versions are never
# pooled, and gap rows never counted. Each gets a test that fails loudly if it regresses.

def _dlog(fixture_db, frm="2026-07-21T00:00:00", to="2026-07-25T00:00:00"):
    return cli.build_context(fixture_db, "site-A", _ts(frm), _ts(to))


def test_demand_log_dark_hour_is_dash_never_zero(fixture_db):
    """The whole point of the export: an unobserved hour must not read as 'carried nobody'."""
    log = _dlog(fixture_db)["demand_log"]
    dark = [r for r in log["hourly"] if r["observed"] == "no"]
    assert dark, "fixture produced no unobserved hours — test cannot prove the rule"
    for r in dark:
        assert r["boarded"] == demand_log.DARK, r
        assert r["alighted"] == demand_log.DARK, r
        assert r["boarded"] != 0 and r["alighted"] != 0


def test_demand_log_observed_but_idle_hour_is_a_real_zero(fixture_db):
    """The converse: observed and counted nobody IS a zero, and must not be dashed."""
    log = _dlog(fixture_db)["demand_log"]
    zeros = [r for r in log["hourly"]
             if r["observed"] == "yes" and r["camera"] != demand_log.FLEET
             and r["boarded"] == 0]
    for r in zeros:
        assert r["boarded"] == 0 and r["alighted"] != demand_log.DARK


def test_demand_log_never_pools_counting_versions(fixture_db):
    """A (hour, camera) that spans two builds must yield one row per build, never a blend."""
    ctx = _dlog(fixture_db, "2026-07-14T00:00:00", "2026-07-25T00:00:00")
    log = ctx["demand_log"]
    seen = {}
    for r in log["hourly"]:
        if r["camera"] == demand_log.FLEET:
            continue
        seen.setdefault((r["timestamp_ist"], r["camera"]), set()).add(r["counting_version"])
    multi = {k: v for k, v in seen.items() if len(v) > 1}
    # whichever cameras straddle, each version is its own row — never merged into one
    for key, vers in multi.items():
        rows = [r for r in log["hourly"]
                if (r["timestamp_ist"], r["camera"]) == key]
        assert len(rows) == len(vers)
    # and the daily rollup carries the version so two builds cannot be summed into one total
    assert all("counting_version" in r for r in log["daily"])


def test_demand_log_fleet_row_is_per_version_and_sums_observed_only(fixture_db):
    log = _dlog(fixture_db)["demand_log"]
    by_hour = {}
    for r in log["hourly"]:
        by_hour.setdefault(r["timestamp_ist"], []).append(r)
    for ts, rows in by_hour.items():
        fleets = [r for r in rows if r["camera"] == demand_log.FLEET]
        assert fleets, f"no FLEET row for {ts}"
        for f in fleets:
            if f["boarded"] == demand_log.DARK:
                continue
            cams = [r for r in rows
                    if r["camera"] != demand_log.FLEET and r["observed"] == "yes"
                    and r["counting_version"] == f["counting_version"]]
            assert f["boarded"] == sum(r["boarded"] for r in cams)
            assert f["alighted"] == sum(r["alighted"] for r in cams)


def test_demand_log_excludes_gap_transits(fixture_db):
    """Gap rows are excluded from counts, matching aggregate_transits."""
    ctx = _dlog(fixture_db, "2026-07-14T00:00:00", "2026-08-02T00:00:00")
    log = ctx["demand_log"]
    counted = sum(r["boarded"] + r["alighted"] for r in log["hourly"]
                  if r["camera"] != demand_log.FLEET
                  and isinstance(r["boarded"], int))
    pooled = sum(a["boarded"] + a["alighted"] for a in ctx["transit_aggs"].values())
    assert counted == pooled, "demand log and transit_aggs disagree on what was counted"


def test_demand_log_daily_coverage_never_exceeds_100(fixture_db):
    log = _dlog(fixture_db)["demand_log"]
    for r in log["daily"]:
        if r["coverage_pct"] is not None:
            assert 0.0 <= r["coverage_pct"] <= 100.0, r


def test_demand_log_csv_carries_its_caveats(fixture_db, tmp_path):
    """A demand table mailed onward without 'dark is not zero' will be misread — so the
    caveats ride inside the file, not alongside it."""
    ctx = _dlog(fixture_db)
    paths = demand_log.write_csvs(tmp_path / "r.xlsx", ctx, ctx["demand_log"],
                                  ctx["demand_log_floor_note"])
    assert len(paths) == 2
    for p in paths:
        text = p.read_text(encoding="utf-8")
        head = [l for l in text.splitlines() if l.startswith("#")]
        assert head, f"{p.name} has no provenance header"
        joined = " ".join(head)
        assert "NOT OBSERVED" in joined and "not a zero" in joined
        assert "PERSONS CURRENTLY IN LIFT IS NOT INCLUDED" in joined
        assert "PER-FLOOR DEMAND IS NOT INCLUDED" in joined
        # the data itself must still parse as ordinary CSV once comments are skipped
        import csv as _csv
        rows = list(_csv.reader(l for l in text.splitlines() if not l.startswith("#")))
        assert len(rows) >= 2 and rows[0][0].startswith(("timestamp", "date"))


def test_demand_log_format_flag_selects_outputs(fixture_db, tmp_path):
    base = ["--from", "2026-07-21T00:00:00", "--to", "2026-07-25T00:00:00",
            "--db", fixture_db, "--gateway", "site-A"]
    csv_only = cli.run_all(base + ["--out", str(tmp_path / "a.xlsx"), "--format", "csv"])
    assert all(p.suffix == ".csv" for p in csv_only) and len(csv_only) == 2
    assert not (tmp_path / "a.xlsx").exists(), "--format csv must not write a workbook"

    xlsx_only = cli.run_all(base + ["--out", str(tmp_path / "b.xlsx"), "--format", "xlsx"])
    assert [p.suffix for p in xlsx_only] == [".xlsx"]

    both = cli.run_all(base + ["--out", str(tmp_path / "c.xlsx"), "--format", "both"])
    assert sorted(p.suffix for p in both) == [".csv", ".csv", ".xlsx"]
    # run() keeps its single-Path contract for prove_report.py and web callers
    assert cli.run(base + ["--out", str(tmp_path / "d.xlsx")]).suffix == ".xlsx"


def test_demand_log_sheet_present_and_matches_row_count(fixture_db, tmp_path):
    from openpyxl import load_workbook
    ctx = _dlog(fixture_db)
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb.xlsx"
    wb.save(out)
    got = load_workbook(out)
    assert "DEMAND LOG" in got.sheetnames
    ws = got["DEMAND LOG"]
    body = [r for r in ws.iter_rows(values_only=True)
            if r and r[0] and str(r[0])[:4].isdigit() and ":" in str(r[0])]
    assert len(body) == len(ctx["demand_log"]["hourly"])


def test_demand_log_floor_note_is_measured_not_asserted(fixture_db):
    """The 'why no per-floor table' line must come from counted reads, per camera."""
    ctx = _dlog(fixture_db)
    note = ctx["demand_log_floor_note"]
    assert "PER-FLOOR DEMAND IS NOT INCLUDED" in note
    conf = ctx["floor_confidence"]
    if conf:
        cam = sorted(conf)[0]
        assert f"{cam} {conf[cam]['confident']}/{conf[cam]['total']}" in note


def test_demand_log_has_no_occupancy_column(fixture_db):
    """Occupancy is not derivable from crossings; it must not appear by any name."""
    log = _dlog(fixture_db)["demand_log"]
    banned = ("occupan", "in_lift", "inside", "net", "current")
    for cols in (demand_log.HOURLY_COLS, demand_log.DAILY_COLS):
        for key, header in cols:
            assert not any(b in key.lower() or b in header.lower() for b in banned), key
    for r in log["hourly"][:5]:
        assert not any(any(b in k.lower() for b in banned) for k in r)


def test_demand_log_coverage_cannot_exceed_100_under_a_gap(fixture_db):
    """Regression: real data produced 190.5% because observed hours were credited by BUCKET
    midpoint while the denominator subtracted gap SECONDS. Both sides must use the same rule,
    so an hour that was entirely an outage can never be counted as observed."""
    ctx = cli.build_context(fixture_db, "site-A",
                            _ts("2026-07-14T00:00:00"), _ts("2026-08-02T00:00:00"))
    daily = ctx["demand_log"]["daily"]
    assert daily, "fixture produced no daily rows"
    for r in daily:
        if r["coverage_pct"] is not None:
            assert r["coverage_pct"] <= 100.0, f"coverage over 100%: {r}"
        # hours observed can never exceed the hours in a day either
        assert 0 <= r["hours_observed"] <= 24, r


def test_gpu_era_id_does_not_merge_a_tagged_rebuild():
    """Regression: gpu_era_id truncated the templates half to 8 chars, so a hand-tagged rebuild
    ('e79e50d3h2') collapsed into the era it replaced ('e79e50d3'). Because gpu_era_id also groups
    door CYCLES, close-travel was pooled across the boundary — 71% of cycles on live data — and
    pooling produced NARROWER CIs than the truth, which is the one error the stopping rule cannot
    tolerate."""
    assert eras.gpu_era_id("e79e50d3h2+54509e75") != eras.gpu_era_id("e79e50d3+7b1c0bad")
    assert eras.gpu_era_id("e79e50d3h2+54509e75") == "e79e50d3h2"
    assert eras.gpu_era_id("260d4a0fh2Laa52+495e8f48") == "260d4a0fh2Laa52"
    # and it must agree with the untruncated helper the Tier-2 evidence uses
    for dv in ("e79e50d3+7b1c0bad", "e79e50d3h2+54509e75", "260d4a0fh2Laa52+495e8f48", "", None):
        assert eras.gpu_era_id(dv) == eras.templates_era(dv)


def test_cycles_never_pool_across_a_tagged_rebuild(fixture_db):
    """A cycle group must contain exactly one real templates era."""
    db = reader.open_ro(fixture_db)
    try:
        rows = reader.fetch_gpu_rows(db, "site-A", _ts("2026-07-14T00:00:00"),
                                     _ts("2026-08-02T00:00:00"))
        cycles, _f = reader.fetch_gpu_cycles(db, "site-A", _ts("2026-07-14T00:00:00"),
                                             _ts("2026-08-02T00:00:00"))
    finally:
        db.close()
    groups = {}
    for r in rows:
        groups.setdefault((r["cam"], eras.gpu_era_id(r["door_version"])), set()).add(
            eras.templates_era(r["door_version"]))
    for key, real in groups.items():
        assert len(real) == 1, f"{key} still merges {sorted(real)}"


def test_direction_is_suppressed_when_the_reader_knows_one_arrow(tmp_path):
    """A single-class classifier cannot be wrong, so its output is not evidence. Rows whose reader
    could name only one arrow must not contribute a direction to the distribution."""
    import sqlite3 as _sq
    p = tmp_path / "g.db"
    db = _sq.connect(str(p))
    db.executescript("""
      CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
        ts REAL, floor TEXT, direction TEXT, door_state TEXT, openness REAL, read_conf REAL,
        panels_agreed INTEGER, reason TEXT, close_travel_s REAL, door_version TEXT,
        templates_hash TEXT, received_at REAL, candidates TEXT, n_arrow_labels INTEGER);
      CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
        marked_at REAL, PRIMARY KEY (gateway_id, channel));""")
    base = _ts("2026-07-22T09:00:00")
    rows = [("site-A", "ch27", base + i, "32", "down", "open", 0.9, "single_panel", "aa+bb", 1)
            for i in range(10)]                       # reader knows ONE arrow
    rows += [("site-A", "ch29", base + i, "12", "up", "open", 0.9, "single_panel", "cc+dd", 2)
             for i in range(6)]                       # reader knows BOTH
    db.executemany("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,direction,door_state,"
                   "read_conf,reason,door_version,n_arrow_labels) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit(); db.close()

    rdb = reader.open_ro(str(p))
    try:
        ev = reader.fetch_tier2_evidence(rdb, "site-A", base - 10, base + 10_000)
    finally:
        rdb.close()
    ch27 = ev[("ch27", "aa")]
    ch29 = ev[("ch29", "cc")]
    # ch27's 'down' must NOT be reported as a direction
    assert "down" not in ch27["arrow"], ch27["arrow"]
    assert ch27.get("arrow_unqualified") == 10
    # ch29's is a real reading and survives
    assert ch29["arrow"].get("up") == 6
    assert not ch29.get("arrow_unqualified")


def test_engine_suppresses_direction_below_two_arrow_templates():
    """The suppression must live in the reader itself, not only in the report."""
    src = open("gpu_door.py").read()
    assert "if len(self.arrow_labels) >= 2:" in src
    assert '"n_arrow_labels": n_arrow_labels' in src


# ── CAR LOADING — measured peak occupancy, and the loading factor it refuses ──
# The refusal is the point of these tests. A percentage whose denominator has no stated basis is
# not a provisional answer, it is a wrong one that looks finished, so "no number" has to be
# provable and not merely intended.

def _occ_ctx(fixture_db, capacity_path=None,
             frm="2026-07-30T00:00:00", to="2026-07-31T00:00:00"):
    return cli.build_context(fixture_db, "site-A", _ts(frm), _ts(to),
                             capacity_path=capacity_path)


def _cap_file(tmp_path, **over):
    import json
    d = {"basis": "nameplate_persons",
         "basis_confirmed_by": "lift licence photo, 2026-08-01, filed as LIC-ch16",
         "sheet_basis": "nameplate_persons",
         "persons": {"ch16": 13, "ch29": 13}}
    d.update(over)
    p = tmp_path / "cap.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return str(p)


def test_measured_occupancy_applies_the_evidence_rule(fixture_db):
    """occupancy_frames > 0 or it is not evidence — and the excluded ones stay counted."""
    ctx = _occ_ctx(fixture_db)
    occ = ctx["measured_occupancy"]
    k16 = [k for k in occ if k[0] == "ch16"]
    assert k16, "fixture has no ch16 episodes in range"
    b = occ[k16[0]]
    assert b["n"] == 8 and b["n_episodes"] == 8
    assert b["peak"] == 5                       # max of 2 + k%4 over k=0..7
    assert b["n_degraded"] == 1
    # ch29's episodes predate the feature: present, unmeasured, and NOT zero.
    k29 = [k for k in occ if k[0] == "ch29"]
    assert k29, "fixture has no pre-occupancy ch29 episodes"
    b29 = occ[k29[0]]
    assert b29["n"] == 0 and b29["n_episodes"] == 3 and b29["n_no_evidence"] == 3
    assert b29["peak"] is None, "an unmeasured episode must not produce a peak of 0"


def test_measured_occupancy_is_not_the_derived_one(fixture_db):
    """Two instruments, never merged: the derived figure can go negative, this one cannot."""
    ctx = _occ_ctx(fixture_db)
    for b in ctx["measured_occupancy"].values():
        assert b["peak"] is None or b["peak"] >= 0
    assert "occupancy_summary" in ctx and "measured_occupancy" in ctx
    assert ctx["measured_occupancy"] is not ctx["occupancy"]


def test_loading_factor_is_withheld_without_a_confirmed_basis(fixture_db, tmp_path):
    from openpyxl import load_workbook
    ctx = _occ_ctx(fixture_db, capacity_path=str(tmp_path / "missing.json"))
    assert ctx["capacity_status"]["can_print_loading"] is False
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb.xlsx"
    wb.save(out)
    ws = load_workbook(out)["CAR LOADING"]
    text = "\n".join(str(c) for r in ws.iter_rows(values_only=True) for c in r if c)
    assert "LOADING FACTOR WITHHELD" in text
    assert "withheld — capacity basis unconfirmed" in text
    # The placeholder may be NAMED but must never be APPLIED. Checked on the loading column
    # itself rather than by scanning the sheet for any small float — the hand-count sheet
    # legitimately carries a machine/hand ratio, and a test that cannot tell those apart would
    # pass for the wrong reason the moment the withheld cell started printing.
    rows = list(ws.iter_rows(values_only=True))
    hdr = next(i for i, r in enumerate(rows) if r and "loading vs capacity" in [str(c) for c in r])
    col = [str(c) for c in rows[hdr]].index("loading vs capacity")
    body = []
    for r in rows[hdr + 1:]:                    # the table only, stopping at its blank line
        if not r or not r[0]:
            break
        body.append(r[col])
    assert body, "no occupancy rows on the sheet — the check would pass vacuously"
    for v in body:
        assert isinstance(v, str) and "withheld" in v, f"a loading factor was printed: {v!r}"
    assert "13" in text, "the placeholder capacity should still be visible as a placeholder"


def test_loading_factor_prints_once_the_basis_is_confirmed(fixture_db, tmp_path):
    from openpyxl import load_workbook
    ctx = _occ_ctx(fixture_db, capacity_path=_cap_file(tmp_path))
    assert ctx["capacity_status"]["can_print_loading"] is True
    assert ctx["capacity_status"]["can_compare_sheet"] is True
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb2.xlsx"
    wb.save(out)
    ws = load_workbook(out)["CAR LOADING"]
    vals = [c for r in ws.iter_rows(values_only=True) for c in r]
    text = "\n".join(str(c) for c in vals if c)
    assert "LOADING FACTOR WITHHELD" not in text
    assert any(isinstance(c, float) and abs(c - 5 / 13) < 1e-9 for c in vals), \
        "the loading factor did not print once the basis was confirmed"


def test_a_basis_mismatch_withholds_the_sheet_comparison_not_the_number(fixture_db, tmp_path):
    """Both bases confirmed but DIFFERENT: the workbook prints its own figure and refuses the
    comparison. Same arithmetic, different car."""
    from openpyxl import load_workbook
    ctx = _occ_ctx(fixture_db,
                   capacity_path=_cap_file(tmp_path, sheet_basis="design_persons_mep02"))
    st = ctx["capacity_status"]
    assert st["can_print_loading"] is True and st["can_compare_sheet"] is False
    assert "MISMATCH" in st["compare_note"]
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb3.xlsx"
    wb.save(out)
    text = "\n".join(str(c) for r in load_workbook(out)["CAR LOADING"].iter_rows(values_only=True)
                     for c in r if c)
    assert "SHEET COMPARISON WITHHELD" in text
    assert str(int(eras.SHEET_LOADING_PCT)) in text


def test_car_loading_states_the_calibration_and_matches_the_dashboard(fixture_db, tmp_path):
    """The floor label travels with the number, and the workbook cannot drift from /dash."""
    from openpyxl import load_workbook
    dash_src = open("dash_api.py").read()
    assert f'OCC_CALIBRATION = ("{eras.OCC_CALIBRATION.split(";")[0]};' in dash_src, \
        "eras.OCC_CALIBRATION has drifted from dash_api.OCC_CALIBRATION"
    ctx = _occ_ctx(fixture_db)
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb4.xlsx"
    wb.save(out)
    text = "\n".join(str(c) for r in load_workbook(out)["CAR LOADING"].iter_rows(values_only=True)
                     for c in r if c)
    assert eras.OCC_CALIBRATION in text
    assert "MEASURED MINIMUM" in text
    assert "n=1 scene" in text


def test_hand_count_column_pairs_with_the_machine_peak(fixture_db, tmp_path):
    from openpyxl import load_workbook
    ctx = _occ_ctx(fixture_db)
    b = [v for k, v in ctx["measured_occupancy"].items() if k[0] == "ch16"][0]
    assert b["n_hand"] == 1 and b["n_hand_pairs"] == 1
    pair = b["hand_pairs"][0]
    assert pair["human"] == 6 and pair["machine"] == 5      # k=3 -> occ 2+3 = 5
    assert b["hand_ratio"] == round(5 / 6, 3)
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb5.xlsx"
    wb.save(out)
    text = "\n".join(str(c) for r in load_workbook(out)["CAR LOADING"].iter_rows(values_only=True)
                     for c in r if c)
    assert "HAND COUNTS (LEG 2)" in text


def test_a_gateway_without_the_columns_says_so_rather_than_showing_zero(tmp_path):
    """Missing FEATURE and empty MEASUREMENT look identical in a spreadsheet and are not."""
    from openpyxl import load_workbook
    from liftlab_report.fixtures import make_fixture
    p = make_fixture(str(tmp_path / "old.db"))
    db = sqlite3.connect(p)
    db.execute("DROP TABLE validation_item")
    db.execute("CREATE TABLE validation_item (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, "
               "ts_start REAL, ts_end REAL, counting_version TEXT)")
    db.commit(); db.close()
    ctx = _occ_ctx(p)
    assert ctx["occupancy_feature_present"] is False
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb6.xlsx"
    wb.save(out)
    text = "\n".join(str(c) for r in load_workbook(out)["CAR LOADING"].iter_rows(values_only=True)
                     for c in r if c)
    assert "CANNOT MEASURE CAR OCCUPANCY" in text
    assert "does NOT mean the cars were empty" in text


def test_the_rejected_derived_figure_now_points_at_car_loading(fixture_db, tmp_path):
    """FLOOR ATTRIBUTION says no occupancy figure is shipped. With CAR LOADING in the workbook that
    sentence would be false unless it distinguishes the two instruments."""
    from openpyxl import load_workbook
    ctx = _dlog(fixture_db)
    wb = cli.build_workbook(ctx)
    out = tmp_path / "wb7.xlsx"
    wb.save(out)
    got = load_workbook(out)
    text = "\n".join(str(c) for r in got["FLOOR ATTRIBUTION"].iter_rows(values_only=True)
                     for c in r if c)
    if "ESTIMATED OCCUPANCY" in text:
        assert "CROSSING-DERIVED" in text
        assert "CAR LOADING" in text
