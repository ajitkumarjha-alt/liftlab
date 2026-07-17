"""
/dash — the ONE page. Tabbed by camera + an always-on top strip, auto-refresh.

ADDITIVE and READ-ONLY: aggregates data that already lives across the other routers'
tables (channel_map, gw_event/gw_source, transit_event, analyzer_status,
camera_validation, watch_status, relay_status) into one view. It writes nothing and
creates no tables. /ops, /events, /validate, /pihealth stay as the deep views; /dash
links out to them.

The point of the project is one panel: the sheet ASSUMPTION sitting beside the
OBSERVATION, same eyeline, no verdict — "ch29 door close: observed median 2.81s
(p85 6.44s, n=1012) · sheet assumes 2.00s · non-compliant above 2.31s · 80% of
observed closes exceed 2.31s". Facts side by side; the reader draws the conclusion.

HONEST BLANKS + STALENESS: a camera with no analyser says "not analysed — relay
only"; a stale panel says stale rather than showing an old number as if it's live.
Every panel carries its own timestamp.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")
SNAP_DIR = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
SNAP_STALE_S = float(os.environ.get("SNAP_STALE_S", "20"))
HB_STALE_S = 120.0                          # analyzer heartbeat older than this = down
IST = timezone(timedelta(hours=5, minutes=30))   # the building's clock; door ts are +05:30 local ISO

# Door-close compliance spec per camera (from the sheet). observed vs assumption side by side.
# Only cameras with an entry get the headline compliance panel; others show observed-only.
DOOR_SPECS = {
    "ch29": {"sheet_s": 2.00, "compliance_s": 2.31, "bank": "C"},
}

# Fixed lift-camera fallback if channel_map is empty (the set the operator named).
FALLBACK_CHANNELS = [16, 27, 29, 30, 32, 34, 37]

dash_router = APIRouter()


def _db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def _q(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []                            # table not created yet -> honest empty, never a 500


def _ist_today_str():
    return datetime.now(IST).date().isoformat()          # 'YYYY-MM-DD' (local ISO date-prefix)


def _ist_today_epoch():
    d = datetime.now(IST).date()
    return datetime(d.year, d.month, d.day, tzinfo=IST).timestamp()


def _pctl(sorted_vals, q):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def _cameras(db, gw):
    rows = _q(db, "SELECT channel, label, is_lift FROM channel_map WHERE gateway_id=? ORDER BY channel", (gw,))
    lifts = [(r["channel"], r["label"]) for r in rows if r["is_lift"]]
    if not lifts:                            # channel_map unmarked -> the named fallback set
        lifts = [(c, None) for c in FALLBACK_CHANNELS]
    return [{"cam": f"ch{c}", "channel": c, "label": lbl} for c, lbl in lifts]


def _snap(cam, gw):
    f = SNAP_DIR / gw / f"{cam}.jpg"
    try:
        age = time.time() - f.stat().st_mtime
        return {"age_s": round(age, 1), "stale": age > SNAP_STALE_S}
    except Exception:
        return None                          # no snapshot decoded yet


# ---- close-travel histogram: 0-1,1-2,...,6-8,8+ seconds ----
_HIST_EDGES = [1, 2, 3, 4, 5, 6, 8]


def _hist(vals):
    h = [0] * (len(_HIST_EDGES) + 1)
    for v in vals:
        placed = False
        for i, e in enumerate(_HIST_EDGES):
            if v < e:
                h[i] += 1; placed = True; break
        if not placed:
            h[-1] += 1
    return h


def _door_by_cam(db, gw):
    """One pass over gw_event -> per-camera door stats. 'Clean' close = quality NULL/'ok' AND
    close_travel_s present (the /events headline definition; the read-time duplicate withhold and full
    breakdown live on /events, linked from the panel)."""
    rows = _q(db, "SELECT s.camera cam, e.close_travel_s ct, e.door_open_start_ts os, e.quality q "
                  "FROM gw_event e JOIN gw_source s ON s.id = e.source_id WHERE s.gateway_id=?", (gw,))
    today = _ist_today_str()
    by = {}
    for r in rows:
        d = by.setdefault(r["cam"], {"total": 0, "today": 0, "cts": [], "last": None})
        d["total"] += 1
        os_ = r["os"] or ""
        if os_ >= today:
            d["today"] += 1
        if d["last"] is None or os_ > d["last"]:
            d["last"] = os_
        if r["ct"] is not None and (r["q"] is None or r["q"] == "ok"):
            d["cts"].append(float(r["ct"]))
    out = {}
    for cam, d in by.items():
        cts = sorted(d["cts"])
        n = len(cts)
        med = _pctl(cts, 0.5)
        p85 = _pctl(cts, 0.85)
        spec = DOOR_SPECS.get(cam)
        spec_out = None
        if spec and cts:
            over = sum(1 for v in cts if v > spec["compliance_s"])
            spec_out = {**spec, "pct_exceed": round(100.0 * over / n)}
        out[cam] = {
            "total": d["total"], "today": d["today"], "last_open": d["last"],
            "n": n, "median": round(med, 2) if med is not None else None,
            "p85": round(p85, 2) if p85 is not None else None,
            "min": round(cts[0], 2) if cts else None, "max": round(cts[-1], 2) if cts else None,
            "hist": _hist(cts), "hist_edges": _HIST_EDGES, "spec": spec_out,
        }
    return out


def _transit_by_cam(db, gw):
    today = _ist_today_epoch()
    rows = _q(db, "SELECT cam, direction, ts FROM transit_event WHERE gateway_id=?", (gw,))
    by = {}
    for r in rows:
        d = by.setdefault(r["cam"], {"bt": 0, "at": 0, "b": 0, "a": 0, "last": None})
        ins = r["direction"] == "in"
        d["b" if ins else "a"] += 1
        if r["ts"] and r["ts"] >= today:
            d["bt" if ins else "at"] += 1
        if r["ts"] and (d["last"] is None or r["ts"] > d["last"]):
            d["last"] = r["ts"]
    return {cam: {"boarded_today": d["bt"], "alighted_today": d["at"],
                  "boarded_total": d["b"], "alighted_total": d["a"], "last_ts": d["last"]}
            for cam, d in by.items()}


def _analyzers(db, gw):
    rows = _q(db, "SELECT * FROM analyzer_status WHERE gateway_id=?", (gw,))
    now = time.time()
    return {r["cam"]: {**dict(r), "age_s": round(now - (r["ts"] or 0), 1),
                       "up": (now - (r["ts"] or 0)) < HB_STALE_S} for r in rows}


def _validations(db, gw):
    rows = _q(db, "SELECT cam,state,n_reviewed,n_exact,provenance,counting_version "
                  "FROM camera_validation WHERE gateway_id=?", (gw,))
    out = {}
    for r in rows:
        nr, ne = r["n_reviewed"] or 0, r["n_exact"] or 0
        out[r["cam"]] = {"state": r["state"], "n_reviewed": nr, "n_exact": ne,
                         "precision": round(100.0 * ne / nr) if nr else None,
                         "counting_version": r["counting_version"], "provenance": r["provenance"]}
    return out


def _latest(db, table, gw):
    rows = _q(db, f"SELECT * FROM {table} WHERE gateway_id=? ORDER BY id DESC LIMIT 1", (gw,))
    return dict(rows[0]) if rows else None


@dash_router.get("/dash/{gw}/data")
def dash_data(gw: str):
    db = _db()
    now = time.time()
    cams = _cameras(db, gw)
    door = _door_by_cam(db, gw)
    trans = _transit_by_cam(db, gw)
    ana = _analyzers(db, gw)
    val = _validations(db, gw)
    w = _latest(db, "watch_status", gw)
    r = _latest(db, "relay_status", gw)
    db.close()

    # ---- top strip: PI ----
    pi = None
    if w:
        oe, ce = w.get("opens_detected"), w.get("cycles_emitted")
        pi = {"ts": w.get("ts"), "age_s": round(now - (w.get("ts") or 0), 1),
              "state": w.get("state"), "signal_fps": w.get("signal_fps"),
              "soc_temp": w.get("soc_temp"), "throttle_live": w.get("throttle_live"),
              "opens_detected": oe, "cycles_emitted": ce,
              "detect_emit": round(oe / ce, 2) if oe and ce else None,
              "camera": w.get("camera")}
    # ---- top strip: RELAY ----
    relay = None
    if r:
        relay = {"ts": r.get("ts"), "age_s": round(now - (r.get("ts") or 0), 1),
                 "streams_delivering": r.get("streams_delivering"),
                 "sum_delivered_mbps": r.get("sum_delivered_mbps")}
    # ---- top strip: GPU (aggregate across cams) ----
    up = [c for c, a in ana.items() if a["up"]]
    gpu = {"any_up": bool(up), "cams_up": sorted(up),
           "worst": None}
    if ana:
        # show the busiest/worst analyser's throughput+drop as the fleet indicator
        worst = max(ana.values(), key=lambda a: (a.get("drop_frac") or 0, a.get("proc_ms") or 0))
        gpu["worst"] = {"proc_ms": worst.get("proc_ms"), "budget_ms": worst.get("seg_budget_ms"),
                        "drop_frac": worst.get("drop_frac"), "age_s": worst["age_s"]}

    # ---- per-camera assembly (honest blanks) ----
    out_cams = []
    for c in cams:
        cam = c["cam"]
        a = ana.get(cam)
        out_cams.append({
            **c,
            "snap": _snap(cam, gw),
            "door": door.get(cam),
            "transit": (dict(trans[cam], source=(a or {}).get("counting_version")) if cam in trans else None),
            "analyzer": (None if a is None else
                         {"up": a["up"], "age_s": a["age_s"], "mode": a.get("mode"),
                          "counting_version": a.get("counting_version"),
                          "proc_ms": a.get("proc_ms"), "budget_ms": a.get("seg_budget_ms"),
                          "drop_frac": a.get("drop_frac")}),
            "validation": val.get(cam),
        })

    headline = [dict(door[cam]["spec"], cam=cam, median=door[cam]["median"],
                     p85=door[cam]["p85"], n=door[cam]["n"])
                for cam in door if door[cam].get("spec")]

    return JSONResponse({"t": now, "gw": gw, "ist_today": _ist_today_str(),
                         "pi": pi, "relay": relay, "gpu": gpu,
                         "cameras": out_cams, "headline": headline})


@dash_router.get("/dash", response_class=HTMLResponse)
def dash_page():
    return _PAGE.replace("__GW__", os.environ.get("DASH_GW", "site-A"))


_PAGE = r"""<!doctype html><meta charset=utf-8><title>liftlab · dash</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--mono:ui-monospace,Consolas,monospace;--b:#e2e2e2}
*{box-sizing:border-box}
body{background:#fafafa;color:#1a1a1a;font:14px system-ui;max-width:1080px;margin:auto;padding:14px}
h1{font-size:13px;letter-spacing:.18em;text-transform:uppercase;color:#333;margin:0 0 8px}
a{color:#0a6;text-decoration:none}a:hover{text-decoration:underline}
.mut{color:#777}.mono{font-family:var(--mono)}.big{font-size:22px;font-weight:600}
.ok{color:#127a3d}.warn{color:#b06a00}.bad{color:#c0392b}.stale{color:#c0392b}
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin-bottom:10px}
.card{border:1px solid var(--b);border-radius:8px;padding:10px 12px;background:#fff}
.card h3{margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#888}
.kv{display:flex;justify-content:space-between;gap:8px;font-size:13px;padding:1px 0}
.kv b{font-family:var(--mono)}
.headline{border:2px solid #1a1a1a;border-radius:8px;padding:12px 14px;background:#fff;margin-bottom:12px}
.headline .obs{font-size:15px;line-height:1.5}
.tabs{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px;border-bottom:1px solid var(--b)}
.tab{padding:6px 12px;border:1px solid var(--b);border-bottom:none;border-radius:6px 6px 0 0;
     background:#f0f0f0;cursor:pointer;font-size:13px}
.tab.on{background:#fff;font-weight:600}
.tab .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
.panelwrap{display:grid;grid-template-columns:280px 1fr;gap:12px}
@media(max-width:720px){.panelwrap{grid-template-columns:1fr}}
.snap{width:100%;border:1px solid var(--b);border-radius:6px;background:#000;aspect-ratio:4/3;object-fit:contain}
.detail{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:520px){.detail{grid-template-columns:1fr}}
.bars{display:flex;gap:2px;align-items:flex-end;height:44px;margin:4px 0}
.bars > div{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}
.bars .bar{width:100%;background:#127a3d}
.bars span{font-size:8px;color:#999;margin-top:1px}
.blank{color:#999;font-style:italic;font-size:13px;padding:6px 0}
.foot{margin-top:14px;font-size:12px}
</style>
<h1>liftlab · dash <span class=mut id=stamp></span></h1>
<div class=strip id=strip></div>
<div id=headline></div>
<div class=tabs id=tabs></div>
<div id=panel></div>
<div class=foot mut>deep views: <a href="/ops/__GW__">/ops</a> · <a href="/events">/events</a> ·
  <a href="/validate">/validate</a> · <a href="/pihealth/__GW__">/pihealth</a></div>
<script>
var GW="__GW__", cur=null, DATA=null;
function esc(s){return s==null?'':(''+s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function age(s){return s==null?'—':(s<90?Math.round(s)+'s':Math.round(s/60)+'m')+' ago'}
function kv(k,v,c){return '<div class=kv><span class=mut>'+k+'</span><b class="'+(c||'')+'">'+v+'</b></div>'}
function staleCls(s,lim){return s==null?'stale':(s>lim?'stale':'ok')}

function strip(d){
  var p=d.pi,r=d.relay,g=d.gpu,h=[];
  // PI
  if(!p){h.push('<div class=card><h3>Pi</h3><div class=blank>no watch_status — is liftlab-watch running?</div></div>');}
  else{var ps=staleCls(p.age_s,60);
    h.push('<div class=card><h3>Pi · '+esc(p.camera||'')+'</h3>'
      +kv('state',esc(p.state)+'  <span class="'+ps+'">('+age(p.age_s)+')</span>')
      +kv('signal_fps',p.signal_fps==null?'—':(+p.signal_fps).toFixed(2),(p.signal_fps<6?'bad':'ok'))
      +kv('temp / throttle',(p.soc_temp==null?'—':(+p.soc_temp).toFixed(1)+'°C')+' · '+((p.throttle_live&&p.throttle_live!=='')?'<span class=bad>THROTTLE</span>':'clean'))
      +kv('opens / emitted',esc(p.opens_detected)+' / '+esc(p.cycles_emitted))
      +kv('detect:emit',p.detect_emit==null?'—':p.detect_emit+'×',(p.detect_emit>=1.5?'warn':'ok'))+'</div>');}
  // RELAY
  if(!r){h.push('<div class=card><h3>Relay</h3><div class=blank>no relay_status</div></div>');}
  else{h.push('<div class=card><h3>Relay</h3>'
      +kv('delivering',esc(r.streams_delivering)+' streams','ok')
      +kv('throughput',r.sum_delivered_mbps==null?'—':(+r.sum_delivered_mbps).toFixed(1)+' Mbps')
      +kv('updated','<span class="'+staleCls(r.age_s,30)+'">'+age(r.age_s)+'</span>')+'</div>');}
  // GPU
  var gd=g&&g.worst;
  var gc=g&&g.any_up?'ok':'bad';
  h.push('<div class=card><h3>GPU analyzer</h3>'
    +kv('status',(g&&g.any_up)?'<span class=ok>up</span>':'<span class=bad>down</span>')
    +kv('running',(g&&g.cams_up&&g.cams_up.length)?esc(g.cams_up.join(', ')):'—')
    +(gd?kv('throughput',(gd.proc_ms==null?'—':Math.round(gd.proc_ms)+'/'+Math.round(gd.budget_ms||2000)+'ms  ('+((gd.proc_ms/(gd.budget_ms||2000)).toFixed(2))+'x)'),(gd.proc_ms/(gd.budget_ms||2000)>=1?'bad':'ok')):'')
    +(gd?kv('drop',(gd.drop_frac==null?'—':(gd.drop_frac*100).toFixed(2)+'%'),(gd.drop_frac>=0.01?'bad':'ok')):'')+'</div>');
  document.getElementById('strip').innerHTML=h.join('');
}

function headline(d){
  if(!d.headline||!d.headline.length){document.getElementById('headline').innerHTML='';return;}
  var h=d.headline.map(function(x){
    var obs=(x.median==null)?(x.cam+' door close: no clean close measured yet'):
      (x.cam+' door close: observed median <b>'+x.median+'s</b> (p85 '+x.p85+'s, n='+x.n+')'
       +' · sheet assumes <b>'+x.sheet_s.toFixed(2)+'s</b>'
       +' · Bank '+esc(x.bank)+' non-compliant above <b>'+x.compliance_s.toFixed(2)+'s</b>'
       +' · <b class="'+((x.pct_exceed||0)>=50?'bad':'warn')+'">'+esc(x.pct_exceed)+'%</b> of observed closes exceed '+x.compliance_s.toFixed(2)+'s');
    return '<div class="obs mono">'+obs+'</div>';
  }).join('');
  document.getElementById('headline').innerHTML='<div class=headline><h3 class=mut style="margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase">compliance — assumption beside observation</h3>'+h+'</div>';
}

function tabs(d){
  cur=cur||(d.cameras[0]&&d.cameras[0].cam);
  document.getElementById('tabs').innerHTML=d.cameras.map(function(c){
    var live=c.snap&&!c.snap.stale, dot=live?'#127a3d':(c.snap?'#b06a00':'#ccc');
    return '<div class="tab'+(c.cam===cur?' on':'')+'" onclick="pick(\''+c.cam+'\')">'
      +'<span class=dot style="background:'+dot+'"></span>'+esc(c.cam)+(c.label?' '+esc(c.label):'')+'</div>';
  }).join('');
}
function pick(cam){cur=cam;render();}

function bars(door){
  if(!door||!door.n){return '';}
  var h=door.hist, e=door.hist_edges, mx=Math.max.apply(null,h.concat([1]));
  var labs=[]; for(var i=0;i<=e.length;i++){labs.push(i<e.length?('<'+e[i]):('≥'+e[e.length-1]));}
  var b=h.map(function(c,i){var ht=Math.round(4+36*c/mx);
    return '<div><span>'+c+'</span><div class=bar style="height:'+ht+'px"></div><span>'+labs[i]+'</span></div>';}).join('');
  return '<div class=bars>'+b+'</div>';
}

function panel(d){
  var c=d.cameras.filter(function(x){return x.cam===cur})[0];
  if(!c){document.getElementById('panel').innerHTML='';return;}
  var snap=c.snap
    ? '<img class=snap src="/snap/'+GW+'/'+c.cam+'.jpg?t='+Date.now()+'"><div class="mut" style="font-size:12px;margin-top:2px">frame <span class="'+staleCls(c.snap.age_s,20)+'">'+age(c.snap.age_s)+'</span></div>'
    : '<div class=snap style="display:flex;align-items:center;justify-content:center;color:#666">no snapshot</div>';

  // DOOR
  var door=c.door&&c.door.total? (
    kv('cycles today / total',esc(c.door.today)+' / '+esc(c.door.total))
    +kv('close median / p85',(c.door.median==null?'—':c.door.median+'s')+' / '+(c.door.p85==null?'—':c.door.p85+'s')+'  (n='+c.door.n+')')
    +kv('range',(c.door.min==null?'—':c.door.min+'–'+c.door.max+'s'))
    +kv('last cycle',esc((c.door.last_open||'').slice(11,19)||'—'))
    +bars(c.door)
  ) : '<div class=blank>no door cycles recorded</div>';

  // TRANSIT
  var t=c.transit;
  var trans=t? (
    kv('boarded / alighted today','<span class=ok>'+esc(t.boarded_today)+'</span> / '+esc(t.alighted_today))
    +kv('total','+'+esc(t.boarded_total)+' / -'+esc(t.alighted_total))
    +kv('last transit',t.last_ts?age(d.t-t.last_ts):'—')
    +kv('source',esc(t.source||'—'))
  ) : '<div class=blank>no transit counts</div>';

  // STATE
  var a=c.analyzer, v=c.validation, state;
  if(!a){state='<div class=blank>not analysed — relay only</div>';}
  else{
    var vs=v?(v.state==='live'?'<span class=ok>live</span>':'<span class=warn>'+esc(v.state)+'</span>'):'—';
    state=kv('analyser',(a.up?'<span class=ok>up</span>':'<span class=bad>down</span>')+' ('+age(a.age_s)+')')
      +kv('mode / state',esc(a.mode||'—')+' · '+vs)
      +(v&&v.precision!=null?kv('precision',v.precision+'% on n='+v.n_reviewed):'')
      +kv('counting',esc((a.counting_version||(v&&v.counting_version))||'—'));
  }
  var link='<div style="margin-top:6px"><a href="/validate?cam='+c.cam+'">validate this camera →</a></div>';

  document.getElementById('panel').innerHTML=
    '<div class=panelwrap><div>'+snap+link+'</div>'
    +'<div class=detail>'
    +'<div class=card><h3>Door</h3>'+door+'</div>'
    +'<div class=card><h3>Transit</h3>'+trans+'</div>'
    +'<div class=card><h3>State</h3>'+state+'</div>'
    +'<div class=card><h3>Camera</h3>'+kv('channel',esc(c.channel))+kv('label',esc(c.label||'—'))
      +kv('snapshot',c.snap?('<span class="'+staleCls(c.snap.age_s,20)+'">'+age(c.snap.age_s)+'</span>'):'—')+'</div>'
    +'</div></div>';
}

function render(){ if(!DATA)return; strip(DATA); headline(DATA); tabs(DATA); panel(DATA); }
function load(){
  fetch('/dash/'+GW+'/data').then(function(r){return r.json()}).then(function(d){
    DATA=d; document.getElementById('stamp').textContent='· '+d.ist_today+' · updated '+new Date().toLocaleTimeString();
    render();
  }).catch(function(){document.getElementById('stamp').textContent='· FETCH FAILED';});
}
load(); setInterval(load, 15000);
</script>"""
