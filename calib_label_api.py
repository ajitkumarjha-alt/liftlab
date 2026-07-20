"""Labeling wizard — /calib-label/{gw}/{cam}. The FIRST web wizard piece.

The operator labels collected crops ONE at a time (chat-screenshot reading doesn't scale to 121).
Labels persist to /var/lib/liftlab/calib/{gw}/{cam}/labels.json keyed by crop FILENAME (NOT index —
indices shift when crops append). Autosave on every entry. door_calib --build reads labels.json when
--labels isn't given (crops missing a label or marked '-' are excluded).

This module is DELIBERATELY light — reads/writes files + lists crops only, NO cv2, NO model, NO OCR
prefill (the model doesn't exist yet; these labels are what build it). The crop images are served by
ops_api's existing /calib/{gw}/{cam}/{fname} route, so this page just points <img> at it and enlarges
with CSS. Behind the SAME Caddy basicauth as /calib (a human page, not a token API).

Routes:
  GET  /calib-label/{gw}/{cam}         the wizard page
  GET  /calib-label/{gw}/{cam}/state   JSON: crop filenames + current labels + counts
  POST /calib-label/{gw}/{cam}/label   {crop, label} -> validate + autosave to labels.json
"""
import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
# Grammar: a floor string (alnum: digits AND letters — G, P2, MEP) + an OPTIONAL trailing arrow ^/v/V;
# or '-' alone to SKIP (excluded from the build). Same shape door_calib._label_to_glyphs parses.
LABEL_RE = re.compile(r"^(-|[A-Za-z0-9]+[\^vV]?)$")
LABEL_HELP = "floor + optional ^/v — e.g. 35v, 31^, 16, G, P2, MEP^ ; or '-' to skip"

calib_label_router = APIRouter()


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _dir(gw, cam):
    _safe(gw, cam)
    return CALIB_DIR / gw / cam


def _crops(d):
    return sorted(p.name for p in d.glob("_calib_crop_*.png")) if d.exists() else []


def _labels_path(d):
    return d / "labels.json"


def _load_labels(d):
    p = _labels_path(d)
    if p.exists():
        try:
            v = json.loads(p.read_text())
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def _save_labels(d, labels):
    d.mkdir(parents=True, exist_ok=True)
    tmp = _labels_path(d).with_suffix(".json.tmp")          # atomic: write temp then replace
    tmp.write_text(json.dumps(labels, indent=2, sort_keys=True))
    os.replace(tmp, _labels_path(d))


@calib_label_router.get("/calib-label/{gw}/{cam}/state")
def label_state(gw: str, cam: str):
    d = _dir(gw, cam)
    crops = _crops(d)
    labels = {k: v for k, v in _load_labels(d).items() if k in set(crops)}   # drop labels for gone crops
    labeled = sum(1 for c in crops if labels.get(c))
    return JSONResponse({"gw": gw, "cam": cam, "crops": crops, "labels": labels,
                         "n_total": len(crops), "n_labeled": labeled, "help": LABEL_HELP})


@calib_label_router.post("/calib-label/{gw}/{cam}/label")
async def save_label(gw: str, cam: str, request: Request):
    d = _dir(gw, cam)
    body = await request.json()
    crop = str(body.get("crop", ""))
    label = str(body.get("label", "")).strip()
    _safe(crop)
    if crop not in set(_crops(d)):
        raise HTTPException(404, "no such crop")
    if not LABEL_RE.match(label):
        raise HTTPException(400, f"invalid label — {LABEL_HELP}")
    labels = _load_labels(d)
    labels[crop] = label
    _save_labels(d, labels)
    labeled = sum(1 for c in _crops(d) if labels.get(c))
    return {"ok": True, "crop": crop, "label": label, "n_labeled": labeled}


@calib_label_router.get("/calib-label/{gw}/{cam}", response_class=HTMLResponse)
def label_page(gw: str, cam: str):
    _safe(gw, cam)
    return HTMLResponse(_PAGE.replace("__GW__", gw).replace("__CAM__", cam).replace("__HELP__", LABEL_HELP))


_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Label __CAM__ @ __GW__</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#e3e8ec;--fg:#1c2429;--mut:#6b7a84;--ok:#2f9e5f;--bad:#d4483b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e1418;--card:#161d22;--line:#243038;--fg:#d6dee3;--mut:#7f9099}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
h1{font:600 15px var(--mono);margin:0}
.pill{font:11px var(--mono);padding:2px 8px;border-radius:10px;background:var(--line);color:var(--mut)}
.wrap{max-width:520px;margin:16px auto;padding:0 14px;text-align:center}
.imgbox{background:#000;border:1px solid var(--line);border-radius:10px;padding:10px;display:inline-block}
img{image-rendering:pixelated;height:62vh;max-height:560px;width:auto;display:block}
.fn{font:11px var(--mono);color:var(--mut);margin:8px 0 2px}
.row{display:flex;gap:8px;margin-top:12px}
input{flex:1;font:600 20px var(--mono);text-align:center;padding:10px;border:2px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);text-transform:uppercase}
input:focus{outline:none;border-color:#4c8bf5}
button{font:600 14px system-ui;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer}
button.pri{background:#2f9e5f;border-color:#2f9e5f;color:#fff}
button:disabled{opacity:.4;cursor:default}
.err{color:var(--bad);font:12px var(--mono);min-height:16px;margin-top:6px}
.help{color:var(--mut);font:12px var(--mono);margin-top:10px}
.done{color:var(--ok);font:600 13px var(--mono)}
kbd{font:11px var(--mono);background:var(--line);border-radius:4px;padding:1px 5px}
</style></head><body>
<header>
  <h1>label · __CAM__ @ __GW__</h1>
  <span class=pill id=pos>—</span>
  <span class=pill id=prog>—</span>
  <span class=pill id=doneflag></span>
</header>
<div class=wrap>
  <div class=imgbox><img id=crop alt="crop"></div>
  <div class=fn id=fn>—</div>
  <div class=row>
    <button id=back title="previous">◀ Back</button>
    <input id=inp autocomplete=off autocapitalize=characters spellcheck=false placeholder="floor e.g. 16 / 35v / MEP^">
    <button id=skip title="exclude this crop">Skip</button>
    <button id=save class=pri>Save + Next ⏎</button>
  </div>
  <div class=err id=err></div>
  <div class=help>__HELP__ &nbsp;·&nbsp; <kbd>Enter</kbd> save+next &nbsp; <kbd>←</kbd> back</div>
</div>
<script>
var GW="__GW__", CAM="__CAM__";
var RE=/^(-|[A-Za-z0-9]+[\^vV]?)$/;
var crops=[], labels={}, idx=0;
var $=function(id){return document.getElementById(id)};
function counts(){return crops.filter(function(c){return labels[c]}).length}
function render(){
  if(!crops.length){$("fn").textContent="no crops collected yet — run door_calib --collect";$("pos").textContent="0/0";return;}
  var f=crops[idx];
  $("crop").src="/calib/"+GW+"/"+CAM+"/"+f+"?t="+Date.now();
  $("fn").textContent=f;
  $("inp").value=labels[f]||"";
  $("pos").textContent="crop "+(idx+1)+"/"+crops.length;
  var n=counts();
  $("prog").textContent="labeled "+n+"/"+crops.length;
  $("doneflag").textContent=(n===crops.length)?"ALL DONE ✓":"";
  $("doneflag").className=(n===crops.length)?"pill done":"pill";
  $("back").disabled=(idx===0);
  $("err").textContent="";
  $("inp").focus(); $("inp").select();
}
function showErr(m){$("err").textContent=m}
function save(advance){
  var f=crops[idx], v=$("inp").value.trim().replace(/\s+/g,"");
  if(!RE.test(v)){showErr("invalid — "+"__HELP__");return;}
  fetch("/calib-label/"+GW+"/"+CAM+"/label",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({crop:f,label:v})}).then(function(r){
      return r.json().then(function(j){return {ok:r.ok,j:j}});
    }).then(function(res){
      if(!res.ok){showErr((res.j&&res.j.detail)||"save failed");return;}
      labels[f]=v;
      if(advance && idx<crops.length-1){idx++;}
      render();
    }).catch(function(){showErr("network error — not saved");});
}
function back(){if(idx>0){idx--;render();}}
function skip(){$("inp").value="-";save(true);}
$("save").onclick=function(){save(true)};
$("back").onclick=back;
$("skip").onclick=skip;
$("inp").addEventListener("keydown",function(e){
  if(e.key==="Enter"){e.preventDefault();save(true);}
});
document.addEventListener("keydown",function(e){
  if(e.key==="ArrowLeft" && document.activeElement!==$("inp")){back();}
});
fetch("/calib-label/"+GW+"/"+CAM+"/state").then(function(r){return r.json()}).then(function(s){
  crops=s.crops||[]; labels=s.labels||{};
  var first=crops.findIndex(function(c){return !labels[c]});   // resume at first unlabeled
  idx=first>=0?first:0;
  render();
}).catch(function(){$("fn").textContent="failed to load crops";});
</script></body></html>"""
