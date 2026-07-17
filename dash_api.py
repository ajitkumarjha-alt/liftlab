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

# Door-close + transfer compliance spec per camera (from the sheet). observed vs assumption side by side.
# Only cameras with an entry get the headline compliance panel; others show observed-only.
#   sheet_s / compliance_s / bank : door-close (2.31s = Bank C non-compliance line)
#   transfer_sheet_s              : C26 passenger transfer, sheet assumes 1.50 s/person
DOOR_SPECS = {
    "ch29": {"sheet_s": 2.00, "compliance_s": 2.31, "bank": "C", "transfer_sheet_s": 1.50},
}

# Comparability boundaries — data across these isn't directly comparable; charts MARK them and the
# close-travel headline uses only the current (post-boundary) regime.
CLOSE_TRAVEL_MAX_BOUNDARY = "2026-07-16T11:48:11+00:00"   # CLOSE_TRAVEL_MAX 10->30 (admits longer real closes)
_BOUNDARY_EPOCH = datetime.fromisoformat(CLOSE_TRAVEL_MAX_BOUNDARY).timestamp()

# Fixed peak windows (local hours). The PEAK TRAP: sheet coefficients describe a PEAK design
# condition, not an all-day average — report both separately; the ratio is itself a finding.
PEAK_WINDOWS = {"am_peak": (8, 10), "pm_peak": (18, 20)}

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


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()   # tz-aware local ISO -> absolute epoch
    except Exception:
        return None


def _local_hour(iso):
    try:
        return datetime.fromisoformat(iso).hour          # local hour (ts carry +05:30)
    except Exception:
        return None


def _stats(cts):
    cts = sorted(cts)
    n = len(cts)
    return {"n": n, "median": round(_pctl(cts, 0.5), 2) if n else None,
            "p85": round(_pctl(cts, 0.85), 2) if n else None,
            "min": round(cts[0], 2) if n else None, "max": round(cts[-1], 2) if n else None}


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


def _transfer_by_cam(db, gw):
    """C26 passenger transfer: (door_close_start - door_open_full) / (boarded+alighted), per person,
    on rows where boarded/alighted are non-NULL. PROVISIONAL — depends on the transit counts (ch29 at
    80% precision on n=66, re-validating under 11m), so it's only firm once the camera goes live."""
    rows = _q(db, "SELECT s.camera cam, e.door_open_full_ts of, e.door_close_start_ts cs, "
                  "e.boarded b, e.alighted a FROM gw_event e JOIN gw_source s ON s.id=e.source_id "
                  "WHERE s.gateway_id=? AND e.boarded IS NOT NULL AND e.alighted IS NOT NULL", (gw,))
    by = {}
    for r in rows:
        load = (r["b"] or 0) + (r["a"] or 0)
        if load <= 0:
            continue
        o, c = _epoch(r["of"]), _epoch(r["cs"])
        if o is None or c is None or c <= o:
            continue
        by.setdefault(r["cam"], []).append((c - o) / load)
    return {cam: {**_stats(v), "provisional": True} for cam, v in by.items()}


def _floor_coverage(db, gw):
    """How many gw_event rows carry a floor label. NULL on all of them => the whole Tier-2 / per-floor
    family (stops-per-floor, C17/C18 up-down, C21/C22 speed factors) is NOT AVAILABLE — say so, loudly."""
    rows = _q(db, "SELECT COUNT(*) t, COUNT(e.floor) f FROM gw_event e "
                  "JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?", (gw,))
    if not rows:
        return {"total": 0, "with_floor": 0}
    return {"total": rows[0]["t"] or 0, "with_floor": rows[0]["f"] or 0}


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
    xfer = _transfer_by_cam(db, gw)
    floor_cov = _floor_coverage(db, gw)
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

    headline = []
    for cam in door:
        if not door[cam].get("spec"):
            continue
        x = xfer.get(cam)
        headline.append(dict(door[cam]["spec"], cam=cam, median=door[cam]["median"],
                             p85=door[cam]["p85"], n=door[cam]["n"],
                             transfer_median=(x or {}).get("median"), transfer_n=(x or {}).get("n"),
                             transfer_provisional=True))

    # NOT AVAILABLE (Tier-2 ceiling): floor is NULL on every row -> no per-floor family. Say it on the page.
    unavailable = None
    if floor_cov["with_floor"] == 0 and floor_cov["total"] > 0:
        unavailable = {"reason": "needs floor attribution — not built",
                       "detail": f"gw_event.floor is NULL on all {floor_cov['total']} rows",
                       "blocks": ["stops per floor", "boardings/alightings per floor",
                                  "C17/C18 probable up/down stops", "C21/C22 speed factors"],
                       "unlock": "floor OCR (template-match the LED digits + direction arrow)"}

    return JSONResponse({"t": now, "gw": gw, "ist_today": _ist_today_str(),
                         "pi": pi, "relay": relay, "gpu": gpu,
                         "cameras": out_cams, "headline": headline,
                         "floor_coverage": floor_cov, "unavailable": unavailable})


@dash_router.get("/dash/{gw}/trends")
def dash_trends(gw: str, cam: str = "", from_h: int = -1, to_h: int = -1):
    """Hour-of-day profile + window stats. cam='' -> FLEET (all lift cams). close-travel uses only the
    current (post-CLOSE_TRAVEL_MAX-boundary) regime for comparability. Every number carries n."""
    db = _db()
    cam_filter = ""
    args = [gw]
    if cam:
        cam_filter = " AND s.camera=?"
        args.append(cam)
    ev = _q(db, "SELECT e.door_open_start_ts os, e.door_open_full_ts of, e.door_close_start_ts cs, "
                "e.close_travel_s ct, e.quality q, e.boarded b, e.alighted a "
                "FROM gw_event e JOIN gw_source s ON s.id=e.source_id WHERE s.gateway_id=?" + cam_filter, args)
    tr = _q(db, "SELECT ts, direction FROM transit_event WHERE gateway_id=?" +
            (" AND cam=?" if cam else ""), ([gw, cam] if cam else [gw]))
    db.close()

    prof = {h: {"cycles": 0, "boarded": 0, "alighted": 0, "closes": [], "xfer": []} for h in range(24)}
    days = set()
    for r in ev:
        h = _local_hour(r["os"])
        if h is None:
            continue
        days.add((r["os"] or "")[:10])
        prof[h]["cycles"] += 1                                   # every opening = demand (flagged included)
        ep = _epoch(r["os"])
        clean = (r["q"] is None or r["q"] == "ok")
        if r["ct"] is not None and clean and ep is not None and ep >= _BOUNDARY_EPOCH:
            prof[h]["closes"].append(float(r["ct"]))             # comparable regime only
        load = (r["b"] or 0) + (r["a"] or 0)
        o, c = _epoch(r["of"]), _epoch(r["cs"])
        if load > 0 and o is not None and c is not None and c > o:
            prof[h]["xfer"].append((c - o) / load)
    for r in tr:
        try:
            h = datetime.fromtimestamp(r["ts"], IST).hour
        except Exception:
            continue
        prof[h]["boarded" if r["direction"] == "in" else "alighted"] += 1

    ndays = max(1, len(days))
    profile = [{"hour": h, "cycles": prof[h]["cycles"],
                "boarded": prof[h]["boarded"], "alighted": prof[h]["alighted"],
                **{f"close_{k}": v for k, v in _stats(prof[h]["closes"]).items()}} for h in range(24)]

    def window(lo, hi):
        hrs = [h for h in range(24) if lo <= h < hi]
        cyc = sum(prof[h]["cycles"] for h in hrs)
        closes = [v for h in hrs for v in prof[h]["closes"]]
        xfer = [v for h in hrs for v in prof[h]["xfer"]]
        bo = sum(prof[h]["boarded"] for h in hrs); al = sum(prof[h]["alighted"] for h in hrs)
        span = max(1, len(hrs))
        return {"from": lo, "to": hi, "cycles": cyc, "cycles_per_hr": round(cyc / (span * ndays), 2),
                "boarded": bo, "alighted": al, "riders_per_hr": round((bo + al) / (span * ndays), 2),
                "close": _stats(closes), "transfer": {**_stats(xfer), "provisional": True},
                "transits_per_cycle": round((bo + al) / cyc, 2) if cyc else None}

    windows = {"all_day": window(0, 24), **{k: window(*v) for k, v in PEAK_WINDOWS.items()}}
    if 0 <= from_h < to_h <= 24:
        windows["custom"] = window(from_h, to_h)
    # THE PEAK TRAP made explicit: peak-vs-all-day ratio (a finding in itself)
    ad = windows["all_day"]["cycles_per_hr"] or 1
    for k in list(windows):
        if k != "all_day":
            windows[k]["demand_ratio_vs_allday"] = round((windows[k]["cycles_per_hr"] or 0) / ad, 2)

    return JSONResponse({"gw": gw, "cam": cam or "fleet", "n_days": ndays, "profile": profile,
                         "windows": windows,
                         "boundaries": {"close_travel_max": {"iso": CLOSE_TRAVEL_MAX_BOUNDARY, "epoch": _BOUNDARY_EPOCH,
                                        "note": "CLOSE_TRAVEL_MAX 10->30s; close-travel here uses the post-boundary regime only"}}})


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
<div class=tabs id=nav></div>
<div class=strip id=strip></div>
<div id=headline></div>
<div id=unavail></div>
<div id=camview><div class=tabs id=tabs></div><div id=panel></div></div>
<div id=trendview style="display:none"></div>
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
    var dl=(x.median==null)?(x.cam+' door close: no clean close measured yet'):
      (x.cam+' door close: observed median <b>'+x.median+'s</b> (p85 '+x.p85+'s, n='+x.n+')'
       +' · sheet assumes <b>'+x.sheet_s.toFixed(2)+'s</b>'
       +' · Bank '+esc(x.bank)+' non-compliant above <b>'+x.compliance_s.toFixed(2)+'s</b>'
       +' · <b class="'+((x.pct_exceed||0)>=50?'bad':'warn')+'">'+esc(x.pct_exceed)+'%</b> of observed closes exceed '+x.compliance_s.toFixed(2)+'s');
    var out='<div class="obs mono">'+dl+'</div>';
    // C26 passenger transfer — beside door-close, same format. PROVISIONAL (transit precision).
    if(x.transfer_sheet_s!=null){
      var tl=(x.transfer_median==null)?(x.cam+' transfer: no counted cycles yet'):
        (x.cam+' transfer: observed median <b>'+x.transfer_median+' s/person</b> (n='+x.transfer_n+')'
         +' · sheet assumes <b>'+x.transfer_sheet_s.toFixed(2)+' s/person</b>');
      out+='<div class="obs mono">'+tl+'</div>'
        +'<div class=mut style="font-size:11px">transfer is PROVISIONAL — depends on the transit counts (ch29 80% precision on n=66, re-validating under 11m); firm once ch29 goes live.</div>';
    }
    return out;
  }).join('<hr style="border:none;border-top:1px solid #eee;margin:8px 0">');
  document.getElementById('headline').innerHTML='<div class=headline><h3 class=mut style="margin:0 0 6px;font-size:11px;letter-spacing:.1em;text-transform:uppercase">compliance — assumption beside observation, no verdict</h3>'+h+'</div>';
}

function unavail(d){
  var u=d.unavailable;
  if(!u){document.getElementById('unavail').innerHTML='';return;}
  document.getElementById('unavail').innerHTML=
    '<div class=card style="border-color:#b06a00;background:#fffaf0"><h3 style="color:#b06a00">not available — '+esc(u.reason)+'</h3>'
    +'<div class=mut style="font-size:12px;margin-bottom:4px">'+esc(u.detail)+' · Tier-1 ceiling. These need per-floor data:</div>'
    +'<ul style="margin:2px 0 4px 18px;font-size:13px">'+u.blocks.map(function(b){return '<li>'+esc(b)+'</li>'}).join('')+'</ul>'
    +'<div class=mut style="font-size:12px">unlock: '+esc(u.unlock)+'</div></div>';
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

var mode='cams', trCam='', TR=null;
function nav(){
  document.getElementById('nav').innerHTML=
    '<div class="tab'+(mode==='cams'?' on':'')+'" onclick="setMode(\'cams\')">Cameras</div>'
   +'<div class="tab'+(mode==='trends'?' on':'')+'" onclick="setMode(\'trends\')">Trends</div>';
}
function setMode(m){mode=m;
  document.getElementById('camview').style.display=(m==='cams')?'':'none';
  document.getElementById('trendview').style.display=(m==='trends')?'':'none';
  nav(); if(m==='trends')loadTrends();
}

// ---- inline SVG charts (CSP-safe, no libs) ----
function svgBars(title,hours,vals,color,ref,refLab){
  var W=560,H=140,pad=30,bot=16, mx=Math.max.apply(null,vals.map(function(v){return v||0}).concat([1]));
  var bw=(W-2*pad)/vals.length;
  var bars=vals.map(function(v,i){var bh=(H-14-bot)*(v||0)/mx;return '<rect x="'+(pad+i*bw+0.5)+'" y="'+(H-bot-bh)+'" width="'+(bw-1)+'" height="'+bh+'" fill="'+color+'"></rect>';}).join('');
  var labs=hours.map(function(h,i){return (h%3===0)?'<text x="'+(pad+i*bw+bw/2)+'" y="'+(H-4)+'" font-size="8" fill="#999" text-anchor="middle">'+h+'</text>':''}).join('');
  var rl=''; if(ref!=null){var y=H-bot-(H-14-bot)*ref/mx;rl='<line x1="'+pad+'" x2="'+(W-pad)+'" y1="'+y+'" y2="'+y+'" stroke="#c0392b" stroke-dasharray="4 3"></line><text x="'+(W-pad)+'" y="'+(y-2)+'" font-size="9" fill="#c0392b" text-anchor="end">'+refLab+'</text>';}
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto"><text x="'+pad+'" y="11" font-size="11" fill="#555">'+esc(title)+'</text>'+rl+bars+labs+'</svg>';
}
function svgLine(title,hours,vals,color,ref,refLab){
  var W=560,H=150,pad=30,bot=16, real=vals.filter(function(v){return v!=null}), mx=Math.max.apply(null,real.concat([ref||1,1]));
  var bw=(W-2*pad)/vals.length;
  function xy(v,i){return [pad+i*bw+bw/2, H-bot-(H-14-bot)*v/mx];}
  var pts=vals.map(function(v,i){return v==null?null:xy(v,i).join(',')}).filter(Boolean).join(' ');
  var dots=vals.map(function(v,i){if(v==null)return '';var c=xy(v,i);return '<circle cx="'+c[0]+'" cy="'+c[1]+'" r="2" fill="'+color+'"></circle>';}).join('');
  var rl=''; if(ref!=null){var y=H-bot-(H-14-bot)*ref/mx;rl='<line x1="'+pad+'" x2="'+(W-pad)+'" y1="'+y+'" y2="'+y+'" stroke="#c0392b" stroke-dasharray="4 3"></line><text x="'+(W-pad)+'" y="'+(y-2)+'" font-size="9" fill="#c0392b" text-anchor="end">'+refLab+'</text>';}
  var labs=hours.map(function(h,i){return (h%3===0)?'<text x="'+(pad+i*bw+bw/2)+'" y="'+(H-4)+'" font-size="8" fill="#999" text-anchor="middle">'+h+'</text>':''}).join('');
  return '<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto"><text x="'+pad+'" y="11" font-size="11" fill="#555">'+esc(title)+'</text>'+rl+'<polyline points="'+pts+'" fill="none" stroke="'+color+'" stroke-width="1.5"></polyline>'+dots+labs+'</svg>';
}
function winCard(name,w){
  if(!w)return '';
  var ratio=(w.demand_ratio_vs_allday!=null)?('  <b class="'+(w.demand_ratio_vs_allday>=1.3?'bad':'')+'">'+w.demand_ratio_vs_allday+'× all-day</b>'):'';
  return '<div class=card><h3>'+esc(name)+' <span class=mut>'+w.from+':00–'+w.to+':00</span></h3>'
    +kv('cycles/hr',w.cycles_per_hr+ratio)
    +kv('close med / p85',(w.close.median==null?'—':w.close.median+'s')+' / '+(w.close.p85==null?'—':w.close.p85+'s')+' (n='+w.close.n+')')
    +kv('transfer',(w.transfer.median==null?'—':w.transfer.median+' s/pp')+' (n='+w.transfer.n+') *')
    +kv('riders/hr',w.riders_per_hr)
    +kv('transits/cycle',w.transits_per_cycle==null?'—':w.transits_per_cycle)+'</div>';
}
function trCams(){
  var cams=(DATA&&DATA.cameras)?DATA.cameras.map(function(c){return c.cam}):[];
  return '<div class=tabs style="margin-bottom:6px">'
    +['',].concat(cams).map(function(c){var lbl=c||'fleet';
       return '<div class="tab'+(trCam===c?' on':'')+'" onclick="trCam=\''+c+'\';loadTrends()">'+esc(lbl)+'</div>';}).join('')+'</div>';
}
function renderTrends(){
  if(!TR){document.getElementById('trendview').innerHTML=trCams()+'<div class=mut>loading…</div>';return;}
  var prof=TR.profile, hours=prof.map(function(p){return p.hour}), W=TR.windows;
  var bd=TR.boundaries.close_travel_max.iso.slice(0,10);
  var h=trCams()
    +'<div class=mut style="font-size:12px;margin:2px 0 6px">'+esc(TR.cam)+' · '+TR.n_days+' day(s) · close-travel uses the post-'+bd+' regime only (CLOSE_TRAVEL_MAX comparability boundary)</div>'
    +'<div class=strip>'+winCard('all-day',W.all_day)+winCard('AM peak',W.am_peak)+winCard('PM peak',W.pm_peak)+'</div>'
    +'<div class=mut style="font-size:11px;margin:2px 0 8px">* transfer PROVISIONAL (transit precision, re-validating). <b>THE PEAK TRAP</b>: the sheet coefficients describe a PEAK design condition, not an all-day average — peak &amp; all-day are shown SEPARATELY; the ratio is itself a finding.</div>'
    +'<div class=card>'+svgBars('cycles / hour-of-day — the demand curve',hours,prof.map(function(p){return p.cycles}),'#127a3d',null,'')+'</div>'
    +'<div class=card>'+svgBars('riders (boardings+alightings) / hour-of-day',hours,prof.map(function(p){return p.boarded+p.alighted}),'#2a6db0',null,'')+'</div>'
    +'<div class=card>'+svgLine('close-travel median / hour-of-day (s)',hours,prof.map(function(p){return p.close_median}),'#b06a00',2.31,'2.31 Bank C')+'</div>';
  document.getElementById('trendview').innerHTML=h;
}
function loadTrends(){
  renderTrends();  // show selector immediately
  fetch('/dash/'+GW+'/trends'+(trCam?('?cam='+trCam):'')).then(function(r){return r.json()}).then(function(t){TR=t;renderTrends();}).catch(function(){});
}

function render(){ if(!DATA)return; nav(); strip(DATA); headline(DATA); unavail(DATA); if(mode==='cams'){tabs(DATA); panel(DATA);} }
function load(){
  fetch('/dash/'+GW+'/data').then(function(r){return r.json()}).then(function(d){
    DATA=d; document.getElementById('stamp').textContent='· '+d.ist_today+' · updated '+new Date().toLocaleTimeString();
    render();
  }).catch(function(){document.getElementById('stamp').textContent='· FETCH FAILED';});
}
load(); setInterval(load, 15000);
</script>"""
