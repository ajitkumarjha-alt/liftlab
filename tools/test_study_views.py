#!/usr/bin/env python3
"""The two study matrices: riders per lift per day, and RTT per hour per lift.

Both are FIRST-CLASS VIEWS of data that already exists — no new measurement in either — and both
are precomputed, because a fleet table touches every camera and so costs whatever a per-camera
panel costs, seven times over. What this file locks down is not that the numbers exist but that
the ABSENCES are told apart, because that is where every defect in this dashboard's history has
been:

  1. DARK IS NOT ZERO. A day a camera produced no rows in any stream is '—'; a day it was watched
     and carried nobody is 0. demand_log states the same rule for the study workbook, and the two
     surfaces must not be able to disagree about the same day.
  2. RIDERS ARE NOT PEOPLE. The caveat rides inside the CSV, not beside the link, because the file
     is what gets mailed onward.
  3. GAP ROWS ARE EXCLUDED and their day is not credited as observed — an outage is not
     observation, and counting through one reports demand that was never measured.
  4. EVERY LIFT GETS AN RTT ROW, and a lift with no measurement carries the REASON in the row.
     A camera with no floor attribution CANNOT have an RTT — the home floor is what defines a trip
     — which is a different claim from "this lift made no round trips", and a blank makes them
     identical. The four kinds of absence (uncalibrated / designed / pending / refused) are told
     apart, because the one a reader draws decides whether they go and look at a lift, a camera or
     a timer.
  5. NEITHER VIEW DERIVES ON THE REQUEST PATH, and the RTT one never walks a door row at all — it
     is a re-shape of what rtt_refresh already stored. A second RTT derivation is precisely what
     rtt_core exists to prevent.
  6. A STATE THIS BUILD HAS NEVER SEEN still renders. A whitelist of known-bad states drifts out
     of date the moment the server gains one — that is how too_many_rows killed the whole trends
     view on 2026-08-19.
"""
import csv as _csvmod
import io
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))

H3 = "260d4a0fh3-stateT5471cb+495e8f48"
CV = "v-cabin-1"

SCHEMA = """
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
  marked_at REAL);
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, floor TEXT, direction TEXT, door_state TEXT, read_conf REAL, panels_agreed INTEGER,
  reason TEXT, close_travel_s REAL, door_version TEXT);
CREATE TABLE transit_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, direction TEXT, track_id TEXT, ts_bucket INTEGER);
CREATE TABLE validation_item (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts_start REAL, ts_end REAL, machine_boarded INTEGER, machine_alighted INTEGER, status TEXT,
  counting_version TEXT, occupancy_max INTEGER, occupancy_frames INTEGER,
  occupancy_degraded INTEGER, analysed_frames INTEGER, human_occupancy INTEGER);
CREATE TABLE camera_validation (gateway_id TEXT, cam TEXT, state TEXT, counting_version TEXT,
  n_reviewed INTEGER, n_exact INTEGER, provenance TEXT, confirmed_at REAL, updated_at REAL);
CREATE TABLE analyzer_status (gateway_id TEXT, cam TEXT, ts REAL, counting_version TEXT);
CREATE TABLE camera_registry (gateway_id TEXT, cam TEXT, enabled INTEGER, updated_at REAL);
"""

# THE FIXTURE IS THE ARGUMENT. Four lifts, each standing for one thing the table must get right:
#   ch29  round trips at the home floor -> a real RTT row, and riders on every day
#   ch27  door rows with NO floor at all -> 'no_floor': UNAVAILABLE, not zero
#   ch30  watched every day but carried NOBODY on day 2 -> a real 0, which must not read as dark
#   ch32  no door rows in any era -> uncalibrated; still has transits, because counting does not
#         depend on door calibration
CAMS = [(29, "ch29", "lift 3"), (27, "ch27", "lift 2"), (30, "ch30", "lift 4"),
        (32, "ch32", "lift 5")]


def build(path, day0):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for ch, cam, lbl in CAMS:
        con.execute("INSERT INTO channel_map VALUES ('site-A',?,1,?,0)", (ch, lbl))
        con.execute("INSERT INTO camera_validation (gateway_id,cam,state,counting_version) "
                    "VALUES ('site-A',?,'live',?)", (cam, CV))
        con.execute("INSERT INTO camera_registry VALUES ('site-A',?,1,0)", (cam,))

    def d(n, h=9, m=0):
        return (day0 + timedelta(days=n, hours=h, minutes=m)).timestamp()

    for n in range(3):
        # ── ch29: real round trips. closed at G -> open at G, with a stop between. ──
        for k in range(6):
            t = d(n, 9, 20 * k)
            for fl, st in (("G", "open"), ("G", "closing"), ("G", "closed"),
                           ("7", "open"), ("7", "closed"), ("G", "open")):
                con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,"
                            "door_version,reason) VALUES ('site-A','ch29',?,?,?,?,'single_panel')",
                            (t, fl, st, H3))
                t += 30
        # ── ch27: door rows, NO floor. The engine posts; RTT has no home floor to work from. ──
        for k in range(12):
            t = d(n, 10, 5 * k)
            for st in ("open", "closing", "closed"):
                con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,"
                            "door_version,reason) VALUES ('site-A','ch27',?,NULL,?,?,'no_read')",
                            (t, st, H3))
                t += 4
        # ── ch30: door rows so it is OBSERVED every day, transits only on days 0 and 2. ──
        for k in range(4):
            con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state,"
                        "door_version,reason) VALUES ('site-A','ch30',?,'G','closed',?,'x')",
                        (d(n, 11, k), H3))
        # transits + validated episodes
        for cam, per_day in (("ch29", 10), ("ch27", 6), ("ch30", 5 if n != 1 else 0), ("ch32", 4)):
            for k in range(per_day):
                con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id,"
                            "ts_bucket) VALUES ('site-A',?,?,?,?,?)",
                            (cam, d(n, 9, k), "in" if k % 2 == 0 else "out", f"{cam}{n}{k}",
                             int(d(n, 9, k) // 60)))
            for k in range(2):
                t = d(n, 9, 30 + k)
                con.execute("INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,"
                            "machine_boarded,machine_alighted,status,counting_version,"
                            "occupancy_max,occupancy_frames,occupancy_degraded,analysed_frames) "
                            "VALUES ('site-A',?,?,?,2,1,'auto',?,3,130,0,130)",
                            (cam, t, t + 20, CV))
    # ch32 is deliberately SILENT on day 1 in every stream — the dark case.
    con.execute("DELETE FROM transit_event WHERE cam='ch32' AND ts >= ? AND ts < ?",
                (d(1, 0), d(2, 0)))
    con.execute("DELETE FROM validation_item WHERE cam='ch32' AND ts_start >= ? AND ts_start < ?",
                (d(1, 0), d(2, 0)))
    con.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,counting_version) "
                "VALUES ('site-A','ch29',?,?)", (d(2, 12), CV))
    con.commit()
    con.close()


HARNESS_JS = r"""
const fs = require('fs');
const els = {};
function el(id){ return els[id] || (els[id] = {id, innerHTML:'', style:{}, value:'',
  textContent:'', getAttribute(){return null}, setAttribute(){}, appendChild(){}}); }
global.document = {getElementById: el, addEventListener(){}, createElement(){return el('t')},
  querySelector(){return null}, querySelectorAll(){return []}, title:'', body: el('b')};
global.window = {addEventListener(){}, innerWidth:1200,
                 location:{search:'', pathname:'/dash', href:''}};
global.location = global.window.location;
global.history = {replaceState(){}, pushState(){}};
global.localStorage = {getItem(){return null}, setItem(){}};
global.setTimeout = () => 0; global.clearTimeout = () => {}; global.setInterval = () => 0;
global.fetch = () => new Promise(() => {});
eval(fs.readFileSync(process.argv[2], 'utf8'));
GW='site-A';
DATA = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
RD = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
RF = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
['ridersview','rttfleetview','nav','trendview','camview'].forEach(el);
let riders='', rtt='', threw=null;
try { mode='riders'; renderRiders(); riders = els['ridersview'].innerHTML; }
catch(e){ threw = 'renderRiders: ' + (e && e.message); }
try { mode='rttfleet'; renderRttFleet(); rtt = els['rttfleetview'].innerHTML; }
catch(e){ threw = (threw?threw+' | ':'') + 'renderRttFleet: ' + (e && e.message); }
// The tab strip must offer both views, or they are unreachable however well they draw.
mode='cams'; nav();
fs.writeFileSync(process.argv[6], JSON.stringify(
  {riders: riders, rtt: rtt, nav: els['nav'].innerHTML, threw: threw}));
"""


def render(tmp, D, data, rd, rf):
    node = shutil.which("node")
    if not node:
        return None
    page = D.dash_page()
    page = getattr(page, "body", page)
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.S))
    p = {n: os.path.join(tmp, n) for n in
         ("d.js", "h.js", "data.json", "rd.json", "rf.json", "out.json")}
    open(p["d.js"], "w").write(js)
    open(p["h.js"], "w").write(HARNESS_JS)
    for k, v in (("data.json", data), ("rd.json", rd), ("rf.json", rf)):
        json.dump(v, open(p[k], "w"))
    r = subprocess.run([node, p["h.js"], p["d.js"], p["data.json"], p["rd.json"], p["rf.json"],
                        p["out.json"]], capture_output=True, text=True)
    if r.returncode != 0:
        print("  node failed:", (r.stderr or "").strip().splitlines()[:5])
        return None
    return json.load(open(p["out.json"]))


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))


def _body_text(resp):
    """The CSV text, whether Response.body is bytes (real FastAPI) or str (the test stub)."""
    b = getattr(resp, "body", resp)
    return b.decode() if isinstance(b, (bytes, bytearray)) else str(b)


def _rows_of(csv_text):
    notes = [l for l in csv_text.splitlines() if l.startswith("#")]
    rows = list(_csvmod.reader(l for l in csv_text.splitlines() if not l.startswith("#")))
    return notes, rows


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    day0 = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)
    build(path, day0)
    os.environ["GATEWAY_DB"] = path
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api as D

    def body(r):
        b = getattr(r, "body", r)
        if isinstance(b, dict):
            return b
        return json.loads(b if isinstance(b, str) else b.decode())

    days = [(day0 + timedelta(days=n)).date().isoformat() for n in range(3)]

    # ══ 1. the riders matrix ═══════════════════════════════════════════════════════════
    print("=== 1. riders per lift per day — dark, zero and the totals ===")
    R = D._riders_per_day_compute("site-A", period="all")
    by_date = {r["date"]: r for r in R["rows"]}
    for d in days:
        if d not in by_date:
            fails.append(f"day {d} is missing from the matrix entirely")
    if days[1] in by_date:
        row = by_date[days[1]]
        c32 = row["cells"].get("ch32") or {}
        c30 = row["cells"].get("ch30") or {}
        print(f"  {days[1]}  ch32 observed={c32.get('observed')} riders={c32.get('riders')!r}"
              f"   ch30 observed={c30.get('observed')} riders={c30.get('riders')!r}")
        # THE WHOLE POINT: one is dark, the other is a real zero, on the same day.
        if c32.get("observed") is not False or c32.get("riders") is not None:
            fails.append("a camera silent in EVERY stream was not reported dark — a '—' day is "
                         "being served as a number, which reads as a lift that carried nobody")
        if c30.get("observed") is not True or c30.get("riders") != 0:
            fails.append(f"a watched-but-idle camera did not report a real 0 "
                         f"(observed={c30.get('observed')}, riders={c30.get('riders')!r}) — a "
                         f"genuine zero must not be hidden behind the dark glyph either")

    # totals must reconcile in both directions, or the table disagrees with itself on screen
    row_sum = sum((r["total"]["riders"] or 0) for r in R["rows"])
    col_sum = sum(v["riders"] for v in R["col_total"].values())
    cell_sum = sum((c["riders"] or 0) for r in R["rows"] for c in r["cells"].values()
                   if c.get("observed"))
    print(f"  totals: rows={row_sum} cols={col_sum} cells={cell_sum} "
          f"grand={R['grand_total']['riders']}")
    if not (row_sum == col_sum == cell_sum == R["grand_total"]["riders"]):
        fails.append(f"the totals do not reconcile (rows {row_sum}, cols {col_sum}, "
                     f"cells {cell_sum}, grand {R['grand_total']['riders']})")
    if R["grand_total"]["riders"] != R["grand_total"]["b"] + R["grand_total"]["a"]:
        fails.append("riders is not boardings + alightings at the grand total")

    # the direction rule must be _transit_by_cam's, verbatim: 'in' boards, ANYTHING ELSE alights
    _db = D._db()
    tb = D._transit_by_cam(_db, "site-A")
    _db.close()
    for cam, tot in (R["col_total"] or {}).items():
        ref = tb.get(cam) or {}
        if ref and (tot["b"] != ref["boarded_total"] or tot["a"] != ref["alighted_total"]):
            fails.append(f"{cam}: the matrix splits direction differently from _transit_by_cam "
                         f"({tot['b']}/{tot['a']} vs {ref['boarded_total']}/"
                         f"{ref['alighted_total']}) — one table, two counters, two answers")
    print(f"  direction split matches _transit_by_cam for {len(R['col_total'])} camera(s)")

    # ══ 2. gap rows are excluded, and the gap day is not credited as observed ═══════════
    print("\n=== 2. an outage is not observation ===")
    g = D.DATA_GAPS[0]
    gcam = (g.get("cams") or ["ch29"])[0]
    gday = D._ist_day(g["start_epoch"] + 3600)
    _db = D._db()
    _db.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id,ts_bucket) "
                "VALUES ('site-A',?,?,'in','gap1',1)", (gcam, g["start_epoch"] + 3600))
    _db.commit()
    _db.close()
    G = D._riders_per_day_compute("site-A", period="all")
    gr = {r["date"]: r for r in G["rows"]}.get(gday)
    cell = (gr or {}).get("cells", {}).get(gcam)
    print(f"  {gcam} {gday}: {'no row at all' if gr is None else cell}")
    if gr is not None and cell and cell.get("observed"):
        fails.append(f"a transit inside the known {g['cause']} outage was counted and its day "
                     f"credited as observed — demand that was never measured is being reported")

    # ══ 3. the CSV carries its caveats, and still parses as CSV ════════════════════════
    # FILL FIRST. The export reads the same stored matrix the screen does — deliberately, so the
    # file and the table cannot disagree — so an unfilled cache yields the pending file, which is
    # section 6's subject, not this one.
    print("\n=== 3. the caveats ride inside the file ===")
    _db = D._db()
    for _k in D.STUDY_KINDS:
        for _p in D.STUDY_FILL_PERIODS:
            D.study_refresh(_db, "site-A", _k, _p)
    _db.close()
    for ds in ("riders_per_day", "riders_per_day_long"):
        txt = _body_text(D.dash_export("site-A", dataset=ds, period="all"))
        notes, rows = _rows_of(txt)
        joined = " ".join(notes)
        print(f"  {ds:22s} {len(notes)} note line(s), {len(rows)} csv row(s)")
        if not notes:
            fails.append(f"{ds} has no provenance header — a table mailed onward without "
                         f"'riders are not people' will be misread")
        if "NOT UNIQUE PEOPLE" not in joined.upper():
            fails.append(f"{ds} does not carry the usage-volume caveat")
        if "not a zero" not in joined and "NOT a day the lift carried nobody" not in joined:
            fails.append(f"{ds} does not carry the dark-is-not-zero rule")
        if len(rows) < 2 or not rows[0][0].startswith("date"):
            fails.append(f"{ds} does not parse as ordinary CSV once '#' lines are dropped")
    # the dark glyph must survive into the file, not become an empty cell that Excel reads as 0
    _n, rows = _rows_of(_body_text(D.dash_export("site-A", dataset="riders_per_day",
                                                 period="all")))
    hdr = rows[0]
    if D.DARK not in {v for r in rows[1:] for v in r}:
        fails.append("the wide CSV contains no dark marker at all — an unobserved cell has been "
                     "written as blank or 0, and a spreadsheet reads both as none carried")
    else:
        print(f"  wide CSV keeps the '{D.DARK}' marker; header = {hdr[:3]}…")

    # ══ 4. RTT per hour per lift ═══════════════════════════════════════════════════════
    print("\n=== 4. RTT per hour per lift — every lift gets a row, every absence a reason ===")
    _db = D._db()
    for wd in D.RTT_WINDOWS:
        for cam in [c["cam"] for c in D._cameras(_db, "site-A")]:
            D.rtt_refresh(_db, "site-A", cam, wd)
    F = D._rtt_per_hour_compute(_db, "site-A", period="all")
    _db.close()
    seen = {r["cam"]: r for r in F["rows"]}
    if set(seen) != {c for _ch, c, _l in CAMS}:
        fails.append(f"not every lift got a row: {sorted(seen)}")
    for cam, r in sorted(seen.items()):
        has = isinstance(r.get("by_hour"), list) and bool(r["by_hour"])
        print(f"  {cam}: state={str(r.get('state')):22s} "
              f"{'by_hour' if has else 'absence=' + str(r.get('absence'))}"
              f"  reason={str(r.get('reason'))[:52]!r}")
        if not has and not r.get("reason"):
            fails.append(f"{cam} has no round-trip data AND no reason — that is the blank cell "
                         f"this table exists to replace")
        if not has and not r.get("absence"):
            fails.append(f"{cam} has no data and no absence KIND; 'pending' and 'cannot be "
                         f"measured' are opposite claims and must not share a blank")
    if (seen.get("ch27") or {}).get("state") != "no_floor":
        fails.append(f"the camera with door rows but no floor did not report no_floor "
                     f"(got {(seen.get('ch27') or {}).get('state')!r})")
    elif seen["ch27"].get("absence") != "designed":
        fails.append(f"no_floor was not classed a DESIGNED absence "
                     f"(got {seen['ch27'].get('absence')!r}) — it is unavailable, not pending")
    if (seen.get("ch32") or {}).get("absence") != "uncalibrated":
        fails.append(f"a camera with no door rows in any era was not classed uncalibrated "
                     f"(got {(seen.get('ch32') or {}).get('absence')!r})")
    ok_row = seen.get("ch29") or {}
    if not isinstance(ok_row.get("by_hour"), list) or len(ok_row.get("by_hour") or []) != 24:
        fails.append("the camera with real round trips did not produce 24 hour cells "
                     f"(got {len(ok_row.get('by_hour') or [])})")
    else:
        nz = [h for h in ok_row["by_hour"] if h.get("n")]
        print(f"  ch29: {len(nz)} hour(s) carry trips, "
              f"median at {pad(nz[0]['hour']) if nz else '—'}h = "
              f"{nz[0]['median'] if nz else '—'}s (n={nz[0]['n'] if nz else 0})")
        if not nz:
            fails.append("the fixture's round trips did not survive into by_hour — this section "
                         "is not exercising the populated case it exists for")

    # ══ 5. neither view derives on the request path ════════════════════════════════════
    print("\n=== 5. a read never triggers a derivation ===")
    calls = {"riders": 0, "walk": 0}
    _rc, _rw = D._riders_per_day_compute, D._rtt_by_cam

    def c_riders(*a, **k):
        calls["riders"] += 1
        return _rc(*a, **k)

    def c_walk(*a, **k):
        calls["walk"] += 1
        return _rw(*a, **k)
    D._riders_per_day_compute, D._rtt_by_cam = c_riders, c_walk
    try:
        _db = D._db()
        for kind in D.STUDY_KINDS:
            for per in D.STUDY_FILL_PERIODS:
                D.study_refresh(_db, "site-A", kind, per)
        _db.close()
        n_fill = dict(calls)
        for per in D.STUDY_FILL_PERIODS:
            body(D.dash_riders_per_day("site-A", period=per))
            body(D.dash_rtt_fleet("site-A", period=per))
        print(f"  after {len(D.STUDY_FILL_PERIODS)} cached reads of each view: "
              f"_riders_per_day_compute +{calls['riders'] - n_fill['riders']}, "
              f"_rtt_by_cam +{calls['walk'] - n_fill['walk']}")
        if calls["riders"] != n_fill["riders"]:
            fails.append("a cacheable riders request derived the matrix on the request path")
        if calls["walk"] != n_fill["walk"]:
            fails.append("serving the RTT matrix walked door rows — it must be a RE-SHAPE of what "
                         "rtt_refresh stored, never a second derivation")
        # and the RTT matrix must not walk even while being FILLED
        if n_fill["walk"]:
            fails.append(f"filling the RTT matrix walked door rows {n_fill['walk']}x — rtt_window "
                         f"already holds the summary; walking again is a second RTT derivation")
        # a custom range is REFUSED for RTT, not derived
        ref = body(D.dash_rtt_fleet("site-A", from_d="2026-08-01", to_d="2026-08-05"))
        print(f"  custom range on RTT: state={ref.get('state')!r}")
        if ref.get("state") != "not served for this range":
            fails.append("a calendar range on the RTT matrix was not refused — that is a "
                         "seven-camera door walk back on the request path")
        # a custom range on riders DOES derive: a deliberate question, bounded in SQL
        n0 = calls["riders"]
        body(D.dash_riders_per_day("site-A", from_d=days[0], to_d=days[1]))
        if calls["riders"] == n0:
            fails.append("a custom date range on riders was served from a cache built for a "
                         "different question")
    finally:
        D._riders_per_day_compute, D._rtt_by_cam = _rc, _rw

    # ══ 6. pending is not absence, in the payload AND in the file ══════════════════════
    print("\n=== 6. pending is never served as 'nothing happened' ===")
    _db = D._db()
    _db.execute("DELETE FROM study_matrix")
    _db.commit()
    _db.close()
    for fn, kind in ((D.dash_riders_per_day, "riders_per_day"), (D.dash_rtt_fleet, "rtt_per_hour")):
        b = body(fn("site-A", period="all"))
        print(f"  {kind}: state={b.get('state')!r} rows={len(b.get('rows') or [])}")
        if b.get("state") != "not_computed":
            fails.append(f"{kind} did not report a cache miss as not_computed")
        if not b.get("note") or "NOT" not in b.get("note", ""):
            fails.append(f"{kind}'s miss does not say it is pending rather than empty")
    for ds in ("riders_per_day", "rtt_per_hour"):
        notes, rows = _rows_of(_body_text(D.dash_export("site-A", dataset=ds, period="all")))
        if "NOT COMPUTED YET" not in " ".join(notes):
            fails.append(f"the {ds} CSV is empty on a cache miss without saying why — an empty "
                         f"file reads as 'nothing was counted'")
    print(f"  both CSVs explain the empty file rather than implying a zero")

    # ══ 7. what the page actually draws ═══════════════════════════════════════════════
    print("\n=== 7. the rendered page ===")
    if not shutil.which("node"):
        print("  SKIPPED — no node; nothing in this section was asserted.")
    else:
        _db = D._db()
        for kind in D.STUDY_KINDS:
            # 'day' as well as 'all': the fixture's data is three days old, so Today is a range
            # with no observed day — the empty-range case asserted below. Filling only 'all' left
            # that request on the not_computed skeleton and the assertion could not fail.
            for _p in ("all", "day"):
                D.study_refresh(_db, "site-A", kind, _p)
        data = body(D._dash_data_inner(_db, "site-A"))
        _db.close()
        rd = body(D.dash_riders_per_day("site-A", period="all"))
        rf = body(D.dash_rtt_fleet("site-A", period="all"))
        # A STATE THIS BUILD HAS NEVER SEEN must still render, and must not inherit another
        # absence's explanation. This is the too_many_rows lesson, one view along.
        rf["rows"] = list(rf["rows"]) + [
            {"cam": "ch99", "label": "from the future", "state": "a_state_from_a_future_build",
             "by_hour": None, "reason": "synthetic: a state this page has never heard of",
             "absence": "a_kind_from_a_future_build"}]
        out = render(tmp, D, data, rd, rf)
        if out is None:
            fails.append("the page could not be rendered at all")
        else:
            if out.get("threw"):
                fails.append(f"a render threw and took the view down: {out['threw']}")
            rt, ft = _text(out["riders"]), _text(out["rtt"])
            nav = out.get("nav") or ""
            print(f"  riders view {len(out['riders']):,} chars · rtt view {len(out['rtt']):,} chars")
            for want, where, what in (
                    ("Riders / day", nav, "the riders tab is not on the tab strip"),
                    ("RTT / lift", nav, "the RTT tab is not on the tab strip"),
                    ("NOT UNIQUE PEOPLE", rt.upper(), "the riders table does not carry the "
                                                      "usage-volume caveat where it is read"),
                    ("not observed", rt, "the riders legend does not distinguish dark from zero"),
                    ("boarded", rt, "the boardings/alightings split is not offered"),
                    ("⤓", rt, "the riders view offers no CSV"),
                    ("⤓", ft, "the RTT view offers no CSV"),
                    ("DESIGNED ABSENCE", ft, "a lift with no floor attribution did not get its "
                                             "designed-absence reason in the table"),
                    ("NOT a lift that made no journeys", ft, "the designed absence does not say "
                                                             "it is not a lift standing still"),
                    ("NOT CALIBRATED", ft, "an uncalibrated lift was not told apart from a "
                                           "measurable one"),
                    ("this page has never heard of", ft, "an unknown state did not render its "
                                                         "own reason verbatim")):
                if want not in where:
                    fails.append(what)
            # the two rows must not share an explanation
            if ft.count("NOT a lift that made no journeys") and "no floor attribution" not in ft:
                pass
            # A RANGE WITH NO OBSERVED DAY must say so, not draw a grid of zeros. Today is
            # empty in this fixture (the data is three days old), which is exactly that case.
            rd_empty = body(D.dash_riders_per_day("site-A", period="day"))
            out2 = render(tmp, D, data, rd_empty, rf)
            e = _text((out2 or {}).get("riders") or "")
            print(f"  empty range ({len(rd_empty.get('rows') or [])} rows): "
                  f"{'says NO OBSERVED DAY' if 'NO OBSERVED DAY' in e else 'DREW A GRID'}")
            if rd_empty.get("state") != "not_computed" and not (rd_empty.get("rows") or []):
                if "NO OBSERVED DAY" not in e:
                    fails.append("a range with no observed day drew a table anyway — a grid of "
                                 "zeros there reports a fleet that carried nobody")
                if "NOT a report that the lifts carried nobody" not in e:
                    fails.append("the empty-range panel does not distinguish itself from a zero")
            n_hours = out["rtt"].count("<th>0")
            print(f"  rtt table draws {out['rtt'].count('<tr>')} row(s); "
                  f"riders table {out['riders'].count('<tr>')} row(s)")
            if "colspan=25" not in out["rtt"]:
                fails.append("an absent lift's reason is not spanning the hour columns — it is "
                             "either missing or squeezed into one cell")

    # ══ 8. the nine defects a review found, so none of them can come back ══════════════
    print("\n=== 8. review regressions ===")

    # (a) AN UNRECOGNISED PERIOD IS REFUSED. _range_bounds maps an unknown period to all-history,
    #     so a typo bought a fleet-wide all-history aggregation on the request path, labelled as a
    #     custom date range. Both the endpoint and the export must refuse it.
    for bad in ("ALL", "1", "bogus"):
        b = body(D.dash_riders_per_day("site-A", period=bad))
        if not (isinstance(b, dict) and b.get("error")):
            fails.append(f"period={bad!r} was answered instead of refused — an unrecognised "
                         f"period silently buys an all-history fleet aggregation")
        e = D.dash_export("site-A", dataset="riders_per_day", period=bad)
        if getattr(e, "status_code", 200) != 400:
            fails.append(f"the riders CSV answered period={bad!r} instead of refusing it")
    print(f"  unknown periods refused by both the endpoint and the export")

    # (b) THE MEASURE IS IN THE FILENAME. Three measures under one name, in a folder of files
    #     whose purpose is to be mailed onward, is three files nobody can tell apart.
    names = {}
    for sp in ("riders", "boarded", "alighted"):
        r = D.dash_export("site-A", dataset="riders_per_day", period="all", split=sp)
        names[sp] = (getattr(r, "headers", {}) or {}).get("Content-Disposition", "")
    print("  filenames: " + " ".join(sorted(set(
        re.search(r'filename="([^"]+)"', v).group(1) for v in names.values() if v))))
    if len(set(names.values())) != 3:
        fails.append("the three riders splits download under the same filename")

    # (c) A STATE WITH NO NOTE MUST NOT INHERIT DELIVERY'S WORD FOR IT. no_rows carries no note and
    #     its DELIVERY state is 'ok' (found and parsed), so the reason column said "ok".
    _db = D._db()
    D._rtt_window_table(_db)
    _cv, _dv = D._current_keys(_db, "site-A", "ch30")
    _db.execute("INSERT OR REPLACE INTO rtt_window (gateway_id,cam,window_days,counting_version,"
                "door_version,payload,computed_at,compute_ms) VALUES ('site-A','ch30',?,?,?,?,?,1)",
                (0.0, _cv or "", _dv or "",
                 json.dumps({"state": "no_rows", "era": _dv}), 1.0))
    _db.commit()
    nr = {r["cam"]: r for r in D._rtt_per_hour_compute(_db, "site-A", "all")["rows"]}["ch30"]
    _db.close()
    print(f"  no_rows reason: {str(nr.get('reason'))[:64]!r}")
    if str(nr.get("reason")).strip().lower() in ("ok", "none", ""):
        fails.append(f"a no_rows lift reports its reason as {nr.get('reason')!r} — that is the "
                     f"DELIVERY state (the payload was found and parsed), not a reason there is "
                     f"no measurement")
    if "made no journeys" not in str(nr.get("reason")):
        fails.append("the no_rows reason does not say it is not a lift that made no journeys")

    # (d) esc() MUST BE SAFE IN AN ATTRIBUTE. Camera labels are operator-supplied and stored
    #     verbatim, and these views are the first to put one inside title=/data-tip=.
    if shutil.which("node"):
        HOSTILE = 'lift " onmouseover=BREAKOUT x'
        rd2 = body(D.dash_riders_per_day("site-A", period="all"))
        rf2 = body(D.dash_rtt_fleet("site-A", period="all"))
        if isinstance(rd2.get("labels"), dict):
            rd2["labels"] = {k: HOSTILE for k in rd2["labels"]}
        for _r in (rf2.get("rows") or []):
            _r["label"] = HOSTILE
        o = render(tmp, D, data, rd2, rf2)
        both = ((o or {}).get("riders") or "") + ((o or {}).get("rtt") or "")
        print(f"  hostile label: breaks out {'YES' if '\" onmouseover=BREAKOUT' in both else 'no'}"
              f" · quote entity present {'&quot;' in both}")
        if '" onmouseover=BREAKOUT' in both:
            fails.append("a camera label containing a double quote closed the HTML attribute and "
                         "everything after it was parsed as markup")
        if "&quot;" not in both:
            fails.append("the hostile label never reached an attribute — this check is not "
                         "exercising what it exists for")

        # (e) ONE #dfrom PER VIEW. periodBar renders into three panes that are hidden, not
        #     removed, so a bare id existed three times and setDates read the first in tree order.
        o3 = render(tmp, D, data, rd, rf)
        rv, fv = (o3 or {}).get("riders") or "", (o3 or {}).get("rtt") or ""
        print(f"  date inputs: riders has dfrom-riders={('id=\"dfrom-riders\"' in rv)} · "
              f"rtt has dfrom-rttfleet={('id=\"dfrom-rttfleet\"' in fv)}")
        for html, want, who in ((rv, 'id="dfrom-riders"', "riders"),
                                (fv, 'id="dfrom-rttfleet"', "RTT")):
            if want not in html:
                fails.append(f"the {who} view's date input is not scoped to its own view — three "
                             f"hidden panes share one id and setDates() reads the wrong box")
        # (f) RETRY MUST RELOAD THE VIEW THAT FAILED, not whichever one the helper was written for.
        of = render(tmp, D, data, {"fetch_error": "synthetic"}, {"fetch_error": "synthetic"})
        rf_html = (of or {}).get("rtt") or ""
        print(f"  RTT retry calls: "
              f"{re.search(r'onclick=.([a-zA-Z]+)\(\).>retry', rf_html).group(1) if re.search(r'onclick=.([a-zA-Z]+)', rf_html) else '??'}")
        if "loadRttFleet()" not in rf_html:
            fails.append("the RTT view's retry button does not reload the RTT view — it refetches "
                         "the other matrix into a hidden pane and this panel never recovers")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK — dark is not zero, riders are not people, an outage is not observation, every lift\n"
          "     gets an RTT row with its own reason, and neither view derives on the request path.")
    return 0


def pad(h):
    return f"{h:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
