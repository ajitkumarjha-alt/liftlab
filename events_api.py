"""
Events feature for liftlab cloud (B4 graft) — ADDITIVE.

Adds the on-site-derived events intake + an operator events dashboard to the
existing B3 job-cloud, WITHOUT touching the gateway/job/upload code or their
tables. Everything here is new: new tables (created IF NOT EXISTS, so safe on
the live gateway.db) and new routes.

  POST /api/gw/events   Bearer-authed with the gateway token (matched to the
                        payload's gateway_id, same trust model as the other
                        /api/gw/* endpoints). Stores derived door events only —
                        NO imagery. Excluded from Caddy basicauth like the rest
                        of /api/gw/* , so the Pi can reach it with its token.
  GET  /events          Operator dashboard (behind Caddy basicauth, since it is
                        NOT under /api/gw/*). Per gateway/camera door-event log and
                        close-travel distribution — no thresholds, no verdicts;
                        the page reports observations, conclusions are the
                        reader's. Events whose OPENING
                        ramp was clipped (open_valid=false) still count — their
                        close is measured — and are flagged, because motion
                        exports routinely drop the opening pre-roll.

EMIT-FACTS-FLAG-QUALITY (2026-07-17): the machine emits every detected cycle and
a QUALITY flag; ANALYSIS filters. Two filters used to silently shrink the
headline close-travel distribution: (1) write-time _quality_ok on the Pi
(instrumented separately on /pihealth), and (2) the read-time DUPLICATE-CLUSTER
withhold below — openings that emitted >1 row were dropped from the stats. Both
are now REPORTED, not hidden: the clean median plus how many openings were
withheld each way and what their closes look like. Default view is clean;
?flagged=1 surfaces the withheld/flagged rows. Rejecting at emission is the
machine drawing the conclusion — so we flag instead.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("GATEWAY_DB", "./gateway.db")

# Same token map the gateway API uses (gateway_id -> token), read from env.
GATEWAY_TOKENS = {
    g.split(":", 1)[0]: g.split(":", 1)[1]
    for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",")
    if ":" in g
}


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS gw_source (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gateway_id TEXT NOT NULL, camera TEXT NOT NULL,
        declared_tz TEXT, tz_source TEXT, start_ts TEXT, created_at REAL);
    CREATE TABLE IF NOT EXISTS gw_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        door_open_start_ts TEXT, door_open_full_ts TEXT,
        door_close_start_ts TEXT, door_close_full_ts TEXT,
        close_travel_s REAL, open_valid INTEGER DEFAULT 1,
        plateau REAL, ramp_residual REAL,
        floor TEXT, boarded INTEGER, alighted INTEGER, created_at REAL);
    """)
    # emit-facts-flag-quality: CREATE TABLE IF NOT EXISTS will NOT add a column to the existing
    # (999-row) table, so a guarded ALTER is REQUIRED — else `quality` silently never appears while
    # everything looks fine. Legacy rows keep quality=NULL, which the dashboard treats as 'ok'.
    cols = [r[1] for r in db.execute("PRAGMA table_info(gw_event)").fetchall()]
    if "quality" not in cols:
        db.execute("ALTER TABLE gw_event ADD COLUMN quality TEXT")
    return db


events_router = APIRouter()


class GwEvents(BaseModel):
    gateway_id: str
    camera: str
    declared_tz: str = "Asia/Kolkata"
    tz_source: str = "guessed"
    start_ts: str | None = None
    events: list[dict] = []
    door_signal: list = []          # numeric openness only — never imagery


def _auth(gateway_id: str, authorization: str) -> None:
    tok = authorization.removeprefix("Bearer ").strip()
    if not tok or GATEWAY_TOKENS.get(gateway_id) != tok:
        raise HTTPException(401, "bad gateway token")


@events_router.post("/api/gw/events")
def gw_events(payload: GwEvents, authorization: str = Header(default="")):
    """Cloud intake for on-site-derived events. Bearer-authed; stores rows keyed
    by a fresh source (one analyze_local window). Receives NO imagery."""
    _auth(payload.gateway_id, authorization)
    db = _db()
    sid = db.execute(
        "INSERT INTO gw_source (gateway_id,camera,declared_tz,tz_source,start_ts,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (payload.gateway_id, payload.camera, payload.declared_tz, payload.tz_source,
         payload.start_ts, time.time())).lastrowid
    stored = 0
    for ev in payload.events:
        q = ev.get("quality") or "ok"       # emit-facts-flag-quality: 'ok' | reason string
        # close_travel_s DERIVED at ingest ONLY when the close is trustworthy (quality ok). A cycle
        # flagged as a bad close keeps close_travel_s=NULL — we emit the FACT (open/close ts) but
        # WITHHOLD the untrustworthy measurement rather than fabricate one for a door we couldn't measure.
        if (q == "ok" and ev.get('close_travel_s') is None
                and ev.get('door_close_full_ts') and ev.get('door_close_start_ts')):
            try:
                from datetime import datetime as _cdt
                ev['close_travel_s'] = round((_cdt.fromisoformat(ev['door_close_full_ts']) - _cdt.fromisoformat(ev['door_close_start_ts'])).total_seconds(), 3)
            except Exception:
                pass
        try:
            db.execute(
                "INSERT INTO gw_event (source_id,door_open_start_ts,door_open_full_ts,"
                "door_close_start_ts,door_close_full_ts,close_travel_s,open_valid,"
                "plateau,ramp_residual,floor,boarded,alighted,quality,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, ev.get("door_open_start_ts"), ev.get("door_open_full_ts"),
                 ev.get("door_close_start_ts"), ev.get("door_close_full_ts"),
                 ev.get("close_travel_s"), 1 if ev.get("open_valid", True) else 0,
                 ev.get("plateau"), ev.get("ramp_residual"), ev.get("floor"),
                 ev.get("boarded"), ev.get("alighted"), q, time.time()))
            stored += 1
        except Exception:
            continue
    db.commit(); db.close()
    return {"stored": stored, "source_file_id": sid,
            "gateway": payload.gateway_id, "camera": payload.camera}


def _esc(x) -> str:
    return "" if x is None else str(x)


@events_router.get("/events", response_class=HTMLResponse)
def events_dashboard(flagged: int = 0):
    import statistics as _st
    from collections import Counter as _Counter
    from datetime import datetime as _dt
    db = _db()
    rows = db.execute(
        "SELECT s.gateway_id AS gw, s.camera AS cam, e.door_open_start_ts AS os,"
        " e.close_travel_s AS ct, e.open_valid AS ov, e.floor AS floor, e.quality AS q"
        " FROM gw_event e JOIN gw_source s ON s.id = e.source_id"
        " ORDER BY e.door_open_start_ts DESC").fetchall()
    db.close()

    def _pt(v):
        try:
            return _dt.fromisoformat(v)
        except Exception:
            return None

    def _clean_q(r):
        return r["q"] is None or r["q"] == "ok"   # legacy NULL == clean

    by_cam = {}
    for r in rows:
        by_cam.setdefault(f'{r["gw"]}/{r["cam"]}', []).append(r)

    CAP = 100
    blocks = []
    for key, rs in sorted(by_cam.items()):
        total = len(rs)
        # cluster on the sub-ms-stable open edge (rows are DESC; sort a copy ASC) to
        # count PHYSICAL opens and hold a cluster's disagreeing closes out of the stats.
        _asc = sorted(rs, key=lambda r: (r["os"] or ""))
        _cl, _cur, _prev = [], [], None
        for r in _asc:
            t = _pt(r["os"])
            if _cur and _prev and t and (t - _prev).total_seconds() < 0.5:
                _cur.append(r)
            else:
                if _cur:
                    _cl.append(_cur)
                _cur = [r]
            _prev = t
        if _cur:
            _cl.append(_cur)
        opens = len(_cl)
        dup_clusters = [g for g in _cl if len(g) > 1]
        singles = [g[0] for g in _cl if len(g) == 1]

        # THE HEADLINE (clean): single-cluster AND quality-clean AND ct present. BOTH filters explicit.
        clean_cts = sorted(float(r["ct"]) for r in singles if r["ct"] is not None and _clean_q(r))
        n = len(clean_cts)
        n_clip = sum(1 for r in rs if not r["ov"])

        # WITHHOLD LAYER 1 — read-time duplicate pairing: EVERY row in a multi-row cluster is dropped
        # from the distribution today. MEASURE it (the question: are these the messy/held openings?).
        withheld_cts = sorted(float(r["ct"]) for g in dup_clusters for r in g if r["ct"] is not None)
        n_dup = len(dup_clusters)

        # WITHHOLD LAYER 2 — write-time quality flag (NULL today; populated once the watch emits flagged).
        flagged_rows = [r for r in rs if not _clean_q(r)]
        qcounts = _Counter(r["q"] for r in flagged_rows)

        if clean_cts:
            p85 = clean_cts[min(n - 1, int(0.85 * n))]
            dist = ("%d opens · %d CLEAN closes · median %.2fs · p85 %.2fs · min %.2fs · max %.2fs"
                    % (opens, n, _st.median(clean_cts), p85, clean_cts[0], clean_cts[-1]))
        else:
            dist = "%d opens · no clean close measured" % opens

        notes = []
        if n_dup:
            if withheld_cts:
                notes.append("%d openings WITHHELD from the median (duplicate closes, read-time) — "
                             "their closes: median %.2fs, range %.2f–%.2fs, n=%d"
                             % (n_dup, _st.median(withheld_cts), withheld_cts[0], withheld_cts[-1], len(withheld_cts)))
            else:
                notes.append("%d openings withheld (duplicate closes) — none carried a close_travel" % n_dup)
        if flagged_rows:
            notes.append("%d flagged (write-time quality, withheld from median): %s"
                         % (len(flagged_rows), ", ".join("%s×%d" % (k, v) for k, v in qcounts.most_common())))
        note_html = ("<br>".join('<span style="color:#b06a00">%s</span>' % _esc(x) for x in notes)) if notes else ""

        # table: default shows CLEAN rows; ?flagged=1 surfaces the withheld/flagged ones to eyeball.
        view = rs if flagged else [r for r in rs if _clean_q(r)]
        shown = min(len(view), CAP)
        trs = []
        for r in view[:CAP]:
            f = ""
            if not r["ov"]:
                f += '<span title="opening ramp clipped in the export; close still measured" style="color:#b06a00">open?</span> '
            if not _clean_q(r):
                f += ('<span title="quality flag: %s — close_travel withheld; open/close ts are still real" '
                      'style="color:#c0392b">%s</span>' % (_esc(r["q"]), _esc(r["q"])))
            ct = "—" if r["ct"] is None else ("%.2f" % r["ct"])
            trs.append('<tr><td>%s</td><td>%s</td><td style="text-align:right">%s</td><td>%s</td></tr>'
                       % (_esc(r["os"])[11:19], _esc(r["floor"]), ct, f))
        _gw = key.split("/", 1)[0]
        toggle = ('<a href="/events">← clean only</a>' if flagged
                  else '<a href="/events?flagged=1">show withheld/flagged rows</a>')
        blocks.append("""
        <section>
          <h2>%s <span style="color:#666;font-family:var(--mono);font-size:12px">%s</span></h2>
          <p class=muted style="font-size:12px">%d openings · %d with clipped opening (close still counted)
          &nbsp;·&nbsp; showing %d of %d rows &nbsp;·&nbsp; %s &nbsp;·&nbsp; <a href="/pihealth/%s">pi health</a></p>
          %s
          <table><thead><tr><th>open</th><th>floor</th><th>close (s)</th><th></th></tr></thead>
          <tbody>%s</tbody></table>
        </section>""" % (key, dist, opens, n_clip, shown, total, toggle, _gw,
                         ('<p class=muted style="font-size:12px;margin-top:2px">%s</p>' % note_html) if note_html else "",
                         "".join(trs)))

    body = ("<p class='muted'>No gateway events yet. The Pi posts to "
            "<code>/api/gw/events</code>.</p>" if not blocks else "".join(blocks))
    return """<!doctype html><meta charset=utf-8><title>liftlab · events</title>
    <meta http-equiv="refresh" content="30">
    <style>:root{--mono:ui-monospace,Consolas,monospace}
    body{background:#fff;color:#1a1a1a;font:14px system-ui;max-width:900px;margin:auto;padding:20px}
    h1{font-size:14px;letter-spacing:.2em;text-transform:uppercase;color:#333}
    h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#666;margin:18px 0 4px}
    section{border-bottom:1px solid #e2e2e2;padding-bottom:14px;margin-bottom:8px}
    table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px;margin-top:8px}
    th{color:#888;text-align:left;font-weight:500;border-bottom:1px solid #ddd;padding:5px}
    td{padding:5px;border-bottom:1px solid #eee} .muted{color:#666}
    code{color:#b06a00} a{color:#0a6}</style>
    <h1>liftlab · cloud events</h1>
    <p class=muted>Per-camera door events from on-site gateways. Footage stays on site;
    only derived rows arrive here. Live (30s refresh). &nbsp;·&nbsp;
    <a href="/events.csv">download CSV</a> &nbsp;·&nbsp; <a href="/">← fleet dashboard</a></p>
    """ + body


@events_router.get("/events.csv")
def events_csv():
    import csv as _csv, io as _io
    from fastapi.responses import Response as _Resp
    db = _db()
    rows = db.execute("SELECT e.*, s.gateway_id AS _gw, s.camera AS _cam"
                      " FROM gw_event e JOIN gw_source s ON s.id = e.source_id"
                      " ORDER BY e.door_open_start_ts DESC").fetchall()
    db.close()
    cols = ["id", "gateway_id", "camera", "door_open_start_ts", "door_open_full_ts",
            "door_close_start_ts", "door_close_full_ts", "close_travel_s", "open_valid",
            "plateau", "ramp_residual", "floor", "boarded", "alighted", "quality", "created_at"]

    def _cell(r, c):
        if c == "gateway_id":
            return r["_gw"]
        if c == "camera":
            return r["_cam"]
        try:
            return r[c]
        except Exception:
            return ""
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if _cell(r, c) is None else _cell(r, c) for c in cols])
    return _Resp(buf.getvalue(), media_type="text/csv",
                 headers={"Content-Disposition": "attachment; filename=gw_events.csv"})
