"""Declared comparability boundaries, data gaps, and era attribution.

Everything in this file is a DECLARATION, not a query: boundaries and gaps are
facts about the deployment history that the DB does not record about itself.
New gaps are APPENDED to DATA_GAPS; never encode a gap inside a query.

Sources of truth mirrored here (do not silently diverge from them):
  dash_api.DOORWATCH_RETIRED_BOUNDARY   2026-07-21T00:00:00+00:00
  dash_api.CLOSE_TRAVEL_MAX_BOUNDARY    2026-07-16T11:48:11+00:00
  dash_api.DOOR_SPECS                   ch29 2.00s / 2.31s Bank C, C26 1.50 s/p
  dash_api.DATA_GAPS                    Jul 19-20 relay stall (ch29)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))   # the building's clock

# ── Instruments ───────────────────────────────────────────────────────────────
# gw_event (Pi door-watch) and gw_door_event (GPU DoorFloorEngine) are DIFFERENT
# INSTRUMENTS — different edge detector, clock, sampling. Split at d7a7a49.
# NEVER pool a figure across this boundary.
PI_WATCH = "pi_watch"
GPU_ENGINE = "gpu_engine"
INSTRUMENT_SPLIT = "2026-07-21T00:00:00+00:00"
INSTRUMENT_SPLIT_EPOCH = datetime.fromisoformat(INSTRUMENT_SPLIT).timestamp()

# Pi-era sub-boundary: CLOSE_TRAVEL_MAX 10→30 admitted longer real closes, so
# pi_watch close-travel pools on either side are not comparable.
CLOSE_TRAVEL_MAX_BOUNDARY = "2026-07-16T11:48:11+00:00"
CLOSE_TRAVEL_MAX_EPOCH = datetime.fromisoformat(CLOSE_TRAVEL_MAX_BOUNDARY).timestamp()

# GPU-era sub-boundary: the DoorTracker time-guards changed what gets EMITTED
# without moving the door_version era. Same env knob the dashboard uses.
DOOR_GUARD_TS_ENV = "DASH_DOOR_GUARD_TS"


def guard_epoch() -> float | None:
    v = os.environ.get(DOOR_GUARD_TS_ENV, "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


# Cycle plausibility + flap classification, mirroring the dashboard's pool.
PLAUS_LO, PLAUS_HI = 0.3, 30.0
MIN_OPEN_DWELL_S = float(os.environ.get("DASH_MIN_OPEN_DWELL_S", "1.5"))
FLAP_GAP_S = float(os.environ.get("DASH_FLAP_GAP_S", "1.0"))

# ── One-frame quantization floor ─────────────────────────────────────────────
# Door travel is measured by frame sampling, so every value is a multiple of the
# frame interval (~0.08 s at 12.5 fps on the live gateway — detected at run time,
# never assumed: see stats.detect_quantum). A lift door does not close in one
# frame; a one-frame "close" is a door-state flip artifact, not a close.
#
# MIN_PLAUSIBLE_CLOSE_S is a SECOND, higher floor sitting above the dashboard's
# PLAUS_LO=0.3 plausibility bound. Cycles below it are rejected from every
# close-travel statistic and counted on COVERAGE & ERAS.
MIN_PLAUSIBLE_CLOSE_S = float(os.environ.get("LIFTLAB_MIN_CLOSE_S", "0.5"))

# A pool losing more than this share to the floor gets a visible SUMMARY warning.
FLOOR_REJECT_WARN_FRAC = 0.05

# When more than this share of a pool's values sit AT the one-frame quantum, the
# measurement is resolution-bound: emit no verdict, emit the reason instead.
QUANTUM_SUPPRESS_FRAC = 0.20

# ── The sheet (MEP-02 v28) ───────────────────────────────────────────────────
SHEET_NAME = "MEP-02 v28"
DOOR_SPECS = {
    "ch29": {"sheet_close_s": 2.00, "compliance_s": 2.31, "bank": "C",
             "transfer_sheet_s": 1.50, "sheet_open_s": 3.00},
}
# Defaults used for cameras with no explicit spec entry (sheet-wide values).
SHEET_CLOSE_S = 2.00
SHEET_OPEN_S = 3.00
SHEET_TRANSFER_S_PP = 1.50
COMPLIANCE_CLIFF_S = 2.31            # Bank C non-compliance line
HC_PEAK_DESIGN_PCT = 8.0             # 5-min handling capacity design assumption

# ── Channels ─────────────────────────────────────────────────────────────────
CHANNELS = [16, 27, 29, 30, 32, 34, 37]
ALL_CAMS = [f"ch{c}" for c in CHANNELS]

# ── Known data gaps ──────────────────────────────────────────────────────────
# Declared table: (start ISO, end ISO, channels_affected or None for ALL, reason).
# Excluded from every rate/duration statistic; listed on COVERAGE & ERAS.
# Times carry explicit offsets. The 2026-07-31/08-01 entries were reported in
# building-local IST (+05:30). APPEND new gaps here.
DATA_GAPS = [
    {"start": "2026-07-19T09:50:00+00:00", "end": "2026-07-20T04:17:00+00:00",
     "cams": ["ch29"],
     "reason": "relay stall — ffmpeg alive-but-not-delivering; collect blind "
               "(incl. Monday AM peak). Jul 19 15:20 – Jul 20 09:47 IST."},
    {"start": "2026-07-31T11:54:00+05:30", "end": "2026-08-01T22:35:00+05:30",
     "cams": None,
     "reason": "full outage (bcmgenet NIC wedge)"},
    {"start": "2026-08-01T22:35:00+05:30", "end": "2026-08-01T22:52:00+05:30",
     "cams": ["ch16", "ch37"],
     "reason": "ch16, ch37 dark (stale channel list)"},
    {"start": "2026-08-01T22:52:00+05:30", "end": "2026-08-01T23:12:00+05:30",
     "cams": None,
     "reason": "fragmented churn (stall-detector restart loop)"},
    {"start": "2026-08-01T23:16:00+05:30", "end": "2026-08-01T23:18:00+05:30",
     "cams": [c for c in ALL_CAMS if c != "ch16"],
     "reason": "all except ch16 dark"},
    # Surfaced by the auto-detector on the first live run and DECLARED here by
    # hand (the detector only ever flags; declaring stays a human decision).
    # Left live, these two days counted as covered time with no boardings —
    # deflating the 5-min average and inflating every peak:average ratio.
    {"start": "2026-07-24T00:00:00+05:30", "end": "2026-07-25T00:00:00+05:30",
     "cams": None,
     "reason": "undeclared — no rows; cause unknown. Whole IST day: 0 transits "
               "fleet-wide and 94 gw_door_event rows on ch16 only, against a "
               "12-13k/day neighbour baseline."},
    {"start": "2026-07-27T00:00:00+05:30", "end": "2026-07-28T00:00:00+05:30",
     "cams": None,
     "reason": "undeclared — no rows; cause unknown. Whole IST day: 1 transit "
               "fleet-wide and 65 gw_door_event rows on ch16 only, against a "
               "4-13k/day neighbour baseline."},
]
for _g in DATA_GAPS:
    _g["start_epoch"] = datetime.fromisoformat(_g["start"]).timestamp()
    _g["end_epoch"] = datetime.fromisoformat(_g["end"]).timestamp()


def gaps_for(cam: str) -> list[dict]:
    """Gap windows affecting this camera (cams=None means fleet-wide)."""
    return [g for g in DATA_GAPS if g["cams"] is None or cam in g["cams"]]


def gap_overlap_s(cam: str, t0: float, t1: float) -> float:
    """Seconds of [t0,t1] falling inside declared gaps for cam. Overlapping
    declared windows are unioned so a second count is never excluded twice."""
    ivs = sorted((max(t0, g["start_epoch"]), min(t1, g["end_epoch"]))
                 for g in gaps_for(cam))
    total, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in ivs:
        if hi <= lo:
            continue
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total


def in_gap(cam: str, ts: float) -> bool:
    return any(g["start_epoch"] <= ts < g["end_epoch"] for g in gaps_for(cam))


# ── Counting-version eras (transit/counting logic, not the door instrument) ──
# Declared epochs: (cam or None for all, effective-from ISO, version). The DB
# stores counting_version only as CURRENT state (analyzer_status) and as the
# go-live stamp (camera_validation), so historical attribution is declared here
# and cross-checked against those tables at run time (mismatches are flagged on
# COVERAGE, never papered over).
COUNTING_EPOCHS = [
    (None, "2026-07-17T00:00:00+05:30", "2026-07-17-yolo11m-dwell-disp"),
    (None, "2026-07-29T00:00:00+05:30", "2026-07-28-registry-zones"),
]
PRE_EPOCH_VERSION = "pre-2026-07-17 (unattributed)"
_EPOCHS_PARSED = sorted(
    ((cam, datetime.fromisoformat(ts).timestamp(), ver)
     for cam, ts, ver in COUNTING_EPOCHS), key=lambda x: x[1])


def counting_version_at(cam: str, epoch_ts: float,
                        validation_stamps: dict | None = None) -> str:
    """Counting version in effect for cam at epoch_ts.

    validation_stamps: {cam: [(confirmed_at_epoch, version), ...]} from
    camera_validation — a per-camera stamp takes precedence over the declared
    fleet-wide epoch for times at/after that stamp."""
    ver = PRE_EPOCH_VERSION
    cut = -1.0
    for c, t, v in _EPOCHS_PARSED:
        if (c is None or c == cam) and t <= epoch_ts and t > cut:
            ver, cut = v, t
    if validation_stamps:
        for t, v in sorted(validation_stamps.get(cam, [])):
            if t is not None and v and t <= epoch_ts and t > cut:
                ver, cut = v, t
    return ver


# ── Pi-watch sub-era id ──────────────────────────────────────────────────────
def pi_era_id(epoch_ts: float) -> str:
    if epoch_ts < CLOSE_TRAVEL_MAX_EPOCH:
        return "pi_watch/pre-ctmax30"
    return "pi_watch/ctmax30"


def gpu_era_id(door_version: str) -> str:
    """door_version is templates_hash[:8]+'+'+geometry_hash[:8] — a content
    hash with no ordering. The era is the templates half, prefix-matched."""
    return (door_version or "").split("+", 1)[0][:8] or "unversioned"


# ── Bank mapping (sidecar; registry edits are out of scope for this module) ──
# Aj populates this file; every channel defaults to blank/UNKNOWN and the
# workbook must NOT guess bank assignments.
BANKS_SIDECAR_DEFAULT = "lift_banks.json"


def load_banks(path: str | Path | None = None) -> dict[str, str]:
    """{cam: bank}. Missing file / missing cam → '' (UNKNOWN). Never guessed."""
    p = Path(path) if path else Path(__file__).resolve().parent.parent / BANKS_SIDECAR_DEFAULT
    banks = {cam: "" for cam in ALL_CAMS}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for cam, bank in (data.get("banks") or {}).items():
            if cam in banks and isinstance(bank, str):
                banks[cam] = bank.strip()
    except (OSError, ValueError):
        pass
    return banks


def lift_label(cam: str) -> str:
    return f"lift {cam.removeprefix('ch')}"


# ── Bank derivation from the registry ────────────────────────────────────────
# All lifts here serve ONE tower, but a tall tower is normally ZONED and MEP-02
# treats each bank as a separate design case — so which lifts share a bank is a
# real question, not bookkeeping.
#
# The only evidence the DB holds is camera_registry.floor_range, an
# operator-entered fact. Lifts serving the SAME floor range are one bank. Where
# the field is blank the lift stays UNKNOWN: a bank is never inferred from
# channel number, from observed floors (floor attribution is unreliable — see
# TIER-2 BLOCKED), or from anything else. Guessing a bank would silently merge
# two design cases.

def normalise_floor_range(raw: str | None) -> str:
    """Canonical form of a floor_range string, so '1-30' and ' 1 - 30 ' agree.
    Returns '' for anything blank or unparseable — never a guess."""
    if not raw:
        return ""
    s = " ".join(str(raw).split()).strip().strip(",;")
    if not s:
        return ""
    return s.replace(" - ", "-").replace(" -", "-").replace("- ", "-").upper()


def derive_banks(registry: dict, cams: list[str] | None = None) -> tuple[dict, dict]:
    """(banks, evidence) from camera_registry.floor_range.

    banks:    {cam: bank_name or ''}  — '' means UNKNOWN, and stays UNKNOWN.
    evidence: {cam: {'floor_range', 'source', 'shared_with'}} so COVERAGE & ERAS
              can show WHY a lift was placed in a bank, or why it was not.

    Banks are named after the floor range they serve ("floors 1-30"), because a
    bank letter is not in the DB and inventing one would imply knowledge that
    does not exist."""
    cams = list(cams or registry.keys())
    by_range: dict[str, list[str]] = {}
    for cam in cams:
        fr = normalise_floor_range((registry.get(cam) or {}).get("floor_range"))
        if fr:
            by_range.setdefault(fr, []).append(cam)
    banks, evidence = {}, {}
    for cam in cams:
        fr = normalise_floor_range((registry.get(cam) or {}).get("floor_range"))
        if fr:
            shared = sorted(c for c in by_range[fr] if c != cam)
            banks[cam] = f"floors {fr}"
            evidence[cam] = {
                "floor_range": fr,
                "source": "camera_registry.floor_range (operator-entered)",
                "shared_with": shared}
        else:
            banks[cam] = ""
            evidence[cam] = {
                "floor_range": "",
                "source": "camera_registry.floor_range is EMPTY — bank not "
                          "derivable; never guessed",
                "shared_with": []}
    return banks, evidence


def load_banks_with_derivation(registry: dict, cams: list[str],
                               path: str | Path | None = None
                               ) -> tuple[dict, dict]:
    """Sidecar first (an operator's explicit statement outranks a derivation),
    then fill the blanks from the registry. Evidence records which won."""
    sidecar = load_banks(path)
    derived, evidence = derive_banks(registry, cams)
    banks = {}
    for cam in cams:
        if sidecar.get(cam):
            banks[cam] = sidecar[cam]
            evidence[cam] = dict(evidence.get(cam, {}),
                                 source="lift_banks.json (explicit operator "
                                        "assignment; overrides derivation)")
        else:
            banks[cam] = derived.get(cam, "")
    return banks, evidence
