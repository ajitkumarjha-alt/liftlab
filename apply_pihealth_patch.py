#!/usr/bin/env python3
"""Patch gateway_api.py (Heartbeat telemetry fields + pi_telemetry table + 1/min
ring-buffer insert/prune in the heartbeat handler) and dashboard.html (per-card
Pi health panel: temp/load/mem sparklines + throttle badge). Anchored, idempotent,
backup-first. Run as the liftlab owner."""
import pathlib
import shutil
import time

APP = "/opt/liftlab-b3/cloud"


def patch(path, edits, marker, label):
    p = pathlib.Path(path)
    s = p.read_text()
    if marker in s:
        print(f"  {label}: already patched — skip")
        return
    for old, _ in edits:
        if old not in s:
            print(f"  {label}: ANCHOR NOT FOUND {old[:46]!r} — SKIP (no write)")
            return
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    for old, new in edits:
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"  {label}: patched")


# ---- gateway_api.py ----
model_old = "    log_tail: list[str] | None = None"
model_new = ("    log_tail: list[str] | None = None\n"
             "    throttled: str | None = None\n"
             "    loadavg: list[float] | None = None\n"
             "    mem_total_mb: int | None = None\n"
             "    mem_free_mb: int | None = None")

tbl_old = '''        PRIMARY KEY (job_id, idx));
    """)'''
tbl_new = '''        PRIMARY KEY (job_id, idx));
    CREATE TABLE IF NOT EXISTS pi_telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
        temp REAL, throttled TEXT, load1 REAL, load5 REAL, load15 REAL,
        mem_free_mb INTEGER, mem_total_mb INTEGER, disk_free_gb REAL, uptime_s INTEGER);
    """)'''

hb_old = '''         hb.model_dump_json()))
    db.commit(); db.close()
    return {"ok": True, "server_time": time.time()}'''
hb_new = '''         hb.model_dump_json()))
    try:
        _now = time.time()
        _last = db.execute("SELECT MAX(ts) FROM pi_telemetry WHERE gateway_id=?", (gateway_id,)).fetchone()[0]
        if not _last or _now - _last >= 55:
            def _pt(x):
                try:
                    return float(str(x).split("=")[1].split("'")[0])
                except Exception:
                    return None
            _la = hb.loadavg or [None, None, None]
            db.execute("INSERT INTO pi_telemetry (gateway_id,ts,temp,throttled,load1,load5,load15,mem_free_mb,mem_total_mb,disk_free_gb,uptime_s)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (gateway_id, _now, _pt(hb.soc_temp), hb.throttled, _la[0], _la[1], _la[2],
                        hb.mem_free_mb, hb.mem_total_mb, hb.disk_free_gb, hb.uptime_s))
            db.execute("DELETE FROM pi_telemetry WHERE gateway_id=? AND ts < ?", (gateway_id, _now - 48 * 3600))
    except Exception:
        pass
    db.commit(); db.close()
    return {"ok": True, "server_time": time.time()}'''

patch(f"{APP}/gateway_api.py",
      [(model_old, model_new), (tbl_old, tbl_new), (hb_old, hb_new)],
      "pi_telemetry", "gateway_api.py")


# ---- dashboard.html ----
panel_old = '''        <div class="cmds">'''
panel_new = '''        <div class="pihealth" id="ph-${g.id}"></div>
        <div class="cmds">'''

css_old = '''</style>'''
css_new = '''.pihealth{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-family:var(--mono);font-size:11px;color:#7a8b93}
.phrow{margin-bottom:3px}.phrow b{color:#dbe3e6;letter-spacing:.06em;text-transform:uppercase;font-size:10px}
.phspk{display:flex;align-items:center;gap:6px;margin:1px 0}
.thr-ok{color:#63a37e;margin-left:6px}.thr-bad{color:#d0574d;margin-left:6px;font-weight:600}
.phmuted{color:#4a5960}.phcabins{margin-top:4px}</style>'''

health_old = '''refresh(); setInterval(refresh,5000);'''
health_new = '''const _healthAt={};
function _spark(vals,lo,hi,thr,red){const w=90,h=18,n=vals.length;if(!n)return '';
 const X=i=>i/((n-1)||1)*w,Y=v=>h-(Math.max(lo,Math.min(hi,v))-lo)/((hi-lo)||1)*h;
 const pts=vals.map((v,i)=>X(i).toFixed(1)+','+Y(v).toFixed(1)).join(' ');
 let tl='';if(thr!=null){const y=Y(thr).toFixed(1);tl='<line x1="0" y1="'+y+'" x2="'+w+'" y2="'+y+'" stroke="#e3a53f" stroke-dasharray="2,2" stroke-width=".5"/>';}
 return '<svg width="'+w+'" height="'+h+'" style="vertical-align:middle">'+tl+'<polyline points="'+pts+'" fill="none" stroke="'+(red?'#d0574d':'#63a37e')+'" stroke-width="1"/></svg>';}
async function renderHealth(gw){const now=Date.now();if(_healthAt[gw]&&now-_healthAt[gw]<25000)return;_healthAt[gw]=now;
 const el=document.getElementById('ph-'+gw);if(!el)return;let d;try{d=await (await fetch('/telemetry/'+gw)).json();}catch(e){return;}
 const rows=d.rows||[];if(!rows.length){el.innerHTML='<span class="phmuted">Pi health: no telemetry yet</span>';return;}
 const thrTs=d.last_throttle_ts;const anyThr=!!thrTs;
 const badge=thrTs?('<span class="thr-bad">\\u26a0 throttled '+Math.round((d.now-thrTs)/60)+'m ago</span>'):'<span class="thr-ok">throttle clean</span>';
 const temp=rows.map(r=>r.temp||0),load=rows.map(r=>r.load1||0),memf=rows.map(r=>r.mem_free_mb||0);
 const memtot=rows[rows.length-1].mem_total_mb||4096;
 el.innerHTML='<div class="phrow"><b>Pi health</b> '+badge+'</div>'+
  '<div class="phspk">temp '+_spark(temp,35,90,80,anyThr)+' '+(temp[temp.length-1]||0).toFixed(0)+'C</div>'+
  '<div class="phspk">load '+_spark(load,0,4,null,false)+' '+(load[load.length-1]||0).toFixed(1)+'</div>'+
  '<div class="phspk">mem&nbsp; '+_spark(memf,0,memtot,null,false)+' '+(memf[memf.length-1]||0)+'MB free</div>'+
  '<div class="phcabins" id="phc-'+gw+'"><span class="phmuted">per-cabin liveness: wired when continuous capture lands</span></div>';}
refresh(); setInterval(refresh,5000);'''

call_old = '''    if([...sel.options].some(o=>o.value===cur))sel.value=cur;'''
call_new = '''    if([...sel.options].some(o=>o.value===cur))sel.value=cur;
    st.gateways.forEach(g=>renderHealth(g.id));'''

patch(f"{APP}/dashboard.html",
      [(panel_old, panel_new), (css_old, css_new), (health_old, health_new), (call_old, call_new)],
      "pihealth", "dashboard.html")

print("pi-health patches done")
