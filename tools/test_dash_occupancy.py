#!/usr/bin/env python3
"""Peak car occupancy on the dash: panel, hour-of-day trend, and CSV, against a built gateway DB.

WHAT IS BEING PROVEN, in the order it matters:

1. THE EVIDENCE RULE HOLDS EVERYWHERE. An episode with occupancy_frames NULL or 0 has no coverage
   behind its peak. It must be excluded from every statistic and COUNTED in n_no_evidence — never
   silently dropped and never read as an empty cabin. The panel, the trend chart and the window cards
   must all apply the same rule, because three surfaces with three denominators is how a dashboard
   starts disagreeing with itself.

2. PEAKS ARE MAXIMA, NOT SUMS. The hour-of-day series and the table's total row both aggregate by
   max. Summing per-episode peaks would invent a car load that never existed.

3. ERA SCOPING. A peak counted under a different counting_version came out of a different cabin
   polygon. Excluded from the stats, counted in n_off_era, named in versions_seen — visible, never
   pooled. Same discipline the compliance panel applies to door eras.

4. THE CALIBRATION IS PRESENT. "measured minimum; ~0.5x at heavy crowding (n=1 scene, ch30)" ships
   in the /data payload, the /trends payload, and the rendered panel HTML. The number is a floor and
   every surface that prints it says so — a peak of 3 quoted bare in a 13-person car reads as a
   quarter-full lift when the frame held 5-7 people.

5. THE CSV IS THE RAW GRAIN. It keeps the no-coverage rows, with an `evidence` column stating the
   rule, so a filter applied in Excel is the same filter the dashboard applied.
"""
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
IST = timezone(timedelta(hours=5, minutes=30))


def _stub():
    try:
        import fastapi  # noqa: F401
        return
    except Exception:
        pass
    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            super().__init__(f"{status_code} {detail}")
            self.status_code, self.detail = status_code, detail

    class APIRouter:
        def __init__(self, *a, **k):
            pass

        def _noop(self, *a, **k):
            return lambda f: f

        get = post = put = delete = patch = _noop

    fa.APIRouter, fa.HTTPException = APIRouter, HTTPException
    fa.Header = lambda default="", **k: default
    fa.Form = lambda default=None, **k: default
    fa.Query = lambda default=None, **k: default
    fa.Request = type("Request", (), {})
    fa.UploadFile = type("UploadFile", (), {})
    resp = types.ModuleType("fastapi.responses")

    class Response:
        def __init__(self, content=None, media_type=None, headers=None, status_code=200, **k):
            self.body, self.media_type = content, media_type
            self.headers, self.status_code = headers or {}, status_code

    class JSONResponse(Response):
        def __init__(self, content=None, status_code=200, **k):
            super().__init__(content, "application/json", status_code=status_code)
            self.payload = content

    resp.Response, resp.JSONResponse = Response, JSONResponse
    resp.HTMLResponse = type("HTMLResponse", (Response,), {})
    resp.RedirectResponse = type("RedirectResponse", (Response,), {})
    resp.PlainTextResponse = type("PlainTextResponse", (Response,), {})
    resp.FileResponse = type("FileResponse", (Response,), {})
    resp.StreamingResponse = type("StreamingResponse", (Response,), {})
    fa.responses = resp
    sys.modules["fastapi"], sys.modules["fastapi.responses"] = fa, resp


HARNESS_JS = r"""
const fs = require('fs');
const els = {};
function el(id){ return els[id] || (els[id] = {id, innerHTML:'', style:{}, value:'', textContent:'',
  getAttribute(){return null}, setAttribute(){}, appendChild(){}}); }
global.document = {getElementById: el, addEventListener(){}, createElement(){return el('tmp')},
  querySelector(){return null}, querySelectorAll(){return []}, title:'', body: el('body')};
global.window = {addEventListener(){}, innerWidth:1200,
  location:{search:'', pathname:'/dash', href:''}};
global.location = global.window.location;
global.history = {replaceState(){}, pushState(){}};
global.localStorage = {getItem(){return null}, setItem(){}};
global.setTimeout = () => 0; global.clearTimeout = () => {}; global.setInterval = () => 0;
global.requestAnimationFrame = () => 0;
global.fetch = () => new Promise(() => {});          // the page must render from DATA, never fetch
global.EventSource = function(){ this.addEventListener=function(){}; this.close=function(){}; };
const src = fs.readFileSync(process.argv[2], 'utf8');
const _D = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const _T = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
eval(src);
GW='site-A'; DATA=_D; cur='ch30'; trCam='ch30'; TR=_T;
panel(DATA); renderTrends();
fs.writeFileSync(process.argv[5], JSON.stringify(
  {panel: els['panel'].innerHTML, trend: els['trendview'].innerHTML}));
"""


def _render_with_node(tmp, page_html, data, trends):
    """Eval the page's own <script> in node against the real payloads. -> (panel_html, trend_html)."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page_html, re.S))
    paths = {n: os.path.join(tmp, n) for n in
             ("dash.js", "harness.js", "data.json", "trends.json", "out.json")}
    open(paths["dash.js"], "w").write(js)
    open(paths["harness.js"], "w").write(HARNESS_JS)
    json.dump(data, open(paths["data.json"], "w"))
    json.dump(trends, open(paths["trends.json"], "w"))
    r = subprocess.run([node, paths["harness.js"], paths["dash.js"], paths["data.json"],
                        paths["trends.json"], paths["out.json"]], capture_output=True, text=True)
    if r.returncode != 0:
        print("  node failed — the page JS did not run:")
        print("   ", (r.stderr or "").strip().splitlines()[0] if r.stderr else "(no stderr)")
        raise SystemExit(1)
    o = json.load(open(paths["out.json"]))
    return o["panel"], o["trend"]


SCHEMA = """
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, cam TEXT, label TEXT, enabled INTEGER);
CREATE TABLE validation_item (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, ts_start REAL,
  ts_end REAL, machine_boarded INTEGER, machine_alighted INTEGER, n_images INTEGER, status TEXT,
  counting_version TEXT, occupancy_max INTEGER, occupancy_frames INTEGER,
  occupancy_degraded INTEGER, analysed_frames INTEGER, human_occupancy INTEGER,
  human_boarded INTEGER, human_alighted INTEGER, created_at REAL);
CREATE TABLE camera_validation (gateway_id TEXT, cam TEXT, state TEXT, n_reviewed INTEGER,
  n_exact INTEGER, provenance TEXT, counting_version TEXT, confirmed_at REAL, updated_at REAL);
CREATE TABLE transit_event (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, ts REAL,
  direction TEXT, track_id INTEGER);
CREATE TABLE gw_source (id INTEGER PRIMARY KEY, gateway_id TEXT, camera TEXT);
CREATE TABLE gw_event (id INTEGER PRIMARY KEY, source_id INTEGER, door_open_start_ts TEXT,
  door_open_full_ts TEXT, door_close_start_ts TEXT, door_close_full_ts TEXT, close_travel_s REAL,
  quality TEXT, boarded INTEGER, alighted INTEGER, floor TEXT);
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, ts REAL, floor TEXT,
  direction TEXT, door_state TEXT, read_conf REAL, panels_agreed INTEGER, reason TEXT,
  close_travel_s REAL, door_version TEXT);
CREATE TABLE analyzer_status (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, ts REAL, mode TEXT,
  counting_version TEXT, proc_ms REAL, seg_budget_ms REAL, drop_frac REAL);
CREATE TABLE watch_status (id INTEGER PRIMARY KEY, gateway_id TEXT, ts REAL);
CREATE TABLE relay_status (id INTEGER PRIMARY KEY, gateway_id TEXT, ts REAL);
CREATE TABLE camera_registry (gateway_id TEXT, cam TEXT, enabled INTEGER, updated_at REAL);
CREATE TABLE door_aggregate (gateway_id TEXT, cam TEXT, window_days REAL, counting_version TEXT,
  door_version TEXT, computed_at REAL, payload TEXT);
"""

V = "v-cabin-1"          # the current counting version
VOLD = "v-cabin-0"       # an older one: different cabin polygon, not comparable


def build_db(path):
    """Episodes with a deliberately awkward mix: real peaks, no-coverage rows, a degraded one, an
    old-era one, and a today row — so every branch of the rule is exercised by one fixture."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO channel_map VALUES ('site-A',30,'ch30','Lift C',1)")
    con.execute("INSERT INTO channel_map VALUES ('site-A',27,'ch27','Lift B',1)")
    con.execute("INSERT INTO camera_validation (gateway_id,cam,state,counting_version) "
                "VALUES ('site-A','ch30','live',?)", (V,))
    con.execute("INSERT INTO camera_validation (gateway_id,cam,state,counting_version) "
                "VALUES ('site-A','ch27','validating',?)", (V,))

    now = datetime.now(IST)
    midnight = datetime(now.year, now.month, now.day, tzinfo=IST).timestamp()

    def ep(cam, ts, occ, frames, deg=0, ver=V):
        con.execute("INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,machine_boarded,"
                    "machine_alighted,status,counting_version,occupancy_max,occupancy_frames,"
                    "occupancy_degraded,analysed_frames,created_at) "
                    "VALUES ('site-A',?,?,?,1,1,'auto',?,?,?,?,?,?)",
                    (cam, ts, ts + 18, ver, occ, frames, deg, frames or 0, ts))

    # ch30 — yesterday 09:xx IST (hour 9), three episodes with coverage
    y9 = midnight - 86400 + 9 * 3600
    ep("ch30", y9 + 60, 2, 120)
    ep("ch30", y9 + 600, 5, 140)                 # the window peak
    ep("ch30", y9 + 900, 3, 130)
    # ch30 — yesterday 18:xx, one degraded episode (thin coverage; a floor of a floor)
    ep("ch30", midnight - 86400 + 18 * 3600 + 30, 4, 40, deg=1)
    # ch30 — no coverage at all. A worker predating the feature, and a posted episode with 0 frames.
    ep("ch30", y9 + 1200, None, None)
    ep("ch30", y9 + 1500, 0, 0)
    # ch30 — an episode counted under the OLD cabin polygon, with a peak that would win if pooled
    ep("ch30", y9 + 1800, 9, 200, ver=VOLD)
    # ch30 — today, hour 7
    ep("ch30", midnight + 7 * 3600 + 120, 3, 150)
    # ch27 — episodes exist but NONE carry coverage: the "present but unmeasured" case
    ep("ch27", y9 + 300, None, None)
    ep("ch27", y9 + 400, 4, 0)

    con.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,mode,counting_version,proc_ms,"
                "seg_budget_ms,drop_frac) VALUES ('site-A','ch30',?,'live',?,300,2000,0.01)",
                (datetime.now(IST).timestamp(), V))
    con.commit()
    con.close()
    return midnight



def _precompute_trends(D, cams):
    """Fill trends_cache the way the timer does, so dash_trends has something to serve.

    /trends no longer derives on the request path — the default view is precomputed per (cam, era)
    and served from trends_cache. A test that calls dash_trends on a fresh database is testing the
    'not computed yet' path, not the numbers. Production runs precompute_job before serving; so does
    this. The pending path has its own coverage in test_trends_cache.py."""
    db = D._db()
    try:
        for c in cams:
            D.trends_refresh(db, "site-A", c)
    finally:
        db.close()

def main():
    fails = []
    _stub()
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "gw.db")
    midnight = build_db(db_path)
    os.environ["GATEWAY_DB"] = db_path
    import dash_api as D

    db = D._db()

    # ---------------------------------------------------------------- 1. the aggregate
    print("=== _occupancy_by_cam (7-day window) ===")
    t0, t1, _w = D._window(7)
    occ = D._occupancy_by_cam(db, "site-A", t0, t1)
    for cam in sorted(occ):
        o = occ[cam]
        print(f"  {cam}: peak={o['peak']} p95={o['p95']} median={o['median']} "
              f"today={o['today_peak']} n={o['n']}/{o['n_episodes']} "
              f"no_evidence={o['n_no_evidence']} off_era={o['n_off_era']} degraded={o['n_degraded']}")

    o30 = occ.get("ch30") or {}
    if o30.get("peak") != 5:
        fails.append(f"ch30 peak should be 5 (the max with coverage in-era), got {o30.get('peak')}")
    if o30.get("n") != 5:
        fails.append(f"ch30 n should be 5 episodes with coverage, got {o30.get('n')}")
    if o30.get("n_episodes") != 8:
        fails.append(f"ch30 should see 8 episodes in the window, got {o30.get('n_episodes')}")
    if o30.get("n_no_evidence") != 2:
        fails.append(f"ch30 should count 2 no-coverage episodes, got {o30.get('n_no_evidence')}")
    if o30.get("n_off_era") != 1:
        fails.append(f"ch30 should exclude 1 old-era episode, got {o30.get('n_off_era')}")
    if o30.get("n_degraded") != 1:
        fails.append(f"ch30 should count 1 degraded episode, got {o30.get('n_degraded')}")
    if o30.get("today_peak") != 3:
        fails.append(f"ch30 today_peak should be 3 (IST-today only), got {o30.get('today_peak')}")
    if o30.get("today_n") != 1:
        fails.append(f"ch30 today_n should be 1, got {o30.get('today_n')}")
    vers = {v["version"] for v in (o30.get("versions_seen") or [])}
    if VOLD not in vers:
        fails.append("the excluded era is not named in versions_seen — exclusion must stay visible")
    if o30.get("calibration") != D.OCC_CALIBRATION:
        fails.append("the calibration string is missing from the per-camera aggregate")

    o27 = occ.get("ch27") or {}
    if o27.get("n") != 0 or o27.get("n_episodes") != 2:
        fails.append(f"ch27 should be 0 measured of 2 episodes, got {o27.get('n')}/{o27.get('n_episodes')}")
    if o27.get("peak") is not None:
        fails.append(f"ch27 has no coverage; peak must be None, got {o27.get('peak')} "
                     "— a 0 here would read as an empty car")

    print("\n=== the rule that a peak is never a sum ===")
    tot = sum(v for v in [2, 5, 3, 4, 3])
    print(f"  sum of ch30's in-era peaks = {tot}; reported peak = {o30.get('peak')}")
    if o30.get("peak") == tot:
        fails.append("peak equals the sum of episode peaks — it is being summed, not maximised")

    # ---------------------------------------------------------------- 2. /data payload + panel HTML
    print("\n=== /dash/{gw}/data ===")
    data = D._dash_data_inner(db, "site-A").payload
    cams = {c["cam"]: c for c in data["cameras"]}
    if (cams.get("ch30") or {}).get("occupancy", {}).get("peak") != 5:
        fails.append("the per-camera payload does not carry occupancy")
    note = data.get("occupancy_note") or {}
    print(f"  occupancy_note.calibration = {note.get('calibration')!r}")
    if note.get("calibration") != D.OCC_CALIBRATION:
        fails.append("/data does not carry the calibration string")
    if "measured minimum" not in (note.get("label") or ""):
        fails.append("/data label does not say measured minimum")

    # ---------------------------------------------------------------- 3. /trends
    print("\n=== /dash/{gw}/trends (cam=ch30, all) ===")
    _precompute_trends(D, ["ch30", ""])
    tr = D.dash_trends("site-A", cam="ch30", period="all").payload
    hourly = {p["hour"]: (p["occ_peak"], p["occ_n"]) for p in tr["profile"] if p["occ_n"]}
    print(f"  hours with episodes: {hourly}")
    if hourly.get(9) != (5, 3):
        fails.append(f"hour 9 should be peak 5 over n=3, got {hourly.get(9)}")
    if hourly.get(18) != (4, 1):
        fails.append(f"hour 18 should be peak 4 over n=1, got {hourly.get(18)}")
    if hourly.get(7) != (3, 1):
        fails.append(f"hour 7 (today) should be peak 3 over n=1, got {hourly.get(7)}")
    if any(h for h in hourly if h not in (7, 9, 18)):
        fails.append(f"episodes leaked into hours with none: {sorted(hourly)}")
    tot_n = sum(n for _p, n in hourly.values())
    if tot_n != 5:
        fails.append(f"trends counted {tot_n} episodes with coverage; the panel counted 5 — the two "
                     "surfaces are applying different evidence rules")
    ad = tr["windows"]["all_day"]["occupancy"]
    print(f"  all-day window: {ad}")
    if ad["peak"] != 5 or ad["n"] != 5:
        fails.append(f"all-day occupancy window wrong: {ad}")
    if ad["degraded"] != 1:
        fails.append(f"all-day degraded count wrong: {ad['degraded']}")
    am = tr["windows"]["am_peak"]["occupancy"]
    if am["peak"] != 5 or am["n"] != 3:
        fails.append(f"AM-peak window (08-10) should hold hour 9's three episodes, got {am}")
    if (tr.get("occupancy_note") or {}).get("calibration") != D.OCC_CALIBRATION:
        fails.append("/trends does not carry the calibration string")

    print("\n  fleet view (cam='') — ch27 contributes 0 episodes, not 0 people:")
    trf = D.dash_trends("site-A", cam="", period="all").payload
    fn = trf["windows"]["all_day"]["occupancy"]
    print(f"    fleet all-day: {fn}")
    if fn["n"] != 5 or fn["peak"] != 5:
        fails.append(f"fleet occupancy should equal ch30's (ch27 has no coverage), got {fn}")

    # ---------------------------------------------------------------- 4. CSV
    print("\n=== export.csv?dataset=episodes ===")
    csv_resp = D.dash_export("site-A", dataset="episodes", cam="ch30", period="all")
    body = csv_resp.body
    lines = [ln for ln in body.strip().split("\n") if ln]
    print("  header:", lines[0])
    for ln in lines[1:]:
        print("   ", ln)
    if len(lines) - 1 != 8:
        fails.append(f"CSV should keep ALL 8 ch30 episodes including the unmeasured ones, "
                     f"got {len(lines) - 1}")
    if "occupancy_max_MEASURED_MINIMUM" not in lines[0]:
        fails.append("CSV header does not mark occupancy_max as a measured minimum")
    if "evidence" not in lines[0]:
        fails.append("CSV has no evidence column, so the spreadsheet cannot reproduce the dash filter")
    ev_col = lines[0].split(",").index("evidence")
    n_ev = sum(1 for ln in lines[1:] if ln.split(",")[ev_col] == "1")
    if n_ev != 6:
        fails.append(f"CSV evidence flag set on {n_ev} rows; 6 have occupancy_frames>0 "
                     "(the old-era row included — the CSV labels era, it does not filter it)")

    # ---------------------------------------------------------------- 5. the rendered page
    print("\n=== rendered page ===")
    page = D.dash_page()                       # returns the HTML string itself
    page = getattr(page, "body", page)
    # The calibration is NOT baked into the page: it arrives with the data, from OCC_CALIBRATION, and
    # the page prints whatever the payload carries. That is deliberate — one constant, server-side,
    # so a change to the wording cannot leave a stale copy behind in the markup. What the page must
    # therefore prove is that it PRINTS the field on both surfaces rather than dropping it.
    for needle, why in (("oc.calibration", "the camera panel must print the calibration it was sent"),
                        ("TR.occupancy_note.calibration", "the trend chart must print it too"),
                        ("MEASURED MINIMUM", "the chart title must say what the number is"),
                        ("peak car occupancy", "the chart must be named"),
                        ("measured minimum", "the panel must label the figure")):
        ok = needle in page
        print(f"  {'present' if ok else 'MISSING'}: {needle}")
        if not ok:
            fails.append(f"{why} — {needle!r} absent from the page")
    if re.search(r"occ_peak[^;]*\+=", page):
        fails.append("the table total sums occ_peak somewhere — a peak total must be a max")

    # ---------------------------------------------------------------- 6. actually RENDER it
    # A page that parses is not a page that renders. This evals the real page JS in node against the
    # real payloads with a minimal DOM, and asserts on what an operator would READ. Skipped, loudly,
    # where node is absent — a skipped check must never look like a passed one.
    print("\n=== rendered output (node) ===")
    rendered = _render_with_node(tmp, page, data, tr)
    if rendered is None:
        print("  SKIPPED — no `node` on this box. The static checks above still ran; the assertions "
              "below (what the panel actually prints) did not.")
    else:
        pan, trend = rendered
        for needle, where in ((">5</b> people", "panel: the window peak"),
                              ("measured minimum · floor, not a count", "panel: the label"),
                              (D.OCC_CALIBRATION, "panel: the calibration"),
                              ("excluded, not pooled", "panel: the era exclusion"),
                              ("had no analysed frames — excluded", "panel: the coverage exclusion")):
            ok = needle in pan
            print(f"  {'printed' if ok else 'NOT PRINTED'} — {where}")
            if not ok:
                fails.append(f"{where}: {needle!r} never reaches the screen")
        for needle, where in (("peak car occupancy / hour-of-day — MEASURED MINIMUM", "trend: title"),
                              ("&ge;<b>5</b>", "trend: the all-day peak card"),
                              (D.OCC_CALIBRATION, "trend: the calibration under the chart")):
            ok = needle in trend
            print(f"  {'printed' if ok else 'NOT PRINTED'} — {where}")
            if not ok:
                fails.append(f"{where}: {needle!r} never reaches the screen")
        if "&ge;<b>17</b>" in trend or ">17</b> people" in pan:
            fails.append("17 appears on screen — the sum of episode peaks is being displayed as a peak")

    db.close()
    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — evidence rule identical on panel/trend/window, peaks maximised not summed, "
          "old era excluded and named, calibration on every surface, CSV keeps the raw grain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
