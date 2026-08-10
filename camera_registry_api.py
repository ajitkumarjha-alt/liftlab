"""GPU camera registry — /api/gw/{gw}/cameras. The FIFTH wizard piece.

Which cameras the GPU analyses is DATA, not deployment. Adding ch16 becomes a toggle on /dash: no
env edit, no SSH, no unit restart. The GPU fleet supervisor polls this the same way the door engine
polls templates — by content hash, so a poll that finds nothing changed costs one comparison.

  GET  /api/gw/{gw}/cameras          Bearer (gateway OR analysis token) — what the fleet should run
  POST /api/gw/{gw}/cameras/{cam}    operator toggle from /dash (Caddy basicauth, same as the
                                     /floorcheck review POST — a human page, no token in a browser)

FIELDS
  cam          "ch29"
  enabled      does the fleet run a worker for it
  stride       door-pass stride (DOOR_STRIDE): 2 = ~12fps of the 25fps decode
  analyze_fps  counting subsample; 0 = every frame. THE fleet capacity knob — 7 cameras do not fit
               at 25fps, so this is what gets turned down, not `stride`.
  door_levels  OPTIONAL per-camera DoorTracker levels, e.g. {"close_th": 0.20}. Door edge contrast
               is per-camera physics (2026-07-30: h2 tripled close-completion on ch16/ch27 and
               REGRESSED ch29 to 0.9% — ch29's edge never reads below the global close_th, so
               closes never complete), so the fully-shut/fully-open levels are per-camera DATA.
               Keys: near_open, close_th, close_start_th, close_debounce_s. Omitted keys keep the
               worker's env/defaults. NON-DEFAULT LEVELS MOVE THE DOOR ERA (gpu_analyze stamps a
               levels tag into the door_version prefix): completion rates under different levels
               are different instruments and must never pool. Recalibration procedure: raise
               close_th stepwise; the target is ch27's born-clean-era funnel (~13% with_travel/closed,
               mean travel ~4.2s) — matching THAT shape, not maximizing completions, is the verdict.

A row is the operator's intent. The fleet's job is to converge on it, and — critically — to do
NOTHING when it cannot read it. See gpu_fleet.py: an unreachable cloud must never stop a camera
that is currently producing data.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
GATEWAY_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                  for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g}
ANALYSIS_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                   for g in os.environ.get("ANALYSIS_TOKENS", "").split(",") if ":" in g}
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_ENABLED = int(os.environ.get("FLEET_MAX_ENABLED", "7"))   # the L4 fits ~7 at 14%/cam
CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))


def _geometry(gw, cam):
    """This camera's door geometry, from roi.json — the wizard's output and the source of truth.

    THE LAST SELF-CONFIGURE GAP. Workers are spawned by the fleet from the registry, but door
    geometry was env-based and per-camera, so a fleet-started worker counted transits and could not
    read floors: nobody had put DOOR_ROI_FRAME/PANEL_ROIS/DIGIT_CELLS in its environment. Geometry
    is per-camera DATA and already lives per-camera on disk, so it travels with the rest of the
    camera's configuration instead of being hand-placed in a unit file.

    Returned in the exact string shapes gpu_analyze parses, so the fleet passes them through
    untouched — a second place that formats geometry is a second place it can be formatted wrong.
    """
    try:
        d = json.loads((CALIB_DIR / gw / cam / "roi.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}

    def _xywh(v):
        return ",".join(str(int(round(float(n)))) for n in v) if isinstance(v, (list, tuple)) and len(v) == 4 else None

    door = _xywh(d.get("door_roi_frame"))
    if door:
        out["door_roi_frame"] = door
    panels = [p for p in (_xywh(x) for x in (d.get("panel_rois") or [])) if p]
    if panels:
        out["panel_rois"] = ";".join(panels)
    cells = d.get("cells") or {}
    digits = [c for c in (_xywh(x) for x in (cells.get("digit_cells") or [])) if c]
    if digits:
        out["digit_cells"] = ";".join(digits)
    arrow = _xywh(cells.get("arrow_cell"))
    if arrow:
        out["arrow_cell"] = arrow
    # Cells measured against a panel that has since moved describe different pixels. The wizard
    # already flags this; carry the flag so a worker is not configured from geometry known stale.
    if cells.get("stale"):
        out["cells_stale"] = str(cells["stale"])[:200]

    # Counting zones travel the same way (the ch16 undercount was ch29's polygons scaled onto a
    # different camera's optics). BOTH-or-neither: a counter needs landing AND cabin. Served as the
    # exact JSON strings gpu_analyze parses; zone_frame is the size the polygons were drawn at.
    def _poly(v):
        ok = (isinstance(v, list) and len(v) >= 3
              and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in v))
        return json.dumps([[int(round(float(x))), int(round(float(y)))] for x, y in v]) if ok else None

    zl, zc = _poly(d.get("zone_landing")), _poly(d.get("zone_cabin"))
    if zl and zc:
        out["zone_landing"] = zl
        out["zone_cabin"] = zc
        zf = d.get("zone_frame") or d.get("frame_wh")
        if isinstance(zf, (list, tuple)) and len(zf) == 2:
            out["zone_frame"] = f"{int(zf[0])},{int(zf[1])}"
    return out

camera_registry_router = APIRouter()


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS camera_registry (
      gateway_id TEXT, cam TEXT, enabled INTEGER DEFAULT 0, stride INTEGER DEFAULT 2,
      analyze_fps REAL DEFAULT 0, note TEXT, updated_at REAL,
      PRIMARY KEY (gateway_id, cam))""")
    try:                                          # migration: pre-door_levels tables lack the column
        db.execute("ALTER TABLE camera_registry ADD COLUMN door_levels TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass                                      # already there
    try:                                          # migration: per-camera floor range (2026-07-30)
        db.execute("ALTER TABLE camera_registry ADD COLUMN floor_range TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass                                      # already there
    try:                                          # migration: per-camera door engine (2026-08-05)
        db.execute("ALTER TABLE camera_registry ADD COLUMN door_tracker TEXT DEFAULT 'h2'")
    except sqlite3.OperationalError:
        pass                                      # already there
    try:                                          # migration: per-camera floor-OCR cadence (2026-08-10)
        db.execute("ALTER TABLE camera_registry ADD COLUMN floor_stride INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass                                      # already there
    return db


def _parse_floor_range(v):
    """Canonical 'lo-hi' from operator input, or '' to clear. OPERATOR-ENTERED from the tower
    fact sheet, never derived from observed reads — deriving from reads would bless the
    phantoms (ch16's 7X band and ch29's 68 both look legitimate in the read stream).
    Consumed by the door_event admission validator: numeric floors outside the range become
    reason='invalid_label'. Absent range = numerics pass through unchecked, by design —
    grammar catches 20.2% of ch16's phantom load, range detection lifts it to 25.8%."""
    if v in (None, "", {}):
        return ""
    s = str(v).strip()
    if "-" not in s:
        raise HTTPException(400, "floor_range must be 'lo-hi' (e.g. '1-60') or empty to clear")
    lo, hi = s.split("-", 1)
    try:
        lo, hi = int(lo), int(hi)
    except ValueError:
        raise HTTPException(400, "floor_range bounds must be integers")
    if not (1 <= lo < hi <= 999):
        raise HTTPException(400, "floor_range must satisfy 1 <= lo < hi <= 999")
    return f"{lo}-{hi}"


# DoorTracker level knobs an operator may set per camera, with sane bounds. Bounds are wide on
# purpose — they reject typos (close_th=20 for 0.20), not judgement calls.
_LEVEL_KEYS = {"near_open": (0.5, 1.0), "close_th": (0.01, 0.6),
               "close_start_th": (0.2, 0.95), "close_debounce_s": (0.0, 5.0)}


def _parse_levels(v):
    """Validated canonical door_levels dict from operator input; {} means 'use defaults'.
    Raises HTTPException(400) with the reason — a bad level silently dropped would look exactly
    like the recalibration not working."""
    if v in (None, "", {}):
        return {}
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise HTTPException(400, "door_levels must be a JSON object")
    if not isinstance(v, dict):
        raise HTTPException(400, "door_levels must be an object of {level: number}")
    out = {}
    for k, x in v.items():
        if k not in _LEVEL_KEYS:
            raise HTTPException(400, f"unknown door level '{k}' (valid: {sorted(_LEVEL_KEYS)})")
        lo, hi = _LEVEL_KEYS[k]
        try:
            x = float(x)
        except (TypeError, ValueError):
            raise HTTPException(400, f"door level '{k}' must be a number")
        if not (lo <= x <= hi):
            raise HTTPException(400, f"door level '{k}'={x} outside [{lo}, {hi}]")
        out[k] = x
    # ordering sanity where both ends are being set: shut must sit below the closing-entry level
    if "close_th" in out and "close_start_th" in out and out["close_th"] >= out["close_start_th"]:
        raise HTTPException(400, "close_th must be below close_start_th")
    return out


def _auth(gw, authorization):
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if tok and (GATEWAY_TOKENS.get(gw) == tok or ANALYSIS_TOKENS.get(gw) == tok):
        return
    raise HTTPException(401, "bad token")


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _rows(db, gw):
    return [dict(r) for r in db.execute(
        "SELECT cam, enabled, stride, analyze_fps, note, updated_at, door_levels, floor_range, "
        "door_tracker, floor_stride FROM camera_registry WHERE gateway_id=? ORDER BY cam", (gw,)).fetchall()]


def _payload(rows, gw):
    """The fleet's view + a content hash. The hash covers only what CHANGES WHAT RUNS — including
    geometry, so redrawing an ROI restarts that worker — but not notes or updated_at, which would
    make an edited comment look like a restart-worthy change."""
    cams = []
    for r in rows:
        try:
            levels = json.loads(r.get("door_levels") or "{}")
        except ValueError:
            levels = {}
        c = {"cam": r["cam"], "enabled": bool(r["enabled"]), "stride": int(r["stride"] or 2),
             "analyze_fps": float(r["analyze_fps"] or 0), "door_levels": levels,
             "floor_range": (r.get("floor_range") or ""),
             "door_tracker": (r.get("door_tracker") or "h2"),
             "floor_stride": int(r.get("floor_stride") or 0)}
        c["geometry"] = _geometry(gw, r["cam"])
        cams.append(c)
    # door_levels is in the hash: a level change must restart that worker (env is read at import),
    # and the worker's fresh door_version era-splits the data from the moment it lands.
    # door_tracker is in the hash for the same reason and more strongly — it does not adjust the
    # instrument, it REPLACES it. Switching a camera between h2 and h3 must restart that worker and
    # must era-split its data, or the pool silently mixes two engines' cycles.
    h = hashlib.sha256(json.dumps(sorted(
        (c["cam"], c["enabled"], c["stride"], c["analyze_fps"],
         json.dumps(c["door_levels"], sort_keys=True), c["door_tracker"], c["floor_stride"],
         json.dumps(c["geometry"], sort_keys=True)) for c in cams)).encode()).hexdigest()[:12]
    return cams, h


@camera_registry_router.get("/api/gw/{gw}/cameras")
def cameras_get(gw: str, authorization: str = Header("")):
    _safe(gw)
    _auth(gw, authorization)
    db = _db()
    rows = _rows(db, gw)
    db.close()
    cams, h = _payload(rows, gw)
    return JSONResponse({"gateway": gw, "cameras": cams, "hash": h, "t": time.time(),
                         "enabled_count": sum(1 for c in cams if c["enabled"]),
                         "max_enabled": MAX_ENABLED})


@camera_registry_router.post("/api/gw/{gw}/cameras/{cam}")
async def cameras_set(gw: str, cam: str, request: Request):
    """Operator toggle. Basicauth (Caddy) like the other human POSTs — a browser has no Bearer."""
    _safe(gw, cam)
    d = await request.json()
    db = _db()
    cur = db.execute("SELECT enabled, stride, analyze_fps, door_levels, floor_range, door_tracker, "
                     "floor_stride FROM camera_registry WHERE gateway_id=? AND cam=?", (gw, cam)).fetchone()
    enabled = bool(d.get("enabled", cur["enabled"] if cur else False))
    stride = int(d.get("stride", (cur["stride"] if cur else 2) or 2))
    afps = float(d.get("analyze_fps", (cur["analyze_fps"] if cur else 0) or 0))
    # door_levels: absent = keep stored; explicit null/{} = clear back to defaults (an intentional
    # de-calibration is a config change too, and it also moves the era back).
    if "door_levels" in d:
        levels_json = json.dumps(_parse_levels(d["door_levels"]), sort_keys=True)
    else:
        levels_json = (cur["door_levels"] if cur else "") or "{}"
    # floor_range: absent = keep stored; explicit '' = clear (a tower fact was retracted)
    if "floor_range" in d:
        floor_range = _parse_floor_range(d["floor_range"])
    else:
        floor_range = (cur["floor_range"] if cur else "") or ""
    # door_tracker: which door ENGINE this camera runs. Absent = keep stored. The rollout is
    # per-camera data, so a new camera joins h3 by a POST here rather than by a code change — but
    # the gateway still refuses to run h3 without a template that loads and verifies, and falls
    # back to h2 loudly if one is missing. Setting this to h3 is a REQUEST, not a guarantee.
    if "door_tracker" in d:
        door_tracker = str(d.get("door_tracker") or "h2").strip().lower()
        if door_tracker not in ("h2", "h3"):
            db.close()
            raise HTTPException(400, "door_tracker must be 'h2' or 'h3'")
    else:
        door_tracker = ((cur["door_tracker"] if cur else "") or "h2")
    # floor_stride: frames between FLOOR OCR reads; 0 = every door pass (current behaviour). The
    # door state machine is unaffected — this only thins the expensive panel OCR, whose cost scales
    # with the camera's alphabet. Absent = keep stored.
    if "floor_stride" in d:
        floor_stride = int(d.get("floor_stride") or 0)
        if floor_stride < 0 or floor_stride > 250:
            db.close()
            raise HTTPException(400, "floor_stride must be 0..250 frames (0 = every door pass)")
    else:
        floor_stride = int((cur["floor_stride"] if cur else 0) or 0)
    if not (1 <= stride <= 25):
        db.close()
        raise HTTPException(400, "stride must be 1..25 (frames between door-pass reads)")
    if afps < 0 or afps > 25:
        db.close()
        raise HTTPException(400, "analyze_fps must be 0..25 (0 = every frame)")
    if enabled and not (cur and cur["enabled"]):
        n = db.execute("SELECT COUNT(*) c FROM camera_registry WHERE gateway_id=? AND enabled=1",
                       (gw,)).fetchone()["c"]
        if n >= MAX_ENABLED:
            db.close()
            # A hard stop, not a warning: past the GPU's capacity every camera degrades together,
            # which looks like a model problem rather than an over-subscription problem.
            raise HTTPException(409, f"{n} cameras already enabled (max {MAX_ENABLED} for this GPU) — "
                                     f"disable one first, or raise FLEET_MAX_ENABLED if the box grew")
    db.execute("INSERT INTO camera_registry (gateway_id,cam,enabled,stride,analyze_fps,note,updated_at,"
               "door_levels,floor_range,door_tracker,floor_stride) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "enabled=excluded.enabled, stride=excluded.stride, analyze_fps=excluded.analyze_fps, "
               "note=excluded.note, updated_at=excluded.updated_at, door_levels=excluded.door_levels, "
               "floor_range=excluded.floor_range, door_tracker=excluded.door_tracker, "
               "floor_stride=excluded.floor_stride",
               (gw, cam, 1 if enabled else 0, stride, afps, str(d.get("note", ""))[:200], time.time(),
                levels_json, floor_range, door_tracker, floor_stride))
    db.commit()
    rows = _rows(db, gw)
    db.close()
    cams, h = _payload(rows, gw)
    return {"ok": True, "cam": cam, "enabled": enabled, "stride": stride, "analyze_fps": afps,
            "door_levels": json.loads(levels_json), "hash": h, "cameras": cams,
            "note": "the GPU fleet picks this up on its next poll (~30s); a door_levels change "
                    "restarts that worker and MOVES ITS DOOR ERA (fresh comparability pool)"}
