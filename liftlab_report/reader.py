"""Read-only access to the gateway DB and era-attributed extraction.

Guarantees:
  * The connection is opened with sqlite URI mode=ro AND query_only=ON.
    Nothing in this module can mutate a gateway table.
  * Every extracted row carries: instrument, era_id, counting_version.
  * Missing tables yield empty results, never exceptions — an empty range or a
    fresh DB must still produce a workbook.

Cycle reconstruction for gw_door_event ports the dashboard's flap-aware walk
(dash_api._door_gpu_by_cam) so the workbook's quotable pool ties out against
/dash: FLAP and REOPENED closes are excluded from the sheet-comparable pool
but counted; implausible (<0.3s or >30s) survivors are excluded and flagged.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import eras
from .eras import GPU_ENGINE, PI_WATCH


def open_ro(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _q(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []                    # table absent → honest empty, never a crash


def _epoch(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=eras.IST)     # Pi timestamps are building-local
    return dt.timestamp()


# ── validation / precision ───────────────────────────────────────────────────

def fetch_validation(db, gw: str) -> dict[str, dict]:
    """{cam: {state, n_reviewed, n_exact, precision_pct, confirmed_at,
              counting_version, updated_at}}"""
    out = {}
    for r in _q(db, "SELECT cam, state, n_reviewed, n_exact, confirmed_at, "
                    "counting_version, updated_at FROM camera_validation "
                    "WHERE gateway_id=?", (gw,)):
        n = r["n_reviewed"] or 0
        out[r["cam"]] = {
            "state": r["state"], "n_reviewed": n, "n_exact": r["n_exact"] or 0,
            "precision_pct": (100.0 * (r["n_exact"] or 0) / n) if n else None,
            "confirmed_at": r["confirmed_at"], "updated_at": r["updated_at"],
            "counting_version": r["counting_version"]}
    return out


def validation_stamps(validation: dict) -> dict[str, list]:
    """{cam: [(confirmed_at_epoch, version)]} for counting_version_at()."""
    return {cam: [(v["confirmed_at"], v["counting_version"])]
            for cam, v in validation.items()
            if v.get("confirmed_at") and v.get("counting_version")}


def precision_str(validation: dict, cam: str, counting_version: str) -> str:
    """Precision annotation for a row/count, honest about version mismatch."""
    v = validation.get(cam)
    if not v or v["precision_pct"] is None:
        return "unvalidated"
    if v.get("counting_version") and v["counting_version"] != counting_version:
        return (f"unvalidated under {counting_version} "
                f"(last: {v['precision_pct']:.0f}% n={v['n_reviewed']} "
                f"under {v['counting_version']})")
    return f"{v['precision_pct']:.0f}% (n={v['n_reviewed']})"


# ── registry / channel map ───────────────────────────────────────────────────

def fetch_registry(db, gw: str) -> dict[str, dict]:
    out = {}
    for r in _q(db, "SELECT cam, enabled, note, updated_at, door_levels, "
                    "floor_range FROM camera_registry WHERE gateway_id=?", (gw,)):
        out[r["cam"]] = dict(r)
    return out


def fetch_cams(db, gw: str) -> list[str]:
    rows = _q(db, "SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1", (gw,))
    if rows:
        return [f"ch{int(r['channel'])}" for r in rows]
    return list(eras.ALL_CAMS)


def fetch_channel_labels(db, gw: str) -> dict[str, str]:
    """{cam: the building's own name for this lift} from channel_map.label.

    This is what the lift is called in the building — "lift 1" — as distinct
    from ch16, which is the camera channel watching it. The workbook is an
    external artifact read by people who know the building, not the wiring, so
    the building's name is the one to show. Channels with no label are absent
    from the mapping and the caller must mark the fallback rather than quietly
    invent a name from the channel number."""
    out = {}
    for r in _q(db, "SELECT channel, label FROM channel_map "
                    "WHERE gateway_id=? AND is_lift=1", (gw,)):
        label = (r["label"] or "").strip()
        if label:
            out[f"ch{int(r['channel'])}"] = label
    return out


# ── Pi door-watch (gw_event) — the retired instrument ────────────────────────

def fetch_pi_cycles(db, gw: str, t0: float, t1: float) -> list[dict]:
    """gw_event rows in [t0,t1) as unified cycle dicts. clean close = quality
    NULL/'ok' AND close_travel_s present (the /events headline definition)."""
    rows = _q(db, "SELECT s.camera cam, e.door_open_start_ts os, e.door_open_full_ts ofl, "
                  "e.door_close_start_ts cs, e.door_close_full_ts cf, e.close_travel_s ct, "
                  "e.quality q, e.boarded b, e.alighted a, e.floor f, e.open_valid ov "
                  "FROM gw_event e JOIN gw_source s ON s.id=e.source_id "
                  "WHERE s.gateway_id=?", (gw,))
    out = []
    for r in rows:
        ts = _epoch(r["os"])
        if ts is None or not (t0 <= ts < t1):
            continue
        o_full, c_start, c_full = _epoch(r["ofl"]), _epoch(r["cs"]), _epoch(r["cf"])
        clean = r["ct"] is not None and (r["q"] is None or r["q"] == "ok")
        dwell = (c_start - o_full) if (o_full and c_start and c_start > o_full) else None
        open_travel = (o_full - ts) if (o_full and o_full > ts) else None
        out.append({
            "cam": r["cam"], "ts": ts, "instrument": PI_WATCH,
            "era_id": eras.pi_era_id(ts),
            "open_ts": ts, "close_ts": c_full or c_start,
            "open_start_iso": r["os"], "close_full_iso": r["cf"],
            "close_travel_s": float(r["ct"]) if clean else None,
            "quality": r["q"] or "ok", "clean_close": clean,
            "boarded": r["b"], "alighted": r["a"],
            "floor": r["f"], "floor_source": ("pi_watch" if r["f"] is not None else None),
            "dwell_s": dwell, "open_travel_s": open_travel,
            "cycle_class": "clean" if clean else f"withheld({r['q']})",
        })
    out.sort(key=lambda c: (c["cam"], c["ts"]))
    return out


# ── GPU DoorFloorEngine (gw_door_event) — the live instrument ────────────────

def fetch_gpu_rows(db, gw: str, t0: float, t1: float) -> list[sqlite3.Row]:
    return _q(db, "SELECT cam, ts, floor, direction, door_state, openness, read_conf, "
                  "panels_agreed, reason, close_travel_s, door_version "
                  "FROM gw_door_event WHERE gateway_id=? AND ts>=? AND ts<? "
                  "ORDER BY cam, ts, id", (gw, t0, t1))


def gpu_cycles_from_rows(rows, guard_epoch: float | None) -> tuple[list[dict], dict]:
    """Flap-aware cycle classification over one camera+era's door_state stream
    (port of dash_api._door_gpu_by_cam). Returns (cycles, funnel_counts).

    Each cycle: {ts(open), close_ts, close_travel_s, cycle_class, dwell_s,
    open_travel_s, floor, floor_source}. cycle_class ∈ clean | flap | reopened |
    implausible. The quotable pool is class=='clean' only."""
    n_preguard = 0
    if guard_epoch is not None:
        n_preguard = sum(1 for r in rows if r["close_travel_s"] is not None
                         and (r["ts"] is None or float(r["ts"]) < guard_epoch))
        rows = [r for r in rows if r["ts"] is not None and float(r["ts"]) >= guard_epoch]
    seq = []                     # collapsed state transitions [ts, state, ct, floor]
    for r in rows:
        if r["door_state"] is None:
            continue
        if not seq or seq[-1][1] != r["door_state"]:
            seq.append([float(r["ts"]), r["door_state"], r["close_travel_s"],
                        r["floor"]])
        else:
            if r["close_travel_s"] is not None and seq[-1][2] is None:
                seq[-1][2] = r["close_travel_s"]
            if r["floor"] is not None and seq[-1][3] is None:
                seq[-1][3] = r["floor"]
    cycles = []
    open_since = None
    opening_since = None
    cycle_open_ts = None
    cycle_floor = None
    flap_w = reopen_w = False
    dwell = None
    open_travel = None
    for k, (t, st, ct, fl) in enumerate(seq):
        prev = seq[k - 1][1] if k else None
        if st == "opening" and prev == "closed":
            flap_w = reopen_w = False
            open_since = None
            opening_since = t
            cycle_open_ts = t
            cycle_floor = None
            dwell = open_travel = None
        elif st == "open":
            open_since = t
            if cycle_open_ts is None:
                cycle_open_ts = t
            if fl is not None and cycle_floor is None:
                cycle_floor = fl
            if prev == "opening" and opening_since is not None:
                open_travel = t - opening_since
            elif prev == "closing":
                if (t - seq[k - 1][0]) < eras.FLAP_GAP_S:
                    flap_w = True
                else:
                    reopen_w = True
        elif st == "closing" and prev == "open":
            dwell = (t - open_since) if open_since is not None else None
            if dwell is not None and dwell < eras.MIN_OPEN_DWELL_S:
                flap_w = True
        elif st == "closed" and prev == "closing":
            v = seq[k - 1][2] if seq[k - 1][2] is not None else ct
            if v is not None and float(v) > 0:
                v = float(v)
                if flap_w:
                    klass = "flap"
                elif reopen_w:
                    klass = "reopened"
                elif not (eras.PLAUS_LO <= v <= eras.PLAUS_HI):
                    klass = "implausible"
                else:
                    klass = "clean"
                cycles.append({
                    "ts": cycle_open_ts if cycle_open_ts is not None else t,
                    "close_ts": t, "close_travel_s": v, "cycle_class": klass,
                    "dwell_s": dwell, "open_travel_s": open_travel,
                    "floor": cycle_floor,
                    "floor_source": ("gpu_ocr" if cycle_floor is not None else None)})
            flap_w = reopen_w = False
            cycle_open_ts = None
            cycle_floor = None
            dwell = open_travel = None
    funnel = {
        "n_rows": len(rows), "n_preguard_excluded": n_preguard,
        "n_cycles": len(cycles),
        "n_clean": sum(1 for c in cycles if c["cycle_class"] == "clean"),
        "n_flap": sum(1 for c in cycles if c["cycle_class"] == "flap"),
        "n_reopened": sum(1 for c in cycles if c["cycle_class"] == "reopened"),
        "n_implausible": sum(1 for c in cycles if c["cycle_class"] == "implausible"),
    }
    return cycles, funnel


def fetch_gpu_cycles(db, gw: str, t0: float, t1: float) -> tuple[list[dict], dict]:
    """All GPU cycles in range, era-attributed. Returns (cycles, funnels) where
    funnels is {(cam, era_id): funnel_counts}. Rows are grouped per (cam,
    door_version-era) BEFORE the walk — a cycle can never straddle eras."""
    rows = fetch_gpu_rows(db, gw, t0, t1)
    by = {}
    for r in rows:
        by.setdefault((r["cam"], eras.gpu_era_id(r["door_version"])), []).append(r)
    guard = eras.guard_epoch()
    cycles, funnels = [], {}
    for (cam, era_id), group in sorted(by.items()):
        cyc, funnel = gpu_cycles_from_rows(group, guard)
        for c in cyc:
            c["cam"] = cam
            c["instrument"] = GPU_ENGINE
            c["era_id"] = era_id
            c["clean_close"] = c["cycle_class"] == "clean"
            c["quality"] = c["cycle_class"]
            c["boarded"] = None       # GPU boarded/alighted live in transit_event
            c["alighted"] = None
            c["open_start_iso"] = None
            c["close_full_iso"] = None
        cycles.extend(cyc)
        funnels[(cam, era_id)] = funnel
    cycles.sort(key=lambda c: (c["cam"], c["ts"]))
    return cycles, funnels


def fetch_floor_read_status(db, gw: str, t0: float, t1: float) -> dict[str, dict]:
    """Tier-2 floor-attribution status per cam over the range: confident reads,
    no_read, ambiguous, invalid, distinct glyphs seen. Documents WHY C21/C22
    are unavailable."""
    rows = _q(db, "SELECT cam, floor, reason, read_conf FROM gw_door_event "
                  "WHERE gateway_id=? AND ts>=? AND ts<?", (gw, t0, t1))
    out = {}
    for r in rows:
        d = out.setdefault(r["cam"], {"rows": 0, "confident": 0, "no_read": 0,
                                      "ambiguous": 0, "invalid": 0, "other": 0,
                                      "glyphs": set()})
        d["rows"] += 1
        reason = (r["reason"] or "").strip()
        if r["floor"] is not None and reason in ("", "ok"):
            d["confident"] += 1
            d["glyphs"].add(str(r["floor"]))
        elif reason.startswith("no_read") or (r["floor"] is None and not reason):
            d["no_read"] += 1
        elif reason.startswith("ambiguous"):
            d["ambiguous"] += 1
        elif reason.startswith("invalid_label"):
            d["invalid"] += 1
        else:
            d["other"] += 1
    for d in out.values():
        d["glyphs"] = sorted(d["glyphs"])
    return out


# ── transits (GPU counting pipeline) ─────────────────────────────────────────

def fetch_transits(db, gw: str, t0: float, t1: float) -> list[dict]:
    rows = _q(db, "SELECT cam, ts, direction, track_id FROM transit_event "
                  "WHERE gateway_id=? AND ts>=? AND ts<? ORDER BY cam, ts",
              (gw, t0, t1))
    return [{"cam": r["cam"], "ts": float(r["ts"]), "direction": r["direction"],
             "track_id": r["track_id"], "instrument": GPU_ENGINE}
            for r in rows if r["ts"] is not None]


def fetch_analyzer_versions(db, gw: str) -> dict[str, str]:
    return {r["cam"]: r["counting_version"]
            for r in _q(db, "SELECT cam, counting_version FROM analyzer_status "
                            "WHERE gateway_id=?", (gw,))}


# ── coverage buckets ─────────────────────────────────────────────────────────

BUCKET_S = 900          # 15-min coverage buckets


def coverage_buckets(db, gw: str, cams: list[str], t0: float, t1: float) -> dict:
    """{cam: set(bucket_index)} — buckets with ≥1 row in ANY stream. Bucket
    index = int((ts - t0) // BUCKET_S)."""
    cov = {cam: set() for cam in cams}

    def _mark(cam, ts):
        if cam in cov and ts is not None and t0 <= ts < t1:
            cov[cam].add(int((ts - t0) // BUCKET_S))

    for r in _q(db, "SELECT cam, ts FROM gw_door_event WHERE gateway_id=? "
                    "AND ts>=? AND ts<?", (gw, t0, t1)):
        _mark(r["cam"], float(r["ts"]) if r["ts"] is not None else None)
    for r in _q(db, "SELECT cam, ts FROM transit_event WHERE gateway_id=? "
                    "AND ts>=? AND ts<?", (gw, t0, t1)):
        _mark(r["cam"], float(r["ts"]) if r["ts"] is not None else None)
    for r in _q(db, "SELECT s.camera cam, e.door_open_start_ts os FROM gw_event e "
                    "JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?", (gw,)):
        _mark(r["cam"], _epoch(r["os"]))
    return cov
