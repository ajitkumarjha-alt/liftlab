#!/usr/bin/env python3
"""The daily health line: every state it can be in, and the false alarm it must NOT raise.

THE CENTRAL ASSERTION is negative. A healthy camera that has simply carried nobody for an hour must
report YES. That is not a detail — measured over the restored gateway snapshot, 51-55% of daytime
hours on days a camera was demonstrably live contain zero transits, and ch29's single busiest day
ever (1,310 transits) still held three empty hours. An hourly transit alarm would have fired on a
healthy camera more than half the time, and a monitor that cries wolf gets muted.

The rest is the state machine: breach edges, recovery, "silent since" surviving across runs, and
the fact that a run with no delivery channel records that fact rather than passing quietly.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE camera_registry (gateway_id TEXT, cam TEXT, enabled INTEGER, stride INTEGER,
  floor_range TEXT, updated_at REAL, PRIMARY KEY (gateway_id, cam));
CREATE TABLE channel_map (gateway_id TEXT, channel INTEGER, is_lift INTEGER, label TEXT,
  marked_at REAL, PRIMARY KEY (gateway_id, channel));
CREATE TABLE analyzer_status (gateway_id TEXT, cam TEXT, ts REAL, segments INTEGER,
  dropped INTEGER, posted INTEGER, last_transit_ts REAL, mode TEXT, counting_version TEXT,
  PRIMARY KEY (gateway_id, cam));
CREATE TABLE transit_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, direction TEXT, track_id INTEGER);
CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT,
  ts REAL, floor TEXT, door_state TEXT, door_version TEXT);
CREATE TABLE relay_status (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
  sum_delivered_mbps REAL, streams_alive INTEGER, streams_delivering INTEGER);
"""
CAMS = ["ch16", "ch27", "ch29", "ch30", "ch32", "ch34", "ch37"]


def build(path, now):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for i, c in enumerate(CAMS):
        con.execute("INSERT INTO camera_registry (gateway_id,cam,enabled,stride) VALUES "
                    "('site-A',?,1,2)", (c,))
        con.execute("INSERT INTO analyzer_status (gateway_id,cam,ts,segments,dropped,posted,mode) "
                    "VALUES ('site-A',?,?,?,0,0,'live')", (c, now - 20, 1000 + 10 * i))
        # last transit 50 MINUTES ago for every camera — inside the brief's one-hour rule and
        # comfortably normal traffic. Nothing here is unhealthy.
        con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                    "VALUES ('site-A',?,?, 'in', 1)", (c, now - 3000))
        con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,door_state) "
                    "VALUES ('site-A',?,?, 'open')", (c, now - 2900))
    con.execute("INSERT INTO relay_status (gateway_id,ts,streams_alive) VALUES ('site-A',?,7)",
                (now - 30,))
    con.commit()
    con.close()


def step(H, path, now, advance_segments=True, quiet_cams=(), dead_cams=()):
    """Run one check at `now`, after nudging the fixture to look like time passed."""
    con = sqlite3.connect(path)
    for c in CAMS:
        if c in dead_cams:
            continue                                   # heartbeat frozen: worker gone
        seg = "segments = segments + 5" if (advance_segments and c not in quiet_cams) else "segments = segments"
        con.execute(f"UPDATE analyzer_status SET ts=?, {seg} WHERE cam=?", (now - 20, c))
    con.execute("INSERT INTO relay_status (gateway_id,ts,streams_alive) VALUES ('site-A',?,7)",
                (now - 30,))
    con.commit()
    con.close()
    db = H._db(path)
    try:
        res = H.evaluate(db, "site-A", now=now)
        send, why = H.should_send(db, res, now=now)
        ch, err = (H.deliver(res) if send else (None, None))
        H.record(db, res, ch, err if send else None, sent=send)
    finally:
        db.close()
    return res, send, why, ch, err


HARNESS_JS = r"""
const fs = require('fs');
const els = {};
function el(id){ return els[id] || (els[id] = {id, innerHTML:'', style:{}, value:'', textContent:'',
  getAttribute(){return null}, setAttribute(){}, appendChild(){}}); }
global.document = {getElementById: el, addEventListener(){}, createElement(){return el('t')},
  querySelector(){return null}, querySelectorAll(){return []}, title:'', body: el('b')};
global.window = {addEventListener(){}, innerWidth:1200, location:{search:'', pathname:'/dash', href:''}};
global.location = global.window.location;
global.history = {replaceState(){}, pushState(){}};
global.localStorage = {getItem(){return null}, setItem(){}};
global.setTimeout = () => 0; global.clearTimeout = () => {}; global.setInterval = () => 0;
global.requestAnimationFrame = () => 0;
global.fetch = () => new Promise(() => {});
global.EventSource = function(){ this.addEventListener=function(){}; this.close=function(){}; };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const states = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const bar = el('healthbar');            // create it before the page's own code looks it up
const out = states.map(function(h){ bar.innerHTML=''; healthbar({health: h}); return bar.innerHTML; });
fs.writeFileSync(process.argv[4], JSON.stringify(out));
"""


def _render_banner(tmp, cases):
    """Render the REAL dash banner in node for each health state. -> [html] or None if no node.

    The banner is the only surface this line has when no push channel is configured, so what it
    actually prints is the deliverable — not the fact that the function exists.
    """
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_dash_occupancy import _stub
    _stub()
    import dash_api as D
    page = D.dash_page()
    page = getattr(page, "body", page)
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", page, re.S))
    paths = {n: os.path.join(tmp, n) for n in ("d.js", "h.js", "states.json", "out.json")}
    open(paths["d.js"], "w").write(js)
    open(paths["h.js"], "w").write(HARNESS_JS)
    json.dump([c[0] for c in cases], open(paths["states.json"], "w"))
    r = subprocess.run([node, paths["h.js"], paths["d.js"], paths["states.json"], paths["out.json"]],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  node failed:", (r.stderr or "").strip().splitlines()[:1])
        raise SystemExit(1)
    got = json.load(open(paths["out.json"]))
    return [(got[i], cases[i][1], cases[i][2]) for i in range(len(cases))]


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    # 09:00 IST: past the 08:30 daily trigger AND inside the 07:00-23:00 active window, so the
    # transit rule is armed. A test anchored outside active hours would pass while proving nothing.
    base = datetime(2026, 8, 12, 9, 0, tzinfo=IST).timestamp()
    build(path, base)
    os.environ["GATEWAY_DB"] = path
    os.environ.pop("HEALTH_WEBHOOK_URL", None)
    os.environ.pop("HEALTH_SMTP_HOST", None)
    import health_check as H

    print("=== 1. healthy fleet, last transit 50 minutes ago on every camera ===")
    res, send, why, ch, err = step(H, path, base)
    print(f"  {res['line'][:150]}")
    print(f"  ok={res['ok']} source={res['cam_source']} sent={send} ({why})")
    if not res["ok"]:
        fails.append(f"FALSE ALARM on a healthy fleet: {res['bad']} — {res['detail']}")
    if res["n_cams"] != 7:
        fails.append(f"expected 7 cameras from the registry, got {res['n_cams']}")
    print(f"  litestream: {res['litestream_ok']} ({res['litestream_note']}); "
          f"pi_age={res['pi_age_s']}s")
    if res["litestream_ok"] is not None:
        fails.append("a host with no litestream unit must report UNKNOWN, not a verdict — "
                     "reporting a missing unit as a broken backup cries wolf on every dev box")
    if not res["line"].startswith("LiftLab health ") or "— OK" not in res["line"]:
        fails.append(f"the healthy line does not match the specified format: {res['line']!r}")

    print("\n=== 2. >60 min silent INSIDE active hours IS a breach, and names the class ===")
    t = base + 2 * 3600
    res, send, why, ch, err = step(H, path, t)
    print(f"  {res['line'][:220]}")
    print(f"  sent={send} ({why}) channel={ch} err={(err or '')[:50]}")
    if res["ok"]:
        fails.append("2h of active-hours silence did not breach the 60-minute rule")
    if not res["line"].startswith("LiftLab health "):
        fails.append(f"line does not match the specified format: {res['line'][:60]!r}")
    if "BREACH:" not in res["line"]:
        fails.append("a breach line does not say BREACH")
    if "silent since" not in res["line"]:
        fails.append("the breach line does not say when the camera went silent")
    # segments were advancing in step(), so this is the worker-stall class
    if "segments flowing = worker stall class" not in res["line"]:
        fails.append(f"the breach line does not name the stall class: {res['line'][:200]!r}")
    if not send or why != "breach":
        fails.append(f"a breach edge did not send (send={send} why={why})")
    if not err or "no push channel" not in err:
        fails.append("a run with no delivery channel must RECORD that fact, not pass quietly")

    print("\n=== 4. a standing breach does not spam ===")
    t += 900
    res, send, why, ch, err = step(H, path, t)
    print(f"  sent={send} ({why})")
    if send:
        fails.append("a standing breach re-sent — a camera down for three days would produce "
                     "hundreds of messages and drown the daily line")

    print("\n=== 5. one worker dies: heartbeat stale, and 'silent since' survives runs ===")
    con = sqlite3.connect(path)              # give everyone a fresh transit so only ch29 is at fault
    for c in CAMS:
        con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                    "VALUES ('site-A',?,?, 'in', 2)", (c, t + 60))
    con.commit(); con.close()
    t += 120
    res, _s, _w, _c, _e = step(H, path, t, dead_cams=("ch29",))        # ch29 heartbeat frozen
    t += H.HB_STALE_S + 300
    res, send, why, ch, err = step(H, path, t, dead_cams=("ch29",))
    print(f"  {res['line'][:200]}")
    if res["bad"] != ["ch29"]:
        fails.append(f"expected only ch29 bad, got {res['bad']}")
    if "worker silent" not in res["detail"]["ch29"]["reasons"][0]:
        fails.append(f"wrong reason for a dead worker: {res['detail']['ch29']['reasons']}")
    first_seen = res["detail"]["ch29"]["bad_since"]
    t2 = t + 3600
    res2, _s, _w, _c, _e = step(H, path, t2, dead_cams=("ch29",))
    print(f"  an hour later, breach first seen carried forward: "
          f"{res2['detail']['ch29']['bad_since'] == first_seen}")
    if res2["detail"]["ch29"]["bad_since"] != first_seen:
        fails.append("'silent since' was reset on a later run — the operator would be told the "
                     "fault started an hour after it did")

    print("\n=== 6. heartbeat alive, segments frozen — the wedged-Pi case ===")
    con = sqlite3.connect(path)
    con.execute("UPDATE analyzer_status SET ts=?, segments=99 WHERE cam='ch29'", (t2 - 20,))
    for c in CAMS:
        con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                    "VALUES ('site-A',?,?, 'in', 3)", (c, t2 + 30))
    con.commit(); con.close()
    t3 = t2 + 600
    step(H, path, t3, quiet_cams=("ch29",))                 # first run establishes the counter
    t4 = t3 + 600
    res, send, why, ch, err = step(H, path, t4, quiet_cams=("ch29",))
    print(f"  {res['line'][:200]}")
    if "ch29" not in res["bad"]:
        fails.append("a worker heartbeating with a frozen segment counter was reported healthy — "
                     "that is exactly what a wedged Pi looks like from the gateway")
    elif "processing nothing" not in " ".join(res["detail"]["ch29"]["reasons"]):
        fails.append(f"wrong reason for a frozen counter: {res['detail']['ch29']['reasons']}")

    print("\n=== 7. recovery sends, once ===")
    con = sqlite3.connect(path)
    con.execute("UPDATE analyzer_status SET ts=?, segments=500 WHERE cam='ch29'", (t4 + 60,))
    con.commit(); con.close()
    t5 = t4 + 120
    res, send, why, ch, err = step(H, path, t5)
    print(f"  ok={res['ok']} sent={send} ({why})")
    if not res["ok"]:
        fails.append(f"did not recover: {res['bad']} {res['detail'].get('ch29')}")
    if not send or why != "recovered":
        fails.append(f"recovery was not announced (send={send} why={why})")
    res, send, why, ch, err = step(H, path, t5 + 300)
    if send and why not in ("daily",):
        fails.append(f"re-sent after recovery ({why})")

    print("\n=== 8. a camera that vanished from every table is still counted ===")
    con = sqlite3.connect(path)
    con.execute("DELETE FROM analyzer_status WHERE cam='ch37'")
    con.execute("DELETE FROM transit_event WHERE cam='ch37'")
    con.commit(); con.close()
    res, _s, _w, _c, _e = step(H, path, t5 + 600)
    print(f"  n_cams={res['n_cams']} bad={res['bad']}")
    if res["n_cams"] != 7:
        fails.append(f"a vanished camera dropped out of the denominator ({res['n_cams']}) — the "
                     "line would report all-of-nothing healthy")
    if "ch37" not in res["bad"]:
        fails.append("a camera with no rows anywhere was not reported as bad")

    print("\n=== 9. delivery: a configured webhook is used, and a failure is recorded ===")
    os.environ["HEALTH_WEBHOOK_URL"] = "http://127.0.0.1:9/liftlab"      # port 9 = discard, refuses
    H.WEBHOOK_URL = os.environ["HEALTH_WEBHOOK_URL"]
    res = H.evaluate(H._db(path), "site-A", now=t5 + 900)
    ch, err = H.deliver(res)
    print(f"  channel={ch} err={(err or '')[:70]}")
    if ch is not None or not err or "webhook failed" not in err:
        fails.append(f"a failing webhook must be recorded as a failure, got channel={ch} err={err}")
    H.WEBHOOK_URL = ""
    os.environ.pop("HEALTH_WEBHOOK_URL")

    print("\n=== 10. the dashboard banner renders each state, and never_run is not 'fine' ===")
    banner = _render_banner(tmp, [
        ({"state": "never_run", "note": "no health check recorded for this gateway yet"},
         ["HEALTH CHECK HAS NEVER RUN", "absence of an alert means nothing"], ["ALL "]),
        ({"state": "ok", "age_s": 300, "n_cams": 7, "n_bad": 0, "line": "all 7 cameras posting: YES",
          "stale": False, "delivery_error": None},
         ["ALL 7 CAMERAS POSTING"], ["SILENT"]),
        ({"state": "breach", "age_s": 300, "n_cams": 7, "n_bad": 1, "stale": False,
          "line": "all 7 cameras posting: NO — ch29 worker silent 22m",
          "delivery_error": "no push channel configured (set HEALTH_WEBHOOK_URL...)"},
         ["1 OF 7 CAMERAS SILENT", "ch29 worker silent", "no push channel configured"], []),
        ({"state": "ok", "age_s": 4 * 3600, "n_cams": 7, "n_bad": 0, "stale": True,
          "line": "all 7 cameras posting: YES", "delivery_error": None},
         ["the CHECKER", "stopped running"], []),
    ])
    if banner is None:
        print("  SKIPPED — no `node`. The banner's rendered text was NOT asserted.")
    else:
        for i, (html, want, unwanted) in enumerate(banner):
            for w in want:
                ok = w in html
                print(f"  state {i}: {'printed' if ok else 'NOT PRINTED'} — {w[:46]}")
                if not ok:
                    fails.append(f"health banner state {i} never prints {w!r}")
            for u in unwanted:
                if u in html:
                    fails.append(f"health banner state {i} wrongly prints {u!r}")

    print("\n=== 11. quiet OUTSIDE active hours is reported, never alarmed ===")
    # OWN DATABASE, on purpose: this scenario sits at 02:00 and the timeline above ends at 09:00+.
    # Sharing one DB would run the clock backwards, and _prev() reads the newest row — every later
    # comparison would then be against a future it had not reached yet.
    npath = os.path.join(tmp, "night.db")
    t_night = datetime(2026, 8, 13, 2, 0, tzinfo=IST).timestamp()
    build(npath, t_night - 3 * 3600)          # last transit three hours ago, at 23:00
    resn, sendn, whyn, chn, errn = step(H, npath, t_night)
    print(f"  {resn['line'][:160]}")
    print(f"  active_hours={resn['active_hours']} quiet_offhours={resn['quiet_offhours']}")
    if not resn["ok"]:
        fails.append(f"alarmed outside active hours: {resn['bad']} — a residential lift at 02:00 "
                     "carries nobody, and a rule that fires then teaches the reader to ignore it")
    if len(resn["quiet_offhours"]) != len(CAMS):
        fails.append(f"off-hours silence was not REPORTED: {resn['quiet_offhours']}")
    if "reported, not alarmed" not in resn["line"]:
        fails.append("the line does not say the off-hours quiet was reported rather than alarmed")

    print("\n=== 11b. LIVE SHAPE: bad for a NON-transit reason while transits are FLOWING ===")
    # THE CASE THE SNAPSHOT REPLAY NEVER PRODUCED, and the one that broke on the first live run.
    # Every §12-style test silenced a camera's transits, so the transit branch always ran and always
    # set a class. A camera that breaches on its HEARTBEAT while posting normally took the same code
    # path and rendered as "ch29 silent since <last transit> (None)" — a camera that had posted
    # three seconds earlier, described as silent, with an empty class. The check was right and the
    # sentence was false, which is worse than no sentence.
    lpath = os.path.join(tmp, "liveshape.db")
    t_live = datetime(2026, 8, 12, 20, 7, tzinfo=IST).timestamp()
    build(lpath, t_live)
    con = sqlite3.connect(lpath)
    con.execute("DELETE FROM analyzer_status WHERE cam='ch29'")      # worker never reported
    con.execute("INSERT INTO transit_event (gateway_id,cam,ts,direction,track_id) "
                "VALUES ('site-A','ch29',?,'in',9)", (t_live - 3,))  # ...but transits are flowing
    con.commit(); con.close()
    dbl = H._db(lpath)
    try:
        rl = H.evaluate(dbl, "site-A", now=t_live)
    finally:
        dbl.close()
    print(f"  {rl['line'][:190]}")
    dl = rl["detail"]["ch29"]
    print(f"  transit_age={dl['transit_age_s']}s  reasons={dl['reasons'][:1]}")
    if "ch29" not in rl["bad"]:
        fails.append("a camera with no analyzer_status row at all was reported healthy")
    if "(None)" in rl["line"]:
        fails.append("the breach line prints '(None)' as a class — it is rendering a non-transit "
                     "breach through the transit format")
    if "silent since" in rl["line"]:
        fails.append("the line calls a camera SILENT while its transits are 3 s old — the breach "
                     "was about the heartbeat, and saying 'silent' sends the reader to the wrong box")
    if "no heartbeat" not in rl["line"]:
        fails.append(f"the line does not state the actual reason: {rl['line'][:150]!r}")
    if dl["transit_age_s"] > 60:
        fails.append("fixture error: transits were supposed to be flowing")

    print("\n=== 11c. apply_health.sh: the install decision, all three exit paths ===")
    # `if ! cmd; then RC=$?` captures the status of the NEGATION (always 0), never the command's.
    # That made every breach abort with "failed with exit 0", and a genuine crash report the same.
    sh = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "apply_health.sh")).read()
    if re.search(r"if\s*!\s*sudo[^\n]*\n(?:[^\n]*\n)*?\s*RC=\$\?", sh):
        fails.append("apply_health.sh still captures $? inside `if !` — it can only ever see 0")
    import subprocess as _sp
    probe = os.path.join(tmp, "probe.sh")
    open(probe, "w").write(
        'set -uo pipefail\n'
        'run(){ bash -c "exit $1"; RC=$?;'
        ' if [ "$RC" -ne 0 ] && [ "$RC" -ne 1 ]; then echo "ABORT:$RC"; return 9; fi;'
        ' echo "INSTALL:$RC"; return 0; }\n'
        'run 0; run 1; run 3\n')
    got = _sp.run(["bash", probe], capture_output=True, text=True).stdout.split()
    print(f"  healthy/breach/crash -> {got}")
    if got != ["INSTALL:0", "INSTALL:1", "ABORT:3"]:
        fails.append(f"the corrected install decision is wrong: {got}")

    print("\n=== 11d. OUT OF SERVICE is a class, not a fault (ch16, 2026-08-18) ===")
    # The lift was parked at P4 with the indicator reading "P4 OUT". Zero transits were CORRECT and
    # the checker called it "worker stall class" — nothing was broken except the classification.
    # The discriminator is the indicator's STABILITY plus door silence, not the floor VALUE: this
    # reader emits 85 distinct floor strings on ch16, 20.4% implausible by shape, so the string is
    # quoted as an assembly and never as a fact.
    opath = os.path.join(tmp, "oos.db")
    t_oos = datetime(2026, 8, 18, 14, 0, tzinfo=IST).timestamp()
    build(opath, t_oos - 4 * 3600)                 # last transit 4h ago, inside active hours
    con = sqlite3.connect(opath)
    for k in range(200):                           # a static indicator, doors never opening
        con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state) "
                    "VALUES ('site-A','ch16',?, 'P4', 'closed')", (t_oos - 4 * 3600 + 60 * k,))
    # The worker is HEALTHY — fresh heartbeat, segments flowing, Pi reporting. That is the whole
    # point: everything about the fleet is fine and the lift is simply parked.
    con.execute("UPDATE analyzer_status SET ts=?", (t_oos - 20,))
    con.execute("INSERT INTO relay_status (gateway_id,ts,streams_alive) VALUES ('site-A',?,7)",
                (t_oos - 30,))
    con.commit(); con.close()
    dbo = H._db(opath)
    try:
        ro = H.evaluate(dbo, "site-A", now=t_oos)
    finally:
        dbo.close()
    k16 = (ro["detail"].get("ch16") or {}).get("klass") or ""
    print(f"  ch16 class: {k16[:120]}")
    print(f"  ch16 in bad list: {'ch16' in ro['bad']}   line: {ro['line'][:110]}")
    if not k16.startswith("LIFT OUT OF SERVICE"):
        fails.append(f"a parked lift with a static indicator was not classed out of service: {k16!r}")
    if "ch16" in ro["bad"]:
        fails.append("out of service was ALARMED as a fault — it is a fact about the building, and "
                     "alarming it is how an operator learns to ignore the line")
    if "reader assembly" not in k16:
        fails.append("the indicator string is quoted as fact rather than as the reader's assembly")

    print("  a MOVING lift that is merely quiet must NOT be called out of service:")
    mpath = os.path.join(tmp, "moving.db")
    build(mpath, t_oos - 4 * 3600)
    con = sqlite3.connect(mpath)
    for k in range(200):                           # floor changes, doors open: in service, no riders
        con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,floor,door_state) "
                    "VALUES ('site-A','ch16',?,?,?)",
                    (t_oos - 4 * 3600 + 60 * k, str(3 + (k % 9)), "open" if k % 5 == 0 else "closed"))
    con.execute("UPDATE analyzer_status SET ts=?", (t_oos - 20,))
    con.execute("INSERT INTO relay_status (gateway_id,ts,streams_alive) VALUES ('site-A',?,7)",
                (t_oos - 30,))
    con.commit(); con.close()
    dbm = H._db(mpath)
    try:
        rm = H.evaluate(dbm, "site-A", now=t_oos)
    finally:
        dbm.close()
    km = (rm["detail"].get("ch16") or {}).get("klass") or ""
    print(f"  ch16 class: {km[:100]}")
    if km.startswith("LIFT OUT OF SERVICE"):
        fails.append("a lift that is moving and opening its doors was called out of service")

    print("\n=== 12. a silent camera simulated against a COPY OF THE REAL DATABASE ===")
    # A synthetic fixture proves the logic; a real database proves it against the shapes the
    # gateway actually holds — seven cameras, real registry rows, real analyzer_status, real
    # transit history with its real quiet spells. The DB is COPIED first: this test must never be
    # able to touch a live gateway, and health_check writes health_status.
    import shutil as _sh
    src = os.environ.get("HEALTH_TEST_DB") or os.path.expanduser("~/gateway-snapshot.db")
    if not os.path.exists(src):
        print(f"  SKIPPED — no real DB at {src}. The synthetic sections above still ran; this one "
              f"did not, so nothing here has been checked against real data.")
    else:
        real = os.path.join(tmp, "real_copy.db")
        _sh.copy(src, real)
        con = sqlite3.connect(real)
        # Anchor "now" just after the copy's newest transit so the fleet reads healthy, then take
        # ONE camera silent by deleting its last two hours. Everything else is untouched.
        newest = con.execute("SELECT MAX(ts) FROM transit_event").fetchone()[0]
        victim = "ch29"
        con.execute("DELETE FROM transit_event WHERE cam=? AND ts > ?", (victim, newest - 7200))
        # Make the world consistent at that instant: fresh heartbeats, fresh Pi telemetry, and
        # segments ADVANCING (so the class must come out as the worker-stall class, not upstream).
        now_r = newest + 300
        con.execute("UPDATE analyzer_status SET ts=?", (now_r - 20,))
        try:
            con.execute("INSERT INTO relay_status (gateway_id,ts) VALUES ('site-A',?)", (now_r - 30,))
        except sqlite3.OperationalError:
            pass
        con.commit(); con.close()

        dbr = H._db(real)
        try:
            r1 = H.evaluate(dbr, "site-A", now=now_r)          # first run: no previous segment sample
            H.record(dbr, r1, None, None, sent=False)
            con = sqlite3.connect(real)
            con.execute("UPDATE analyzer_status SET ts=?, segments=segments+40", (now_r + 580,))
            con.commit(); con.close()
            r2 = H.evaluate(dbr, "site-A", now=now_r + 600)    # second run: segments have advanced
        finally:
            dbr.close()
        print(f"  cameras in the real registry: {r2['n_cams']} (from {r2['cam_source']})")
        print(f"  {r2['line'][:230]}")
        active = H._active(now_r + 600)
        print(f"  the copy's clock lands in active hours: {active}")
        if not active:
            print("  (the snapshot's newest transit is outside 07:00-23:00 IST, so the transit rule "
                  "is correctly disarmed here — the class assertion below is skipped, not passed)")
        else:
            if victim not in r2["bad"]:
                fails.append(f"the simulated silent camera {victim} was not detected against real "
                             f"data: bad={r2['bad']}")
            if victim not in r2["line"]:
                fails.append(f"the breach line does not NAME the camera: {r2['line'][:160]!r}")
            klass = (r2["detail"].get(victim) or {}).get("klass")
            print(f"  class for {victim}: {klass}")
            if klass != "segments flowing = worker stall class":
                fails.append(f"the breach line does not name the right class for a stalled worker "
                             f"with segments flowing: {klass!r}")
            if "silent since" not in r2["line"]:
                fails.append("the breach line does not say when the camera went silent")

    print("\n=== 13. the state table is the audit trail ===")
    con = sqlite3.connect(path)
    n, nbad = con.execute("SELECT COUNT(*), SUM(ok=0) FROM health_status").fetchone()
    print(f"  {n} checks recorded, {nbad} of them breaches")
    if not n or not nbad:
        fails.append("health_status did not record the run history")
    con.close()

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — off-hours quiet is reported not alarmed, >60min in active hours breaches and names "
          "its class, dead workers and wedged feeds are distinguished, breaches are edge-triggered, "
          "and an unconfigured channel says so")
    return 0


if __name__ == "__main__":
    sys.exit(main())
