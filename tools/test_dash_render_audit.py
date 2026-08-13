#!/usr/bin/env python3
"""Dash render audit: every ABSENCE on the page must say which kind of absence it is.

Six defects from a live review, all one family — the page printed a dash, a zero, or an empty axis
where the honest statement was "this cannot be measured, and here is why".

  1. summary boxes showed close med "—/— (n=0)" while the chart below explained h3 measures no
     travel at all. The boxes are what most readers stop at.
  2. the C26 transfer metric silently switched instruments between tabs: a lift with Pi history
     showed a July figure beside GPU-era demand, a lift without one showed "(n=0)".
  3. peak occupancy printed "—" for hours predating the field's deployment — the same glyph as
     "this hour had no episodes", and in an occupancy column a dash reads as an empty car.
  4. THE SELECTOR: ?cam=, the tab highlight, the summary line and the per-floor panel could all
     disagree. Floors belong to one shaft, so that is a chart of a different building column under
     the wrong heading.
  5. occupancy on door-less cameras — verified live on ch32, no gating bug. Asserted here so it
     stays that way: occupancy is a counting-path product and must not depend on door calibration.
  6. door-derived charts on UNCALIBRATED cameras rendered as empty axes with "cycles/hr 0" and the
     2.31s Bank C line ruled across nothing — a compliance comparison about a lift never measured.

Every assertion below is made against the RENDERED HTML, in node, from the real page JS and real
payloads. What the functions could print is not the defect; what the page did print is.
"""
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

# ch29: h3 door era + Pi history.  ch27: h3 door era, NO Pi history.  ch32: NO door rows at all.
V = "v-cabin-1"
H3_VERSION = "260d4a0fh3-stateT5471cb+495e8f48"

HARNESS_JS = r"""
const fs = require('fs');
const els = {};
function el(id){ return els[id] || (els[id] = {id, innerHTML:'', style:{}, value:'', textContent:'',
  getAttribute(){return null}, setAttribute(){}, appendChild(){}}); }
global.document = {getElementById: el, addEventListener(){}, createElement(){return el('t')},
  querySelector(){return null}, querySelectorAll(){return []}, title:'', body: el('b')};
global.window = {addEventListener(){}, innerWidth:1200,
                 location:{search:'?cam=ch32', pathname:'/dash', href:''}};
global.location = global.window.location;
const urls = [];
global.history = {replaceState(a,b,u){ urls.push(u); }, pushState(){}};
global.localStorage = {getItem(){return null}, setItem(){}};
global.setTimeout = () => 0; global.clearTimeout = () => {}; global.setInterval = () => 0;
global.requestAnimationFrame = () => 0;
let fetches = [];
global.fetch = (u) => { fetches.push(u); return new Promise(() => {}); };
global.EventSource = function(){ this.addEventListener=function(){}; this.close=function(){}; };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const D = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const T = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const want = process.argv[6];
const view = process.argv[7] || 'cams';       // which view the URL opens on
const staleTR = process.argv[8] ? JSON.parse(fs.readFileSync(process.argv[8],'utf8')) : null;
global.window.location.search = '?cam=' + (want||'ch32') + (view==='trends' ? '&view=trends' : '');
GW='site-A'; DATA=D; TR=T;
el('healthbar'); el('strip'); el('tabs'); el('panel'); el('trendview'); el('headline'); el('unavail');
// Drive it the way the page does: tabs() defaults the selection, then a click selects.
tabs(D);
const afterDefault = {cur: cur, trCam: trCam};
if (want) { selectCam(want, {noTrends:true}); }
// STALE-PAYLOAD CASE: plant a payload describing ANOTHER camera, exactly as a cached TR would be
// after switching lifts, then drive the real loadTrends() and see what the page draws before the
// response arrives (fetch never resolves in this harness — that IS the window under test).
if (staleTR) { TR = staleTR; mode='trends'; loadTrends(); }
else { mode='trends'; TR=T; renderTrends(); }
const midFlight = els['trendview'].innerHTML;
if (!staleTR) { renderTrends(); }
panel(D);
fs.writeFileSync(process.argv[5], JSON.stringify({
  panel: els['panel'].innerHTML, trend: els['trendview'].innerHTML,
  tabs: els['tabs'].innerHTML, cur: cur, trCam: trCam, mode: mode,
  midFlight: midFlight, trCamAfter: trCam,
  afterDefault: afterDefault, urls: urls, fetches: fetches,
}));
"""


def build_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT, marked_at REAL);
CREATE TABLE validation_item (id INTEGER PRIMARY KEY, gateway_id TEXT, cam TEXT, ts_start REAL,
  ts_end REAL, machine_boarded INTEGER, machine_alighted INTEGER, n_images INTEGER, status TEXT,
  counting_version TEXT, occupancy_max INTEGER, occupancy_frames INTEGER, occupancy_degraded INTEGER,
  analysed_frames INTEGER, human_occupancy INTEGER, human_boarded INTEGER, human_alighted INTEGER,
  created_at REAL);
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
CREATE TABLE health_status (id INTEGER PRIMARY KEY, gateway_id TEXT, ts REAL, ok INTEGER,
  n_cams INTEGER, n_bad INTEGER, line TEXT, detail TEXT, sent INTEGER, delivered TEXT,
  delivery_error TEXT);
""")
    for ch, cam, lbl in ((29, "ch29", "lift 3"), (27, "ch27", "lift 2"), (32, "ch32", "lift 5")):
        con.execute("INSERT INTO channel_map VALUES ('site-A',?,1,?,0)", (ch, lbl))
        con.execute("INSERT INTO camera_validation (gateway_id,cam,state,counting_version) "
                    "VALUES ('site-A',?,'live',?)", (cam, V))
        con.execute("INSERT INTO camera_registry VALUES ('site-A',?,1,0)", (cam,))

    now = datetime.now(IST)
    mid = datetime(now.year, now.month, now.day, tzinfo=IST).timestamp()
    y = mid - 86400

    # ch29 + ch27: h3 door era -> cycles exist, close_travel_s is NULL by design.
    for cam in ("ch29", "ch27"):
        for k in range(12):
            t = y + 9 * 3600 + 300 * k
            for st in ("closed", "open", "closed"):
                con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,door_state,door_version,"
                            "reason,floor) VALUES ('site-A',?,?,?,?,'single_panel','12')",
                            (cam, t, st, H3_VERSION))
                t += 6
    # ch29 ALONE has retired Pi history — the source of the transfer inconsistency.
    sid = con.execute("INSERT INTO gw_source (gateway_id,camera) VALUES ('site-A','ch29')").lastrowid
    for k in range(30):
        o = datetime(2026, 7, 18, 9, 0, tzinfo=IST) + timedelta(minutes=5 * k)
        of = o + timedelta(seconds=2.8)
        cs = of + timedelta(seconds=8)
        con.execute("INSERT INTO gw_event (source_id,door_open_start_ts,door_open_full_ts,"
                    "door_close_start_ts,door_close_full_ts,close_travel_s,quality,boarded,alighted)"
                    " VALUES (?,?,?,?,?,?,'ok',3,1)",
                    (sid, o.isoformat(), of.isoformat(), cs.isoformat(),
                     (cs + timedelta(seconds=2.0)).isoformat(), 2.0))
    # ch32: NO gw_door_event and NO gw_event at all — never calibrated. It DOES have transits and
    # occupancy episodes, because those come from the counting path.
    for cam in ("ch29", "ch27", "ch32"):
        for k in range(20):
            con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                        "VALUES ('site-A',?,?, 'in', ?)", (cam, y + 9 * 3600 + 120 * k, k))
        for k in range(6):
            t = y + 9 * 3600 + 400 * k
            con.execute("INSERT INTO validation_item (gateway_id,cam,ts_start,ts_end,"
                        "machine_boarded,machine_alighted,status,counting_version,occupancy_max,"
                        "occupancy_frames,occupancy_degraded,analysed_frames,created_at) "
                        "VALUES ('site-A',?,?,?,2,1,'auto',?,?,?,0,?,?)",
                        (cam, t, t + 18, V, 2 + (k % 3), 130, 130, t))
    con.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,mode,counting_version,proc_ms,"
                "seg_budget_ms,drop_frac) VALUES ('site-A','ch29',?,'live',?,300,2000,0.01)",
                (datetime.now(IST).timestamp(), V))
    con.commit()
    con.close()


def render(tmp, D, data, trends, select=None, view="cams", stale=None):
    node = shutil.which("node")
    if not node:
        return None
    page = D.dash_page()
    page = getattr(page, "body", page)
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.S))
    p = {n: os.path.join(tmp, n) for n in ("d.js", "h.js", "data.json", "tr.json", "out.json")}
    open(p["d.js"], "w").write(js)
    open(p["h.js"], "w").write(HARNESS_JS)
    json.dump(data, open(p["data.json"], "w"))
    json.dump(trends, open(p["tr.json"], "w"))
    argv = [node, p["h.js"], p["d.js"], p["data.json"], p["tr.json"], p["out.json"],
            select or "", view]
    if stale is not None:
        sp = os.path.join(tmp, "stale.json")
        json.dump(stale, open(sp, "w"))
        argv.append(sp)
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        print("  node failed:", (r.stderr or "").strip().splitlines()[:3])
        raise SystemExit(1)
    return json.load(open(p["out.json"]))


def main():
    fails = []
    from test_dash_occupancy import _stub
    _stub()
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "gw.db")
    build_db(db_path)
    os.environ["GATEWAY_DB"] = db_path
    import dash_api as D

    db = D._db()
    data = D._dash_data_inner(db, "site-A").payload
    tr29 = D.dash_trends("site-A", cam="ch29", period="all").payload
    tr27 = D.dash_trends("site-A", cam="ch27", period="all").payload
    tr32 = D.dash_trends("site-A", cam="ch32", period="all").payload
    db.close()

    print("=== 1. summary boxes state WHY close-travel is missing (h3), not a bare dash ===")
    out = render(tmp, D, data, tr29, select="ch29")
    if out is None:
        print("  SKIPPED — no node; nothing below was asserted."); return 0
    boxes = out["trend"]
    ok = "not measured — h3 state-only engine" in boxes
    print(f"  {'printed' if ok else 'NOT PRINTED'} — the h3 reason, in the box")
    if not ok:
        fails.append("summary box still prints a bare dash for close med under h3")
    if re.search(r"close med / p85</span><b[^>]*>—/—", boxes):
        fails.append("a bare '—/—' close figure survives in a summary box")

    print("\n=== 2. transfer carries its instrument, and both tabs make the same KIND of claim ===")
    o29, o27 = out, render(tmp, D, data, tr27, select="ch27")
    t29 = re.search(r"transfer</span><b[^>]*>(.*?)</b>", o29["trend"])
    t27 = re.search(r"transfer</span><b[^>]*>(.*?)</b>", o27["trend"])
    print(f"  ch29: {(t29.group(1) if t29 else '??')[:110]}")
    print(f"  ch27: {(t27.group(1) if t27 else '??')[:110]}")
    for tag, m in (("ch29", t29), ("ch27", t27)):
        if not m:
            fails.append(f"{tag}: no transfer row rendered at all")
        elif "RETIRED" not in m.group(1):
            fails.append(f"{tag} transfer does not name its instrument: {m.group(1)[:90]!r} — this "
                         "is the silent era switch, a Pi-era number beside GPU-era demand")
    if t29 and t27 and ("s/pp" in t29.group(1)) and ("(n=0)" in t27.group(1)):
        fails.append("the two tabs still make different KINDS of statement under one label")

    print("  post-retirement range: the metric must say it cannot come from these dates")
    db2 = D._db()
    today = datetime.now(IST).strftime("%Y-%m-%d")
    tr_today = D.dash_trends("site-A", cam="ch29", period="", from_d=today, to_d=today).payload
    db2.close()
    xf = tr_today["windows"]["all_day"]["transfer"]
    print(f"    applies_to_range={xf.get('applies_to_range')} reason={(xf.get('reason') or '')[:60]}")
    if xf.get("applies_to_range") is not False:
        fails.append("a range entirely after the Pi retirement still claims the transfer applies")
    o_today = render(tmp, D, data, tr_today, select="ch29")
    if "not measured in this range" not in o_today["trend"]:
        fails.append("a post-retirement range does not say the transfer cannot come from it")

    print("\n=== 3. occupancy absence says which absence, and names when measurement began ===")
    note = tr29.get("occupancy_note") or {}
    print(f"  first recorded: {note.get('first_ist')}")
    if not note.get("first_ist"):
        fails.append("/trends does not report when occupancy measurement began")
    tbl = render(tmp, D, data, tr29, select="ch29")
    # the table view is behind a toggle; assert the cell renderer directly on a no-coverage hour
    if "no coverage" not in D.dash_page() and "occCell" not in D.dash_page():
        fails.append("the page has no occupancy-absence renderer")
    empty = dict(tr29)
    empty["occupancy_note"] = {**note, "n_episodes": 0}
    empty["profile"] = [{**p, "occ_peak": None, "occ_n": 0} for p in tr29["profile"]]
    o_empty = render(tmp, D, data, empty, select="ch29")
    if "bars are absent, NOT zero" not in o_empty["trend"]:
        fails.append("an occupancy-free range does not say the bars are absent rather than zero")
    if "The measurement begins" not in o_empty["trend"]:
        fails.append("an occupancy-free range does not name when the measurement began")

    print("\n=== 4. ONE SELECTOR: url, tab highlight, summary line and panel cannot disagree ===")
    sel = render(tmp, D, data, tr32, select="ch32")
    print(f"  after tabs() default: cur={sel['afterDefault']['cur']} trCam={sel['afterDefault']['trCam']}")
    print(f"  after selectCam('ch32'): cur={sel['cur']} trCam={sel['trCam']} urls={sel['urls']}")
    if sel["afterDefault"]["cur"] != sel["afterDefault"]["trCam"]:
        fails.append(f"the two selectors diverge at DEFAULT: {sel['afterDefault']}")
    if sel["cur"] != "ch32" or sel["trCam"] != "ch32":
        fails.append(f"selectCam did not move both selectors: cur={sel['cur']} trCam={sel['trCam']}")
    if not sel["urls"] or "cam=ch32" not in sel["urls"][-1]:
        fails.append(f"the URL does not follow the selection: {sel['urls']}")
    tab_on = re.findall(r'<div class="tab on"[^>]*>(?:<span[^>]*></span>)?(ch\d+)', sel["tabs"])
    print(f"  highlighted tab(s): {tab_on}")
    if tab_on != ["ch32"]:
        fails.append(f"tab highlight disagrees with the selection: {tab_on}")
    # ...and the trends payload must actually be REFETCHED for the new camera, or the summary line
    # keeps describing the previous one while the tab highlight moves.
    sel_fetch = render(tmp, D, data, tr32, select=None)
    print(f"  selecting with trends open issues: {sel_fetch['fetches'][:2]}")
    src = D.dash_page()
    if "if(!opts.noTrends && (mode==='trends' || TR)) loadTrends();" not in src:
        fails.append("selectCam does not re-fetch trends, so the summary line can lag the selection")
    writers = re.findall(r"trCam\s*=\s*", src)
    print(f"  writers of trCam in the page source: {len(writers)} "
          f"(declaration + tabs() default + selectCam)")
    if "onclick=\"trCam=" in src or "trCam=\\'" in src:
        fails.append("a click handler still assigns trCam directly, bypassing the one selector")

    print("\n=== 5. occupancy renders on a door-less camera (counting path, not door path) ===")
    c32 = [c for c in data["cameras"] if c["cam"] == "ch32"][0]
    print(f"  ch32 occupancy in /data: {(c32.get('occupancy') or {}).get('peak')} "
          f"(n={(c32.get('occupancy') or {}).get('n')})")
    if not (c32.get("occupancy") or {}).get("n"):
        fails.append("ch32 has occupancy episodes in the DB but /data reports none")
    occ_hours = [p for p in tr32["profile"] if p["occ_n"]]
    print(f"  ch32 occupancy hours in /trends: {len(occ_hours)}")
    if not occ_hours:
        fails.append("occupancy did not render for a door-less camera — it must not gate on door "
                     "calibration; it is a counting-path product")
    if "peak car occupancy" not in sel["trend"]:
        fails.append("the occupancy chart is missing on an uncalibrated camera's page")

    print("\n=== 6. uncalibrated camera: designed absence, not empty axes and a zero ===")
    print(f"  uncalibrated_cams from /trends: {tr32['range'].get('uncalibrated_cams')}")
    if tr32["range"].get("uncalibrated_cams") != ["ch32"]:
        fails.append(f"ch32 not reported uncalibrated: {tr32['range'].get('uncalibrated_cams')}")
    if tr29["range"].get("uncalibrated_cams"):
        fails.append("a calibrated camera was reported uncalibrated: "
                     f"{tr29['range'].get('uncalibrated_cams')}")
    for needle, why in (
            ("no door calibration on ch32", "the cycles chart must name the camera and the cause"),
            ("door cycles UNAVAILABLE", "cycles must read unavailable, not zero"),
            ("n/a — uncalibrated", "the summary box must not print cycles/hr 0"),
            ("close-travel UNAVAILABLE", "close-travel must read unavailable"),
            ("2.31s Bank C compliance line is deliberately NOT drawn",
             "a threshold ruled across an empty axis states a comparison never made")):
        ok = needle in sel["trend"]
        print(f"  {'printed' if ok else 'NOT PRINTED'} — {needle[:52]}")
        if not ok:
            fails.append(f"{why} ({needle!r} absent)")
    if re.search(r"cycles/hr</span><b[^>]*>0", sel["trend"]):
        fails.append("an uncalibrated camera still shows 'cycles/hr 0' as if the lift were idle")
    if "2.31 Bank C" in sel["trend"]:
        fails.append("the Bank C compliance line is still drawn on an uncalibrated camera")

    print("\n=== 7. SELECTOR ON BOTH VIEWS, and a stale payload must never render ===")
    # /dash?cam=ch16&view=trends drew ch27's summary line with ch16's per-floor panel beneath it.
    # The selector VARIABLES were already unified — they were never the disagreement. Two other
    # things were: ?view=trends was written by selectCam and read by nothing, and loadTrends()
    # rendered immediately while TR still held the previous camera's fetch.
    tr16 = D.dash_trends("site-A", cam="ch29", period="all").payload      # the "previous" camera
    for view in ("cams", "trends"):
        o = render(tmp, D, data, tr27, select="ch27", view=view)
        print(f"  view={view:6s} cur={o['cur']} trCam={o['trCam']} mode={o['mode']} "
              f"urls={o['urls'][-1:] }")
        if o["cur"] != "ch27" or o["trCam"] != "ch27":
            fails.append(f"view={view}: selectors disagree — cur={o['cur']} trCam={o['trCam']}")
        tab_on = re.findall(r'<div class="tab on"[^>]*>(?:<span[^>]*></span>)?(ch\d+)', o["tabs"])
        if view == "cams" and tab_on != ["ch27"]:
            fails.append(f"view={view}: tab highlight disagrees: {tab_on}")
        if o["urls"] and "cam=ch27" not in o["urls"][-1]:
            fails.append(f"view={view}: URL does not follow the selection: {o['urls'][-1]}")
        # the summary line names the camera the payload is FOR
        m = re.search(r'font-size:12px;margin:2px 0 6px">([a-z0-9]+) ·', o["trend"])
        print(f"    summary line names: {m.group(1) if m else '??'}")
        if m and m.group(1) != "ch27":
            fails.append(f"view={view}: the summary line names {m.group(1)}, not the selection")

    src_js = D.dash_page()
    if "WANT_VIEW" not in src_js:
        fails.append("?view=trends is still written by selectCam and read by nothing")
    if "if(WANT_VIEW==='trends' && mode!=='trends')" not in src_js:
        fails.append("?view=trends is parsed but never applied after the first data load")

    print("  mid-flight, with a stale ch29 payload cached and ch27 selected:")
    o = render(tmp, D, data, tr27, select="ch27", view="trends", stale=tr16)
    mid = o["midFlight"]
    m = re.search(r'font-size:12px;margin:2px 0 6px">([a-z0-9]+) ·', mid)
    print(f"    summary line mid-flight: {m.group(1) if m else '(none — loading)'}")
    if m and m.group(1) != "ch27":
        fails.append(f"a stale payload rendered under the new selection: the summary line said "
                     f"{m.group(1)} while ch27 was selected — this is the reported regression")
    if "loading" not in mid and not m:
        fails.append("mid-flight the trends view shows neither the right camera nor 'loading'")
    if "ch29" in mid and "ch27" not in mid:
        fails.append("mid-flight the view is describing ch29 while ch27 is selected")

    print("\n=== 8. h2-era travel carries its invalidation ON THE CAMERA CARD, not just the panel ===")
    # ch16 showed "close median 1.47s · LIVE" from the h2 edge-column instrument the compliance
    # panel marks SUPERSEDED. The rule lived in a loop that skipped every camera without a
    # DOOR_SPECS entry, so it reached exactly one camera.
    h2 = dict(data)
    h2["door_gpu"] = {"ch16": {"era": "aa11bb22", "n_cycles": 40, "n": 40, "median": 1.47,
                               "p85": 1.9, "min": 1.1, "max": 2.4, "hist": [1, 2, 3],
                               "hist_edges": [0, 1, 2, 3], "superseded": True,
                               "superseded_note": D.H2_SUPERSEDED_NOTE}}
    h2["cameras"] = [dict(c, cam="ch16") if c["cam"] == "ch32" else c for c in data["cameras"]]
    o = render(tmp, D, h2, tr27, select="ch16", view="cams")
    pan = o["panel"]
    for needle, why in (("SUPERSEDED", "the card must mark the era superseded"),
                        ("not a current measurement", "the number must be caveated inline"),
                        ("invalidated 2026-08-05", "the card must name the invalidation")):
        ok = needle in pan
        print(f"  {'printed' if ok else 'NOT PRINTED'} — {needle}")
        if not ok:
            fails.append(f"{why} ({needle!r} absent from the camera card)")
    if re.search(r"era aa11bb22 · LIVE", pan):
        fails.append("the card still labels a superseded h2 era as LIVE")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — every absence on the page states which kind of absence it is, the transfer metric "
          "carries its instrument, and one selector drives the URL, the tabs and every panel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
