"""ROI drawing wizard — /calib-roi/{gw}/{cam}. The SECOND web wizard piece.

Replaces the last ruler-read in per-camera setup. The operator drags two boxes on the frame — DOOR
(the leaf) and PANEL (the floor display) — instead of reading pixel coordinates off a ruler render
and typing them into an env var. Saves to /var/lib/liftlab/calib/{gw}/{cam}/roi.json in FRAME px;
door_calib reads it when DOOR_ROI_FRAME / PANEL_ROIS are absent (env still wins — ch29's flow is
untouched).

Same shape as /calib-label: files only. NO cv2, NO model, NO tables. The frame JPEG is served by
ops_api's existing /calib/{gw}/{cam}/{fname} route; this module only reads its header for dimensions
and writes JSON. Behind the SAME Caddy basicauth as /calib (a human page, not a token API).

WHY THE IMAGE CHOICE MATTERS (the correctness crux of this page): a drawn box is only as accurate as
the pixel grid it is drawn on. `_calib_frame.jpg` is written by door_calib at NATIVE frame resolution
(704x576) — one image pixel is one frame pixel, so a box drawn on it needs no rescaling and carries no
rounding. The live snapshot (SNAP_DIR) is rescaled by ffmpeg to SNAP_WIDTH (480 by default), where one
image pixel is ~1.47 frame pixels — on a panel ROI that is genuinely ~40px wide, that is a ~4% error
before the operator's own aim. So the native frame is strongly preferred, the snapshot is a clearly
labelled fallback, and a scaled source is CONVERTED explicitly (never assumed 1:1) and only when the
true frame size is known. If it isn't known, the page refuses to save rather than write plausible
nonsense.

Routes:
  GET  /calib-roi/{gw}/{cam}          the drawing page
  GET  /calib-roi/{gw}/{cam}/state    JSON: image url + dims, frame dims, saved rois, freshness
  POST /calib-roi/{gw}/{cam}/refresh  copy the live snapshot into the calib dir (file copy, no cv2)
  POST /calib-roi/{gw}/{cam}/save     {door_roi_frame, panel_rois} -> validate -> roi.json
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

import nav_common as nc
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
SNAP_DIR = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
FRESH_S = float(os.environ.get("CALIB_ROI_FRESH_S", "900"))   # older than this = offer a refresh
MIN_SIDE = 4                                                  # a 3px box is a misclick, not an ROI

calib_roi_router = APIRouter()


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _dir(gw, cam):
    _safe(gw, cam)
    return CALIB_DIR / gw / cam


def _jpeg_wh(path):
    """(w, h) from a JPEG's SOF marker — pure stdlib, because this app has no cv2/PIL and must not
    grow one for an image header. Returns None if it isn't a JPEG we can read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xD9:
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        # SOF0..SOF15 carry the dimensions; DHT/DAC/DNL (C4/CC/C8) do not.
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        if seglen < 2:
            break
        i += 2 + seglen
    return None


def _frame_wh(d):
    """The TRUE frame size, from door_calib's own record. This is the only trustworthy source: it is
    written by the process that decoded the frame. Absent => a scaled image cannot be converted."""
    p = d / "_calib_rois.json"
    try:
        v = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    wh = v.get("frame_wh")
    if isinstance(wh, list) and len(wh) == 2 and all(isinstance(x, int) and x > 0 for x in wh):
        return [wh[0], wh[1]]
    return None


def _roi_path(d):
    return d / "roi.json"


def _load_roi(d):
    try:
        v = json.loads(_roi_path(d).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _age(p):
    try:
        return round(time.time() - p.stat().st_mtime, 1)
    except OSError:
        return None


def _pick_image(d, gw, cam):
    """Which JPEG to draw on, and everything the page needs to convert coordinates honestly.

    Preference order is about ACCURACY, not recency: a native-resolution frame beats a fresher but
    rescaled snapshot, because the rescale is the larger error. The page shows both ages so the
    operator can refresh deliberately.
    """
    frame_wh = _frame_wh(d)
    native = d / "_calib_frame.jpg"
    snap_copy = d / "_calib_roi_frame.jpg"
    for path, source, annotated in ((native, "calib_frame", True), (snap_copy, "snapshot", False)):
        if not path.exists():
            continue
        wh = _jpeg_wh(path)
        if not wh:
            continue
        if source == "snapshot":
            # snapshot.py ALWAYS rescales (-vf scale=SNAP_WIDTH:-2), so this image is scaled by
            # construction — we simply may not know by how much. Deriving "scaled" from a
            # frame_wh comparison would report False when frame_wh is unknown, i.e. exactly when
            # we are least able to convert, and the page would then assume 1:1 and write boxes
            # that are wrong by ~1.5x with nothing anywhere saying so.
            scaled, convertible = True, bool(frame_wh)
        else:
            # door_calib writes this at native resolution; the image IS the frame, so it converts
            # 1:1 even with no recorded frame size. A mismatch means the camera changed resolution
            # since that record was written — scaled, and only convertible because we know both.
            scaled = bool(frame_wh) and [wh[0], wh[1]] != frame_wh
            convertible = True
        return {"image": f"/calib/{gw}/{cam}/{path.name}", "image_wh": [wh[0], wh[1]],
                "image_age_s": _age(path), "source": source, "annotated": annotated,
                "scaled": scaled, "convertible": convertible}
    return None


@calib_roi_router.get("/calib-roi/{gw}/{cam}/state")
def roi_state(gw: str, cam: str):
    d = _dir(gw, cam)
    img = _pick_image(d, gw, cam)
    frame_wh = _frame_wh(d)
    saved = _load_roi(d)
    snap = SNAP_DIR / gw / f"{cam}.jpg"
    return JSONResponse({
        "gw": gw, "cam": cam,
        "image": img,                       # None => nothing to draw on yet
        "frame_wh": frame_wh,               # None => a scaled image cannot be converted
        "fresh_s": FRESH_S,
        "stale": bool(img and img["image_age_s"] is not None and img["image_age_s"] > FRESH_S),
        "snapshot_available": snap.exists(),
        "snapshot_age_s": _age(snap),
        "saved": {"door_roi_frame": saved.get("door_roi_frame"),
                  "panel_rois": saved.get("panel_rois") or [],
                  "saved_at": saved.get("saved_at"), "frame_wh": saved.get("frame_wh")},
        "min_side": MIN_SIDE,
    })


@calib_roi_router.post("/calib-roi/{gw}/{cam}/refresh")
def roi_refresh(gw: str, cam: str):
    """Copy the live snapshot into the calib dir as a drawing surface. A FILE COPY — the JPEG is
    already encoded, so this needs no image library. It is a rescaled image (see module docstring),
    hence only useful when door_calib has recorded the true frame size."""
    d = _dir(gw, cam)
    snap = SNAP_DIR / gw / f"{cam}.jpg"
    if not snap.exists():
        raise HTTPException(404, f"no live snapshot at {snap} — is the relay delivering this camera?")
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "_calib_roi_frame.jpg"
    tmp = dst.with_suffix(".jpg.tmp")
    try:
        shutil.copyfile(snap, tmp)
        os.replace(tmp, dst)                # atomic: the page never fetches a half-written JPEG
    except OSError as e:
        raise HTTPException(500, f"copy failed: {e}")
    wh = _jpeg_wh(dst)
    fw = _frame_wh(d)
    return {"ok": True, "image_wh": list(wh) if wh else None, "frame_wh": fw,
            "note": ("snapshot is rescaled — coordinates are converted to frame px"
                     if (wh and fw and [wh[0], wh[1]] != fw) else "1:1 with the frame")}


def _rect(v, frame_wh, what):
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise HTTPException(400, f"{what}: expected [x,y,w,h]")
    try:
        x, y, w, h = (int(round(float(n))) for n in v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what}: non-numeric value")
    if w < MIN_SIDE or h < MIN_SIDE:
        raise HTTPException(400, f"{what}: {w}x{h} is too small (min {MIN_SIDE}px) — a misclick, not an ROI")
    if x < 0 or y < 0:
        raise HTTPException(400, f"{what}: negative origin ({x},{y})")
    if frame_wh:
        fw, fh = frame_wh
        if x + w > fw or y + h > fh:
            raise HTTPException(400, f"{what}: [{x},{y},{w},{h}] falls outside the {fw}x{fh} frame")
    return [x, y, w, h]


@calib_roi_router.post("/calib-roi/{gw}/{cam}/save")
async def roi_save(gw: str, cam: str, request: Request):
    d = _dir(gw, cam)
    body = await request.json()
    frame_wh = _frame_wh(d)
    img = _pick_image(d, gw, cam)
    # A scaled source with no known frame size cannot produce frame px. Refuse — a wrong ROI is worse
    # than no ROI: it fails silently downstream as bad OCR, not as an error anyone would trace here.
    if img and not img.get("convertible"):
        raise HTTPException(409, f"the drawing image ({img.get('source')}) is rescaled and the true "
                                 "frame size is unknown (_calib_rois.json absent) — run "
                                 "door_calib --frames 1 once, then reload and redraw")
    door = _rect(body.get("door_roi_frame"), frame_wh, "door_roi_frame")
    panels_in = body.get("panel_rois") or []
    if not isinstance(panels_in, list) or not panels_in:
        raise HTTPException(400, "panel_rois: at least one panel rectangle is required")
    if len(panels_in) > 2:
        raise HTTPException(400, "panel_rois: at most two panels (the engine reads panel0 and panel1)")
    panels = [_rect(p, frame_wh, f"panel_rois[{i}]") for i, p in enumerate(panels_in)]

    # MERGE, never replace. roi.json is shared with /calib-cells (which stores "cells" here), so a
    # whole-file write would silently delete a cell geometry the operator had already approved —
    # and they would only find out from a bad build. Redrawing ROIs must touch ROIs only.
    out = _load_roi(d)
    out.update({"door_roi_frame": door, "panel_rois": panels,
                # Provenance: which frame these were drawn on. A later frame-size change (camera
                # reconfig, sub-stream resolution change) invalidates them, and this makes that
                # detectable rather than mysterious.
                "frame_wh": frame_wh, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source_image": (img or {}).get("source"),
                "drawn_on_wh": (img or {}).get("image_wh")})
    # Cells are measured WITHIN the panel ROI. Move the panel and the cells no longer describe the
    # same pixels — so flag them rather than leaving stale geometry that still looks approved.
    old_panels = _load_roi(d).get("panel_rois")
    if out.get("cells") and old_panels and old_panels != panels:
        out["cells"]["stale"] = (f"panel ROI changed {old_panels} -> {panels} after these cells were "
                                 f"drawn — redraw them at /calib-cells/{gw}/{cam}")
    d.mkdir(parents=True, exist_ok=True)
    tmp = _roi_path(d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, _roi_path(d))           # atomic
    return {"ok": True, **out}


@calib_roi_router.post("/calib-roi/{gw}/{cam}/zones")
async def zones_save(gw: str, cam: str, request: Request):
    """Counting zones (landing + cabin polygons, FRAME px) -> roi.json. MERGE like /save — door,
    panels and cells untouched. A separate endpoint so zones can be added to an already-calibrated
    camera without redrawing ROIs. The registry serves them, the fleet passes them through, and
    gpu_analyze refuses to count without them — ch29's built-in polygons scaled onto another
    camera's optics ARE the ch16 undercount this closes.

    Body: {"zone_landing": [[x,y],...>=3], "zone_cabin": [[x,y],...>=3], "provenance": "..."?}
    """
    d = _dir(gw, cam)
    body = await request.json()
    # The frame the polygons were DRAWN at. Defaults to the calib frame size, but the body may
    # declare its own (e.g. ch29's verified polygons were drawn on the 1920x1080 MAIN stream while
    # the calib wizard works on the sub frame) — polygons must carry their drawn frame or the
    # worker scales them wrong, silently.
    frame_wh = body.get("zone_frame") or _frame_wh(d)
    if not (isinstance(frame_wh, (list, tuple)) and len(frame_wh) == 2
            and all(isinstance(x, (int, float)) and x > 0 for x in frame_wh)):
        raise HTTPException(409, "frame size unknown — pass \"zone_frame\": [w,h] (the size the "
                                 "polygons were drawn at) or run door_calib --frames 1 once")
    frame_wh = [int(frame_wh[0]), int(frame_wh[1])]

    def _poly(v, name):
        if not (isinstance(v, list) and len(v) >= 3
                and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in v)):
            raise HTTPException(400, f"{name}: need a polygon of >=3 [x,y] points")
        pts = []
        for x, y in v:
            try:
                xi, yi = int(round(float(x))), int(round(float(y)))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{name}: non-numeric point")
            if not (0 <= xi <= frame_wh[0] and 0 <= yi <= frame_wh[1]):
                raise HTTPException(400, f"{name}: point ({xi},{yi}) outside frame {frame_wh}")
            pts.append([xi, yi])
        return pts

    zl = _poly(body.get("zone_landing"), "zone_landing")
    zc = _poly(body.get("zone_cabin"), "zone_cabin")
    out = _load_roi(d)
    out.update({"zone_landing": zl, "zone_cabin": zc, "zone_frame": frame_wh,
                "zones_saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if body.get("provenance"):
        out["zones_provenance"] = str(body["provenance"])[:200]
    d.mkdir(parents=True, exist_ok=True)
    tmp = _roi_path(d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, _roi_path(d))           # atomic
    return {"ok": True, "zone_landing": zl, "zone_cabin": zc, "zone_frame": frame_wh,
            "note": "registry hash changes on next poll — the fleet restarts this worker with zones"}


@calib_roi_router.get("/calib-roi/{gw}/{cam}", response_class=HTMLResponse)
def roi_page(gw: str, cam: str):
    _safe(gw, cam)
    nav = nc.header('', gw, cam) + nc.cam_bar(gw, cam, 'rois')
    page = (_PAGE.replace("__GW__", gw).replace("__CAM__", cam)
                 .replace("__NAV__", nav)
                 .replace("</style>", nc.NAV_CSS + "</style>", 1))
    page += nc.switcher_js(gw, cam, f"/calib-roi/{gw}/__C__")
    return HTMLResponse(page)


_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ROI __CAM__ @ __GW__</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#e3e8ec;--fg:#1c2429;--mut:#6b7a84;--ok:#2f9e5f;--warn:#d98a1f;--bad:#d4483b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e1418;--card:#161d22;--line:#243038;--fg:#d6dee3;--mut:#7f9099}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h1{font:600 15px var(--mono);margin:0}
.pill{font:11px var(--mono);padding:2px 8px;border-radius:10px;background:var(--line);color:var(--mut)}
.pill.warn{background:#fff3e0;color:#b06a00}.pill.bad{background:#fdecea;color:var(--bad)}
.wrap{padding:14px;display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.stage{position:relative;display:inline-block;background:#000;border:1px solid var(--line);border-radius:8px;overflow:hidden;line-height:0}
.stage img{display:block;image-rendering:pixelated;user-select:none;-webkit-user-drag:none}
.box{position:absolute;border:2px solid;pointer-events:none;font:10px var(--mono);color:#fff}
.box b{position:absolute;top:-1px;left:-1px;padding:0 3px;background:inherit;border:0;line-height:14px}
.box.door{border-color:#4c8bf5}.box.door b{background:#4c8bf5}
.box.panel{border-color:#2f9e5f}.box.panel b{background:#2f9e5f}
.box.live{border-style:dashed;border-color:#d98a1f}
.side{min-width:290px;flex:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
.card h3{margin:0 0 8px;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}
button{font:600 13px system-ui;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer;margin:0 6px 6px 0}
button.on{background:#4c8bf5;border-color:#4c8bf5;color:#fff}
button.on.panel{background:#2f9e5f;border-color:#2f9e5f}
button.pri{background:#2f9e5f;border-color:#2f9e5f;color:#fff}
button:disabled{opacity:.4;cursor:default}
.kv{display:flex;justify-content:space-between;gap:10px;padding:2px 0;border-bottom:1px solid var(--line);font:12px var(--mono)}
.kv:last-child{border-bottom:0}
.msg{font:12px var(--mono);min-height:18px;margin-top:6px}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.note{color:var(--mut);font:12px/1.5 var(--mono)}
.warnbox{background:#fff8ec;border:1px solid #e8c88a;color:#7a5200;border-radius:8px;padding:8px 10px;font:12px/1.5 var(--mono);margin-bottom:10px}
@media(prefers-color-scheme:dark){.warnbox{background:#2a2113;border-color:#5c4a24;color:#e8c88a}}
a{color:#4c8bf5}

#runlog{background:#0e1418;color:#cfe3d6;font:11px/1.45 var(--mono);padding:8px;border-radius:6px;
  max-height:230px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:8px 0 0}
#runlog:empty{display:none}
.runbtn{font:600 12px system-ui;padding:7px 11px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);color:var(--fg);cursor:pointer;margin:0 6px 6px 0}
.runbtn:disabled{opacity:.45;cursor:default}

</style></head><body>
__NAV__
<header>
  <h1>roi · __CAM__ @ __GW__</h1>
  <span class=pill id=src>—</span>
  <span class=pill id=dims>—</span>
  <span class=pill id=agepill>—</span>
  <a class=pill href="/calib-label/__GW__/__CAM__">label wizard →</a>
  <a class=pill href="/calib-cells/__GW__/__CAM__">draw cells →</a>
</header>
<div class=wrap>
  <div>
    <div class=stage id=stage><img id=frame alt="frame"><div id=boxes></div></div>
    <div class=note id=hint style="margin-top:6px">drag on the image to draw the selected box</div>
  </div>
  <div class=side>
    <div id=warnings></div>
    <div class=card>
      <h3>1 · what are you drawing</h3>
      <button id=bDoor class=on>DOOR (leaf)</button>
      <button id=bP0>PANEL 1</button>
      <button id=bP1>PANEL 2 <span class=note>(optional)</span></button>
      <div class=note style="margin-top:6px">Draw a box by dragging. Drawing again <b>replaces</b> that box.</div>
    </div>
    <div class=card>
      <h3>2 · boxes (frame px)</h3>
      <div class=kv><span>door</span><b id=vDoor>—</b></div>
      <div class=kv><span>panel 1</span><b id=vP0>—</b></div>
      <div class=kv><span>panel 2</span><b id=vP1>—</b></div>
      <div style="margin-top:8px">
        <button id=clear>Clear all</button>
        <button id=save class=pri disabled>Save roi.json</button>
      </div>
      <div class=msg id=msg></div>
    </div>
    <div class=card>
      <h3>3 · frame</h3>
      <div class=kv><span>zoom</span><b><button id=zo>−</button><span id=zval>1x</span><button id=zi>+</button></b></div>
      <div class=kv><span>saved earlier</span><b id=savedAt>—</b></div>
      <div style="margin-top:6px"><button id=refresh>Refresh from live snapshot</button></div>
      <div class=note style="margin-top:6px" id=srcnote></div>
    </div>
<div class=card id=runcard>
  <h3>run <span class=note style="font-weight:400" id=runwhy></span></h3>
  <div id=runbtns></div>
  <div class=msg id=runmsg></div>
  <pre id=runlog></pre>
</div>
<script>
// ---- run buttons (wizard piece 4) ----
// Shared by /calib-roi, /calib-cells and /calib-label. Starts a door_calib subprocess on the cloud
// and polls its output; polling rather than SSE so a reload or a dropped lobby connection does not
// lose the run. Buttons disable while anything is running for THIS camera — two collects at once
// would interleave crops.
// Self-contained: this block sits ABOVE the page's main <script>, so it cannot rely on that
// script's GW/CAM having been evaluated yet. The template placeholders are substituted
// server-side, so reading them here is order-independent.
var RGW="__GW__", RCAM="__CAM__";
var RUN={since:0,timer:null,busy:false};
function runBtnHtml(jobs){
  return (jobs||[]).map(function(j){
    return '<button class=runbtn data-job="'+j.id+'" title="'+escR(j.help)+'">'+escR(j.label)+'</button>';
  }).join('')+'<button class=runbtn id=runstop style="display:none">Stop</button>';
}
function escR(s){return s==null?'':(''+s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function runPaint(d){
  var btns=document.querySelectorAll('.runbtn');
  for(var i=0;i<btns.length;i++){
    if(btns[i].id==='runstop'){btns[i].style.display=d.running?'':'none';continue;}
    btns[i].disabled=d.running||!(d.runner&&d.runner.ok);
  }
  document.getElementById('runwhy').textContent=(d.runner&&d.runner.ok)?'':(d.runner?d.runner.reason:'');
  var m=document.getElementById('runmsg');
  if(d.running){m.className='msg';m.textContent=d.job+' running… '+Math.round(d.elapsed_s)+'s';}
  else if(d.rc===0){m.className='msg ok';m.textContent=d.job+' finished OK in '+Math.round(d.elapsed_s)+'s';}
  else if(d.rc!=null){m.className='msg err';m.textContent=d.job+' FAILED rc='+d.rc+' — see the output';}
}
function runPoll(){
  fetch('/calib-run/'+RGW+'/'+RCAM+'/status?since='+RUN.since).then(function(r){return r.json()}).then(function(d){
    if(!document.getElementById('runbtns').innerHTML){document.getElementById('runbtns').innerHTML=runBtnHtml(d.jobs);}
    if(d.lines&&d.lines.length){
      var pre=document.getElementById('runlog');
      pre.textContent+=(pre.textContent?'\n':'')+d.lines.join('\n');
      pre.scrollTop=pre.scrollHeight;
      RUN.since=d.next_since;
    }
    var wasBusy=RUN.busy; RUN.busy=!!d.running;
    runPaint(d);
    if(d.running){ if(!RUN.timer)RUN.timer=setInterval(runPoll,1500); }
    else if(RUN.timer){ clearInterval(RUN.timer); RUN.timer=null;
      // A finished job usually changed what this page shows (a new frame, new crops, new cells) —
      // reload the page's own state rather than making the operator guess whether it took.
      if(wasBusy&&typeof runFinished==='function')runFinished(d);
    }
  }).catch(function(){});
}
document.addEventListener('click',function(e){
  var b=e.target; if(!b.classList||!b.classList.contains('runbtn')||b.disabled)return;
  if(b.id==='runstop'){fetch('/calib-run/'+RGW+'/'+RCAM+'/stop',{method:'POST'}).then(runPoll);return;}
  var job=b.getAttribute('data-job'); if(!job)return;
  b.disabled=true;
  fetch('/calib-run/'+RGW+'/'+RCAM+'/'+job,{method:'POST'}).then(function(r){
    return r.json().then(function(j){
      if(!r.ok){document.getElementById('runmsg').className='msg err';
                document.getElementById('runmsg').textContent=j.detail||'could not start';}
      runPoll();
    });
  }).catch(function(){runPoll()});
});
runPoll();
</script>
    <div class=card>
      <h3>next</h3>
      <div class=note>after saving: <code>door_calib --collect</code> → <a href="/calib-label/__GW__/__CAM__">label the crops</a> → <a href="/calib-cells/__GW__/__CAM__">draw the cells</a> → <code>--build</code>.
      roi.json is used only when <code>DOOR_ROI_FRAME</code>/<code>PANEL_ROIS</code> are unset — env still wins.</div>
    </div>
  </div>
</div>
<script>
var GW="__GW__", CAM="__CAM__";
var $=function(id){return document.getElementById(id)};
var S=null, zoom=1, mode="door";
var R={door:null,p0:null,p1:null};          // FRAME px
var drag=null;

function fmt(r){return r?("["+r[0]+","+r[1]+","+r[2]+","+r[3]+"]"):"—"}
// Image px -> FRAME px. When the source is native these scales are exactly 1; when it is the
// rescaled snapshot they are the honest per-axis ratios (ffmpeg's -2 height rounding makes sx and
// sy differ slightly, so they are NEVER collapsed into a single factor).
function scales(){
  if(!S||!S.image)return[1,1];
  var iw=S.image.image_wh[0], ih=S.image.image_wh[1];
  if(!S.frame_wh) return [1,1];
  return [S.frame_wh[0]/iw, S.frame_wh[1]/ih];
}
function toFrame(px,py,w,h){
  var s=scales(), img=$("frame");
  var dx=img.clientWidth/S.image.image_wh[0], dy=img.clientHeight/S.image.image_wh[1]; // display->image
  return [Math.round(px/dx*s[0]),Math.round(py/dy*s[1]),Math.round(w/dx*s[0]),Math.round(h/dy*s[1])];
}
function toDisplay(r){
  var s=scales(), img=$("frame");
  var dx=img.clientWidth/S.image.image_wh[0], dy=img.clientHeight/S.image.image_wh[1];
  return [r[0]/s[0]*dx, r[1]/s[1]*dy, r[2]/s[0]*dx, r[3]/s[1]*dy];
}
function drawBoxes(live){
  var h="";
  function add(r,cls,lab){ if(!r)return; var d=toDisplay(r);
    h+='<div class="box '+cls+'" style="left:'+d[0]+'px;top:'+d[1]+'px;width:'+d[2]+'px;height:'+d[3]+'px"><b>'+lab+'</b></div>';}
  add(R.door,"door","DOOR"); add(R.p0,"panel","P1"); add(R.p1,"panel","P2");
  if(live){h+='<div class="box live" style="left:'+live[0]+'px;top:'+live[1]+'px;width:'+live[2]+'px;height:'+live[3]+'px"></div>';}
  $("boxes").innerHTML=h;
  $("vDoor").textContent=fmt(R.door); $("vP0").textContent=fmt(R.p0); $("vP1").textContent=fmt(R.p1);
  $("save").disabled=!(R.door&&R.p0);
}
function setMode(m){mode=m;
  $("bDoor").className=(m==="door")?"on":""; $("bP0").className=(m==="p0")?"on panel":"";
  $("bP1").className=(m==="p1")?"on panel":"";
  $("hint").textContent="drag to draw "+(m==="door"?"the DOOR (leaf)":(m==="p0"?"PANEL 1":"PANEL 2"));
}
function applyZoom(){
  if(!S||!S.image)return;
  $("frame").style.width=(S.image.image_wh[0]*zoom)+"px";
  $("zval").textContent=zoom+"x";
  drawBoxes(null);
}
function msg(t,cls){$("msg").textContent=t;$("msg").className="msg "+(cls||"");}

$("stage").addEventListener("mousedown",function(e){
  if(e.target.id!=="frame")return;
  var b=$("frame").getBoundingClientRect();
  drag={x0:e.clientX-b.left,y0:e.clientY-b.top};
  e.preventDefault();
});
window.addEventListener("mousemove",function(e){
  if(!drag)return;
  var b=$("frame").getBoundingClientRect();
  var x=Math.max(0,Math.min(b.width,e.clientX-b.left)), y=Math.max(0,Math.min(b.height,e.clientY-b.top));
  drag.x1=x; drag.y1=y;
  drawBoxes([Math.min(drag.x0,x),Math.min(drag.y0,y),Math.abs(x-drag.x0),Math.abs(y-drag.y0)]);
});
window.addEventListener("mouseup",function(){
  if(!drag)return;
  if(drag.x1!=null){
    var x=Math.min(drag.x0,drag.x1), y=Math.min(drag.y0,drag.y1);
    var w=Math.abs(drag.x1-drag.x0), h=Math.abs(drag.y1-drag.y0);
    var f=toFrame(x,y,w,h);
    if(f[2]>=(S.min_side||4)&&f[3]>=(S.min_side||4)){
      R[mode==="door"?"door":mode]=f;
      msg("");
      if(mode==="door"&&!R.p0)setMode("p0");        // natural next step, no extra click
    } else { msg("too small ("+f[2]+"x"+f[3]+" frame px) — ignored","err"); }
  }
  drag=null; drawBoxes(null);
});

$("bDoor").onclick=function(){setMode("door")};
$("bP0").onclick=function(){setMode("p0")};
$("bP1").onclick=function(){setMode("p1")};
$("clear").onclick=function(){R={door:null,p0:null,p1:null};msg("");drawBoxes(null)};
$("zi").onclick=function(){zoom=Math.min(4,zoom+1);applyZoom()};
$("zo").onclick=function(){zoom=Math.max(1,zoom-1);applyZoom()};
$("refresh").onclick=function(){
  msg("refreshing…");
  fetch("/calib-roi/"+GW+"/"+CAM+"/refresh",{method:"POST"}).then(function(r){return r.json().then(function(j){
    if(!r.ok){msg(j.detail||"refresh failed","err");return;}
    msg("refreshed — "+(j.note||""),"ok"); load();
  })}).catch(function(){msg("refresh failed","err")});
};
$("save").onclick=function(){
  var panels=[]; if(R.p0)panels.push(R.p0); if(R.p1)panels.push(R.p1);
  msg("saving…");
  fetch("/calib-roi/"+GW+"/"+CAM+"/save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({door_roi_frame:R.door,panel_rois:panels})}).then(function(r){
      return r.json().then(function(j){
        if(!r.ok){msg(j.detail||"save failed","err");return;}
        msg("saved roi.json ✓  door="+fmt(j.door_roi_frame)+" panels="+j.panel_rois.length,"ok");
        load();
      });
    }).catch(function(){msg("save failed","err")});
};

// a finished job (notably 'frame') replaces the image this page draws on
function runFinished(){load();}
function load(){
  fetch("/calib-roi/"+GW+"/"+CAM+"/state").then(function(r){return r.json()}).then(function(d){
    S=d;
    var w=[];
    if(!d.image){
      w.push("No frame to draw on yet. Use <b>Refresh from live snapshot</b>, or run <code>door_calib --frames 1</code> for a native-resolution frame (more accurate).");
      $("warnings").innerHTML='<div class=warnbox>'+w.join("<br>")+'</div>';
      $("src").textContent="no image"; return;
    }
    $("frame").src=d.image.image+"?t="+Date.now();
    $("src").textContent=d.image.source;
    $("dims").textContent=d.image.image_wh[0]+"x"+d.image.image_wh[1]
      +(d.frame_wh?(" · frame "+d.frame_wh[0]+"x"+d.frame_wh[1]):" · frame size unknown");
    var a=d.image.image_age_s;
    $("agepill").textContent=a==null?"—":(a<90?Math.round(a)+"s old":Math.round(a/60)+"m old");
    $("agepill").className="pill"+(d.stale?" warn":"");
    if(d.image.scaled){
      w.push("This image is <b>rescaled</b> ("+d.image.image_wh[0]+"px wide vs a "+(d.frame_wh?d.frame_wh[0]:"?")+"px frame). Boxes are converted to frame px, but one drawn pixel is more than one frame pixel — for a tight panel box, prefer a native frame from <code>door_calib --frames 1</code>.");
    }
    if(!d.image.convertible){
      w.push("<b>Cannot save:</b> the image is rescaled and the true frame size is unknown. Run <code>door_calib --frames 1</code> once, then reload.");
    }
    if(d.image.annotated){
      w.push("This frame already has a grid and the <b>current</b> ROI boxes drawn on it by door_calib. Those green/blue rectangles are the EXISTING geometry (for a new camera they are the built-in defaults from another lift) — not yours. Yours appear as you drag.");
    }
    if(d.stale){w.push("Frame is older than "+Math.round(d.fresh_s/60)+" min"+(d.snapshot_available?" — Refresh pulls the live snapshot.":"."));}
    $("warnings").innerHTML=w.length?('<div class=warnbox>'+w.join("<br><br>")+'</div>'):"";
    $("srcnote").innerHTML=d.image.source==="calib_frame"
      ? "drawing on the native door_calib frame (1 image px = 1 frame px)"
      : "drawing on a copy of the live snapshot";
    var s=d.saved||{};
    $("savedAt").textContent=s.saved_at||"never";
    if(s.door_roi_frame&&!R.door&&!R.p0){       // pre-load what is on disk so redraw is a correction
      R.door=s.door_roi_frame;
      if(s.panel_rois&&s.panel_rois[0])R.p0=s.panel_rois[0];
      if(s.panel_rois&&s.panel_rois[1])R.p1=s.panel_rois[1];
    }
    $("frame").onload=function(){applyZoom()};
    if($("frame").complete)applyZoom();
  }).catch(function(){$("src").textContent="state failed"});
}
setMode("door"); load();
</script></body></html>"""
