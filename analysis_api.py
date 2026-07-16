"""
Analysis worker interface: Bearer segment PULL + transit ingest, for the GPU box. ADDITIVE.

The GPU box has NO service-account scopes (no gcloud/GCS) — it pulls segments over HTTPS with a
Bearer token, same scheme the Pi uses to PUT. A SEPARATE read-only analysis token (ANALYSIS_TOKENS)
authorizes only: GET segments + POST transits. It CANNOT do the relay PUT or other gateway writes.

  GET  /api/gw/{gw}/live/{cam}/{fname}   pull a segment / playlist (Bearer: gateway OR analysis)
  POST /api/gw/{gw}/transit             a boarded/alighted transit (Bearer; idempotent, deduped)
  GET  /api/gw/{gw}/transits            recent transits (Bearer) — for /ops

Transit counts land in transit_event (deduped). gw_event.boarded/alighted are NOT written here —
that fills once the gw_event cycle-window schema is confirmed (this logs it on startup). We do not
write the sacred event table on a guess.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
GATEWAY_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                  for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g}
ANALYSIS_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                   for g in os.environ.get("ANALYSIS_TOKENS", "").split(",") if ":" in g}
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
_CT = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}

analysis_router = APIRouter()


def _auth_rw(gw: str, authorization: str) -> None:
    """Accept the gateway token OR the read-only analysis token for this gateway."""
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if tok and (GATEWAY_TOKENS.get(gw) == tok or ANALYSIS_TOKENS.get(gw) == tok):
        return
    raise HTTPException(401, "bad token")


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS transit_event (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL, ts_bucket INTEGER,
      direction TEXT, track_id INTEGER, received_at REAL)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_transit "
               "ON transit_event (gateway_id,cam,track_id,direction,ts_bucket)")
    return db


# ---- segment pull (Bearer) ----
@analysis_router.get("/api/gw/{gw}/live/{cam}/{fname}")
def pull_segment(gw: str, cam: str, fname: str, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    _safe(gw, cam, fname)
    f = LIVE_DIR / gw / cam / fname
    if not f.exists():
        raise HTTPException(404, "not found")
    ct = _CT.get(Path(fname).suffix, "application/octet-stream")
    return Response(content=f.read_bytes(), media_type=ct,
                    headers={"Cache-Control": "no-store, no-cache, max-age=0"})


_GW_COLS = None
_BACKFILL_LOGGED = False


def _gw_cols():
    global _GW_COLS
    if _GW_COLS is None:
        try:
            db = sqlite3.connect(DB_PATH)
            _GW_COLS = {r[1] for r in db.execute("PRAGMA table_info(gw_event)").fetchall()}
            db.close()
        except Exception:
            _GW_COLS = set()
    return _GW_COLS


def _backfill_gw_event(db, gw, cam, ts):
    """Fill the NULL boarded/alighted on the gw_event whose door-open window contains this transit.
    Recomputes from transit_event (idempotent SET). GUARDED: only writes if gw_event has the expected
    wall-time columns + gw_source.camera; otherwise a safe no-op. Never touches door timing columns."""
    global _BACKFILL_LOGGED
    cols = _gw_cols()
    need = {"door_open_start_ts", "door_close_full_ts", "boarded", "alighted", "source_id"}
    if not need.issubset(cols):
        if not _BACKFILL_LOGGED:
            print(f"[analysis_api] gw_event backfill DISABLED: columns {sorted(cols)} lack {sorted(need - cols)} "
                  f"— transits still stored + shown on /ops; paste the schema to finalize."); _BACKFILL_LOGGED = True
        return None
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    try:
        cyc = db.execute(
            "SELECT e.id id, e.door_open_start_ts o, e.door_close_full_ts c FROM gw_event e "
            "JOIN gw_source s ON s.id=e.source_id WHERE s.camera=? "
            "AND e.door_open_start_ts<=? AND e.door_close_full_ts>=? ORDER BY e.id DESC LIMIT 1",
            (cam, iso, iso)).fetchone()
    except sqlite3.OperationalError as e:
        if not _BACKFILL_LOGGED:
            print(f"[analysis_api] gw_event backfill match query failed ({e}) — no-op; paste the schema."); _BACKFILL_LOGGED = True
        return None
    if not cyc:
        return None                                # transit outside any known cycle window (a signal, kept)
    try:
        o_ep = datetime.fromisoformat(cyc["o"]).timestamp()
        c_ep = datetime.fromisoformat(cyc["c"]).timestamp()
    except Exception:
        return None
    b = db.execute("SELECT COUNT(*) FROM transit_event WHERE gateway_id=? AND cam=? AND direction='in' "
                   "AND ts BETWEEN ? AND ?", (gw, cam, o_ep, c_ep)).fetchone()[0]
    a = db.execute("SELECT COUNT(*) FROM transit_event WHERE gateway_id=? AND cam=? AND direction='out' "
                   "AND ts BETWEEN ? AND ?", (gw, cam, o_ep, c_ep)).fetchone()[0]
    db.execute("UPDATE gw_event SET boarded=?, alighted=? WHERE id=?", (b, a, cyc["id"]))
    return (cyc["id"], b, a)


# ---- transit ingest (Bearer, idempotent) ----
@analysis_router.post("/api/gw/{gw}/transit")
async def transit_ingest(gw: str, request: Request, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    d = await request.json()
    cam = str(d.get("cam", ""))
    direction = str(d.get("direction", ""))
    if not _SAFE.match(cam) or direction not in ("in", "out"):
        raise HTTPException(400, "bad transit")
    ts = float(d.get("ts", time.time()))
    db = _db()
    # INSERT OR IGNORE on (gw,cam,track_id,direction, 1s bucket) -> a retry after preemption/network
    # is a no-op; two different people reusing a track_id later fall in a different bucket -> kept.
    db.execute("INSERT OR IGNORE INTO transit_event "
               "(gateway_id,cam,ts,ts_bucket,direction,track_id,received_at) VALUES (?,?,?,?,?,?,?)",
               (gw, cam, ts, int(ts), direction, int(d.get("track_id", -1)), time.time()))
    filled = _backfill_gw_event(db, gw, cam, ts)   # fill NULL boarded/alighted on the matching cycle
    db.commit()
    db.close()
    return {"ok": True, "gw_event": filled}


# ---- recent transits (Bearer) — for /ops / debugging ----
@analysis_router.get("/api/gw/{gw}/transits")
def recent_transits(gw: str, authorization: str = Header(""), n: int = 50):
    _auth_rw(gw, authorization)
    db = _db()
    rows = db.execute("SELECT cam,ts,direction,track_id FROM transit_event WHERE gateway_id=? "
                      "ORDER BY id DESC LIMIT ?", (gw, min(n, 500))).fetchall()
    day0 = time.time() - 86400
    tot = db.execute("SELECT direction, COUNT(*) c FROM transit_event WHERE gateway_id=? AND ts>=? "
                     "GROUP BY direction", (gw, day0)).fetchall()
    db.close()
    counts = {r["direction"]: r["c"] for r in tot}
    return {"recent": [dict(r) for r in rows],
            "today": {"boarded": counts.get("in", 0), "alighted": counts.get("out", 0)}}


def _log_gw_event_schema():
    """Log gw_event's columns once, so we can fill boarded/alighted CORRECTLY later (not on a guess)."""
    try:
        db = sqlite3.connect(DB_PATH)
        cols = [r[1] for r in db.execute("PRAGMA table_info(gw_event)").fetchall()]
        db.close()
        print(f"[analysis_api] gw_event columns: {cols} "
              f"(need the cycle wall-time window to backfill boarded/alighted; not writing gw_event yet)")
    except Exception as e:
        print(f"[analysis_api] could not read gw_event schema: {e}")


_log_gw_event_schema()
