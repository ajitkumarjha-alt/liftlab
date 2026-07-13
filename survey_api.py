"""
Survey + channel-map feature for liftlab cloud (commissioning). ADDITIVE.

Pi-facing (Bearer, under /api/gw/*):
  POST /api/gw/{gw}/survey/{ch}        upload a snapshot (raw JPEG body);
                                       ?has_zones=0|1 &mode=live|playback,
                                       or ?error=... to mark a per-channel failure.
Operator (basicauth via Caddy, NOT under /api/gw/*):
  POST /survey/{gw}/start              queue a survey job, state -> survey_needed
  GET  /survey/{gw}                    marking page (thumbnails + is_lift + label)
  GET  /survey/{gw}/img/{fn}           serve a snapshot
  POST /survey/{gw}/save               write channel_map, state -> channels_marked
  POST /survey/{gw}/delete-images      wipe snapshots (commissioning aid)

Snapshots are FILES under DATA_DIR/survey/{gw}/ch{NN}.jpg — behind basicauth,
deletable, never public, never committed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

DATA_DIR = Path(os.environ.get("LIFTLAB_DATA", "./data"))
DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
SURVEY_DIR = DATA_DIR / "survey"
GATEWAY_TOKENS = {
    g.split(":", 1)[0]: g.split(":", 1)[1]
    for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g
}


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS channel_map (
      gateway_id TEXT, channel INTEGER, is_lift INTEGER DEFAULT 0,
      label TEXT, marked_at REAL, PRIMARY KEY (gateway_id, channel));
    CREATE TABLE IF NOT EXISTS survey_channel (
      gateway_id TEXT, channel INTEGER, has_image INTEGER DEFAULT 0,
      has_zones INTEGER DEFAULT 0, error TEXT, surveyed_at REAL,
      PRIMARY KEY (gateway_id, channel));
    """)
    # guarded state column on the pre-existing gateway table (idempotent)
    cols = [r[1] for r in db.execute("PRAGMA table_info(gateway)")]
    if cols and "state" not in cols:
        db.execute("ALTER TABLE gateway ADD COLUMN state TEXT DEFAULT 'installed'")
    db.commit()
    return db


survey_router = APIRouter()


def _auth(gw: str, authorization: str) -> None:
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok or GATEWAY_TOKENS.get(gw) != tok:
        raise HTTPException(401, "bad gateway token")


def _set_state(db, gw: str, state: str) -> None:
    db.execute("INSERT INTO gateway (id, state) VALUES (?,?) "
               "ON CONFLICT(id) DO UPDATE SET state=excluded.state", (gw, state))


# ---------------- Pi-facing: snapshot upload (Bearer) ----------------
@survey_router.post("/api/gw/{gw}/survey/{ch}")
async def survey_upload(gw: str, ch: int, request: Request,
                        has_zones: int = 0, mode: str = "", error: str = "",
                        authorization: str = Header(default="")):
    _auth(gw, authorization)
    db = _db()
    if error:
        db.execute(
            "INSERT INTO survey_channel (gateway_id,channel,has_image,has_zones,error,surveyed_at)"
            " VALUES (?,?,0,?,?,?) ON CONFLICT(gateway_id,channel) DO UPDATE SET"
            " has_image=0, error=excluded.error, surveyed_at=excluded.surveyed_at",
            (gw, ch, has_zones, error[:200], time.time()))
        db.commit(); db.close()
        return {"ok": True, "marked": "error"}
    body = await request.body()
    if len(body) < 500:
        db.close(); raise HTTPException(400, "empty image")
    d = SURVEY_DIR / gw
    d.mkdir(parents=True, exist_ok=True)
    (d / f"ch{ch:02d}.jpg").write_bytes(body)
    db.execute(
        "INSERT INTO survey_channel (gateway_id,channel,has_image,has_zones,error,surveyed_at)"
        " VALUES (?,?,1,?,NULL,?) ON CONFLICT(gateway_id,channel) DO UPDATE SET"
        " has_image=1, has_zones=excluded.has_zones, error=NULL, surveyed_at=excluded.surveyed_at",
        (gw, ch, has_zones, time.time()))
    db.commit(); db.close()
    return {"ok": True, "bytes": len(body), "mode": mode}


# ---------------- Operator: start survey ----------------
@survey_router.post("/survey/{gw}/start")
def survey_start(gw: str, c_from: int = 1, c_to: int = 40):
    db = _db()
    jid = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO job (id,gateway_id,type,params,created_at,updated_at)"
               " VALUES (?,?,?,?,?,?)",
               (jid, gw, "survey", json.dumps({"from": c_from, "to": c_to}),
                time.time(), time.time()))
    _set_state(db, gw, "survey_needed")
    db.commit(); db.close()
    return {"job_id": jid, "state": "survey_needed"}


# ---------------- Operator: serve a snapshot ----------------
@survey_router.get("/survey/{gw}/img/{fn}")
def survey_img(gw: str, fn: str):
    if not re.fullmatch(r"ch\d{2}\.jpg", fn):
        raise HTTPException(404, "not found")
    p = SURVEY_DIR / gw / fn
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p), media_type="image/jpeg")


# ---------------- Operator: save marking ----------------
@survey_router.post("/survey/{gw}/save")
async def survey_save(gw: str, request: Request):
    data = await request.json()
    db = _db()
    now = time.time()
    for row in data.get("channels", []):
        db.execute(
            "INSERT INTO channel_map (gateway_id,channel,is_lift,label,marked_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(gateway_id,channel) DO UPDATE SET"
            " is_lift=excluded.is_lift, label=excluded.label, marked_at=excluded.marked_at",
            (gw, int(row["channel"]), 1 if row.get("is_lift") else 0,
             (row.get("label") or "").strip()[:64], now))
    _set_state(db, gw, "channels_marked")
    db.commit(); db.close()
    return {"ok": True, "state": "channels_marked"}


# ---------------- Operator: delete snapshots ----------------
@survey_router.post("/survey/{gw}/delete-images")
def survey_delete(gw: str):
    shutil.rmtree(SURVEY_DIR / gw, ignore_errors=True)
    db = _db()
    db.execute("UPDATE survey_channel SET has_image=0 WHERE gateway_id=?", (gw,))
    db.commit(); db.close()
    return {"ok": True}


# ---------------- Operator: the marking page ----------------
@survey_router.get("/survey/{gw}", response_class=HTMLResponse)
def survey_page(gw: str):
    db = _db()
    sc = {r["channel"]: dict(r) for r in db.execute(
        "SELECT * FROM survey_channel WHERE gateway_id=? ORDER BY channel", (gw,))}
    cm = {r["channel"]: dict(r) for r in db.execute(
        "SELECT * FROM channel_map WHERE gateway_id=?", (gw,))}
    st = db.execute("SELECT state FROM gateway WHERE id=?", (gw,)).fetchone()
    db.close()
    state = (st["state"] if st and st["state"] else "installed")
    channels = sorted(set(sc) | set(cm))

    tiles = []
    for ch in channels:
        s, m = sc.get(ch, {}), cm.get(ch, {})
        if s.get("has_image"):
            img = (f'<img src="/survey/{gw}/img/ch{ch:02d}.jpg" loading="lazy">')
        else:
            img = f'<div class="noimg">{(s.get("error") or "no image")[:44]}</div>'
        lift = "checked" if m.get("is_lift") else ""
        label = (m.get("label") or "").replace('"', "&quot;")
        if m.get("is_lift") and not s.get("has_zones"):
            zone = '<span class="nozone" title="no door_roi calibrated">no zones</span>'
        elif s.get("has_zones"):
            zone = '<span class="zoned">zoned</span>'
        else:
            zone = ""
        tiles.append(f"""<div class="tile" data-ch="{ch}">{img}
          <div class="cap"><b>ch{ch:02d}</b> {zone}</div>
          <label class="lift"><input type="checkbox" class="islift" {lift}> lift camera</label>
          <input class="lbl" placeholder="label e.g. PL-1A" value="{label}"></div>""")

    body = "".join(tiles) or ('<p class="muted">No survey yet. Run a channel survey '
                              'from the fleet page.</p>')
    return f"""<!doctype html><meta charset=utf-8><title>survey · {gw}</title>
    <style>:root{{--mono:ui-monospace,Consolas,monospace}}
    body{{background:#0e1417;color:#dbe3e6;font:14px system-ui;max-width:1100px;margin:auto;padding:18px}}
    h1{{font-size:14px;letter-spacing:.15em;text-transform:uppercase;color:#e3a53f}}
    .state{{font-family:var(--mono);font-size:11px;color:#7a8b93;margin-left:8px;text-transform:none;letter-spacing:0}}
    .muted{{color:#7a8b93}} a{{color:#63a37e}}
    .bar{{display:flex;gap:10px;align-items:center;margin:12px 0}}
    button{{background:#1b252b;color:#dbe3e6;border:1px solid #26333a;border-radius:5px;padding:7px 12px;cursor:pointer}}
    button.danger{{border-color:#d0574d;color:#d0574d}}
    #grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}}
    .tile{{background:#151d22;border:1px solid #26333a;border-radius:6px;overflow:hidden;padding-bottom:6px}}
    .tile img{{width:100%;height:120px;object-fit:cover;background:#0e1417;display:block}}
    .noimg{{height:120px;display:flex;align-items:center;justify-content:center;color:#d0574d;font-size:11px;text-align:center;padding:6px}}
    .cap{{font-family:var(--mono);font-size:12px;padding:5px 8px 2px}}
    .lift{{display:block;font-size:12px;padding:2px 8px;color:#dbe3e6}}
    .lbl{{width:calc(100% - 16px);margin:4px 8px;background:#0e1417;color:#dbe3e6;border:1px solid #26333a;border-radius:4px;padding:5px;font-family:var(--mono);font-size:12px}}
    .nozone{{color:#e3a53f;font-size:10px}} .zoned{{color:#63a37e;font-size:10px}}</style>
    <h1>survey · {gw} <span class=state>{state.replace('_',' ')}</span></h1>
    <p class=muted>Commissioning aids — snapshots are behind auth; delete after marking.</p>
    <div class=bar>
      <button onclick="save()">Save marking</button>
      <button class=danger onclick="if(confirm('Delete all survey snapshots for {gw}?'))del_imgs()">Delete survey images</button>
      <a href="/">&larr; fleet</a>
    </div>
    <div id=grid>{body}</div>
    <script>
    async function save(){{
      const channels=[...document.querySelectorAll('.tile')].map(t=>({{
        channel:+t.dataset.ch, is_lift:t.querySelector('.islift').checked,
        label:t.querySelector('.lbl').value}}));
      await fetch('/survey/{gw}/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{channels}})}});
      location.reload();
    }}
    async function del_imgs(){{ await fetch('/survey/{gw}/delete-images',{{method:'POST'}}); location.reload(); }}
    </script>"""


# Run the migration at import so fleet_status (SELECT *) sees the state column.
try:
    _db().close()
except Exception:
    pass
