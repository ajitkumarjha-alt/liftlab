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
VALIDATION_DIR = DATA_DIR / "validation"
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
    CREATE TABLE IF NOT EXISTS validation_frame (
      gateway_id TEXT, channel INTEGER, requested_start TEXT, fn TEXT, mode TEXT,
      uploaded_at REAL, PRIMARY KEY (gateway_id, channel, requested_start));
    CREATE TABLE IF NOT EXISTS loadtest (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, t REAL, temp REAL,
      throttled TEXT, load1 REAL, mem_mb INTEGER, payload TEXT, uploaded_at REAL);
    CREATE TABLE IF NOT EXISTS pi_telemetry (
      id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
      temp REAL, throttled TEXT, load1 REAL, load5 REAL, load15 REAL,
      mem_free_mb INTEGER, mem_total_mb INTEGER, disk_free_gb REAL, uptime_s INTEGER);
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
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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
            img = (f'<img src="/survey/{gw}/img/ch{ch:02d}.jpg?t={int(s.get("surveyed_at") or 0)}"'
                   f' loading="lazy">')
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


# ---------------- Validation frames (keep_validation_frame proof) ----------------
# One decoded frame (OSD intact) per keep_validation_frame pull/analyze. Same
# privacy as survey: behind basicauth, deletable, files under DATA_DIR/validation.
@survey_router.post("/api/gw/{gw}/validation/{ch}")
async def validation_upload(gw: str, ch: int, request: Request,
                            requested_start: str = "", mode: str = "",
                            authorization: str = Header(default="")):
    _auth(gw, authorization)
    body = await request.body()
    if len(body) < 500:
        raise HTTPException(400, "empty image")
    safe = re.sub(r"[^0-9A-Za-z]", "", requested_start)[:20] or "na"
    fn = f"ch{ch:02d}_{safe}.jpg"
    d = VALIDATION_DIR / gw
    d.mkdir(parents=True, exist_ok=True)
    (d / fn).write_bytes(body)
    db = _db()
    db.execute(
        "INSERT INTO validation_frame (gateway_id,channel,requested_start,fn,mode,uploaded_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(gateway_id,channel,requested_start) DO UPDATE SET"
        " fn=excluded.fn, mode=excluded.mode, uploaded_at=excluded.uploaded_at",
        (gw, ch, requested_start, fn, mode, time.time()))
    db.commit(); db.close()
    return {"ok": True, "fn": fn}


@survey_router.get("/validation/{gw}/img/{fn}")
def validation_img(gw: str, fn: str):
    if not re.fullmatch(r"ch\d{2}_[0-9A-Za-z]{1,20}\.jpg", fn):
        raise HTTPException(404, "not found")
    p = VALIDATION_DIR / gw / fn
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@survey_router.post("/validation/{gw}/delete")
def validation_delete(gw: str):
    shutil.rmtree(VALIDATION_DIR / gw, ignore_errors=True)
    db = _db()
    db.execute("DELETE FROM validation_frame WHERE gateway_id=?", (gw,))
    db.commit(); db.close()
    return {"ok": True}


@survey_router.get("/validation/{gw}", response_class=HTMLResponse)
def validation_page(gw: str):
    db = _db()
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM validation_frame WHERE gateway_id=? ORDER BY channel, requested_start", (gw,))]
    db.close()
    tiles = "".join(
        f'<div class="tile"><img src="/validation/{gw}/img/{r["fn"]}?t={int(r["uploaded_at"] or 0)}">'
        f'<div class="cap">ch{r["channel"]:02d} · req {r["requested_start"]} · {r.get("mode") or ""}</div></div>'
        for r in rows)
    body = tiles or ('<p class="muted">No validation frames yet. Queue a pull/analyze with '
                     '<code>keep_validation_frame=on</code>.</p>')
    return f"""<!doctype html><meta charset=utf-8><title>validation · {gw}</title>
    <style>:root{{--mono:ui-monospace,Consolas,monospace}}
    body{{background:#0e1417;color:#dbe3e6;font:14px system-ui;max-width:1100px;margin:auto;padding:18px}}
    h1{{font-size:14px;letter-spacing:.15em;text-transform:uppercase;color:#e3a53f}}
    .muted{{color:#7a8b93}} a{{color:#63a37e}} code{{color:#e3a53f}}
    .bar{{display:flex;gap:10px;align-items:center;margin:12px 0}}
    button{{background:#1b252b;color:#dbe3e6;border:1px solid #26333a;border-radius:5px;padding:7px 12px;cursor:pointer}}
    button.danger{{border-color:#d0574d;color:#d0574d}}
    #grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
    .tile{{background:#151d22;border:1px solid #26333a;border-radius:6px;overflow:hidden}}
    .tile img{{width:100%;display:block;background:#0e1417}}
    .cap{{font-family:var(--mono);font-size:12px;padding:6px 8px;color:#7a8b93}}</style>
    <h1>validation · {gw}</h1>
    <p class=muted>One decoded frame per <code>keep_validation_frame</code> pull/analyze — OSD intact.
    Read the burned-in clock to confirm the footage is from the REQUESTED window (not live).
    Commissioning aid; delete after.</p>
    <div class=bar>
      <button class=danger onclick="if(confirm('Delete all validation frames for {gw}?'))fetch('/validation/{gw}/delete',{{method:'POST'}}).then(()=>location.reload())">Delete validation frames</button>
      <a href="/">&larr; fleet</a></div>
    <div id=grid>{body}</div>"""


# ---------------- Load-test telemetry (Pi throughput probe) ----------------
@survey_router.post("/api/gw/{gw}/loadtest")
async def loadtest_upload(gw: str, request: Request, authorization: str = Header(default="")):
    _auth(gw, authorization)
    b = await request.json()
    db = _db()
    db.execute(
        "INSERT INTO loadtest (gateway_id,t,temp,throttled,load1,mem_mb,payload,uploaded_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (gw, b.get("t"), b.get("temp"), ",".join(b.get("throttle_flags") or []),
         b.get("load1"), b.get("mem_mb"), json.dumps(b.get("streams") or {}), time.time()))
    db.commit(); db.close()
    return {"ok": True}


@survey_router.get("/loadtest/{gw}", response_class=HTMLResponse)
def loadtest_page(gw: str):
    db = _db()
    rows = [dict(r) for r in db.execute(
        "SELECT t,temp,throttled,load1,mem_mb,payload FROM loadtest WHERE gateway_id=? ORDER BY t", (gw,))]
    db.close()
    if not rows:
        return f"<!doctype html><meta charset=utf-8><body style='background:#0e1417;color:#dbe3e6;font:14px system-ui;padding:20px'><h1 style='color:#e3a53f'>load-test · {gw}</h1><p>No telemetry yet. Run load_probe.py.</p><a style='color:#63a37e' href='/'>&larr; fleet</a>"
    W, H, pad = 900, 240, 36
    tmax = max(r["t"] or 0 for r in rows) or 1
    tempmin, tempmax = 35, 90
    def X(t): return pad + (W - 2 * pad) * (t / tmax)
    def Y(v): return H - pad - (H - 2 * pad) * ((v - tempmin) / (tempmax - tempmin))
    templine = " ".join(f"{X(r['t']):.0f},{Y(r['temp'] or tempmin):.0f}" for r in rows)
    thr = [r for r in rows if r["throttled"]]
    thrmarks = "".join(f'<line x1="{X(r["t"]):.0f}" y1="{pad}" x2="{X(r["t"]):.0f}" y2="{H-pad}" stroke="#d0574d" stroke-width="1" opacity=".5"/>' for r in thr)
    y80 = Y(80)
    last = json.loads(rows[-1]["payload"] or "{}")
    fps_rows = "".join(f"<tr><td>ch{ch}</td><td>{d.get('fps')}</td><td>{d.get('decoded')}</td><td>{d.get('dropped')}</td><td>{d.get('events')}</td><td>{d.get('backjumps')}</td><td style='color:#d0574d'>{d.get('err') or ''}</td></tr>" for ch, d in last.items())
    peak = max((r["temp"] or 0) for r in rows)
    return f"""<!doctype html><meta charset=utf-8><title>load-test · {gw}</title>
    <style>body{{background:#0e1417;color:#dbe3e6;font:14px system-ui;max-width:960px;margin:auto;padding:18px}}
    h1{{font-size:14px;letter-spacing:.15em;text-transform:uppercase;color:#e3a53f}}
    table{{width:100%;border-collapse:collapse;font-family:ui-monospace,Consolas,monospace;font-size:12px;margin-top:10px}}
    th,td{{text-align:left;padding:5px;border-bottom:1px solid #26333a}} th{{color:#7a8b93}} a{{color:#63a37e}}</style>
    <h1>load-test · {gw}</h1>
    <p style="color:#7a8b93">SoC temp over time (red lines = throttle flags set). Peak {peak:.1f}C.
    {"<b style='color:#d0574d'>THROTTLING OCCURRED</b>" if thr else "<b style='color:#63a37e'>throttle-clean</b>"} &nbsp;·&nbsp; <a href="/">&larr; fleet</a></p>
    <svg viewBox="0 0 {W} {H}" style="width:100%;background:#151d22;border:1px solid #26333a;border-radius:6px">
      {thrmarks}
      <line x1="{pad}" y1="{y80:.0f}" x2="{W-pad}" y2="{y80:.0f}" stroke="#e3a53f" stroke-dasharray="4,4" stroke-width="1"/>
      <text x="{W-pad}" y="{y80-4:.0f}" fill="#e3a53f" font-size="10" text-anchor="end" font-family="monospace">80C</text>
      <polyline points="{templine}" fill="none" stroke="#63a37e" stroke-width="2"/>
      <text x="{pad}" y="16" fill="#7a8b93" font-size="10" font-family="monospace">SoC temp {tempmin}-{tempmax}C, t=0..{tmax:.0f}s</text>
    </svg>
    <table><thead><tr><th>cabin</th><th>fps</th><th>decoded</th><th>dropped</th><th>events</th><th>backjumps</th><th>err</th></tr></thead><tbody>{fps_rows}</tbody></table>"""


# ---------------- Pi health telemetry (heartbeat ring buffer, for card sparklines) ----------------
@survey_router.get("/telemetry/{gw}")
def telemetry_series(gw: str):
    db = _db()
    since = time.time() - 48 * 3600
    rows = [dict(r) for r in db.execute(
        "SELECT ts,temp,throttled,load1,load5,load15,mem_free_mb,mem_total_mb,disk_free_gb,uptime_s"
        " FROM pi_telemetry WHERE gateway_id=? AND ts>? ORDER BY ts", (gw, since))]
    db.close()

    def _flagged(t):
        try:
            return int(str(t), 16) != 0
        except Exception:
            return False
    last_thr = next((r["ts"] for r in reversed(rows) if _flagged(r["throttled"])), None)
    return {"rows": rows, "last_throttle_ts": last_thr, "now": time.time()}


# Run the migration at import so fleet_status (SELECT *) sees the state column.
try:
    _db().close()
except Exception:
    pass
