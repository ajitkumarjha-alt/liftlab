"""liftlab-report CLI.

  liftlab-report --from <ISO ts> --to <ISO ts> [--out <path>]
                 [--peak-window auto|HH:MM-HH:MM] [--population N]
                 [--min-close 0.5]
                 [--db <sqlite path>] [--gateway site-A] [--banks <json>]

Naive (offset-less) --from/--to are interpreted as IST (+05:30), the
building's clock. The DB is opened READ-ONLY (sqlite mode=ro + query_only);
this tool cannot mutate gateway tables.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import eras, model, reader, workbook


class ReportError(Exception):
    """Raised instead of SystemExit so the builder stays web-callable."""


def parse_ts(s: str) -> float:
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ReportError(f"bad timestamp {s!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=eras.IST)
    return dt.timestamp()


def default_out_dir() -> Path:
    p = Path("/mnt/user-data/outputs")
    if p.is_dir():
        return p
    q = Path("./reports")
    q.mkdir(parents=True, exist_ok=True)
    return q


def build_context(db_path: str, gw: str, t0: float, t1: float,
                  peak_window: str = "auto", population: int | None = None,
                  banks_path: str | None = None,
                  min_close_s: float | None = None) -> dict:
    """Everything the workbook needs, precomputed. Read-only throughout."""
    if t1 <= t0:
        raise ReportError("--to must be after --from")
    db = reader.open_ro(db_path)
    try:
        cams = [c for c in reader.fetch_cams(db, gw)]
        validation = reader.fetch_validation(db, gw)
        stamps = reader.validation_stamps(validation)
        registry = reader.fetch_registry(db, gw)
        analyzer_versions = reader.fetch_analyzer_versions(db, gw)
        pi_cycles = reader.fetch_pi_cycles(db, gw, t0, t1)
        gpu_cycles, funnels = reader.fetch_gpu_cycles(db, gw, t0, t1)
        transits = reader.fetch_transits(db, gw, t0, t1)
        floor_status = reader.fetch_floor_read_status(db, gw, t0, t1)
        cov_buckets = reader.coverage_buckets(db, gw, cams, t0, t1)
    finally:
        db.close()

    floor_s = (eras.MIN_PLAUSIBLE_CLOSE_S if min_close_s is None
               else float(min_close_s))
    all_cycles = pi_cycles + gpu_cycles
    aggs = model.aggregate_cycles(all_cycles, t0, t1, min_close_s=floor_s)
    suspected_gaps = model.detect_suspected_gaps(all_cycles, transits, cams, t0, t1)
    transit_aggs = model.aggregate_transits(transits, stamps, t0, t1)
    profile = model.hourly_profile(transits, pi_cycles)
    fixed = model.parse_peak_window(peak_window)
    peaks = model.peak_by_day(transits, pi_cycles, t0, t1, fixed)
    boundaries = model.boundaries_crossed(t0, t1, aggs, transit_aggs)

    # coverage % per cam: buckets with data / non-gap buckets
    n_buckets = max(1, int((t1 - t0) // reader.BUCKET_S))
    coverage_pct, coverage_daily = {}, {"days": [], "by_cam": {}}
    from datetime import timedelta
    day0 = datetime.fromtimestamp(t0, eras.IST).replace(hour=0, minute=0,
                                                        second=0, microsecond=0)
    days = []
    d = day0
    while d.timestamp() < t1:
        days.append(d)
        d += timedelta(days=1)
    coverage_daily["days"] = [d.date().isoformat() for d in days]
    for cam in cams:
        gap_s = eras.gap_overlap_s(cam, t0, t1)
        gap_buckets = int(gap_s // reader.BUCKET_S)
        usable = max(1, n_buckets - gap_buckets)
        have = len([b for b in cov_buckets.get(cam, ())
                    if not eras.in_gap(cam, t0 + (b + 0.5) * reader.BUCKET_S)])
        coverage_pct[cam] = min(100.0, 100.0 * have / usable)
        daily = []
        for d in days:
            d0 = max(d.timestamp(), t0)
            d1 = min((d + timedelta(days=1)).timestamp(), t1)
            nb = max(1, int((d1 - d0) // reader.BUCKET_S))
            gb = int(eras.gap_overlap_s(cam, d0, d1) // reader.BUCKET_S)
            usable_d = max(0, nb - gb)
            have_d = len([b for b in cov_buckets.get(cam, ())
                          if d0 <= t0 + b * reader.BUCKET_S < d1
                          and not eras.in_gap(cam, t0 + (b + 0.5) * reader.BUCKET_S)])
            daily.append(round(100.0 * have_d / usable_d, 1) if usable_d else None)
        coverage_daily["by_cam"][cam] = daily

    # counting-version cross-check: declared epoch vs live tables
    version_mismatches = []
    cv_attribution = {}
    now_ver = {cam: eras.counting_version_at(
        cam, min(t1, datetime.now(eras.IST).timestamp()), stamps) for cam in cams}
    for cam in cams:
        src = ("camera_validation stamp" if stamps.get(cam) else "declared epochs")
        cv_attribution[cam] = src
        live = analyzer_versions.get(cam)
        if live and live != now_ver[cam]:
            version_mismatches.append(
                f"{cam}: declared/stamped attribution says {now_ver[cam]!r} but "
                f"analyzer_status reports {live!r} — update COUNTING_EPOCHS "
                f"in eras.py before trusting counting-era splits for this camera")

    banks = eras.load_banks(banks_path)

    # Fleet-wide sampling resolution, for the plain-English sheet: the eras
    # agree in practice, but say so from the data rather than assume it.
    era_quanta = {k: a["quantum_s"] for k, a in aggs.items()
                  if a["quantum_s"] is not None}
    fleet_quantum = None
    if era_quanta:
        vals = sorted(era_quanta.values())
        fleet_quantum = vals[len(vals) // 2]

    # raw rows
    raw_rows = []
    for c in all_cycles:
        ver = ("pi-watch (on-device counter)" if c["instrument"] == eras.PI_WATCH
               else eras.counting_version_at(c["cam"], c["ts"], stamps))
        raw_rows.append({
            "ts_ist": datetime.fromtimestamp(c["ts"], eras.IST).isoformat(),
            "ts_epoch": round(c["ts"], 3), "channel": c["cam"],
            "lift_label": eras.lift_label(c["cam"]),
            "bank": banks.get(c["cam"]) or "UNKNOWN",
            "instrument": c["instrument"], "counting_version": ver,
            "era_id": c["era_id"], "event_type": "door_cycle",
            "door_open_ts": (c.get("open_start_iso")
                             or datetime.fromtimestamp(c["ts"], eras.IST).isoformat()),
            "door_close_ts": (c.get("close_full_iso")
                              or (datetime.fromtimestamp(c["close_ts"], eras.IST).isoformat()
                                  if c.get("close_ts") else None)),
            "close_travel_s": c.get("close_travel_s"),
            "below_floor": ("YES" if (c.get("close_travel_s") is not None
                                      and float(c["close_travel_s"]) < floor_s)
                            else ""),
            "cycle_class": c.get("cycle_class"),
            "boarded": c.get("boarded"), "alighted": c.get("alighted"),
            "floor": c.get("floor"), "floor_source": c.get("floor_source"),
            "precision_at_time": reader.precision_str(validation, c["cam"], ver),
            "in_gap": "YES" if eras.in_gap(c["cam"], c["ts"]) else "",
        })
    for t in transits:
        ver = eras.counting_version_at(t["cam"], t["ts"], stamps)
        raw_rows.append({
            "ts_ist": datetime.fromtimestamp(t["ts"], eras.IST).isoformat(),
            "ts_epoch": round(t["ts"], 3), "channel": t["cam"],
            "lift_label": eras.lift_label(t["cam"]),
            "bank": banks.get(t["cam"]) or "UNKNOWN",
            "instrument": t["instrument"], "counting_version": ver,
            "era_id": f"counting:{ver}", "event_type": "transit",
            "door_open_ts": None, "door_close_ts": None, "close_travel_s": None,
            "below_floor": "", "cycle_class": None,
            "boarded": 1 if t["direction"] == "in" else None,
            "alighted": 1 if t["direction"] == "out" else None,
            "floor": None, "floor_source": None,
            "precision_at_time": reader.precision_str(validation, t["cam"], ver),
            "in_gap": "YES" if eras.in_gap(t["cam"], t["ts"]) else "",
        })
    raw_rows.sort(key=lambda r: r["ts_epoch"])

    return {
        "t0": t0, "t1": t1,
        "from_iso": datetime.fromtimestamp(t0, eras.IST).isoformat(),
        "to_iso": datetime.fromtimestamp(t1, eras.IST).isoformat(),
        "generated_at": datetime.now(eras.IST).isoformat(timespec="seconds"),
        "gw": gw, "db_path": str(db_path), "cams": cams, "banks": banks,
        "validation": validation, "registry": registry,
        "analyzer_versions": analyzer_versions,
        "aggs": aggs, "transit_aggs": transit_aggs, "funnels": funnels,
        "boundaries": boundaries, "profile": profile, "peaks": peaks,
        "floor_status": floor_status, "coverage_pct": coverage_pct,
        "coverage_daily": coverage_daily, "population": population,
        "peak_window_spec": peak_window, "all_cycles": all_cycles,
        "raw_rows": raw_rows, "n_raw_rows": len(raw_rows),
        "version_mismatches": version_mismatches,
        "cv_attribution": cv_attribution,
        "stamps": stamps,
        "min_close_s": floor_s,
        "suspected_gaps": suspected_gaps,
        "era_quanta": era_quanta,
        "fleet_quantum_s": fleet_quantum,
        "transits": transits,
    }


def build_workbook(ctx):
    """Data sheets first — they record WHERE they put each figure in `anchors`.
    READ THIS FIRST is built last from those anchors, so every 'Evidence:' line
    on it names a cell range that really holds the number it quotes, then it is
    moved to the front where a first-time reader will meet it."""
    from openpyxl import Workbook
    wb = Workbook()
    cd = workbook.ChartData(wb)
    anchors: dict = {}
    workbook.sheet_summary(wb, ctx, cd, anchors)
    workbook.sheet_vs_sheet(wb, ctx, cd, anchors)
    workbook.sheet_per_lift(wb, ctx, cd, anchors)
    workbook.sheet_fleet(wb, ctx, cd, anchors)
    workbook.sheet_peak(wb, ctx, cd, anchors)
    workbook.sheet_raw(wb, ctx, anchors)
    workbook.sheet_coverage(wb, ctx, cd, anchors)
    workbook.sheet_tier2(wb, ctx, anchors)
    workbook.sheet_read_this_first(wb, ctx, anchors)
    wb.move_sheet("READ THIS FIRST", offset=-(len(wb.sheetnames) - 1))
    # keep DATA_CHARTS last in the tab order
    wb.move_sheet("DATA_CHARTS", offset=len(wb.sheetnames))
    return wb


def run(argv=None) -> Path:
    ap = argparse.ArgumentParser(
        prog="liftlab-report",
        description="Export an era-hygienic Excel workbook of lift analytics "
                    "from the gateway DB (read-only).")
    ap.add_argument("--from", dest="from_ts", required=True,
                    help="range start, ISO timestamp (naive = IST)")
    ap.add_argument("--to", dest="to_ts", required=True,
                    help="range end (exclusive), ISO timestamp (naive = IST)")
    ap.add_argument("--out", default=None, help="output .xlsx path")
    ap.add_argument("--peak-window", default="auto",
                    help="'auto' (worst 5-min window per day) or fixed HH:MM-HH:MM")
    ap.add_argument("--population", type=int, default=None,
                    help="tower population for peak-demand %% vs the 8%% HC assumption")
    ap.add_argument("--min-close", dest="min_close", type=float,
                    default=eras.MIN_PLAUSIBLE_CLOSE_S,
                    help="one-frame quantization floor in seconds: closes below "
                         "this are door-state flip artifacts and are rejected "
                         f"from every close-travel statistic "
                         f"(default {eras.MIN_PLAUSIBLE_CLOSE_S})")
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB",
                                                   "/var/lib/liftlab/gateway.db"),
                    help="gateway sqlite DB path (default: $GATEWAY_DB)")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_ID", "site-A"))
    ap.add_argument("--banks", default=None,
                    help="bank sidecar json (default: lift_banks.json beside the module)")
    args = ap.parse_args(argv)

    t0, t1 = parse_ts(args.from_ts), parse_ts(args.to_ts)
    if not Path(args.db).exists():
        raise ReportError(f"DB not found: {args.db}")
    if args.min_close < 0:
        raise ReportError("--min-close must be >= 0")
    ctx = build_context(args.db, args.gateway, t0, t1,
                        peak_window=args.peak_window,
                        population=args.population, banks_path=args.banks,
                        min_close_s=args.min_close)
    wb = build_workbook(ctx)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now(eras.IST).strftime("%Y%m%d-%H%M%S")
        f0 = datetime.fromtimestamp(t0, eras.IST).strftime("%Y%m%d")
        f1 = datetime.fromtimestamp(t1, eras.IST).strftime("%Y%m%d")
        out = default_out_dir() / f"liftlab-report_{f0}-{f1}_{stamp}.xlsx"
    wb.save(out)
    return out


def main(argv=None) -> int:
    try:
        out = run(argv)
    except ReportError as e:
        print(f"liftlab-report: {e}", file=sys.stderr)
        return 2
    print(f"wrote {out}")
    return 0
