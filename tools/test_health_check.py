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
  ts REAL, door_state TEXT, door_version TEXT);
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
    # Anchor at 09:00 IST so the daily-line logic is past its 08:30 trigger deterministically.
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
    if "YES" not in res["line"]:
        fails.append("a healthy fleet did not say YES")

    print("\n=== 2. THE FALSE ALARM THAT MUST NOT HAPPEN: 3h with no transit anywhere ===")
    # No new transits at all; heartbeats fresh, segments advancing. Real, common, and healthy:
    # ch29's busiest day ever had three such hours.
    t = base + 3 * 3600
    res, send, why, ch, err = step(H, path, t)
    print(f"  {res['line'][:150]}")
    if not res["ok"]:
        fails.append("a 3h transit-quiet period raised an alarm — this is the measured normal case "
                     "(51-55% of live daytime hours carry zero transits)")
    if send:
        fails.append(f"sent a message on an unchanged healthy state ({why}) — edge-triggering broken")

    print("\n=== 3. beyond 6h with no transit — the derived threshold — now it IS an alarm ===")
    t = base + 6 * 3600 + 600
    res, send, why, ch, err = step(H, path, t)
    print(f"  {res['line'][:200]}")
    print(f"  sent={send} ({why}) channel={ch} err={(err or '')[:60]}")
    if res["ok"]:
        fails.append("7h of fleet-wide transit silence did not alarm")
    if not send or why != "breach":
        fails.append(f"a breach edge did not send (send={send} why={why})")
    if ch is not None:
        fails.append(f"no channel is configured, so nothing should have been delivered: {ch}")
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

    print("\n=== 11. the state table is the audit trail ===")
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
    print("OK — quiet hours do not alarm, 6h+ silence does, dead workers and wedged feeds are "
          "distinguished, breaches are edge-triggered, and an unconfigured channel says so")
    return 0


if __name__ == "__main__":
    sys.exit(main())
