"""
Operator view: live snapshot grid + Pi/relay health on ONE self-updating page. ADDITIVE.

The relay ships HEVC to /dev/shm/liftlab-live (unplayable in Chrome), so we show SNAPSHOTS: a
separate niced process (snapshot.py) decodes one frame per new segment into /run/liftlab-snap
(RAM, NOT the capped relay store). This module serves those + relay/watch health.

Pi-facing (Bearer):
  POST /api/gw/{gw}/relay_status      relay_soak.sh posts its metrics (does not touch the watch)
Operator (basicauth via Caddy):
  GET  /ops/{gw}                      the one page (grid + health, self-updating)
  GET  /ops/{gw}/data                 JSON: watch + relay latest + short series
  GET  /ops/{gw}/snapmeta            JSON: per-cam snapshot freshness
  GET  /snap/{gw}/{cam}.jpg          the latest decoded frame
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
SNAP_DIR = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
GATEWAY_TOKENS = {
    g.split(":", 1)[0]: g.split(":", 1)[1]
    for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g
}
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

ops_router = APIRouter()


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS relay_status (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
      sum_delivered_mbps REAL, streams_alive INTEGER, streams_delivering INTEGER,
      ff_cpu REAL, soc_temp REAL, throttle_live TEXT, mem_avail_mb INTEGER,
      door_fps REAL, guard_trips INTEGER, per_stream TEXT)""")
    return db


def _auth(gw: str, authorization: str) -> None:
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if not tok or GATEWAY_TOKENS.get(gw) != tok:
        raise HTTPException(401, "bad gateway token")


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


# ---------------- Pi-facing: relay metrics ingest ----------------
@ops_router.post("/api/gw/{gw}/relay_status")
async def relay_status_ingest(gw: str, request: Request, authorization: str = Header("")):
    _auth(gw, authorization)
    d = await request.json()
    db = _db()
    db.execute(
        "INSERT INTO relay_status (gateway_id,ts,sum_delivered_mbps,streams_alive,streams_delivering,"
        "ff_cpu,soc_temp,throttle_live,mem_avail_mb,door_fps,guard_trips,per_stream) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (gw, time.time(), d.get("sum_delivered_mbps"), d.get("streams_alive"), d.get("streams_delivering"),
         d.get("ff_cpu"), d.get("soc_temp"), d.get("throttle_live"), d.get("mem_avail_mb"),
         d.get("door_fps"), d.get("guard_trips", 0), json.dumps(d.get("per_stream", {}))))
    db.execute("DELETE FROM relay_status WHERE gateway_id=? AND id NOT IN "
               "(SELECT id FROM relay_status WHERE gateway_id=? ORDER BY id DESC LIMIT 720)", (gw, gw))
    db.commit()
    db.close()
    return {"ok": True}


# ---------------- Operator: snapshots ----------------
@ops_router.get("/snap/{gw}/{cam}.jpg")
def snap_jpg(gw: str, cam: str):
    _safe(gw, cam)
    f = SNAP_DIR / gw / f"{cam}.jpg"
    if not f.exists():
        raise HTTPException(404, "no snapshot yet")
    return Response(f.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, no-cache, max-age=0"})


SNAP_STALE_S = float(os.environ.get("SNAP_STALE_S", "20"))   # NVR->Pi->2s HLS cut->upload->decode
                                                            # is 6-12s normally; only flag past 20s


@ops_router.get("/ops/{gw}/snapmeta")
def snapmeta(gw: str):
    _safe(gw)
    d = SNAP_DIR / gw
    now = time.time()
    cams = {}
    if d.exists():
        for jpg in sorted(d.glob("*.jpg")):
            if jpg.stem == "zonecheck" or jpg.stem.startswith("_"):
                continue                            # verification image, not a live stream
            age = now - jpg.stat().st_mtime
            cams[jpg.stem] = {"age_s": round(age, 1), "stale": age > SNAP_STALE_S}
    return {"cams": cams, "t": now}


# ---------------- Operator: combined health data ----------------
def _row(db, table, gw, cols="*"):
    try:
        r = db.execute(f"SELECT {cols} FROM {table} WHERE gateway_id=? ORDER BY id DESC LIMIT 1", (gw,)).fetchone()
        return dict(r) if r else None
    except sqlite3.OperationalError:
        return None


def _series(db, table, cols, gw, n=120):
    try:
        rows = db.execute(f"SELECT {cols} FROM {table} WHERE gateway_id=? ORDER BY id DESC LIMIT ?", (gw, n)).fetchall()
        return [dict(r) for r in reversed(rows)]
    except sqlite3.OperationalError:
        return []


@ops_router.get("/ops/{gw}/data")
def ops_data(gw: str):
    _safe(gw)
    db = _db()
    watch = _row(db, "watch_status", gw)
    relay = _row(db, "relay_status", gw)
    if relay and relay.get("per_stream"):
        try:
            relay["per_stream"] = json.loads(relay["per_stream"])
        except Exception:
            relay["per_stream"] = {}
    transit = None
    try:
        day0 = time.time() - 86400
        tc = {r["direction"]: r["c"] for r in db.execute(
            "SELECT direction, COUNT(*) c FROM transit_event WHERE gateway_id=? AND ts>=? GROUP BY direction",
            (gw, day0)).fetchall()}
        last = db.execute("SELECT ts FROM transit_event WHERE gateway_id=? ORDER BY id DESC LIMIT 1", (gw,)).fetchone()
        transit = {"boarded": tc.get("in", 0), "alighted": tc.get("out", 0),
                   "last_ts": last["ts"] if last else None}
    except sqlite3.OperationalError:
        pass                                       # transit_event not created yet (analysis not deployed)
    validation = {}
    try:
        for r in db.execute("SELECT cam,state,n_reviewed,n_exact,provenance FROM camera_validation "
                            "WHERE gateway_id=?", (gw,)).fetchall():
            validation[r["cam"]] = dict(r)
    except sqlite3.OperationalError:
        pass                                       # camera_validation not created yet
    analyzer = None
    try:
        a = db.execute("SELECT * FROM analyzer_status WHERE gateway_id=? ORDER BY ts DESC LIMIT 1", (gw,)).fetchone()
        analyzer = dict(a) if a else None
    except sqlite3.OperationalError:
        pass                                       # analyzer never reported (GPU down or not deployed)
    out = {
        "t": time.time(),
        "watch": watch,
        "relay": relay,
        "transit": transit,
        "validation": validation,
        "analyzer": analyzer,
        "relay_series": _series(db, "relay_status", "ts,sum_delivered_mbps,soc_temp,door_fps,streams_delivering", gw),
        "watch_series": _series(db, "watch_status", "ts,signal_fps,soc_temp", gw),
    }
    db.close()
    return out


# ---------------- Operator: the page ----------------
_PAGE = r"""<!doctype html><meta charset=utf-8><title>ops · __GW__</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#e3e8ec;--fg:#1c2429;--mut:#6b7a84;--ok:#2f9e5f;--warn:#d98a1f;--bad:#d4483b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline;background:var(--card)}
h1{font-size:16px;margin:0} .sub{font:12px var(--mono);color:var(--mut)} .wrap{max-width:1200px;margin:0 auto;padding:16px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:20px 4px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.tile{background:#000;border:1px solid var(--line);border-radius:8px;overflow:hidden;position:relative;aspect-ratio:16/10}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.tile .lab{position:absolute;top:0;left:0;right:0;display:flex;justify-content:space-between;padding:5px 8px;font:11px var(--mono);color:#fff;background:linear-gradient(#000a,#0000)}
.tile .st{position:absolute;bottom:0;left:0;right:0;padding:4px 8px;font:11px var(--mono);color:#cfe;background:linear-gradient(#0000,#000a)}
.tile.stale{filter:grayscale(1) brightness(.55)} .tile.stale .st{color:#f7b}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.kv{display:flex;justify-content:space-between;gap:10px;padding:2px 0;font:12px var(--mono)} .kv b{font-weight:600}
.big{font:600 22px var(--mono)} .pill{font:11px var(--mono);padding:1px 7px;border-radius:10px;background:#eef2f4;color:var(--mut)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.ps{font:11px var(--mono);display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:2px 10px}
#stamp{font:11px var(--mono);color:var(--mut)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1418;--card:#161d22;--line:#243038;--fg:#d6dee3;--mut:#7f9099}.pill{background:#1c262c}}
</style>
<header><h1>liftlab ops · __GW__</h1><span class=sub id=stamp>loading…</span></header>
<div class=wrap>
  <h2>Live cameras <span class=pill id=camcount></span></h2>
  <div class=grid id=grid></div>
  <h2>GPU analyzer</h2><div class=cards id=analyzer></div>
  <h2>Transit (GPU) <span class=pill>boarded / alighted</span></h2><div class=cards id=transit></div>
  <h2>Door watch</h2><div class=cards id=watch></div>
  <h2>Relay</h2><div class=cards id=relay></div>
</div>
<script>
var GW="__GW__";
function esc(s){return (s==null?'':(''+s))}
function cls(v,warn,bad,inv){if(v==null)return'';v=+v;if(inv)return v<=bad?'bad':v<=warn?'warn':'ok';return v>=bad?'bad':v>=warn?'warn':'ok'}
var cams=[];
function drawGrid(meta){
  var g=document.getElementById('grid'),names=Object.keys(meta.cams).sort();
  document.getElementById('camcount').textContent=names.length+' cams';
  if(names.join()!==cams.join()){
    cams=names; g.innerHTML='';
    cams.forEach(function(c){
      g.insertAdjacentHTML('beforeend','<div class=tile id="t_'+c+'"><img alt="'+c+'"><div class=lab><span>'+c+'</span><span class=age></span></div><div class=st></div></div>');
    });
  }
  cams.forEach(function(c){
    var t=document.getElementById('t_'+c),m=meta.cams[c];
    t.querySelector('img').src='/snap/'+GW+'/'+c+'.jpg?t='+Date.now();
    t.querySelector('.age').textContent=m.age_s+'s';
    t.querySelector('.st').textContent=m.stale?('STALE — no segment '+m.age_s+'s'):('live · '+m.age_s+'s ago');
    t.classList.toggle('stale',m.stale);
  });
}
function tickGrid(){fetch('/ops/'+GW+'/snapmeta').then(function(r){return r.json()}).then(drawGrid).catch(function(){});}
function kv(k,v,c){return '<div class=kv><span>'+k+'</span><b class="'+(c||'')+'">'+esc(v)+'</b></div>'}
function rejcard(an){
  // Distribution of would-be crossings the guards KILLED, by the frac of the gap the foot achieved.
  // Lets disp_frac (guard=0.35) be chosen from data: amber buckets .20–.35 are crossings that would be
  // RECOVERED by lowering the threshold; the .35+ bucket is real crossings lost to the dwell guard.
  if(an.rej_disp==null&&an.rej_dwell==null)return '';           // pre-rejection-telemetry worker
  var parts=(an.rej_hist||'').split(',').map(function(x){return +x||0});
  while(parts.length<8)parts.push(0);
  var mx=Math.max.apply(null,parts.concat([1]));
  var labs=['.00','.05','.10','.15','.20','.25','.30','.35+'];
  var bars=parts.map(function(c,i){
    var h=Math.round(4+40*c/mx);
    var col=i>=7?'#8a8a8a':(i>=4?'#e0a800':'#c0392b');         // <.20 red (flicker), .20–.35 amber (recoverable), .35+ grey (dwell-lost)
    return '<div style="display:flex;flex-direction:column;align-items:center;justify-content:flex-end">'
      +'<span style="font-size:10px;line-height:1">'+c+'</span>'
      +'<div style="width:15px;height:'+h+'px;background:'+col+';margin-top:2px"></div>'
      +'<span style="font-size:9px;opacity:.65;margin-top:1px">'+labs[i]+'</span></div>';
  }).join('');
  return '<div class=card><h3>rejected crossings</h3>'
    +'<div style="font-size:11px;opacity:.75;margin-bottom:4px">frac of gap achieved (guard=0.35)</div>'
    +'<div style="display:flex;gap:3px;align-items:flex-end;height:66px">'+bars+'</div>'
    +kv('displacement / dwell',esc(an.rej_disp)+' / '+esc(an.rej_dwell))+'</div>';
}
function drawData(d){
  document.getElementById('stamp').textContent='updated '+new Date().toLocaleTimeString();
  var w=d.watch||{},r=d.relay||{},tr=d.transit,val=d.validation||{},an=d.analyzer;
  var anhtml;
  if(!an){anhtml='<div class=card><h3>GPU analyzer</h3><div class="big bad">no heartbeat</div>'
    +'<div class=kv><span>never reported — worker down or not deployed</span></div></div>';}
  else{var hbage=Math.round(d.t-an.ts),down=hbage>120,ttage=an.last_transit_ts?Math.round(d.t-an.last_transit_ts):null;
    anhtml='<div class=card><h3>GPU analyzer</h3><div class="big '+(down?'bad':'ok')+'">'+(down?'DOWN':'up')+'</div>'
      +kv('heartbeat',hbage+'s ago',cls(hbage,60,120))
      +kv('counting',esc(an.counting_version))+kv('mode',esc(an.mode))
      +kv('segments / dropped',esc(an.segments)+' / '+esc(an.dropped))
      +kv('last transit',ttage==null?'—':ttage+'s ago')
      +kv('uptime',an.uptime_s?Math.round(an.uptime_s/60)+'m':'—')+'</div>'
      +rejcard(an);}
  document.getElementById('analyzer').innerHTML=anhtml;
  var vcards=Object.keys(val).sort().map(function(cam){var v=val[cam];
    var pct=v.n_reviewed?Math.round(100*v.n_exact/v.n_reviewed):0;
    var prov=v.provenance||(v.n_reviewed?(v.n_reviewed+' reviewed, '+pct+'% exact'):'not reviewed');
    return '<div class=card><h3>'+cam+' provenance</h3>'
      +'<div class="big '+(v.state==='live'?'ok':'warn')+'">'+esc(v.state)+'</div>'
      +kv('precision',prov)+(v.state!=='live'?'<div class=kv><a href="/validate">review →</a></div>':'')+'</div>';}).join('');
  document.getElementById('transit').innerHTML = (tr ?
    ('<div class=card><h3>boarded (24h)</h3><div class="big ok">'+esc(tr.boarded)+'</div></div>'
    +'<div class=card><h3>alighted (24h)</h3><div class=big>'+esc(tr.alighted)+'</div></div>'
    +'<div class=card><h3>last transit</h3>'+kv('ago',tr.last_ts?Math.round(d.t-tr.last_ts)+'s':'—')
    +kv('source','GPU L4')+'</div>')
    : '<div class=card><h3>transit</h3><div class=kv><span>GPU analyzer not reporting yet</span></div></div>') + vcards;
  var age=w.ts?Math.round(d.t-w.ts):null;
  document.getElementById('watch').innerHTML=
    '<div class=card><h3>state</h3><div class=big>'+esc(w.state||'—')+'</div>'
    +kv('baseline',esc(w.baseline_source)+(w.baseline_confirmed?' ✓':''))
    +kv('watch_status age',age==null?'—':age+'s',cls(age,30,60))+'</div>'
    +'<div class=card><h3>signal</h3>'
    +kv('signal_fps',w.signal_fps==null?'—':(+w.signal_fps).toFixed(2),cls(w.signal_fps,8,6,true))
    +kv('latency med/max',(w.latency_med_s!=null?(+w.latency_med_s).toFixed(2):'—')+' / '+(w.latency_max_s!=null?(+w.latency_max_s).toFixed(2):'—'))
    +kv('cycles_emitted',esc(w.cycles_emitted))+kv('samples',esc(w.samples))+'</div>'
    +'<div class=card><h3>pi</h3>'
    +kv('soc_temp',w.soc_temp!=null?(+w.soc_temp).toFixed(1)+'°C':'—',cls(w.soc_temp,70,80))
    +kv('throttle live',esc(w.throttle_live)||'none',(w.throttle_live&&w.throttle_live!=='none'&&w.throttle_live!=='[]')?'bad':'ok')
    +kv('throttle sticky',esc(w.throttle_sticky)||'none')
    +kv('mem_avail',esc(w.mem_avail_mb)+' MB')+kv('rss',esc(w.rss_mb)+' MB')+'</div>';
  var rage=r.ts?Math.round(d.t-r.ts):null;
  var ps=r.per_stream||{},psh=Object.keys(ps).sort().map(function(c){return '<span>'+c+' '+ps[c]+'k</span>'}).join('');
  document.getElementById('relay').innerHTML=
    '<div class=card><h3>delivery</h3>'
    +'<div class=big>'+(r.streams_delivering==null?'—':r.streams_delivering)+'/'+esc(r.streams_alive)+'</div>'
    +kv('sum delivered',r.sum_delivered_mbps!=null?(+r.sum_delivered_mbps).toFixed(2)+' Mbps':'—')
    +kv('relay_status age',rage==null?'— (no relay data)':rage+'s',cls(rage,45,120))+'</div>'
    +'<div class=card><h3>per-stream kbps</h3><div class=ps>'+(psh||'—')+'</div></div>'
    +'<div class=card><h3>relay pi</h3>'
    +kv('ff_cpu',r.ff_cpu!=null?(+r.ff_cpu).toFixed(0)+'%':'—')
    +kv('door_fps',r.door_fps!=null?(+r.door_fps).toFixed(2):'—',cls(r.door_fps,8,6,true))
    +kv('soc_temp',r.soc_temp!=null?(+r.soc_temp).toFixed(1)+'°C':'—',cls(r.soc_temp,70,80))
    +kv('guard_trips',esc(r.guard_trips),r.guard_trips>0?'bad':'')+'</div>';
}
function tickData(){fetch('/ops/'+GW+'/data').then(function(r){return r.json()}).then(drawData).catch(function(){});}
tickGrid();tickData();setInterval(tickGrid,2000);setInterval(tickData,15000);
</script>"""


@ops_router.get("/ops/{gw}", response_class=HTMLResponse)
def ops_page(gw: str):
    _safe(gw)
    return HTMLResponse(_PAGE.replace("__GW__", gw))
