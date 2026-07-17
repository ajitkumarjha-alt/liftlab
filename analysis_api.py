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


BACKFILL_OPEN_PAD = float(os.environ.get("BACKFILL_OPEN_PAD_S", "5"))    # clock skew slack, open side
BACKFILL_CLOSE_PAD = float(os.environ.get("BACKFILL_CLOSE_PAD_S", "25"))  # GPU processing lag, close side


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS transit_event (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL, ts_bucket INTEGER,
      direction TEXT, track_id INTEGER, received_at REAL)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_transit "
               "ON transit_event (gateway_id,cam,track_id,direction,ts_bucket)")
    try:
        db.execute("ALTER TABLE transit_event ADD COLUMN cycle_id INTEGER")   # gw_event.id it belongs to
    except sqlite3.OperationalError:
        pass                                       # already added
    db.execute("""CREATE TABLE IF NOT EXISTS analyzer_status (
      gateway_id TEXT, cam TEXT, ts REAL, counting_version TEXT, uptime_s REAL,
      segments INTEGER, dropped INTEGER, posted INTEGER, last_transit_ts REAL, mode TEXT,
      rej_disp INTEGER, rej_dwell INTEGER, rej_hist TEXT,
      rej_in INTEGER, rej_out INTEGER, proc_ms REAL, track_ms REAL, seg_budget_ms REAL,
      PRIMARY KEY (gateway_id, cam))""")
    for col, typ in (("rej_disp", "INTEGER"), ("rej_dwell", "INTEGER"), ("rej_hist", "TEXT"),
                     ("rej_in", "INTEGER"), ("rej_out", "INTEGER"),
                     ("proc_ms", "REAL"), ("track_ms", "REAL"), ("seg_budget_ms", "REAL"),
                     ("fetch_ms", "REAL"), ("decode_ms", "REAL"),
                     ("drop_frac", "REAL"), ("drop_rate_hr", "REAL")):
        try:
            db.execute(f"ALTER TABLE analyzer_status ADD COLUMN {col} {typ}")   # migrate pre-existing table
        except sqlite3.OperationalError:
            pass                                                                  # column already present
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


def _cam_forms(cam):
    """gw_source.camera might be 'ch29' or '29' — match either."""
    bare = cam[2:] if cam.startswith("ch") else cam
    return list({cam, bare, "ch" + bare})


def _match_cycle(db, cam, ts):
    """The gw_event.id whose door-open window contains this transit's wall time. Compares as EPOCHS
    (parsing the tz-aware ISO offset) — NOT strings: door_open_start_ts is local-offset ISO, a string
    compare against a UTC ISO is meaningless. Pads the close side for the GPU's processing lag."""
    forms = _cam_forms(cam)
    qmarks = ",".join("?" * len(forms))
    try:
        rows = db.execute(
            f"SELECT e.id id, e.door_open_start_ts o, e.door_close_full_ts c FROM gw_event e "
            f"JOIN gw_source s ON s.id=e.source_id WHERE s.camera IN ({qmarks}) "
            f"ORDER BY e.id DESC LIMIT 300", forms).fetchall()
    except sqlite3.OperationalError:
        return None
    for r in rows:
        try:
            o = datetime.fromisoformat(r["o"]).timestamp()
            c = datetime.fromisoformat(r["c"]).timestamp()
        except Exception:
            continue
        if o - BACKFILL_OPEN_PAD <= ts <= c + BACKFILL_CLOSE_PAD:
            return r["id"]
    return None


def _backfill_gw_event(db, gw, cam, ts, track_id, direction, ts_bucket):
    """Attribute this transit to its door cycle and recompute that gw_event's boarded/alighted from
    ALL transits attributed to it (idempotent SET). GUARDED: only writes if gw_event has the expected
    columns; else a safe no-op. Never touches the door-timing columns or the ingest."""
    global _BACKFILL_LOGGED
    cols = _gw_cols()
    need = {"door_open_start_ts", "door_close_full_ts", "boarded", "alighted", "source_id"}
    if not need.issubset(cols):
        if not _BACKFILL_LOGGED:
            print(f"[analysis_api] backfill DISABLED: gw_event lacks {sorted(need - cols)}"); _BACKFILL_LOGGED = True
        return None
    cid = _match_cycle(db, cam, ts)
    if not _BACKFILL_LOGGED:
        print(f"[analysis_api] backfill active: transit cam={cam} ts={ts:.0f} -> cycle_id={cid} "
              f"(epoch match, pads open={BACKFILL_OPEN_PAD}s close={BACKFILL_CLOSE_PAD}s)"); _BACKFILL_LOGGED = True
    if cid is None:
        return None                                # no cycle window contains it (a signal, kept)
    db.execute("UPDATE transit_event SET cycle_id=? WHERE gateway_id=? AND cam=? AND track_id=? "
               "AND direction=? AND ts_bucket=?", (cid, gw, cam, track_id, direction, ts_bucket))
    b = db.execute("SELECT COUNT(*) FROM transit_event WHERE cycle_id=? AND direction='in'", (cid,)).fetchone()[0]
    a = db.execute("SELECT COUNT(*) FROM transit_event WHERE cycle_id=? AND direction='out'", (cid,)).fetchone()[0]
    db.execute("UPDATE gw_event SET boarded=?, alighted=? WHERE id=?", (b, a, cid))
    return (cid, b, a)


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
    track_id = int(d.get("track_id", -1))
    ts_bucket = int(ts)
    db = _db()
    # INSERT OR IGNORE on (gw,cam,track_id,direction, 1s bucket) -> a retry after preemption/network
    # is a no-op; two different people reusing a track_id later fall in a different bucket -> kept.
    db.execute("INSERT OR IGNORE INTO transit_event "
               "(gateway_id,cam,ts,ts_bucket,direction,track_id,received_at) VALUES (?,?,?,?,?,?,?)",
               (gw, cam, ts, ts_bucket, direction, track_id, time.time()))
    filled = _backfill_gw_event(db, gw, cam, ts, track_id, direction, ts_bucket)   # attribute + fill cycle
    db.commit()
    db.close()
    return {"ok": True, "gw_event": filled}


# ---- GPU analyzer heartbeat (Bearer) — so a DEAD worker is visible on /ops, not silent ----
@analysis_router.post("/api/gw/{gw}/analyzer_status")
async def analyzer_status_ingest(gw: str, request: Request, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    d = await request.json()
    db = _db()
    db.execute(
        "INSERT INTO analyzer_status (gateway_id,cam,ts,counting_version,uptime_s,segments,dropped,"
        "posted,last_transit_ts,mode,rej_disp,rej_dwell,rej_hist,rej_in,rej_out,proc_ms,track_ms,seg_budget_ms,"
        "fetch_ms,decode_ms,drop_frac,drop_rate_hr) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(gateway_id,cam) DO UPDATE SET ts=excluded.ts, counting_version=excluded.counting_version,"
        "uptime_s=excluded.uptime_s, segments=excluded.segments, dropped=excluded.dropped, "
        "posted=excluded.posted, last_transit_ts=excluded.last_transit_ts, mode=excluded.mode, "
        "rej_disp=excluded.rej_disp, rej_dwell=excluded.rej_dwell, rej_hist=excluded.rej_hist, "
        "rej_in=excluded.rej_in, rej_out=excluded.rej_out, proc_ms=excluded.proc_ms, "
        "track_ms=excluded.track_ms, seg_budget_ms=excluded.seg_budget_ms, "
        "fetch_ms=excluded.fetch_ms, decode_ms=excluded.decode_ms, "
        "drop_frac=excluded.drop_frac, drop_rate_hr=excluded.drop_rate_hr",
        (gw, str(d.get("cam", "")), time.time(), d.get("counting_version"), d.get("uptime_s"),
         d.get("segments"), d.get("dropped"), d.get("posted"), d.get("last_transit_ts"), d.get("mode"),
         d.get("rej_disp"), d.get("rej_dwell"), d.get("rej_hist"),
         d.get("rej_in"), d.get("rej_out"), d.get("proc_ms"), d.get("track_ms"), d.get("seg_budget_ms"),
         d.get("fetch_ms"), d.get("decode_ms"), d.get("drop_frac"), d.get("drop_rate_hr")))
    db.commit()
    db.close()
    return {"ok": True}


# ---- one-shot: attribute already-stored transits to cycles + fill gw_event (run once after deploy) ----
@analysis_router.post("/api/gw/{gw}/backfill_transits")
def backfill_transits(gw: str, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    db = _db()
    rows = db.execute("SELECT cam,ts,track_id,direction,ts_bucket FROM transit_event "
                      "WHERE gateway_id=? AND cycle_id IS NULL", (gw,)).fetchall()
    matched = 0
    for r in rows:
        if _backfill_gw_event(db, gw, r["cam"], r["ts"], r["track_id"], r["direction"], r["ts_bucket"]):
            matched += 1
    db.commit()
    db.close()
    return {"processed": len(rows), "matched": matched}


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
        print(f"[analysis_api] gw_event columns: {cols} — backfill matches transits to the "
              f"door_open_start_ts..door_close_full_ts window by EPOCH (tz-aware) and fills boarded/alighted")
    except Exception as e:
        print(f"[analysis_api] could not read gw_event schema: {e}")


_log_gw_event_schema()
