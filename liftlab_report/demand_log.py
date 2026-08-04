"""DEMAND LOG — the tabular export of what was actually counted.

One row per hour per camera, plus a FLEET row per hour, and a daily rollup. Nothing here is
derived, modelled or estimated: every number is a count of transit_event rows that the system
recorded, attributed to the counting build in effect at that moment.

THREE RULES THIS MODULE EXISTS TO ENFORCE
-----------------------------------------
1. DARK IS NOT ZERO. A camera that was not observed in an hour is written as '—', never 0. The
   difference matters: 0 means "this lift was watched and carried nobody", '—' means "nobody was
   watching". Reading the second as the first understates demand exactly where coverage is worst.
   Observation comes from coverage buckets (any row in any stream), not from transit rows, so a
   lift that was up and genuinely idle still reads 0.

2. NEVER POOL ACROSS COUNTING VERSIONS. Counts made by different counting builds are different
   measurements. The grouping key includes the counting version in effect at that timestamp, so a
   camera that spans two builds inside one hour produces two rows, not one blended one. The FLEET
   row is per version for the same reason — summing cameras on different builds would be the same
   error one level up.

3. GAP ROWS ARE EXCLUDED. Transits inside a known outage window are not counted and the hour is
   not credited as observed, matching aggregate_transits() elsewhere in this report.

WHAT IS DELIBERATELY ABSENT
---------------------------
* Persons currently in the lift. The system counts door crossings, not occupancy. A running
  boarded-minus-alighted figure would compound the per-camera counting error across every cycle
  and drift without bound — see notes() for the measured per-camera precision this would compound.
* Any per-floor breakdown. See floor_attribution_note(), which reports the MEASURED read status
  per camera rather than asserting a single fleet-wide number.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from . import eras, reader

DARK = "—"                      # observed nothing. NOT zero.
FLEET = "FLEET"

# Single source of truth — see eras.FLOOR_OK_REASONS for what single_panel means and why it counts.
FLOOR_OK_REASONS = tuple(r for r in eras.FLOOR_OK_REASONS if r)


def _hour_floor(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, eras.IST).replace(minute=0, second=0, microsecond=0)


def _hours(t0: float, t1: float):
    """Every IST hour that overlaps [t0, t1)."""
    h = _hour_floor(t0)
    while h.timestamp() < t1:
        yield h
        h += timedelta(hours=1)


def observed_hours(cov_buckets: dict, cam: str, t0: float, bucket_s: int) -> set:
    """{hour_start_epoch} in which this camera produced at least one row in any stream.

    Buckets are BUCKET_S wide and hour-aligned only when BUCKET_S divides an hour; the bucket's
    midpoint decides which hour it belongs to, and a bucket whose midpoint sits in a known gap is
    not credited — an outage is not observation."""
    out = set()
    for b in cov_buckets.get(cam, ()):
        mid = t0 + (b + 0.5) * bucket_s
        if eras.in_gap(cam, mid):
            continue
        out.add(_hour_floor(mid).timestamp())
    return out


def build(transits: list[dict], cov_buckets: dict, stamps: dict, validation: dict,
          cams: list[str], t0: float, t1: float, bucket_s: int = reader.BUCKET_S) -> dict:
    """-> {'hourly': [...], 'daily': [...], 'versions_by_cam': {...}}."""
    obs = {cam: observed_hours(cov_buckets, cam, t0, bucket_s) for cam in cams}

    # counted[(hour_epoch, cam, version)] = {boarded, alighted}
    counted: dict[tuple, dict] = {}
    versions_by_cam: dict[str, set] = {c: set() for c in cams}
    n_gap_excluded = 0
    for t in transits:
        cam, ts = t["cam"], t["ts"]
        if cam not in obs:
            continue                        # not a lift camera in this report's set
        if eras.in_gap(cam, ts):
            n_gap_excluded += 1
            continue
        ver = eras.counting_version_at(cam, ts, stamps)
        versions_by_cam.setdefault(cam, set()).add(ver)
        key = (_hour_floor(ts).timestamp(), cam, ver)
        d = counted.setdefault(key, {"boarded": 0, "alighted": 0})
        # transit_event.direction is 'in' or 'out' only (verified against the live table); anything
        # else would be a new value and is counted as alighted rather than dropped, matching
        # model.aggregate_transits so the two never disagree.
        d["boarded" if t["direction"] == "in" else "alighted"] += 1

    hourly = []
    for h in _hours(t0, t1):
        he = h.timestamp()
        ts_ist = h.isoformat(timespec="minutes")
        # which versions are in play this hour, per camera
        per_cam_rows = []
        fleet: dict[str, dict] = {}
        for cam in cams:
            seen = obs[cam]
            was_observed = he in seen
            vers = sorted({v for (hh, cc, v) in counted if hh == he and cc == cam})
            if not vers:
                # no counted transits this hour: still need the version for the row label
                vers = [eras.counting_version_at(cam, he + 1800.0, stamps)]
            for ver in vers:
                c = counted.get((he, cam, ver))
                row = {
                    "timestamp_ist": ts_ist, "hour_epoch": he,
                    "camera": cam, "lift": eras.lift_label(cam),
                    "boarded": (c["boarded"] if (was_observed and c) else
                                (0 if was_observed else DARK)),
                    "alighted": (c["alighted"] if (was_observed and c) else
                                 (0 if was_observed else DARK)),
                    "counting_version": ver,
                    "precision_at_time": reader.precision_str(validation, cam, ver),
                    "observed": "yes" if was_observed else "no",
                }
                per_cam_rows.append(row)
                if was_observed:
                    f = fleet.setdefault(ver, {"boarded": 0, "alighted": 0, "cams": 0})
                    f["boarded"] += c["boarded"] if c else 0
                    f["alighted"] += c["alighted"] if c else 0
                    f["cams"] += 1
        hourly.extend(per_cam_rows)
        if fleet:
            for ver in sorted(fleet):
                f = fleet[ver]
                hourly.append({
                    "timestamp_ist": ts_ist, "hour_epoch": he,
                    "camera": FLEET, "lift": f"FLEET ({f['cams']} lift(s) observed)",
                    "boarded": f["boarded"], "alighted": f["alighted"],
                    "counting_version": ver,
                    # A fleet row is a sum of per-camera counts with DIFFERENT precisions; quoting
                    # one number here would imply an accuracy the sum does not have.
                    "precision_at_time": "n/a (sum of cameras with differing precision)",
                    "observed": "yes",
                })
        else:
            hourly.append({
                "timestamp_ist": ts_ist, "hour_epoch": he, "camera": FLEET,
                "lift": "FLEET (0 lifts observed)", "boarded": DARK, "alighted": DARK,
                "counting_version": eras.counting_version_at(cams[0], he + 1800.0, stamps)
                if cams else "", "precision_at_time": "n/a", "observed": "no",
            })

    hourly.sort(key=lambda r: (r["hour_epoch"], r["camera"] == FLEET, r["camera"],
                               r["counting_version"]))

    # ── daily rollup ──────────────────────────────────────────────────────────
    daily = []
    day = datetime.fromtimestamp(t0, eras.IST).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < t1:
        d0, d1 = max(day.timestamp(), t0), min((day + timedelta(days=1)).timestamp(), t1)
        # usable hours = hours in the day inside the range, minus known-gap time
        for cam in cams:
            # DENOMINATOR AND NUMERATOR MUST USE THE SAME RULE. Counting observed hours by BUCKET
            # midpoint while subtracting gap SECONDS lets a bucket land just outside an outage and
            # credit an hour the denominator has already written off — real data produced
            # 2 observed / 1.05 usable = 190.5% coverage. Both sides are now hour slots judged by
            # the hour's own midpoint, and an hour that was entirely an outage cannot be
            # 'observed', so the ratio is <= 100% by construction rather than by clamping.
            slots = []
            hh = _hour_floor(d0)
            while hh.timestamp() < d1:
                mid = hh.timestamp() + 1800.0
                if d0 <= mid < d1 and not eras.in_gap(cam, mid):
                    slots.append(hh.timestamp())
                hh += timedelta(hours=1)
            usable_h = float(len(slots))
            usable_set = set(slots)
            hrs = sorted(h for h in obs[cam] if d0 <= h < d1 and h in usable_set)
            by_ver: dict[str, dict] = {}
            for (hh, cc, ver), c in counted.items():
                if cc == cam and d0 <= hh < d1:
                    a = by_ver.setdefault(ver, {"boarded": 0, "alighted": 0, "hours": set()})
                    a["boarded"] += c["boarded"]
                    a["alighted"] += c["alighted"]
                    a["hours"].add(hh)
            if not by_ver and not hrs:
                continue                    # camera contributed nothing and was never observed
            if not by_ver:                  # observed but carried nobody — a real zero
                by_ver[eras.counting_version_at(cam, d0 + 43200.0, stamps)] = {
                    "boarded": 0, "alighted": 0, "hours": set()}
            for ver in sorted(by_ver):
                a = by_ver[ver]
                daily.append({
                    "date": day.date().isoformat(), "camera": cam,
                    "lift": eras.lift_label(cam),
                    "total_boarded": a["boarded"], "total_alighted": a["alighted"],
                    "hours_observed": len(hrs),
                    "coverage_pct": (round(100.0 * len(hrs) / usable_h, 1)
                                     if usable_h > 0 else None),
                    # Era hygiene: the requested rollup columns do not include the counting
                    # version, but splitting on it is the only way to avoid pooling two builds
                    # into one daily total. The column is added rather than the rows blended.
                    "counting_version": ver,
                    "precision_at_time": reader.precision_str(validation, cam, ver),
                })
        day += timedelta(days=1)

    return {"hourly": hourly, "daily": daily,
            "versions_by_cam": {c: sorted(v) for c, v in versions_by_cam.items() if v},
            "n_gap_excluded": n_gap_excluded}


def floor_attribution_note(conf: dict | None) -> str:
    """One line explaining why there is no per-floor breakdown, from MEASURED reads.

    conf: {cam: {confident, total}} from reader.fetch_floor_confidence. The numbers are stated per
    camera rather than as one fleet figure because attribution is NOT uniform — quoting a single
    camera's ratio as the fleet's would misdescribe every other lift."""
    if not conf:
        return ("PER-FLOOR DEMAND IS NOT INCLUDED: no floor reads were recorded over this range, "
                "so per-floor boardings cannot be stated without inventing them.")
    parts, blind = [], []
    for cam, d in sorted(conf.items()):
        tot, c = d["total"], d["confident"]
        parts.append(f"{cam} {c}/{tot}")
        if tot and c * 200 < tot:          # under 0.5% confident
            blind.append(cam)
    return ("PER-FLOOR DEMAND IS NOT INCLUDED. Confident floor reads per camera over this range "
            "(non-null floor with reason in " + "/".join(FLOOR_OK_REASONS) + "): "
            + ", ".join(parts) + ". "
            + (f"Effectively floor-blind: {', '.join(blind)}. " if blind else "")
            + "Attribution is not uniform across the fleet, so a per-floor table would be solid "
              "for some lifts and fabricated for others; it is omitted rather than mixed.")


def notes(ctx: dict, log: dict) -> list[str]:
    """Provenance lines that travel WITH the data — the caveats are the point."""
    val = ctx.get("validation") or {}
    prec = []
    for cam in ctx.get("cams", []):
        v = val.get(cam) or {}
        p = v.get("precision_pct")
        prec.append(f"{cam} {p:.0f}% (n={v.get('n_reviewed', 0)})" if p is not None
                    else f"{cam} unvalidated")
    out = [
        "LIFTLAB DEMAND LOG — counts of door crossings recorded by the counting engine.",
        f"range: {ctx.get('from_iso')} .. {ctx.get('to_iso')} (IST, +05:30)   "
        f"gateway: {ctx.get('gw')}   generated: {ctx.get('generated_at')}",
        "boarded = transit_event rows with direction 'in'; alighted = direction 'out'. "
        "These are CROSSINGS, not people: one person crossing twice counts twice.",
        f"'{DARK}' means the camera was NOT OBSERVED in that hour. It is not a zero. A 0 means the "
        "camera was observed and counted nobody.",
        "Counting versions are never pooled: a camera spanning two builds in an hour or a day "
        "produces one row per build. FLEET rows are per build for the same reason.",
        f"Transits inside known outage windows are excluded ({log.get('n_gap_excluded', 0)} "
        "excluded in this range) and those hours are not credited as observed.",
        "per-camera precision (latest validation): " + ", ".join(prec),
        "PERSONS CURRENTLY IN LIFT IS NOT INCLUDED: this system counts crossings, not occupancy. "
        "A running boarded-minus-alighted figure would accumulate each camera's counting error "
        "across every cycle and drift without bound, so it is not derivable from this data.",
    ]
    return out


# ── CSV output ───────────────────────────────────────────────────────────────

HOURLY_COLS = [("timestamp_ist", "timestamp (IST)"), ("camera", "camera"), ("lift", "lift"),
               ("boarded", "boarded"), ("alighted", "alighted"),
               ("counting_version", "counting_version"),
               ("precision_at_time", "precision_at_time"), ("observed", "observed")]
DAILY_COLS = [("date", "date"), ("camera", "camera"), ("lift", "lift"),
              ("total_boarded", "total boarded"), ("total_alighted", "total alighted"),
              ("hours_observed", "hours observed"), ("coverage_pct", "coverage %"),
              ("counting_version", "counting_version"),
              ("precision_at_time", "precision_at_time")]


def _write(path: Path, cols, rows, note_lines) -> Path:
    """CSV with the caveats as leading '#' comment lines.

    The notes ride inside the file on purpose: a demand table that gets mailed onward without its
    'dark is not zero' line is a table that will be misread. '#' is the usual convention — pandas
    skips it with comment='#', and a spreadsheet shows it as text at the top rather than choking."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        for line in note_lines:
            fh.write(f"# {line}\n")
        w = csv.writer(fh)
        w.writerow([h for _, h in cols])
        for r in rows:
            w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in cols])
    return path


def write_csvs(out_base: Path, ctx: dict, log: dict, floor_note: str) -> list[Path]:
    base = out_base.with_suffix("")
    note_lines = notes(ctx, log) + [floor_note]
    return [
        _write(Path(f"{base}_demand-log-hourly.csv"), HOURLY_COLS, log["hourly"], note_lines),
        _write(Path(f"{base}_demand-log-daily.csv"), DAILY_COLS, log["daily"], note_lines),
    ]
