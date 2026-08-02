"""Unit tests for liftlab_report — era hygiene, gap exclusion, empty range,
read-only guarantee, the one-frame quantization floor, and the
resolution-bound suppression rule.
Run: python -m pytest test_liftlab_report.py -q"""

import hashlib
import sqlite3
from datetime import datetime

import pytest
from openpyxl.utils.cell import coordinate_to_tuple

from liftlab_report import cli, eras, model, narrative, reader, stats
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
    open_f = [f for f in findings if "OPEN" in f["headline"].upper()]
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


def test_sheets_are_frozen_and_the_raw_sheet_filters(fixture_db):
    t0, t1 = _ts("2026-07-22T00:00:00"), _ts("2026-07-26T00:00:00")
    wb = cli.build_workbook(cli.build_context(fixture_db, "site-A", t0, t1))
    assert wb["RAW"].auto_filter.ref, "RAW has no autofilter"
    for name in ("SUMMARY", "RAW", "PER-LIFT", "TIER-2 BLOCKED"):
        assert wb[name].freeze_panes, f"{name} has no frozen header"
    for name in ("SUMMARY", "RAW", "PER-LIFT"):
        assert wb[name].page_setup.orientation == "landscape"
