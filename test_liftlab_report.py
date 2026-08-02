"""Unit tests for liftlab_report — era hygiene, gap exclusion, empty range,
read-only guarantee. Run: python -m pytest test_liftlab_report.py -q"""

import hashlib
import sqlite3
from datetime import datetime

import pytest

from liftlab_report import cli, eras, model, reader
from liftlab_report.fixtures import make_fixture


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
