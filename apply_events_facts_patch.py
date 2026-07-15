#!/usr/bin/env python3
"""Strip the FABRICATED compliance verdict from /events; render FACTS ONLY.

Deletes DECISION_LINE_S and replaces events_dashboard wholesale with: per-camera
physical-open counts (clusters of emissions within 0.5s on door_open_full_ts = ONE
opening), a SINGLETON-ONLY close-travel distribution (median/p85/min/max, n), the
open_valid 'open?' data-quality flag (kept — a marker, not a verdict), and a
data-quality note disclosing openings whose close-travel is withheld (pairing bug
pending). No threshold, no verdict, no compliant/non-compliant colour.

Binds to the file's own idioms: _db(), _esc(), the gw_event->gw_source join, and the
aliases gw/cam/os. Backup-first, PRE-WRITE compile check (never writes a broken file)."""
import os
import pathlib
import re
import shutil
import time

# The replacement function, verbatim. Uses only """ internally, so '''-wrapping is safe.
FACTS = '''@events_router.get("/events", response_class=HTMLResponse)
def events_dashboard():
    import sqlite3 as _sq, statistics as _st
    from datetime import datetime as _dt
    db = _db()
    try:
        db.row_factory = _sq.Row
    except Exception:
        pass
    rows = db.execute(
        "SELECT s.gateway_id AS gw, s.camera AS cam, e.door_open_start_ts AS os,"
        " e.door_open_full_ts AS ofull, e.close_travel_s AS ct, e.open_valid AS ov,"
        " e.floor AS floor"
        " FROM gw_event e JOIN gw_source s ON s.id = e.source_id"
        " ORDER BY e.door_open_full_ts").fetchall()
    db.close()

    def _parse(v):
        try:
            return _dt.fromisoformat(v)
        except Exception:
            return None

    by_cam = {}
    for r in rows:
        by_cam.setdefault((r["gw"], r["cam"]), []).append(r)

    blocks = []
    total_dupe = 0
    for (gw, cam), rs in sorted(by_cam.items()):
        rr = sorted(rs, key=lambda r: (r["ofull"] or ""))
        clusters, cur, prev = [], [], None
        for r in rr:
            t = _parse(r["ofull"])
            if cur and prev and t and (t - prev).total_seconds() < 0.5:
                cur.append(r)
            else:
                if cur:
                    clusters.append(cur)
                cur = [r]
            prev = t
        if cur:
            clusters.append(cur)
        opens = len(clusters)
        dupe = sum(1 for g in clusters if len(g) > 1)
        total_dupe += dupe
        singles = [g[0] for g in clusters if len(g) == 1]
        cts = sorted(float(r["ct"]) for r in singles if r["ct"] is not None)
        n = len(cts)
        if cts:
            p85 = cts[min(n - 1, int(0.85 * n))]
            dist = ("close-travel (n=%d): median %.2fs &middot; p85 %.2fs &middot; min %.2fs &middot; max %.2fs"
                    % (n, _st.median(cts), p85, cts[0], cts[-1]))
        else:
            dist = "close-travel: no clean closes yet"
        hdr = "%d opens &middot; %d closes" % (opens, n)
        if dupe:
            hdr += " &middot; %d openings close-travel withheld" % dupe
        trs = []
        for r in rr[-60:]:
            cts_cell = "" if r["ct"] is None else ("%.2f" % float(r["ct"]))
            flag = "open?" if r["ov"] in (0, "0", False) else ""
            fl = _esc(str(r["floor"])) if r["floor"] is not None else ""
            trs.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (_esc(str(r["os"] or "")), fl, cts_cell, flag))
        blocks.append(
            "<h2>%s / %s</h2><p class=facts>%s<br>%s</p>"
            "<table><thead><tr><th>open</th><th>floor</th><th>close (s)</th><th>flag</th></tr></thead>"
            "<tbody>%s</tbody></table>"
            % (_esc(str(gw)), _esc(str(cam)), hdr, dist, "".join(trs)))

    note = ("%d openings with unstable close measurement (pairing fix pending) &mdash; "
            "counted, close-travel withheld from stats." % total_dupe) if total_dupe else ""
    return ("<!doctype html><meta charset=utf-8><title>events &middot; facts</title>"
            "<style>body{font-family:system-ui,sans-serif;background:#0f1417;color:#dbe3e6;margin:2rem}"
            "h1{font-size:20px}h2{font-size:15px;color:#9fb0b8;margin-top:1.4rem}"
            ".facts{font-family:monospace;font-size:13px;color:#cfe3d8}"
            ".note{font-family:monospace;font-size:12px;color:#e3a53f}"
            "table{border-collapse:collapse;font-family:monospace;font-size:12px;margin-top:.3rem}"
            "td,th{border:1px solid #223a42;padding:2px 8px;text-align:left}th{color:#7a8b93}</style>"
            "<h1>events &middot; facts</h1><p class=note>" + note + "</p>"
            + ("".join(blocks) or "<p>No events yet.</p>"))
'''

p = pathlib.Path(os.environ.get("EVENTS_API", "/opt/liftlab-b3/cloud/events_api.py"))
s = p.read_text()
if "events &middot; facts" in s:
    print("events_api.py already facts-only — skip")
    raise SystemExit

lines = s.split("\n")
# 1) delete the DECISION_LINE_S assignment (module-level)
lines = [ln for ln in lines if not re.match(r"\s*DECISION_LINE_S\s*=", ln)]

# 2) find the /events route block: decorator -> def -> body, until the next module-level line
dec = next((i for i, ln in enumerate(lines)
            if re.match(r'@events_router\.get\(\s*["\']/events["\']', ln)), None)
if dec is None:
    print("ANCHOR NOT FOUND (@events_router.get('/events')) — no write")
    raise SystemExit
di = next((i for i in range(dec, len(lines)) if lines[i].lstrip().startswith("def ")), None)
if di is None:
    print("ANCHOR NOT FOUND (def after /events decorator) — no write")
    raise SystemExit
end = len(lines)
for i in range(di + 1, len(lines)):
    ln = lines[i]
    if ln.strip() and not ln[0].isspace():   # first column-0 line ends the function
        end = i
        break

new_lines = lines[:dec] + FACTS.rstrip("\n").split("\n") + lines[end:]
new_s = "\n".join(new_lines)

try:
    compile(new_s, str(p), "exec")
except SyntaxError as e:
    print(f"patched content does NOT compile ({e}) — aborting, NO write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
p.write_text(new_s)
print(f"events_api.py patched: /events is FACTS-ONLY (verdict+DECISION_LINE_S removed); backup written")
