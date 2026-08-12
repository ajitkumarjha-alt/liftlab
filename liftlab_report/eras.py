"""Declared comparability boundaries, data gaps, and era attribution.

Everything in this file is a DECLARATION, not a query: boundaries and gaps are
facts about the deployment history that the DB does not record about itself.
New gaps are APPENDED to DATA_GAPS; never encode a gap inside a query.

Sources of truth mirrored here (do not silently diverge from them):
  dash_api.DOORWATCH_RETIRED_BOUNDARY   2026-07-21T00:00:00+00:00
  dash_api.CLOSE_TRAVEL_MAX_BOUNDARY    2026-07-16T11:48:11+00:00
  dash_api.DOOR_SPECS                   ch29 2.00s / 2.31s Bank C, C26 1.50 s/p
  dash_api.DATA_GAPS                    Jul 19-20 relay stall (ch29)
  dash_api.OCC_CALIBRATION              peak car occupancy is a measured minimum
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

# ── The study ────────────────────────────────────────────────────────────────
# What this is: empirical calibration of lift DESIGN FACTORS, measured from
# cabin CCTV in handed-over, occupied residential towers. Lift traffic analysis
# for new towers uses assumed coefficients from a standards table that has
# never been validated against a real occupied building. This measures what
# actually happens and feeds corrected factors back into the design sheet.
#
# It is a BENCHMARKING STUDY across a portfolio, not a compliance audit of one
# building, and this workbook is one site's measurements toward that.
STUDY_QUESTIONS = [
    "Was the lift selection correct for this project — the number of lifts, "
    "their speed and capacity — given how the building is actually used?",
    "Can the same assumed coefficients be reused for future projects, or do "
    "they need revising, and by how much?",
]
STUDY_SCOPE = ("5 projects across different categories, analysed one at a "
               "time, roughly a week of footage each.")

# ── The sheet (MEP-02 v28) ───────────────────────────────────────────────────
SHEET_NAME = "MEP-02 v28"

# The eight assumed coefficients under test. `needs` states what it would take to measure each.
#
# STATUS PROVENANCE (corrected 2026-08-04): the status for every coefficient is computed per run
# from the data by narrative.coefficient_scope(). This comment previously claimed that was true of
# all eight "never declared, so the table cannot drift" — it was NOT true of C17/C18/C21/C22, which
# were hardcoded to BLOCKED in narrative.py and to "not measurable" in model.py regardless of what
# the data showed. They are now derived like the rest, each naming its own actual blocker. If you
# add a coefficient, derive its status; do not declare it.
COEFFICIENTS = [
    {"id": "C17", "name": "probable stops, up peak",
     "assumes": "how many floors a car stops at on an up trip",
     "needs": "trip segmentation, which needs floor attribution"},
    {"id": "C18", "name": "probable stops, down peak",
     "assumes": "how many floors a car stops at on a down trip",
     "needs": "trip segmentation, which needs floor attribution"},
    {"id": "C19", "name": "average passengers per trip",
     "assumes": "car loading as a share of rated capacity",
     "needs": "per-cycle passenger counts joined to trips, plus the car's "
              "rated capacity (not held in this database)"},
    {"id": "C21", "name": "speed factor, up",
     "assumes": "how much of rated speed is achieved between stops",
     "needs": "floor attribution on consecutive stops"},
    {"id": "C22", "name": "speed factor, down",
     "assumes": "how much of rated speed is achieved between stops",
     "needs": "floor attribution on consecutive stops"},
    {"id": "C26", "name": "passenger transfer time",
     "assumes": "seconds per person to board or alight",
     "needs": "per-cycle passenger counts joined to door dwell"},
    {"id": "C27", "name": "door operating time",
     "assumes": "seconds for the doors to close, and to open",
     "needs": "door timing from cabin video at sufficient frame rate"},
    {"id": "B24", "name": "lost time per stop",
     "assumes": "seconds lost per stop beyond transfer and door time",
     "needs": "dwell measured against passenger load"},
]

# The handling-capacity assumption is a DEMAND figure, not an RTT coefficient:
# the share of the building's population wanting to move in the peak 5 minutes.
# Cameras measure it directly and it needs no floor attribution, which is why
# it is the highest-value output of the study and is tracked separately from
# the coefficient table above.
HC_ASSUMPTION_NOTE = (
    "the share of the building's population that wants to move in the busiest "
    "five minutes")
DOOR_SPECS = {
    "ch29": {"sheet_close_s": 2.00, "compliance_s": 2.31, "bank": "C",
             "transfer_sheet_s": 1.50, "sheet_open_s": 3.00},
}
# PEAK CAR OCCUPANCY — mirrored verbatim from dash_api.OCC_CALIBRATION so the
# workbook and the dashboard cannot state the same figure with different limits.
# The number is the most people SEEN AT ONCE inside the cabin zone during one
# door-open episode: a floor, not a count. The 0.5x is one scene on one camera.
OCC_CALIBRATION = ("measured minimum; ~0.5x at heavy crowding (n=1 scene, ch30); "
                   "undercount grows with crowding")
OCC_ANCHOR_NOTE = ("foot anchor — the same membership point as the transit counter; a centre anchor "
                   "measured LOWER against a floor polygon, so it is not the fix")

# Defaults used for cameras with no explicit spec entry (sheet-wide values).
SHEET_CLOSE_S = 2.00
SHEET_OPEN_S = 3.00
SHEET_TRANSFER_S_PP = 1.50
COMPLIANCE_CLIFF_S = 2.31            # Bank C non-compliance line
HC_PEAK_DESIGN_PCT = 8.0             # 5-min handling capacity design assumption

# ── Car capacity, and THE BASIS IT IS STATED ON ──────────────────────────────
# Capacity in PERSONS, per Aj — not kg. A kg rating divided by an assumed body
# mass is a different number with a different error, and the sheet's loading
# assumption is written against persons, so persons is the only basis on which
# the two can be compared at all.
#
# THE REFUSAL. A loading factor is occupancy / capacity. If the basis of that
# denominator is not confirmed, the quotient is not a measurement of anything:
# 5/13 on nameplate persons and 5/13 on design persons are different claims
# about the building even when the arithmetic is identical, and only one of them
# can be checked against the sheet's 80%. So until the basis is CONFIRMED the
# workbook prints the measured occupancy in absolute people and prints NO
# percentage — not a provisional one, not a greyed-out one, not one in a
# footnote. There is no such thing as a provisional denominator.
#
# CAPACITY_PERSONS_PLACEHOLDER is carried so the sheet can show what it WOULD
# use, marked as a placeholder that is not in use. It is never multiplied by
# anything while the basis is unconfirmed.
CAPACITY_PERSONS_PLACEHOLDER = 13
CAPACITY_SIDECAR_DEFAULT = "lift_capacity.json"
CAPACITY_BASES = {
    "nameplate_persons": "nameplate persons — the figure on the car's rating "
                         "plate / lift licence, as installed",
    "design_persons_mep02": f"design persons per {SHEET_NAME} — the figure the "
                            f"sheet designed the car to carry",
}
# The sheet's design car loading. The workbook may only compare against this
# when its own basis MATCHES the basis the sheet's 80% is written on.
SHEET_LOADING_PCT = 80.0


def load_capacity(path: str | Path | None = None) -> dict:
    """The capacity declaration: persons per car, its basis, and the sheet's basis.

    Sidecar JSON, same convention as lift_banks.json:

        {"basis": "nameplate_persons",          # or design_persons_mep02
         "basis_confirmed_by": "who confirmed it, and from what document",
         "sheet_basis": "design_persons_mep02", # what MEP-02's 80% is written on
         "persons": {"ch30": 13, "ch27": 13}}

    Missing file, missing basis, or a basis not in CAPACITY_BASES -> UNCONFIRMED.
    Unconfirmed is the DEFAULT and is never upgraded by inference: a capacity
    number present without a stated basis stays unconfirmed, because the number
    is not the thing in doubt.
    """
    p = (Path(path) if path
         else Path(__file__).resolve().parent.parent / CAPACITY_SIDECAR_DEFAULT)
    out = {"basis": None, "basis_label": None, "basis_confirmed_by": None,
           "sheet_basis": None, "persons": {}, "source": f"{p} (not found)",
           "placeholder_persons": CAPACITY_PERSONS_PLACEHOLDER}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out["source"] = str(p)
    basis = str(data.get("basis") or "").strip()
    if basis in CAPACITY_BASES:
        out["basis"] = basis
        out["basis_label"] = CAPACITY_BASES[basis]
    elif basis:
        out["source"] += f" (basis {basis!r} is not one of {sorted(CAPACITY_BASES)})"
    out["basis_confirmed_by"] = (data.get("basis_confirmed_by") or "").strip() or None
    sb = str(data.get("sheet_basis") or "").strip()
    out["sheet_basis"] = sb if sb in CAPACITY_BASES else None
    for cam, n in (data.get("persons") or {}).items():
        try:
            out["persons"][cam] = int(n)
        except (TypeError, ValueError):
            continue
    return out


def capacity_status(capacity: dict) -> dict:
    """May a loading factor be printed, and if not, exactly what is missing.

    Two independent gates, reported separately because they are fixed by
    different people:
      * `can_print_loading` — is the workbook's own basis confirmed at all
      * `can_compare_sheet` — does that basis MATCH the one the sheet's 80% uses
    A matching-basis failure is not a smaller version of the first; it means the
    workbook and the sheet are both internally consistent and are talking about
    different cars.
    """
    cap = capacity or {}
    basis, sheet_basis = cap.get("basis"), cap.get("sheet_basis")
    who = cap.get("basis_confirmed_by")
    missing = []
    if not basis:
        missing.append(f"capacity basis (one of: {', '.join(sorted(CAPACITY_BASES))})")
    if not who:
        missing.append("who confirmed the basis, and from which document")
    if not cap.get("persons"):
        missing.append("persons per car")
    can = not missing
    if can and not sheet_basis:
        cmp_ok, cmp_why = False, (
            f"the basis {SHEET_NAME}'s {SHEET_LOADING_PCT:.0f}% loading is written on "
            f"has not been stated, so this workbook cannot say whether its own "
            f"basis matches it")
    elif can and sheet_basis != basis:
        cmp_ok, cmp_why = False, (
            f"basis MISMATCH — this workbook measures against "
            f"{CAPACITY_BASES[basis]}, while {SHEET_NAME}'s "
            f"{SHEET_LOADING_PCT:.0f}% is written on {CAPACITY_BASES[sheet_basis]}. "
            f"Same arithmetic, different car. The comparison is withheld rather "
            f"than made across the two.")
    elif can:
        cmp_ok, cmp_why = True, (
            f"basis matches {SHEET_NAME}: both are {CAPACITY_BASES[basis]}")
    else:
        cmp_ok, cmp_why = False, "no basis, so nothing to compare"
    return {"can_print_loading": can, "missing": missing,
            "can_compare_sheet": cmp_ok, "compare_note": cmp_why,
            "basis": basis, "basis_label": (CAPACITY_BASES.get(basis) if basis else None),
            "basis_confirmed_by": who, "sheet_basis": sheet_basis}


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


# ── what counts as a CONFIDENT floor read ────────────────────────────────────
# DoorFloorEngine.process (gpu_door.py) emits reason ∈ ok | single_panel | disagree | ambiguous |
# no_read:
#   ok           = TWO panels read the same floor (agree-or-discard reconciliation)
#   single_panel = ONE panel configured; that panel returned status=='ok'. The read passed the same
#                  per-panel bar as each half of an 'ok'; what it lacks is the cross-check.
# Cameras running single-panel (panel1 cells not calibrated) emit single_panel for EVERY good read
# and never 'ok'. Scoring those as non-confident reports a camera with tens of thousands of reads as
# floor-blind — which is exactly what this module did until 2026-08-04, and it is what made the
# TIER-2 sheet claim zero confident reads fleet-wide.
#
# dash_api.DOOR_OK_REASONS and door_event_api.py both already use the wider set; this module was the
# outlier. '' is kept as a defensive case for a null reason carrying a floor.
#
# THE RESIDUAL RISK IS REAL AND IS NOT HIDDEN: a single panel cannot catch a SYSTEMATIC misread, and
# a stable single-glyph confusion self-corroborates (19->18->17 read as 79->78->77). That is what the
# derived floor alphabet with its anchoring and flip-kill rules defends against, and why the TIER-2
# sheet publishes the per-reason census beside the count rather than the count alone.
FLOOR_OK_REASONS = ("", "ok", "single_panel")

# Plausibility ceiling for a floors/second segment, mirroring dash_api.MAX_FLOORS_PER_S. A segment
# above this is an OCR slip, not a lift.
MAX_FLOORS_PER_S = 3.0


def floor_index(label) -> int | None:
    """Physical index for a floor label, or None when it cannot be ordered.

    Numeric labels map by int(). Anything else (G, LG, MEP, P3) needs a declared order this report
    does not hold, so it is EXCLUDED from speed rather than guessed at."""
    try:
        return int(str(label).strip())
    except (TypeError, ValueError):
        return None


def templates_era(door_version: str) -> str:
    """The FULL templates half of door_version, untruncated.

    gpu_era_id() truncates this to 8 chars on the assumption that door_version is exactly
    templates_hash[:8]+'+'+geometry_hash[:8]. Live data violates that: rebuilds carry a hand-added
    tag, e.g. 'e79e50d3h2+54509e75' and '260d4a0fh2Laa52+495e8f48'. Truncating merges a rebuild
    into the era it replaced — which hid ch16 dropping from 19,424 confident reads in e79e50d3 to
    1 in e79e50d3h2 behind a single healthy-looking total.

    Used by the Tier-2 evidence, which must show a rebuild as its own instrument. gpu_era_id is
    deliberately left alone here: it also groups door CYCLES, and changing that regroups every
    cycle-derived figure in the report. See README_REPORT.md for that open item."""
    return (door_version or "").split("+", 1)[0] or "unversioned"


def gpu_era_id(door_version: str) -> str:
    """The GPU era id — the FULL templates half of door_version.

    This used to truncate to 8 characters, on the documented assumption that door_version is
    exactly templates_hash[:8]+'+'+geometry_hash[:8]. Live data violates that: rebuilds carry a
    hand-added tag (e79e50d3h2, 260d4a0fh2Laa52). Truncating merged a rebuild into the era it
    replaced, and because this function also groups door CYCLES, close-travel was being pooled
    across rebuild boundaries — 7,539 of 10,571 cycles (71%) sat in a merged group.

    Measured effect of splitting them (2026-08-04): no verdict flipped, but every reported n and
    CI moved, and pooling made the study look MORE converged than it is —
        ch29 pooled n=171 median 2.231 CI [1.442, 3.225]
             split  n=101 median 2.129 CI [1.176, 3.383]   <- true CI is 0.42s WIDER
        ch16 pooled n=680 median 2.185 ; current era n=596 median 2.122
        ch27 pooled n=2417 median 3.258 ; current era n=2384 median 3.278
    Since the stopping rule ends the study when the CI band clears the line, an artificially
    narrow CI is the one error this report must not make."""
    return templates_era(door_version)


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


# ── Display names ────────────────────────────────────────────────────────────
# The building calls it "lift 1"; the gateway calls it ch16. Those are two
# different things and a reader handed both without explanation would infer
# twice as many lifts as exist. The workbook shows the BUILDING's name, because
# the workbook is read by people who know the building.
#
# The mapping comes from channel_map.label and is installed once per run by
# build_context. It is process state rather than a threaded-through argument
# because lift_label() is called from every module and every sheet; the setter
# is called unconditionally at the top of every build so a run can never
# inherit the previous run's names.
_DISPLAY_LABELS: dict[str, str] = {}

# Marks a name the workbook had to invent because channel_map had none.
UNLABELLED_SUFFIX = " (unnamed — no channel_map label)"


def set_display_labels(labels: dict | None) -> None:
    """Install this run's {cam: building name}. Always called, even when empty."""
    _DISPLAY_LABELS.clear()
    for cam, label in (labels or {}).items():
        if label and str(label).strip():
            _DISPLAY_LABELS[cam] = str(label).strip()


def has_display_label(cam: str) -> bool:
    return cam in _DISPLAY_LABELS


def lift_label(cam: str) -> str:
    """The building's name for this lift, e.g. 'lift 1'.

    Falls back to the channel number ONLY when the building has not named it,
    and says so visibly — an invented name that looks like a real one is worse
    than an obvious placeholder."""
    if cam in _DISPLAY_LABELS:
        return _DISPLAY_LABELS[cam]
    return f"lift {cam.removeprefix('ch')}{UNLABELLED_SUFFIX}"


def lift_label_with_channel(cam: str) -> str:
    """'lift 1 (ch16)' — for the first mention on a sheet, so the reader can
    tie the building's name to the camera channel used everywhere else."""
    return f"{lift_label(cam)} ({cam})"


# ── Bank derivation from the registry ────────────────────────────────────────
# All lifts here serve ONE tower, but a tall tower is normally ZONED and MEP-02
# treats each bank as a separate design case — so which lifts share a bank is a
# real question, not bookkeeping.
#
# The only evidence the DB holds is camera_registry.floor_range, an
# operator-entered fact. Lifts serving the SAME floor range are one bank. Where
# the field is blank the lift stays UNKNOWN: a bank is never inferred from
# channel number, from observed floors (floor attribution is unreliable — see
# TIER-2 EVIDENCE), or from anything else. Guessing a bank would silently merge
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
