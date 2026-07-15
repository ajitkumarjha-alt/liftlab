#!/usr/bin/env python3
"""Dashboard upgrade for /events (facts-only) + a downloadable /events.csv.
Replaces events_dashboard wholesale (keeping the file's _db()/_esc()/gw_source JOIN/
aliases/styles) with: ORDER BY door_open_start_ts DESC, latest-100-per-camera cap with
"showing latest N of M", 30s meta-refresh, per-camera physical-open counts + singleton-
only close-travel distribution, open_valid flag, withheld-cluster note, CSV + fleet
links. Adds GET /events.csv (full gw_event JOIN gw_source, ALL columns, no cap,
attachment). Also deletes any leftover DECISION_LINE_S. Self-sufficient on original OR
stripped file. Fail-CLOSED anchors, backup-first, pre-write compile check."""
import os
import pathlib
import re
import shutil
import time

REPL = '''@events_router.get("/events", response_class=HTMLResponse)
def events_dashboard():
    import statistics as _st
    from datetime import datetime as _dt
    db = _db()
    rows = db.execute(
        "SELECT s.gateway_id AS gw, s.camera AS cam, e.door_open_start_ts AS os,"
        " e.close_travel_s AS ct, e.open_valid AS ov, e.floor AS floor"
        " FROM gw_event e JOIN gw_source s ON s.id = e.source_id"
        " ORDER BY e.door_open_start_ts DESC").fetchall()
    db.close()

    def _pt(v):
        try:
            return _dt.fromisoformat(v)
        except Exception:
            return None

    by_cam = {}
    for r in rows:
        by_cam.setdefault(f\'{r["gw"]}/{r["cam"]}\', []).append(r)

    CAP = 100
    blocks = []
    for key, rs in sorted(by_cam.items()):
        total = len(rs)
        # cluster on the sub-ms-stable open edge (rows are DESC; sort a copy ASC) to
        # count PHYSICAL opens and hold a cluster\\'s disagreeing closes out of the stats.
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
        dupe = sum(1 for g in _cl if len(g) > 1)
        _singles = [g[0] for g in _cl if len(g) == 1]
        cts = sorted(float(r["ct"]) for r in _singles if r["ct"] is not None)
        n = len(cts)
        n_clip = sum(1 for r in rs if not r["ov"])
        if cts:
            p85 = cts[min(n - 1, int(0.85 * n))]
            dist = ("%d opens · %d closes · median %.2fs · p85 %.2fs · min %.2fs · max %.2fs"
                    % (opens, n, _st.median(cts), p85, cts[0], cts[-1]))
        else:
            dist = "%d opens · no clean close measured" % opens
        dqnote = (" · %d openings close-travel withheld (pairing fix pending)" % dupe) if dupe else ""
        shown = min(total, CAP)
        trs = []
        for r in rs[:CAP]:
            flag = "" if r["ov"] else \'<span title="opening ramp clipped in the export; close still measured" style="color:#e3a53f">open?</span>\'
            ct = "—" if r["ct"] is None else ("%.2f" % r["ct"])
            trs.append(\'<tr><td>%s</td><td>%s</td><td style="text-align:right">%s</td><td>%s</td></tr>\'
                       % (_esc(r["os"])[11:19], _esc(r["floor"]), ct, flag))
        blocks.append("""
        <section>
          <h2>%s <span style="color:#7a8b93;font-family:var(--mono);font-size:12px">%s</span></h2>
          <p class=muted style="font-size:12px">%d openings · %d with clipped opening (close still counted)%s
          &nbsp;·&nbsp; showing latest %d of %d rows</p>
          <table><thead><tr><th>open</th><th>floor</th><th>close (s)</th><th></th></tr></thead>
          <tbody>%s</tbody></table>
        </section>""" % (key, dist, opens, n_clip, dqnote, shown, total, "".join(trs)))

    body = ("<p class=\\'muted\\'>No gateway events yet. The Pi posts to "
            "<code>/api/gw/events</code>.</p>" if not blocks else "".join(blocks))
    return """<!doctype html><meta charset=utf-8><title>liftlab · events</title>
    <meta http-equiv="refresh" content="30">
    <style>:root{--mono:ui-monospace,Consolas,monospace}
    body{background:#0e1417;color:#dbe3e6;font:14px system-ui;max-width:900px;margin:auto;padding:20px}
    h1{font-size:14px;letter-spacing:.2em;text-transform:uppercase;color:#e3a53f}
    h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#7a8b93;margin:18px 0 4px}
    section{border-bottom:1px solid #26333a;padding-bottom:14px;margin-bottom:8px}
    table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px;margin-top:8px}
    th{color:#7a8b93;text-align:left;font-weight:500;border-bottom:1px solid #26333a;padding:5px}
    td{padding:5px;border-bottom:1px solid rgba(38,51,58,.5)} .muted{color:#7a8b93}
    code{color:#e3a53f} a{color:#63a37e}</style>
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
            "plateau", "ramp_residual", "floor", "boarded", "alighted", "created_at"]

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
'''

p = pathlib.Path(os.environ.get("EVENTS_API", "/opt/liftlab-b3/cloud/events_api.py"))
s = p.read_text(encoding="utf-8")
if "/events.csv" in s or "events_csv" in s:
    print("events_api.py already has the CSV/dashboard upgrade — skip")
    raise SystemExit

lines = s.split("\n")
lines = [ln for ln in lines if not re.match(r"\s*DECISION_LINE_S\s*=", ln)]  # drop leftover const

dec = next((i for i, ln in enumerate(lines)
            if re.match(r'@events_router\.get\(\s*["\']/events["\']', ln)), None)
if dec is None:
    print("ANCHOR NOT FOUND (@events_router.get('/events')) — no write"); raise SystemExit
di = next((i for i in range(dec, len(lines)) if lines[i].lstrip().startswith("def ")), None)
if di is None:
    print("ANCHOR NOT FOUND (def after decorator) — no write"); raise SystemExit
end = len(lines)
for i in range(di + 1, len(lines)):
    if lines[i].strip() and not lines[i][0].isspace():
        end = i
        break

new_s = "\n".join(lines[:dec] + REPL.rstrip("\n").split("\n") + lines[end:])
try:
    compile(new_s, str(p), "exec")
except SyntaxError as e:
    print(f"patched content does NOT compile ({e}) — aborting, NO write"); raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
p.write_text(new_s, encoding="utf-8")
print("events_api.py patched: /events DESC+cap100+30s-refresh facts page + /events.csv download; backup written")
