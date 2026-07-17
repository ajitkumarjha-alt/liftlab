"""
Validation UI: a ONE-TIME per-camera gate. In 'validating' state the GPU snaps frame(s) with each
counted door-open episode and posts them here; the operator reviews image + machine count, clicks OK
or corrects it; every verdict is stored (machine, human, who, when); running precision shows on the
page. On GO-LIVE the camera flips to 'live' — no more prompts, no more images captured, counts
auto-store, and the precision is recorded as that camera's PROVENANCE. Reopens only if zones change
/ camera moves / a new camera is added. ADDITIVE.

PRIVACY: the images are residents in a lift. They exist ONLY during validation and are DELETED the
instant a verdict is recorded (or on go-live). The verdict/count persists in the DB (backed up); the
imagery never lingers and is never in the backup (files on disk, not in the DB).

Pi/GPU-facing (Bearer: gateway OR analysis token):
  GET  /api/gw/{gw}/validation_mode/{cam}   -> {"state": "validating"|"live"}  (GPU polls this)
  POST /api/gw/{gw}/validation_item/{cam}   an episode: machine counts + base64 images (validating only)
Operator (basicauth via Caddy):
  GET  /validate                            review page (pending items + precision + go-live)
  GET  /validate/img/{item}/{idx}.jpg       a pending image
  POST /validate/verdict                    {item_id,human_boarded,human_alighted,reviewer} -> store+DELETE imgs
  POST /validate/golive                     {gw,cam,reviewer} -> state=live, record provenance
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
IMG_DIR = Path(os.environ.get("VALIDATION_IMG_DIR", "/var/lib/liftlab/validation_img"))
GATEWAY_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                  for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g}
ANALYSIS_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                   for g in os.environ.get("ANALYSIS_TOKENS", "").split(",") if ":" in g}
MAX_IMGS = int(os.environ.get("VALIDATION_MAX_IMGS", "4"))
MIN_VALIDATE_N = int(os.environ.get("MIN_VALIDATE_N", "20"))   # GO-LIVE refuses below this (load>=1
                                                              # reviews only; a 100%-on-n=1 is meaningless)
# The counting logic verdicts must be valid against. MUST match counting.COUNTING_VERSION on the GPU.
# Only verdicts made against THIS version count toward precision — when the logic changes, prior
# verdicts (a different version) are superseded and validation restarts from n=0.
CURRENT_COUNTING_VERSION = os.environ.get("COUNTING_VERSION", "2026-07-17-dwell-disp")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

validation_router = APIRouter()


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS camera_validation (
      gateway_id TEXT, cam TEXT, state TEXT DEFAULT 'validating',
      n_reviewed INTEGER DEFAULT 0, n_exact INTEGER DEFAULT 0,
      confirmed_at REAL, provenance TEXT, updated_at REAL,
      PRIMARY KEY (gateway_id, cam));
    CREATE TABLE IF NOT EXISTS validation_item (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
      ts_start REAL, ts_end REAL, machine_boarded INTEGER, machine_alighted INTEGER,
      n_images INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
      human_boarded INTEGER, human_alighted INTEGER, reviewer TEXT, reviewed_at REAL, created_at REAL);
    """)
    try:
        db.execute("ALTER TABLE validation_item ADD COLUMN counting_version TEXT")   # logic it was counted under
    except sqlite3.OperationalError:
        pass                                       # already added (old rows -> NULL -> superseded)
    return db


def _auth_rw(gw, authorization):
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if tok and (GATEWAY_TOKENS.get(gw) == tok or ANALYSIS_TOKENS.get(gw) == tok):
        return
    raise HTTPException(401, "bad token")


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _state(db, gw, cam):
    r = db.execute("SELECT state FROM camera_validation WHERE gateway_id=? AND cam=?", (gw, cam)).fetchone()
    return r["state"] if r else "validating"          # a new camera defaults to validating


def _precision(db, gw, cam):
    """n_reviewed / n_exact DERIVED from the reviewed validation_items — the source of truth, never
    an incrementing counter (a counter drifted to 143 on 3 real reviews and wrongly unlocked GO-LIVE).
    A verdict is 'exact' iff the human counts equal the machine counts."""
    r = db.execute(
        "SELECT COUNT(*) nr, COALESCE(SUM(CASE WHEN human_boarded=machine_boarded "
        "AND human_alighted=machine_alighted THEN 1 ELSE 0 END),0) ne "
        "FROM validation_item WHERE gateway_id=? AND cam=? AND status='reviewed' "
        "AND counting_version=?",                  # ONLY verdicts against the CURRENT logic count
        (gw, cam, CURRENT_COUNTING_VERSION)).fetchone()
    return (r["nr"] or 0, r["ne"] or 0)


# ---------------- GPU-facing ----------------
@validation_router.get("/api/gw/{gw}/validation_mode/{cam}")
def validation_mode(gw: str, cam: str, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    _safe(gw, cam)
    db = _db()
    st = _state(db, gw, cam)
    db.close()
    return {"state": st}


@validation_router.post("/api/gw/{gw}/validation_item/{cam}")
async def validation_item(gw: str, cam: str, request: Request, authorization: str = Header("")):
    _auth_rw(gw, authorization)
    _safe(gw, cam)
    db = _db()
    if _state(db, gw, cam) != "validating":
        db.close()
        return {"skip": True, "state": "live"}        # confirmed camera: don't accept images
    d = await request.json()
    cur = db.execute(
        "INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,machine_boarded,machine_alighted,"
        "n_images,counting_version,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (gw, cam, float(d.get("ts_start", 0)), float(d.get("ts_end", 0)),
         int(d.get("machine_boarded", 0)), int(d.get("machine_alighted", 0)), 0,
         d.get("counting_version"), time.time()))
    item_id = cur.lastrowid
    imgs = (d.get("images") or [])[:MAX_IMGS]

    def _write_imgs():                             # base64-decode + write OFF the event loop so the
        dstdir = IMG_DIR / str(item_id)            # image POST can't stall the door-event ingest
        dstdir.mkdir(parents=True, exist_ok=True)
        w = 0
        for i, b64 in enumerate(imgs):
            try:
                (dstdir / f"{i}.jpg").write_bytes(base64.b64decode(b64))
                w += 1
            except Exception:
                pass
        return w
    n = await run_in_threadpool(_write_imgs)
    db.execute("UPDATE validation_item SET n_images=? WHERE id=?", (n, item_id))
    db.commit()
    db.close()
    return {"ok": True, "item_id": item_id, "images": n}


# ---------------- operator: images ----------------
@validation_router.get("/validate/img/{item}/{idx}.jpg")
def validate_img(item: str, idx: str):
    _safe(item, idx)
    f = IMG_DIR / item / f"{idx}.jpg"
    if not f.exists():
        raise HTTPException(404, "image gone (reviewed/deleted)")
    return Response(f.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------- operator: verdict + go-live ----------------
def _provenance(cam, n_reviewed, n_exact):
    pct = round(100.0 * n_exact / n_reviewed) if n_reviewed else 0
    return (f"{cam}: {pct}% exact on n={n_reviewed}, validated {datetime.now(timezone.utc).date().isoformat()} "
            f"(counting {CURRENT_COUNTING_VERSION})")


@validation_router.post("/validate/verdict")
def verdict(item_id: int = Form(...), human_boarded: int = Form(...), human_alighted: int = Form(...),
            reviewer: str = Form("operator")):
    db = _db()
    it = db.execute("SELECT * FROM validation_item WHERE id=? AND status='pending'", (item_id,)).fetchone()
    if not it:
        db.close()
        raise HTTPException(404, "no such pending item")
    exact = 1 if (human_boarded == it["machine_boarded"] and human_alighted == it["machine_alighted"]) else 0
    db.execute("UPDATE validation_item SET status='reviewed',human_boarded=?,human_alighted=?,reviewer=?,"
               "reviewed_at=? WHERE id=?", (human_boarded, human_alighted, reviewer, time.time(), item_id))
    gw, cam = it["gateway_id"], it["cam"]
    # SET n_reviewed/n_exact = derived truth (recompute from reviewed items), never increment -> the
    # column can't drift and unlock the gate on a phantom number. This also self-heals a bad value.
    nr, ne = _precision(db, gw, cam)
    db.execute("INSERT INTO camera_validation (gateway_id,cam,n_reviewed,n_exact,updated_at) "
               "VALUES (?,?,?,?,?) ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "n_reviewed=excluded.n_reviewed, n_exact=excluded.n_exact, updated_at=excluded.updated_at",
               (gw, cam, nr, ne, time.time()))
    db.commit()
    db.close()
    shutil.rmtree(IMG_DIR / str(item_id), ignore_errors=True)   # PRIVACY: imagery gone at the verdict
    return RedirectResponse("/validate", status_code=303)


@validation_router.post("/validate/golive")
def golive(gw: str = Form(...), cam: str = Form(...), reviewer: str = Form("operator"), force: str = Form("")):
    _safe(gw, cam)
    db = _db()
    nr, ne = _precision(db, gw, cam)               # derived truth, not the (formerly-drifting) column
    if nr < MIN_VALIDATE_N and force != "1":
        db.close()
        raise HTTPException(400, f"Only {nr} reviewed (need >= {MIN_VALIDATE_N} with load>=1 before "
                                 f"trusting {cam}). A 100%-on-n=1 provenance is meaningless. Keep reviewing.")
    prov = _provenance(cam, nr, ne)
    db.execute("INSERT INTO camera_validation (gateway_id,cam,state,confirmed_at,provenance,updated_at) "
               "VALUES (?,?,'live',?,?,?) ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "state='live', confirmed_at=excluded.confirmed_at, provenance=excluded.provenance, updated_at=excluded.updated_at",
               (gw, cam, time.time(), prov, time.time()))
    # PRIVACY: drop any remaining pending images for this camera on go-live
    for it in db.execute("SELECT id FROM validation_item WHERE gateway_id=? AND cam=? AND status='pending'", (gw, cam)).fetchall():
        shutil.rmtree(IMG_DIR / str(it["id"]), ignore_errors=True)
    db.commit()
    db.close()
    return RedirectResponse("/validate", status_code=303)


# ---------------- operator: the review page ----------------
@validation_router.get("/validate", response_class=HTMLResponse)
def validate_page():
    db = _db()
    cams = []
    for c in db.execute("SELECT gateway_id,cam,state,provenance FROM camera_validation ORDER BY gateway_id,cam").fetchall():
        nr, ne = _precision(db, c["gateway_id"], c["cam"])   # derived truth
        cams.append({**dict(c), "n_reviewed": nr, "n_exact": ne})
    # NEWEST first so the multi-frame episodes surface (old single-image overnight ones were burying
    # them past the 50-item window, so every reviewable item looked like it had one image).
    pend = db.execute("SELECT * FROM validation_item WHERE status='pending' ORDER BY id DESC LIMIT 50").fetchall()
    db.close()
    return HTMLResponse(_render(cams, pend))


def _render(cams, pend):
    css = """<style>body{margin:0;background:#f6f8fa;color:#1c2429;font:14px/1.5 system-ui,sans-serif}
    header{padding:12px 18px;border-bottom:1px solid #e3e8ec;background:#fff}h1{font-size:16px;margin:0}
    .wrap{max-width:1000px;margin:0 auto;padding:16px}h2{font-size:13px;text-transform:uppercase;color:#6b7a84;letter-spacing:.05em;margin:18px 4px 8px}
    .cam{display:flex;gap:12px;align-items:center;background:#fff;border:1px solid #e3e8ec;border-radius:8px;padding:8px 12px;margin:6px 0;font:12px ui-monospace,monospace}
    .badge{padding:1px 8px;border-radius:10px;font-size:11px}.validating{background:#fef3e0;color:#d98a1f}.live{background:#e6f4ec;color:#2f9e5f}
    .item{background:#fff;border:1px solid #e3e8ec;border-radius:8px;padding:12px;margin:10px 0}
    .imgs{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.imgs img{height:150px;border-radius:4px;border:1px solid #ddd}
    .mc{font:13px ui-monospace,monospace;margin:4px 0}form{display:inline}
    input[type=number]{width:48px}button{font:13px system-ui;padding:4px 12px;border-radius:6px;border:1px solid #cbd5db;background:#fff;cursor:pointer}
    button.ok{background:#2f9e5f;color:#fff;border-color:#2f9e5f}button.go{background:#4a9eda;color:#fff;border-color:#4a9eda}
    button:disabled{opacity:.45;cursor:not-allowed}
    @media(prefers-color-scheme:dark){body{background:#0e1418;color:#d6dee3}header,.cam,.item{background:#161d22;border-color:#243038}}</style>"""
    cam_rows = ""
    for c in cams:
        nr = c["n_reviewed"]
        pct = round(100.0 * c["n_exact"] / nr) if nr else 0
        if c["state"] == "live":
            gl = ""
        elif nr < MIN_VALIDATE_N:
            gl = f'<button class=go disabled title="need {MIN_VALIDATE_N}">GO-LIVE ({nr}/{MIN_VALIDATE_N})</button>'
        else:
            gl = (f'<form method=post action=/validate/golive><input type=hidden name=gw value="{c["gateway_id"]}">'
                  f'<input type=hidden name=cam value="{c["cam"]}"><button class=go '
                  f'onclick="return confirm(\'GO-LIVE {c["cam"]} at {pct}% on n={nr}? No more prompts/images after this.\')">'
                  f'GO-LIVE ({nr}/{MIN_VALIDATE_N})</button></form>')
        prov = c["provenance"] or (f'{nr}/{MIN_VALIDATE_N} reviewed, {pct}% exact' if nr else "not reviewed yet")
        cam_rows += (f'<div class=cam><b>{c["cam"]}</b><span class="badge {c["state"]}">{c["state"]}</span>'
                     f'<span style="flex:1">{prov}</span>{gl}</div>')
    items = ""
    for it in pend:
        imgs = "".join(f'<img src="/validate/img/{it["id"]}/{i}.jpg">' for i in range(it["n_images"]))
        items += (f'<div class=item><div class=mc>{it["cam"]} · machine: <b>boarded {it["machine_boarded"]}'
                  f' / alighted {it["machine_alighted"]}</b></div><div class=imgs>{imgs or "(no images)"}</div>'
                  f'<form method=post action=/validate/verdict>'
                  f'<input type=hidden name=item_id value="{it["id"]}">'
                  f'boarded <input type=number name=human_boarded value="{it["machine_boarded"]}"> '
                  f'alighted <input type=number name=human_alighted value="{it["machine_alighted"]}"> '
                  f'<button class=ok type=submit>OK / submit</button></form></div>')
    return (f"<!doctype html><meta charset=utf-8><title>validate</title>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>{css}"
            f"<header><h1>liftlab · validation</h1></header><div class=wrap>"
            f"<h2>Cameras</h2>{cam_rows or '<p>no cameras yet</p>'}"
            f"<h2>Pending review ({len(pend)})</h2>{items or '<p>nothing to review — machine is caught up.</p>'}"
            f"</div>")


# ---------------- for /ops: per-camera validation summary ----------------
def validation_summary(db, gw):
    try:
        rows = db.execute("SELECT cam,state,n_reviewed,n_exact,provenance FROM camera_validation WHERE gateway_id=?", (gw,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
