"""gw_door_event — the GPU door/floor stream (SEPARATE from gw_event/counting). ADDITIVE.

The GPU_DOOR pass emits floor + direction + door_state per state-change (emit-all-flag-quality): every
read is stored WITH its quality (read_conf, panels_agreed, reason, version), and ANALYSIS filters — we
never drop or guess at ingest. floor is NULL with reason='no_read' when nothing matched (e.g. MEP, whose
M/E glyphs are thin-known) rather than a guess. Rows are version-stamped (templates content hash +
door_version) so comparability boundaries exist from day one.

Also a lightweight SPOT-CHECK loop: the GPU samples N reads/hour WITH the panel crop; /floorcheck shows
recent reads-vs-crops so accuracy can be eyeballed before Tier-2 charts trust the floor data.

  POST /api/gw/{gw}/door_event         a door/floor state row (Bearer: gateway OR analysis)
  POST /api/gw/{gw}/floorcheck         a sampled read + panel crop jpeg (Bearer)
  GET  /floorcheck/{gw}/{cam}          spot-check page (Caddy basicauth, human)
  GET  /floorcheck/{gw}/{cam}/data     recent samples as JSON
  GET  /floorcheck/{gw}/{cam}/img/{id}.jpg   the stored panel crop
"""
import base64
import json
import os
import re
import sqlite3
import time

import nav_common as nc
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
GATEWAY_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                  for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g}
ANALYSIS_TOKENS = {g.split(":", 1)[0]: g.split(":", 1)[1]
                   for g in os.environ.get("ANALYSIS_TOKENS", "").split(",") if ":" in g}
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
FLOORCHECK_KEEP = int(os.environ.get("FLOORCHECK_KEEP", "300"))   # ephemeral spot-check images per cam
DOOR_STATES = {"closed", "opening", "open", "closing"}
# same grammar as the /calib-label wizard: floor + optional ^/v, or '-'. A reviewed sample with a
# confirmed label folds back into the calib crops (door_calib --foldback) -> a self-improving loop.
LABEL_RE = re.compile(r"^(-|[A-Za-z0-9]+[\^vV]?)$")

door_event_router = APIRouter()


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS gw_door_event (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL,
      floor TEXT, direction TEXT, door_state TEXT, openness REAL, read_conf REAL,
      panels_agreed INTEGER, reason TEXT, close_travel_s REAL,
      door_version TEXT, templates_hash TEXT, received_at REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_door_event ON gw_door_event (gateway_id,cam,ts)")
    try:
        db.execute("ALTER TABLE gw_door_event ADD COLUMN candidates TEXT")   # reason='ambiguous' top-2 [[lab,score],..]
    except sqlite3.OperationalError:
        pass                                                    # already present
    db.execute("""CREATE TABLE IF NOT EXISTS floor_sample (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL,
      floor TEXT, direction TEXT, read_conf REAL, panels_agreed INTEGER, reason TEXT,
      door_version TEXT, crop_jpeg BLOB, received_at REAL)""")
    try:
        db.execute("ALTER TABLE floor_sample ADD COLUMN reviewed_label TEXT")   # operator-confirmed truth -> foldback
    except sqlite3.OperationalError:
        pass
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


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------- GPU-facing ingest (Bearer) ----------------
@door_event_router.post("/api/gw/{gw}/door_event")
async def door_event_ingest(gw: str, request: Request, authorization: str = Header("")):
    _auth(gw, authorization)
    d = await request.json()
    cam = str(d.get("cam", ""))
    if not _SAFE.match(cam):
        raise HTTPException(400, "bad cam")
    ds = d.get("door_state")
    if ds is not None and ds not in DOOR_STATES:
        raise HTTPException(400, "bad door_state")
    floor = d.get("floor")                                   # may be null (no_read) — stored as-is, NOT guessed
    floor = str(floor) if floor is not None else None
    direction = d.get("direction")
    direction = str(direction) if direction in ("up", "down") else None
    cand = d.get("candidates")
    cand = json.dumps(cand) if cand else None                # [[lab,score],[lab,score]] when reason='ambiguous'
    db = _db()
    db.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,direction,door_state,openness,read_conf,"
               "panels_agreed,reason,close_travel_s,door_version,templates_hash,candidates,received_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (gw, cam, float(d.get("ts", time.time())), floor, direction, ds, _f(d.get("openness")),
                _f(d.get("read_conf")), 1 if d.get("panels_agreed") else 0, str(d.get("reason", "")),
                _f(d.get("close_travel_s")), str(d.get("door_version", "")), str(d.get("templates_hash", "")),
                cand, time.time()))
    db.commit()
    db.close()
    return {"ok": True}


@door_event_router.post("/api/gw/{gw}/floorcheck")
async def floorcheck_ingest(gw: str, request: Request, authorization: str = Header("")):
    _auth(gw, authorization)
    d = await request.json()
    cam = str(d.get("cam", ""))
    if not _SAFE.match(cam):
        raise HTTPException(400, "bad cam")
    blob = None
    b64 = d.get("crop_jpeg_b64")
    if b64:
        try:
            blob = base64.b64decode(b64)
        except (ValueError, TypeError):
            blob = None
    floor = d.get("floor")
    db = _db()
    db.execute("INSERT INTO floor_sample (gateway_id,cam,ts,floor,direction,read_conf,panels_agreed,reason,"
               "door_version,crop_jpeg,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (gw, cam, float(d.get("ts", time.time())), (str(floor) if floor is not None else None),
                (str(d.get("direction")) if d.get("direction") in ("up", "down") else None),
                _f(d.get("read_conf")), 1 if d.get("panels_agreed") else 0, str(d.get("reason", "")),
                str(d.get("door_version", "")), blob, time.time()))
    # prune this cam's ephemeral spot-check images to the last FLOORCHECK_KEEP
    db.execute("DELETE FROM floor_sample WHERE gateway_id=? AND cam=? AND id NOT IN "
               "(SELECT id FROM floor_sample WHERE gateway_id=? AND cam=? ORDER BY id DESC LIMIT ?)",
               (gw, cam, gw, cam, FLOORCHECK_KEEP))
    db.commit()
    db.close()
    return {"ok": True}


# ---------------- operator spot-check + review (Caddy basicauth) ----------------
@door_event_router.post("/floorcheck/{gw}/{cam}/review")
async def floorcheck_review(gw: str, cam: str, request: Request):
    """Confirm the TRUE floor for a sample (human action, basicauth). Sets reviewed_label; door_calib
    --foldback later appends reviewed samples' crops to the calib set + labels.json and rebuilds."""
    _safe(gw, cam)
    d = await request.json()
    sid = int(d.get("id", -1))
    label = str(d.get("label", "")).strip()
    if not LABEL_RE.match(label):
        raise HTTPException(400, "invalid label — floor + optional ^/v (e.g. 48, 6^, MEP^) or '-'")
    db = _db()
    cur = db.execute("UPDATE floor_sample SET reviewed_label=? WHERE id=? AND gateway_id=? AND cam=?",
                     (label, sid, gw, cam))
    db.commit()
    n = cur.rowcount
    db.close()
    if not n:
        raise HTTPException(404, "no such sample")
    return {"ok": True, "id": sid, "reviewed_label": label}


@door_event_router.get("/floorcheck/{gw}/{cam}/img/{sid}.jpg")
def floorcheck_img(gw: str, cam: str, sid: int):
    _safe(gw, cam)
    db = _db()
    row = db.execute("SELECT crop_jpeg FROM floor_sample WHERE id=? AND gateway_id=? AND cam=?",
                     (sid, gw, cam)).fetchone()
    db.close()
    if not row or row["crop_jpeg"] is None:
        raise HTTPException(404, "no image")
    return Response(bytes(row["crop_jpeg"]), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@door_event_router.get("/floorcheck/{gw}/{cam}/data")
def floorcheck_data(gw: str, cam: str, limit: int = 60):
    _safe(gw, cam)
    db = _db()
    rows = db.execute("SELECT id,ts,floor,direction,read_conf,panels_agreed,reason,door_version,reviewed_label,"
                      "(crop_jpeg IS NOT NULL) AS has_img FROM floor_sample "
                      "WHERE gateway_id=? AND cam=? ORDER BY id DESC LIMIT ?",
                      (gw, cam, max(1, min(limit, 300)))).fetchall()
    # headline stream stats (last 24h) so the page shows read-rate/agreement without trusting it yet
    day = time.time() - 86400
    agg = db.execute("SELECT COUNT(*) n, SUM(floor IS NOT NULL) reads, SUM(panels_agreed) agreed, "
                     "SUM(reason='no_read') noread, SUM(reason='disagree') disagree "
                     "FROM gw_door_event WHERE gateway_id=? AND cam=? AND ts>=?",
                     (gw, cam, day)).fetchone()
    db.close()
    return JSONResponse({"gw": gw, "cam": cam, "samples": [dict(r) for r in rows],
                         "stream_24h": dict(agg) if agg else {}})


@door_event_router.get("/floorcheck/{gw}/{cam}", response_class=HTMLResponse)
def floorcheck_page(gw: str, cam: str):
    _safe(gw, cam)
    nav = nc.header('floorcheck', gw, cam) + nc.cam_bar(gw, cam, '')
    page = (_PAGE.replace("__GW__", gw).replace("__CAM__", cam)
                 .replace("__NAV__", nav)
                 .replace("</style>", nc.NAV_CSS + "</style>", 1))
    page += nc.switcher_js(gw, cam, f"/floorcheck/{gw}/__C__")
    return HTMLResponse(page)


_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>floorcheck __CAM__ @ __GW__</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#e3e8ec;--fg:#1c2429;--mut:#6b7a84;--ok:#2f9e5f;--warn:#d98a1f;--bad:#d4483b;--mono:ui-monospace,Menlo,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e1418;--card:#161d22;--line:#243038;--fg:#d6dee3;--mut:#7f9099}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 system-ui,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
h1{font:600 15px var(--mono);margin:0}
.pill{font:11px var(--mono);padding:2px 8px;border-radius:10px;background:var(--line);color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;padding:14px}
.cell{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;text-align:center}
.cell img{width:100%;image-rendering:pixelated;background:#000;display:block;min-height:60px}
.rd{font:600 16px var(--mono);padding:3px 0 0}
.mut{color:var(--mut);font:10px var(--mono);padding:0 0 4px;word-break:break-all}
.b-ok{border-left:3px solid var(--ok)} .b-single{border-left:3px solid var(--warn)}
.b-noread{border-left:3px solid var(--mut)} .b-disagree{border-left:3px solid var(--bad)}
.note{padding:0 16px;color:var(--mut);font:12px var(--mono)}
.rev{width:90%;margin:2px auto 5px;display:block;font:600 13px var(--mono);text-align:center;text-transform:uppercase;
  border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--fg);padding:2px}
.rev.saved{border-color:var(--ok);color:var(--ok)}

</style></head><body>
__NAV__
<header>
  <h1>floorcheck · __CAM__ @ __GW__</h1>
  <span class=pill id=stream>—</span>
  <span class=pill id=reviewed>—</span>
  <span class=pill id=stamp>—</span>
</header>
<div class=note>Spot-check the OCR before Tier-2 trusts it: read (big) vs the panel crop. Colour = quality:
  <b style="color:var(--ok)">ok</b> (2 panels agree) · <b style="color:var(--warn)">single</b> · <b style="color:var(--mut)">no_read</b> · <b style="color:var(--bad)">disagree</b>.
  Type the TRUE floor in a box + <kbd>Enter</kbd> to confirm it (e.g. 48, 6^, '-' to exclude); reviewed samples fold back into the templates via <code>door_calib --foldback</code> then rebuild — a self-improving loop.</div>
<div class=grid id=grid></div>
<script>
var GW="__GW__", CAM="__CAM__";
function esc(s){return s==null?"":(""+s)}
function cls(reason,agreed){if(reason==="no_read")return"b-noread";if(reason==="disagree")return"b-disagree";if(agreed)return"b-ok";return"b-single";}
function draw(d){
  var s=d.stream_24h||{};
  document.getElementById("stream").textContent="24h: "+esc(s.reads||0)+"/"+esc(s.n||0)+" read, "+esc(s.agreed||0)+" agreed, "+esc(s.noread||0)+" no_read, "+esc(s.disagree||0)+" disagree";
  var nrev=(d.samples||[]).filter(function(r){return r.reviewed_label}).length;
  document.getElementById("reviewed").textContent=nrev+" reviewed";
  document.getElementById("stamp").textContent="updated "+new Date().toLocaleTimeString();
  var g=(d.samples||[]).map(function(r){
    var img=r.has_img?('<img src="/floorcheck/'+GW+'/'+CAM+'/img/'+r.id+'.jpg" alt="crop">'):'<div style="min-height:60px;background:#000"></div>';
    var f=(r.floor==null?'∅':esc(r.floor))+(r.direction==="up"?' ↑':r.direction==="down"?' ↓':'');
    var t=new Date(r.ts*1000).toLocaleTimeString();
    var rev='<input class="rev'+(r.reviewed_label?' saved':'')+'" data-id="'+r.id+'" value="'+esc(r.reviewed_label||"")+'" placeholder="'+(r.floor==null?'true floor':'='+esc(r.floor))+'" autocomplete=off>';
    return '<div class="cell '+cls(r.reason,r.panels_agreed)+'">'+img+'<div class=rd>'+f+'</div>'
      +'<div class=mut>'+t+' · '+esc(r.reason)+(r.read_conf!=null?' · '+r.read_conf:'')+'</div>'+rev+'</div>';
  }).join('');
  document.getElementById("grid").innerHTML=g||'<div class=note>no samples yet — the GPU posts N/hour when GPU_DOOR is on</div>';
}
// delegated: Enter in a review box confirms the true floor (autosave). Attached once.
document.getElementById("grid").addEventListener("keydown",function(e){
  var el=e.target; if(!el.classList||!el.classList.contains("rev")||e.key!=="Enter")return;
  e.preventDefault();
  var v=el.value.trim().replace(/\s+/g,"");
  if(!/^(-|[A-Za-z0-9]+[\^vV]?)$/.test(v)){el.style.borderColor="#d4483b";return;}
  fetch("/floorcheck/"+GW+"/"+CAM+"/review",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:+el.dataset.id,label:v})}).then(function(r){
      if(r.ok){el.classList.add("saved");el.blur();}else{el.style.borderColor="#d4483b";}
    }).catch(function(){el.style.borderColor="#d4483b";});
});
function tick(){fetch("/floorcheck/"+GW+"/"+CAM+"/data").then(function(r){return r.json()}).then(draw).catch(function(){});}
tick(); setInterval(tick, 20000);
</script></body></html>"""
