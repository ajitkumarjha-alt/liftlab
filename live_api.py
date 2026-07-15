"""
Live HLS relay proxy for liftlab cloud — TRANSIENT, not an archive. ADDITIVE.

A Pi ffmpeg PUTs HLS (index.m3u8 + segNNN.ts) as it encodes; the VM serves it to a
browser behind Caddy basicauth. Segments live on TMPFS (/dev/shm) and are pruned to a
rolling window, so nothing durable is written and the disk never fills.

Pi-facing (Bearer, under /api/gw/* — Caddy exempts these from basicauth):
  PUT    /api/gw/{gw}/live/{cam}/{fname}   ffmpeg uploads a playlist or .ts segment
  DELETE /api/gw/{gw}/live/{cam}/{fname}   ffmpeg evicts an old segment (delete_segments)

Operator (basicauth via Caddy):
  GET  /live/{gw}/{cam}         HLS.js viewer page (Chrome; native HLS is Safari-only)
  GET  /live/{gw}/{cam}/{fname} serve index.m3u8 / seg.ts from tmpfs (no-cache)

This is a PROOF-OF-CONCEPT relay for ONE camera. It is not the product.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

# TMPFS by default: segments are RAM-backed, vanish on reboot, never an archive.
LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
KEEP_SEGMENTS = int(os.environ.get("LIVE_KEEP_SEGMENTS", "12"))  # ~24s @ 2s segments
DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")           # same DB survey_api writes
GATEWAY_TOKENS = {
    g.split(":", 1)[0]: g.split(":", 1)[1]
    for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g
}

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")          # no '/', no '..', no traversal
_ALLOWED_EXT = (".m3u8", ".ts", ".mp4", ".m4s")
_CT = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
       ".mp4": "video/mp4", ".m4s": "video/iso.segment"}

# Per-cam DELIVERED-bytes counters. This is ground truth for a relay under a saturated
# uplink: ffmpeg stays 'alive' and reads the source at 1x while the PUT blocks and drops,
# so process liveness lies. Only bytes that LAND here count as delivered. In-memory,
# monotonic; a load test reads deltas, so a restart wiping them is fine.
_STATS: dict[tuple, dict] = {}

live_router = APIRouter()


def _auth(gw: str, authorization: str) -> None:
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if not tok or GATEWAY_TOKENS.get(gw) != tok:
        raise HTTPException(401, "bad gateway token")


def _safe(*parts: str) -> None:
    for p in parts:
        if not p or not _SAFE.match(p):
            raise HTTPException(400, "bad name")


def _camdir(gw: str, cam: str) -> Path:
    d = LIVE_DIR / gw / cam
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(d: Path) -> None:
    """Rolling window: keep only the newest KEEP_SEGMENTS .ts by mtime; delete the rest.
    Belt-and-suspenders with ffmpeg's own delete_segments — the VM never accumulates."""
    segs = sorted(d.glob("*.ts"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in segs[KEEP_SEGMENTS:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------- Pi-facing: HLS upload (Bearer) ----------------
@live_router.put("/api/gw/{gw}/live/{cam}/{fname}")
async def live_put(gw: str, cam: str, fname: str, request: Request,
                   authorization: str = Header("")):
    _auth(gw, authorization)
    _safe(gw, cam, fname)
    if not fname.endswith(_ALLOWED_EXT):
        raise HTTPException(400, "bad extension")
    body = await request.body()
    # Do the blocking disk work OFF the event loop. As an async handler, these sync writes
    # + the glob-based prune ran ON the single uvicorn worker's loop — while one PUT wrote,
    # the loop could not read any other stream's body, stalling every other stream. That is a
    # prime suspect for the one-winner starvation. run_in_threadpool hands it to the anyio
    # worker pool (default 40) so concurrent PUTs actually overlap.
    def _persist():
        d = _camdir(gw, cam)
        tmp = d / (fname + ".part")
        tmp.write_bytes(body)                      # atomic swap so GETs never see a half file
        tmp.replace(d / fname)
        if fname.endswith(".ts"):
            st = _STATS.setdefault((gw, cam), {"bytes": 0, "segs": 0, "last": 0.0})
            st["bytes"] += len(body)               # DELIVERED bytes — the honest per-stream signal
            st["segs"] += 1
            st["last"] = time.time()
            _prune(d)
    await run_in_threadpool(_persist)
    return PlainTextResponse("ok")


@live_router.put("/api/gw/{gw}/blackhole")
async def blackhole(gw: str, request: Request, authorization: str = Header("")):
    """Pure-transport sink: stream the body and discard, NO disk work. Isolates the
    link + Caddy + uvicorn concurrency from the live_put handler. If N concurrent PUTs
    here scale to the link's real capacity but live_put starves, the bug is the handler
    (or ffmpeg), not the link. If this ALSO collapses to one-winner, the wall is below the
    app — Caddy, the uvicorn accept loop, or wifi."""
    _auth(gw, authorization)
    n = 0
    async for chunk in request.stream():
        n += len(chunk)
    return {"bytes": n}


@live_router.get("/api/gw/{gw}/live_stats")
async def live_stats(gw: str, authorization: str = Header("")):
    """Cumulative DELIVERED bytes/segments per cam. A ramp reads this at window start/end;
    (b1-b0)/dt = bytes actually landed per stream. Catches uplink rationing that process
    liveness (ffmpeg speed=1x) hides."""
    _auth(gw, authorization)
    cams = {cam: {"bytes": s["bytes"], "segs": s["segs"], "last": s["last"]}
            for (g, cam), s in _STATS.items() if g == gw}
    return {"gateway": gw, "t": time.time(), "cams": cams}


@live_router.get("/api/gw/{gw}/lift_channels")
async def lift_channels(gw: str, authorization: str = Header("")):
    """The marked lift cabins from the survey (channel_map.is_lift=1). Bearer-authed so
    the Pi can read the source of truth instead of hardcoding a channel list."""
    _auth(gw, authorization)
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1 ORDER BY channel",
                (gw,)).fetchall()
            chans = [int(r["channel"]) for r in rows]
        except sqlite3.OperationalError:
            chans = []                                # channel_map not created yet
        db.close()
    except Exception as e:
        raise HTTPException(500, f"db: {type(e).__name__}")
    return {"gateway": gw, "channels": chans}


@live_router.delete("/api/gw/{gw}/live/{cam}/{fname}")
async def live_delete(gw: str, cam: str, fname: str, authorization: str = Header("")):
    _auth(gw, authorization)
    _safe(gw, cam, fname)
    try:
        (LIVE_DIR / gw / cam / fname).unlink()
    except OSError:
        pass
    return PlainTextResponse("ok")


# ---------------- Operator: viewer + serve (basicauth via Caddy) ----------------
_PAGE = """<!doctype html><meta charset=utf-8><title>live {gw}/{cam}</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{margin:0;background:#0e1418;color:#c9d3d9;font:14px/1.5 system-ui,sans-serif}}
header{{padding:10px 14px;border-bottom:1px solid #22303a;display:flex;gap:14px;align-items:baseline}}
h1{{font-size:15px;margin:0;font-weight:600}} .m{{font:12px ui-monospace,monospace;color:#7a8b93}}
video{{width:100%;max-height:80vh;background:#000;display:block}}
#s{{padding:8px 14px;font:12px ui-monospace,monospace;color:#7a8b93}}</style>
<header><h1>live · {gw} · {cam}</h1><span class=m id=lat>latency —</span>
<span class=m>transient HLS relay · not recorded</span></header>
<video id=v controls autoplay muted playsinline></video><div id=s>loading hls.js…</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<script>
var v=document.getElementById('v'),s=document.getElementById('s'),lat=document.getElementById('lat');
var url="index.m3u8";
function tick(){{try{{var e=v.buffered.length?v.buffered.end(v.buffered.length-1):0;
 lat.textContent="buffer ahead "+(e-v.currentTime).toFixed(1)+"s";}}catch(_){{}}}}
setInterval(tick,1000);
if(window.Hls&&Hls.isSupported()){{
 var h=new Hls({{lowLatencyMode:true,liveSyncDurationCount:2,maxBufferLength:6}});
 h.loadSource(url);h.attachMedia(v);
 h.on(Hls.Events.MANIFEST_PARSED,function(){{s.textContent="playing (hls.js)";v.play().catch(function(){{}});}});
 h.on(Hls.Events.ERROR,function(_,d){{s.textContent="hls error: "+d.type+" / "+d.details+(d.fatal?" (fatal)":"");}});
}}else if(v.canPlayType('application/vnd.apple.mpegurl')){{
 v.src=url;s.textContent="playing (native HLS — Safari)";
}}else{{s.textContent="this browser can't play HLS";}}
</script>"""


@live_router.get("/live/{gw}/{cam}")
async def live_page(gw: str, cam: str):
    _safe(gw, cam)
    return HTMLResponse(_PAGE.format(gw=gw, cam=cam))


@live_router.get("/live/{gw}/{cam}/{fname}")
async def live_serve(gw: str, cam: str, fname: str):
    _safe(gw, cam, fname)
    f = LIVE_DIR / gw / cam / fname
    if not f.exists():
        raise HTTPException(404, "not found")
    ct = _CT.get(f.suffix, "application/octet-stream")
    headers = {"Cache-Control": "no-store, no-cache, max-age=0"}
    return Response(content=f.read_bytes(), media_type=ct, headers=headers)
