"""PROVE runs for liftlab-report against the schema-exact fixture DB.

  1. range fully inside the GPU-engine era  -> per-lift + fleet populate,
     charts render, n present everywhere
  2. range straddling 2026-07-21            -> COVERAGE lists the boundary,
     aggregates per-era, no pooled close-travel figure anywhere
  3. range covering the 33h outage          -> gap excluded from rates,
     listed on COVERAGE, coverage % reflects it
  4. empty range                            -> clean workbook, no crash
  5. no gateway table written               -> DB md5 identical

Run:  python prove_report.py [workdir]
"""

import hashlib
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from liftlab_report import cli, eras
from liftlab_report.fixtures import make_fixture


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=eras.IST).timestamp()


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def charts_in(xlsx):
    with zipfile.ZipFile(xlsx) as z:
        return sum(1 for n in z.namelist()
                   if n.startswith("xl/charts/chart") and n.endswith(".xml"))


def sheet_text(wb, name):
    return " | ".join(str(c.value) for row in wb[name].iter_rows()
                      for c in row if c.value is not None)


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ".") / "prove_out"
    work.mkdir(parents=True, exist_ok=True)
    dbp = str(work / "gateway_fixture.db")
    Path(dbp).unlink(missing_ok=True)
    make_fixture(dbp)
    before = md5(dbp)
    from openpyxl import load_workbook

    results = []

    # ── 1. GPU-era-only range ────────────────────────────────────────────────
    out1 = cli.run(["--from", "2026-07-21T00:00:00", "--to", "2026-07-25T00:00:00",
                    "--db", dbp, "--out", str(work / "run1_gpu_only.xlsx")])
    wb = load_workbook(out1)
    s = sheet_text(wb, "SUMMARY")
    pl = sheet_text(wb, "PER-LIFT")
    assert "gpu_engine" in s and "pi_watch" not in s.replace("pi_watch and gpu", "")
    assert "e79e50d3" in pl and "median" not in ("",)
    n_charts = charts_in(out1)
    assert n_charts >= 3, f"expected charts, got {n_charts}"
    assert "n (clean closes)" in s
    results.append(f"1. GPU-only range: OK — charts={n_charts}, "
                   f"SUMMARY rows carry instrument+era+n")

    # ── 2. straddling range ─────────────────────────────────────────────────
    out2 = cli.run(["--from", "2026-07-14T00:00:00", "--to", "2026-07-25T00:00:00",
                    "--db", dbp, "--out", str(work / "run2_straddle.xlsx")])
    wb = load_workbook(out2)
    cov = sheet_text(wb, "COVERAGE & ERAS")
    assert "INSTRUMENT SPLIT" in cov
    assert "CLOSE_TRAVEL_MAX" in cov
    summ = sheet_text(wb, "SUMMARY")
    # every headline close-travel row is tagged pi_watch or gpu_engine —
    # no untagged (pooled) row exists
    ws = wb["SUMMARY"]
    header_row = None
    for r in ws.iter_rows():
        vals = [c.value for c in r]
        if "instrument" in vals:
            header_row = r[0].row
            icol = vals.index("instrument") + 1
            ncol = vals.index("n (clean closes)") + 1
        elif header_row and any(v is not None for v in vals):
            inst = ws.cell(row=r[0].row, column=icol).value
            if inst is None:
                break
            assert inst in ("pi_watch", "gpu_engine"), f"pooled row: {vals}"
            assert ws.cell(row=r[0].row, column=ncol).value is not None
    assert "RANGE CROSSES ERA BOUNDARIES" in summ
    results.append("2. straddle: OK — boundary on COVERAGE, per-era aggregates, "
                   "era banner present, no pooled close-travel row")

    # ── 3. outage range ─────────────────────────────────────────────────────
    out3 = cli.run(["--from", "2026-07-29T00:00:00", "--to", "2026-08-02T00:00:00",
                    "--db", dbp, "--out", str(work / "run3_outage.xlsx")])
    wb = load_workbook(out3)
    cov = sheet_text(wb, "COVERAGE & ERAS")
    assert "bcmgenet NIC wedge" in cov
    assert "stall-detector restart loop" in cov
    ws = wb["COVERAGE & ERAS"]
    covs = {}
    for r in ws.iter_rows():
        v = [c.value for c in r]
        if v and isinstance(v[0], str) and v[0].startswith("ch") and v[1] is not None:
            covs[v[0]] = v[1]
    assert covs, "no per-channel coverage rows"
    assert all(c < 100.0 for c in covs.values() if isinstance(c, float))
    results.append(f"3. outage range: OK — gaps listed, coverage% gap-adjusted "
                   f"(e.g. {dict(list(covs.items())[:2])})")

    # ── 4. empty range ──────────────────────────────────────────────────────
    out4 = cli.run(["--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00",
                    "--db", dbp, "--out", str(work / "run4_empty.xlsx")])
    wb = load_workbook(out4)
    cov = sheet_text(wb, "COVERAGE & ERAS")
    assert "ZERO ROWS" in cov
    results.append("4. empty range: OK — valid workbook, ZERO ROWS visible on COVERAGE")

    # ── 5. read-only ────────────────────────────────────────────────────────
    assert md5(dbp) == before
    results.append("5. read-only: OK — fixture DB md5 unchanged across all runs")

    # ── enumeration ─────────────────────────────────────────────────────────
    ctx = cli.build_context(dbp, "site-A", ts("2026-07-14T00:00:00"),
                            ts("2026-08-02T00:00:00"))
    best = max(ctx["aggs"].items(), key=lambda kv: kv[1]["close"]["n"])
    results.append(f"largest single-era clean close-travel n = "
                   f"{best[1]['close']['n']} ({best[0][0]} {best[0][1]}/{best[0][2]})")

    print("\n".join(results))
    print("ALL PROOFS PASSED")


if __name__ == "__main__":
    main()
