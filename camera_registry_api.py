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

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
GATEWAY_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                  for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g}
ANALYSIS_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                   for g in os.environ.get("ANALYSIS_TOKENS", "").split(",") if ":" in g}
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_ENABLED = int(os.environ.get("FLEET_MAX_ENABLED", "7"))   # the L4 fits ~7 at 14%/cam

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


def _payload(rows):
    """The fleet's view + a content hash. Hash covers ONLY the fields that change what runs, so an
    edited note or a touched updated_at does not look like a restart-worthy change."""
    cams = [{"cam": r["cam"], "enabled": bool(r["enabled"]), "stride": int(r["stride"] or 2),
             "analyze_fps": float(r["analyze_fps"] or 0)} for r in rows]
    h = hashlib.sha256(json.dumps(sorted((c["cam"], c["enabled"], c["stride"], c["analyze_fps"])
                                         for c in cams)).encode()).hexdigest()[:12]
    return cams, h


@camera_registry_router.get("/api/gw/{gw}/cameras")
def cameras_get(gw: str, authorization: str = Header("")):
    _safe(gw)
    _auth(gw, authorization)
    db = _db()
    rows = _rows(db, gw)
    db.close()
    cams, h = _payload(rows)
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
    cams, h = _payload(rows)
    return {"ok": True, "cam": cam, "enabled": enabled, "stride": stride, "analyze_fps": afps,
            "hash": h, "cameras": cams,
            "note": "the GPU fleet picks this up on its next poll (~30s); nothing restarts"}
