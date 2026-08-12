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

import nav_common as nc
from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
# For the nav bar only: /ops is gateway-scoped and this page is not, so it needs a default.
DASH_GW = os.environ.get("DASH_GW", "site-A")


def _default_cam(gw):
    """A camera for the header's Floorcheck link — this page is gateway-scoped but the operator
    still needs one click there. First lift channel; None omits the entry rather than faking it."""
    try:
        db = _db()
        r = db.execute("SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1 "
                       "ORDER BY channel LIMIT 1", (gw,)).fetchone()
        db.close()
        return f"ch{r[0]}" if r else None
    except Exception:
        return None
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
CURRENT_COUNTING_VERSION = os.environ.get("COUNTING_VERSION", "2026-07-17-yolo11m-dwell-disp")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

validation_router = APIRouter()


def _i(v):
    """int or NULL. A MISSING occupancy field must stay NULL, never become 0: a worker that predates
    the feature reported nothing, and 0 would read downstream as "an empty cabin was measured"."""
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


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
    # PEAK CAR OCCUPANCY (2026-08-12). Additive columns; nothing existing changes meaning, so no era
    # boundary — same precedent as floor_age_s. occupancy_frames is the evidence denominator and
    # occupancy_degraded marks a peak taken while the worker was dropping >20% of segments.
    for _col, _typ in (("occupancy_max", "INTEGER"), ("occupancy_frames", "INTEGER"),
                       ("occupancy_degraded", "INTEGER"), ("analysed_frames", "INTEGER"),
                       ("human_occupancy", "INTEGER")):
        try:
            db.execute(f"ALTER TABLE validation_item ADD COLUMN {_col} {_typ}")
        except sqlite3.OperationalError:
            pass                                  # already there
    for col, typ in (("counting_version", "TEXT"),         # logic it was counted under
                     ("det_max", "INTEGER"), ("det_mean", "REAL"),   # detection audit: what YOLO+tracker saw
                     ("distinct_ids", "INTEGER"), ("det_frames", "INTEGER"),
                     ("conf_min", "REAL"), ("conf_mean", "REAL"), ("conf_max", "REAL")):
        try:
            db.execute(f"ALTER TABLE validation_item ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass                                   # already added (old rows -> NULL)
    try:
        # the counting version the camera went LIVE under. A bump means a live camera is trusting a
        # counter that no longer runs -> _state() reopens it. Pre-migration live rows -> NULL -> reopen.
        db.execute("ALTER TABLE camera_validation ADD COLUMN counting_version TEXT")
    except sqlite3.OperationalError:
        pass
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


def _reopen_if_stale(db, gw, cam):
    """A COUNTING_VERSION change is a comparability boundary: a camera left 'live' would auto-store counts
    from an UNVALIDATED counter while showing provenance earned by one that no longer runs — and it can't
    self-correct (live => GPU stops capturing => 0 episodes => nothing to validate). So when the version it
    went live under != the current one, flip it back to 'validating' and CLEAR the stale provenance/precision.
    This is the state half of version-stamping (resetting the verdicts alone left the CAMERA STATE stale).
    Returns the effective state."""
    r = db.execute("SELECT state, counting_version FROM camera_validation WHERE gateway_id=? AND cam=?",
                   (gw, cam)).fetchone()
    if not r:
        return "validating"                           # a new camera defaults to validating
    if r["state"] == "live" and (r["counting_version"] or "") != CURRENT_COUNTING_VERSION:
        db.execute("UPDATE camera_validation SET state='validating', provenance=NULL, confirmed_at=NULL, "
                   "n_reviewed=0, n_exact=0, updated_at=? WHERE gateway_id=? AND cam=?",
                   (time.time(), gw, cam))
        db.commit()
        return "validating"
    return r["state"]


def _state(db, gw, cam):
    return _reopen_if_stale(db, gw, cam)


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
    d = await request.json()
    if _state(db, gw, cam) != "validating":
        # LIVE (validated under the CURRENT logic): the counts are trusted, so store an 'auto' record —
        # counted, NO imagery, provenance is the camera's recorded precision — and NEVER a pending review
        # item. The transits themselves are already in transit_event/gw_event; this is the door-open audit.
        db.execute(
            "INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,machine_boarded,machine_alighted,"
            "n_images,counting_version,status,occupancy_max,occupancy_frames,occupancy_degraded,"
            "analysed_frames,created_at) VALUES (?,?,?,?,?,?,0,?,'auto',?,?,?,?,?)",
            (gw, cam, float(d.get("ts_start", 0)), float(d.get("ts_end", 0)),
             int(d.get("machine_boarded", 0)), int(d.get("machine_alighted", 0)),
             d.get("counting_version"), _i(d.get("occupancy_max")), _i(d.get("occupancy_frames")),
             _i(d.get("occupancy_degraded")), _i(d.get("analysed_frames")), time.time()))
        db.commit()
        db.close()
        return {"stored": "auto", "state": "live"}
    cur = db.execute(
        "INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,machine_boarded,machine_alighted,"
        "n_images,counting_version,det_max,det_mean,distinct_ids,det_frames,conf_min,conf_mean,conf_max,"
        "occupancy_max,occupancy_frames,occupancy_degraded,analysed_frames,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gw, cam, float(d.get("ts_start", 0)), float(d.get("ts_end", 0)),
         int(d.get("machine_boarded", 0)), int(d.get("machine_alighted", 0)), 0,
         d.get("counting_version"), d.get("det_max"), d.get("det_mean"),
         d.get("distinct_ids"), d.get("det_frames"),
         d.get("conf_min"), d.get("conf_mean"), d.get("conf_max"),
         _i(d.get("occupancy_max")), _i(d.get("occupancy_frames")),
         _i(d.get("occupancy_degraded")), _i(d.get("analysed_frames")), time.time()))
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
    db.execute("INSERT INTO camera_validation (gateway_id,cam,state,confirmed_at,provenance,counting_version,updated_at) "
               "VALUES (?,?,'live',?,?,?,?) ON CONFLICT(gateway_id,cam) DO UPDATE SET "
               "state='live', confirmed_at=excluded.confirmed_at, provenance=excluded.provenance, "
               "counting_version=excluded.counting_version, updated_at=excluded.updated_at",
               (gw, cam, time.time(), prov, CURRENT_COUNTING_VERSION, time.time()))
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
    # reopen any camera left 'live' under a superseded counting version BEFORE rendering, so the operator
    # sees it flip to validating on load (not only after the next GPU poll hits _state).
    for c in db.execute("SELECT gateway_id,cam FROM camera_validation").fetchall():
        _reopen_if_stale(db, c["gateway_id"], c["cam"])
    cams = []
    for c in db.execute("SELECT gateway_id,cam,state,provenance FROM camera_validation ORDER BY gateway_id,cam").fetchall():
        nr, ne = _precision(db, c["gateway_id"], c["cam"])   # derived truth
        cams.append({**dict(c), "n_reviewed": nr, "n_exact": ne})
    # PURGE superseded pending: an episode from a DIFFERENT counting version can't be judged for the
    # current logic and can't count -> presenting it wastes clicks. Mark superseded + delete its
    # resident images (privacy), so it never appears for review again.
    stale = db.execute("SELECT id FROM validation_item WHERE status='pending' AND "
                       "(counting_version IS NULL OR counting_version!=?)", (CURRENT_COUNTING_VERSION,)).fetchall()
    for r in stale:
        shutil.rmtree(IMG_DIR / str(r["id"]), ignore_errors=True)
    if stale:
        db.execute("UPDATE validation_item SET status='superseded' WHERE status='pending' AND "
                   "(counting_version IS NULL OR counting_version!=?)", (CURRENT_COUNTING_VERSION,))
        db.commit()
    # ORPHAN IMAGERY SWEEP (2026-07-29). The purge above only sees version-superseded PENDING rows;
    # an out-of-band supersede (the ch37 zone-edit ran as direct SQL, bypassing every API path) left
    # superseded items' imagery resident on disk. The privacy invariant is: imagery exists ONLY for
    # pending items. Enforce it structurally on every render — any path that forgets to delete
    # (manual SQL, new tools, a crash between UPDATE and rmtree) self-heals here instead of
    # persisting residents' faces outside the deletion path.
    try:
        pending_ids = {str(r["id"]) for r in db.execute(
            "SELECT id FROM validation_item WHERE status='pending'").fetchall()}
        if IMG_DIR.exists():
            for d in IMG_DIR.iterdir():
                if d.is_dir() and d.name not in pending_ids:
                    shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    # LIVE cameras must NOT have pending review items — they were counted by the validated logic. Any
    # pending left over from before go-live (or a race) -> 'auto' + drop images (privacy). Self-heals the
    # queue: a go-live purges its camera's pending here on the next load.
    live_pending = db.execute(
        "SELECT vi.id id FROM validation_item vi JOIN camera_validation cv "
        "ON cv.gateway_id=vi.gateway_id AND cv.cam=vi.cam "
        "WHERE vi.status='pending' AND cv.state='live' AND cv.counting_version=? AND vi.counting_version=?",
        (CURRENT_COUNTING_VERSION, CURRENT_COUNTING_VERSION)).fetchall()
    if live_pending:
        ids = [r["id"] for r in live_pending]
        for i in ids:
            shutil.rmtree(IMG_DIR / str(i), ignore_errors=True)
        db.execute("UPDATE validation_item SET status='auto' WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        db.commit()
    # AUTO-EXPIRE pending episodes whose IMAGERY IS GONE — un-reviewable by construction, so never show a
    # broken <img>. Images are deleted post-verdict/at go-live (privacy); a re-queue, an upload failure,
    # or a tmpfs clear can leave a pending row pointing at files that no longer exist. Scope, not serving:
    # an episode is only reviewable if its snapshot files are actually on disk RIGHT NOW.
    missing = [r["id"] for r in db.execute(
        "SELECT id FROM validation_item WHERE status='pending' AND counting_version=?",
        (CURRENT_COUNTING_VERSION,)).fetchall()
        if not (IMG_DIR / str(r["id"])).is_dir() or not any((IMG_DIR / str(r["id"])).glob("*.jpg"))]
    if missing:
        db.execute("UPDATE validation_item SET status='no_imagery' WHERE id IN (%s)"
                   % ",".join("?" * len(missing)), missing)
        db.commit()
    # only CURRENT-version episodes WITH imagery on disk are reviewable; newest first
    pend = db.execute("SELECT * FROM validation_item WHERE status='pending' AND counting_version=? "
                      "ORDER BY id DESC LIMIT 50", (CURRENT_COUNTING_VERSION,)).fetchall()
    npend = db.execute("SELECT COUNT(*) FROM validation_item WHERE status='pending' AND counting_version=?",
                       (CURRENT_COUNTING_VERSION,)).fetchone()[0]
    nsup = len(stale)
    db.close()
    return HTMLResponse(_render(cams, pend, npend, nsup, len(missing)))


def _render(cams, pend, npend=0, nsup=0, nmiss=0):
    css = "<style>" + nc.NAV_CSS + """body{margin:0;background:#f6f8fa;color:#1c2429;font:14px/1.5 system-ui,sans-serif}
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
        dm = it["det_max"]
        da = ""                                        # detection audit: what YOLO+tracker saw vs the image
        if dm is not None:
            conf = ""
            if it["conf_mean"] is not None:
                conf = (f' · conf {it["conf_min"]:.2f}/{it["conf_mean"]:.2f}/{it["conf_max"]:.2f} '
                        f'(min/mean/max)')
            da = (f'<div class=da style="font-size:12px;opacity:.8">YOLO saw: max <b>{dm}</b> person(s)/frame · '
                  f'<b>{it["distinct_ids"]}</b> distinct track(s) over {it["det_frames"] or 0} frames{conf} — '
                  f'if the image shows more people than this, they were never detected</div>')
        items += (f'<div class=item><div class=mc>{it["cam"]} · machine: <b>boarded {it["machine_boarded"]}'
                  f' / alighted {it["machine_alighted"]}</b></div>{da}<div class=imgs>{imgs or "(no images)"}</div>'
                  f'<form method=post action=/validate/verdict>'
                  f'<input type=hidden name=item_id value="{it["id"]}">'
                  f'boarded <input type=number name=human_boarded value="{it["machine_boarded"]}"> '
                  f'alighted <input type=number name=human_alighted value="{it["machine_alighted"]}"> '
                  f'<button class=ok type=submit>OK / submit</button></form></div>')
    empty = (f"<p>nothing to review under counting <b>{CURRENT_COUNTING_VERSION}</b> yet — "
             f"episodes arrive per door-open from the redeployed GPU. Wait for the queue to fill; "
             f"don't click into the void.</p>")
    sup_note = f" · {nsup} superseded (older logic) purged this load" if nsup else ""
    sup_note += f" · {nmiss} expired (imagery gone) this load" if nmiss else ""
    return (f"<!doctype html><meta charset=utf-8><title>validate</title>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>{css}"
            f"{nc.header('validate', DASH_GW, _default_cam(DASH_GW))}"
            f"<header><h1>liftlab · validation</h1>"
            f"<div class=sub style='font:12px ui-monospace,monospace;color:#6b7a84'>reviewing under counting "
            f"<b>{CURRENT_COUNTING_VERSION}</b> · {npend} episodes pending{sup_note}</div></header><div class=wrap>"
            f"<h2>Cameras</h2>{cam_rows or '<p>no cameras yet</p>'}"
            f"<h2>Pending review ({npend} on current logic)</h2>{items or empty}"
            f"</div>")


# ---------------- for /ops: per-camera validation summary ----------------
def validation_summary(db, gw):
    try:
        rows = db.execute("SELECT cam,state,n_reviewed,n_exact,provenance FROM camera_validation WHERE gateway_id=?", (gw,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
