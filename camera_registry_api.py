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
    return db


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
        "SELECT cam, enabled, stride, analyze_fps, note, updated_at FROM camera_registry "
        "WHERE gateway_id=? ORDER BY cam", (gw,)).fetchall()]


def _payload(rows, gw):
    """The fleet's view + a content hash. The hash covers only what CHANGES WHAT RUNS — including
    geometry, so redrawing an ROI restarts that worker — but not notes or updated_at, which would
    make an edited comment look like a restart-worthy change."""
    cams = []
    for r in rows:
        c = {"cam": r["cam"], "enabled": bool(r["enabled"]), "stride": int(r["stride"] or 2),
             "analyze_fps": float(r["analyze_fps"] or 0)}
        c["geometry"] = _geometry(gw, r["cam"])
        cams.append(c)
    h = hashlib.sha256(json.dumps(sorted(
        (c["cam"], c["enabled"], c["stride"], c["analyze_fps"],
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
    cur = db.execute("SELECT enabled, stride, analyze_fps FROM camera_registry WHERE gateway_id=? AND cam=?",
                     (gw, cam)).fetchone()
    enabled = bool(d.get("enabled", cur["enabled"] if cur else False))
    stride = int(d.get("stride", (cur["stride"] if cur else 2) or 2))
    afps = float(d.get("analyze_fps", (cur["analyze_fps"] if cur else 0) or 0))
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
    db.execute("INSERT INTO camera_registry (gateway_id,cam,enabled,stride,analyze_fps,note,updated_at) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "enabled=excluded.enabled, stride=excluded.stride, analyze_fps=excluded.analyze_fps, "
               "note=excluded.note, updated_at=excluded.updated_at",
               (gw, cam, 1 if enabled else 0, stride, afps, str(d.get("note", ""))[:200], time.time()))
    db.commit()
    rows = _rows(db, gw)
    db.close()
    cams, h = _payload(rows, gw)
    return {"ok": True, "cam": cam, "enabled": enabled, "stride": stride, "analyze_fps": afps,
            "hash": h, "cameras": cams,
            "note": "the GPU fleet picks this up on its next poll (~30s); nothing restarts"}
