#!/usr/bin/env python3
"""DAILY HEALTH LINE — one sentence a human reads, gateway-side, off the request path.

WHAT IT IS FOR: a silent gap should be found by the system, not by someone asking. The whole feature
is one line — "all 7 cameras posted within the last hour: YES", or "NO — ch29 silent since 04:12".

WHY THE ALARM IS NOT "A TRANSIT IN THE LAST HOUR". That was the obvious rule and it is measurably
wrong. Over the 16,753 consecutive transit gaps in the restored gateway snapshot (2026-07-16 →
08-02, seven cameras):

    * 51-55% of DAYTIME hours (06:00-23:59 IST) on days a camera was demonstrably live contain
      ZERO transits. Per camera: ch27 54.6%, ch16 54.4%, ch34 54.2%, ch29 53.7%, ch37 51.4%,
      ch32 51.1%, ch30 51.1%.
    * On ch29's single busiest day ever recorded (2026-07-17, 1,310 transits) THREE hours held
      none: 03:00, 17:00, 18:00.

An hourly transit alarm would therefore have fired on a healthy camera more than half of all live
hours, and three times on the best day this fleet has ever had. A monitor that cries wolf gets
muted, and a muted monitor is worse than none — it converts an unknown into a false assurance.

WHAT THE ALARM IS INSTEAD. Three signals, each covering a failure the others cannot see:

  1. HEARTBEAT AGE (analyzer_status.ts). The worker posts this every segment, whether or not anyone
     walks through the door. Stale => that camera's worker is not running. Catches GPU stalls and a
     dead fleet supervisor.
  2. SEGMENTS NOT ADVANCING (analyzer_status.segments, monotonic per worker run). Heartbeat fresh
     but the counter frozen => the worker is alive and processing nothing. This is what a WEDGED PI
     or a stalled relay looks like from the gateway: the GPU is fine and there is no video.
  3. NO TRANSIT IN 6 HOURS. Kept, but at a threshold derived from the same data rather than
     assumed: only 14 of 16,753 gaps exceed 6h (0.08%), and those coincide with the declared
     outages. This is the signal that survives a worker which heartbeats happily while counting
     nobody.

Transit age is REPORTED for every camera whether or not it breaches, because that is the number
that was asked for and it is informative; it is simply not the trigger.

WHAT IT CANNOT SEE, said out loud. If this VM is down, no line is sent — so ABSENCE OF THE DAILY
LINE IS ITSELF A BREACH, and that is precisely why the daily line is sent on healthy days too. A
monitor that only speaks when something is wrong is indistinguishable, when it goes quiet, from a
monitor whose box has died.

DELIVERY. Configured by environment, never by code edit. If no channel is configured the line still
lands in the journal, in the state table, and on the dashboard banner — and it SAYS SO, on the
surfaces that do exist, so "no alert arrived" can never be read as "all is well".

Run:  python3 health_check.py [site-A] [--daily] [--json]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db")

# ── Thresholds. Every one of these is derived; see the module docstring. ──────────────────────
HB_STALE_S = float(os.environ.get("HEALTH_HB_STALE_S", "900"))        # 15 min: survives a restart
TRANSIT_STALE_S = float(os.environ.get("HEALTH_TRANSIT_STALE_S", str(6 * 3600)))
# A camera is expected to be posting only if the registry says it is enabled. The denominator comes
# from the REGISTRY, never from "cameras that happen to have rows" — a camera that vanished entirely
# would otherwise drop out of the count and the line would cheerfully report all-of-nothing healthy.
# That is the same defect class as the gate that keyed on conditionally-populated evidence.

DAILY_HOUR_IST = int(os.environ.get("HEALTH_DAILY_HOUR", "8"))
DAILY_MIN_IST = int(os.environ.get("HEALTH_DAILY_MIN", "30"))

WEBHOOK_URL = os.environ.get("HEALTH_WEBHOOK_URL", "").strip()
WEBHOOK_FIELD = os.environ.get("HEALTH_WEBHOOK_FIELD", "").strip()   # "" = POST the raw text
SMTP_HOST = os.environ.get("HEALTH_SMTP_HOST", "").strip()
SMTP_TO = os.environ.get("HEALTH_SMTP_TO", "").strip()


def _db(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS health_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, ts REAL,
        ok INTEGER, n_cams INTEGER, n_bad INTEGER, line TEXT, detail TEXT,
        sent INTEGER DEFAULT 0, delivered TEXT, delivery_error TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_health_gw_ts ON health_status(gateway_id, ts)")
    db.commit()
    return db


def _hhmm(epoch, now=None):
    """HH:MM for today, 'MMM-DD HH:MM' otherwise.

    A bare "since 11:54" for something that stopped ten days ago reads as this morning, which is the
    difference between a blip and an outage. The date appears exactly when it changes the meaning.
    """
    if not epoch:
        return "never"
    t = datetime.fromtimestamp(epoch, IST)
    ref = datetime.fromtimestamp(time.time() if now is None else now, IST)
    return t.strftime("%H:%M") if t.date() == ref.date() else t.strftime("%b-%d %H:%M")


def _age_phrase(sec):
    if sec is None:
        return "never"
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{sec / 60:.0f}m"
    return f"{sec / 3600:.1f}h"


def expected_cams(db, gw):
    """The cameras that SHOULD be posting, and where that list came from."""
    rows = db.execute("SELECT cam FROM camera_registry WHERE gateway_id=? AND enabled=1 "
                      "ORDER BY cam", (gw,)).fetchall()
    if rows:
        return [r["cam"] for r in rows], "camera_registry (enabled=1)"
    rows = db.execute("SELECT channel FROM channel_map WHERE gateway_id=? AND is_lift=1 "
                      "ORDER BY channel", (gw,)).fetchall()
    return [f"ch{r['channel']}" for r in rows], "channel_map (is_lift=1) — registry was empty"


def _prev(db, gw):
    r = db.execute("SELECT ts, ok, detail FROM health_status WHERE gateway_id=? "
                   "ORDER BY ts DESC LIMIT 1", (gw,)).fetchone()
    if not r:
        return None
    try:
        return {"ts": r["ts"], "ok": bool(r["ok"]), "detail": json.loads(r["detail"] or "{}")}
    except ValueError:
        return {"ts": r["ts"], "ok": bool(r["ok"]), "detail": {}}


def evaluate(db, gw, now=None):
    """-> the full result. Pure read; writes nothing."""
    now = time.time() if now is None else now
    cams, cam_source = expected_cams(db, gw)
    prev = _prev(db, gw)
    prev_seg = {c: (prev or {}).get("detail", {}).get(c, {}).get("segments") for c in cams}
    prev_ok_since = {c: (prev or {}).get("detail", {}).get(c, {}).get("bad_since") for c in cams}

    hb = {r["cam"]: r for r in db.execute(
        "SELECT cam, ts, segments, dropped, posted, last_transit_ts, mode "
        "FROM analyzer_status WHERE gateway_id=?", (gw,))}
    tr = {r["cam"]: r["mx"] for r in db.execute(
        "SELECT cam, MAX(ts) mx FROM transit_event WHERE gateway_id=? GROUP BY cam", (gw,))}
    dr = {r["cam"]: r["mx"] for r in db.execute(
        "SELECT cam, MAX(ts) mx FROM gw_door_event WHERE gateway_id=? GROUP BY cam", (gw,))}

    detail, bad = {}, []
    for cam in cams:
        h = hb.get(cam)
        hb_ts = h["ts"] if h else None
        segments = h["segments"] if h else None
        hb_age = (now - hb_ts) if hb_ts else None
        tr_ts, dr_ts = tr.get(cam), dr.get(cam)
        tr_age = (now - tr_ts) if tr_ts else None

        reasons = []
        if hb_ts is None:
            reasons.append("no heartbeat ever")
        elif hb_age > HB_STALE_S:
            reasons.append(f"worker silent {_age_phrase(hb_age)} (since {_hhmm(hb_ts, now)})")
        else:
            # Only meaningful while the heartbeat is FRESH: a stale heartbeat carries a stale
            # counter, and reporting "processing nothing" about a worker that is not running at all
            # would name the wrong fault and send someone to the wrong box.
            ps = prev_seg.get(cam)
            if (ps is not None and segments is not None and segments == ps
                    and prev and (now - prev["ts"]) > 120):
                reasons.append(f"alive but processing nothing — segments stuck at {segments} "
                               f"since {_hhmm(prev['ts'], now)} (no video reaching the worker)")
        if tr_ts is None:
            reasons.append("no transit ever recorded")
        elif tr_age > TRANSIT_STALE_S:
            reasons.append(f"no transit in {_age_phrase(tr_age)} (last {_hhmm(tr_ts, now)})")

        bad_since = prev_ok_since.get(cam) if reasons else None
        if reasons and not bad_since:
            bad_since = now                      # first check that saw it — the honest "since"
        detail[cam] = {
            "ok": not reasons, "reasons": reasons, "segments": segments,
            "hb_ts": hb_ts, "hb_age_s": None if hb_age is None else round(hb_age, 1),
            "transit_ts": tr_ts, "transit_age_s": None if tr_age is None else round(tr_age, 1),
            "door_ts": dr_ts, "mode": (h["mode"] if h else None),
            "bad_since": bad_since,
        }
        if reasons:
            bad.append(cam)

    ok = not bad
    n = len(cams)
    if ok:
        line = (f"all {n} cameras posting: YES — "
                + ", ".join(f"{c} {_age_phrase(detail[c]['transit_age_s'])}" for c in cams)
                + " since last transit")
    else:
        parts, res_now = [], now
        for c in bad:
            since = detail[c]["bad_since"]
            parts.append(f"{c} {'; '.join(detail[c]['reasons'])}"
                         + (f" [breach first seen {_hhmm(since, res_now)}]" if since else ""))
        line = f"all {n} cameras posting: NO — " + " · ".join(parts)
    return {"gw": gw, "ts": now, "ok": ok, "cams": cams, "cam_source": cam_source,
            "n_cams": n, "bad": bad, "detail": detail, "line": line,
            "prev_ok": (None if prev is None else prev["ok"])}


def should_send(db, res, now=None):
    """Send on a BREACH EDGE, on recovery, and once a day. -> (bool, why).

    Edge-triggered, not level-triggered: a camera down for three days must not produce 432 messages,
    or the daily line drowns in them and stops being read. The daily line still restates the
    outstanding breach every morning, so a standing fault cannot fade from view either.

    THE DAILY DE-DUPLICATION LIVES HERE, not in the caller. It keys on whether a send was ATTEMPTED
    since today's trigger time — not on whether one succeeded. Keying on success means that with no
    channel configured (or a channel that is down) every single run counts as "not sent yet" and the
    line goes out on every tick forever, which is precisely the flood this function exists to stop.
    """
    now = time.time() if now is None else now
    if res["prev_ok"] is None:
        return True, "first run"
    if res["prev_ok"] and not res["ok"]:
        return True, "breach"
    if res["ok"] and not res["prev_ok"]:
        return True, "recovered"
    ist = datetime.fromtimestamp(now, IST)
    target = ist.replace(hour=DAILY_HOUR_IST, minute=DAILY_MIN_IST,
                         second=0, microsecond=0).timestamp()
    if now >= target and not sent_since(db, res["gw"], target):
        return True, "daily"
    return False, "no change"


def sent_since(db, gw, since_epoch):
    """Was a send ATTEMPTED since this epoch. Attempted, not delivered — see should_send."""
    r = db.execute("SELECT COUNT(*) n FROM health_status WHERE gateway_id=? AND ts>=? AND sent=1",
                   (gw, since_epoch)).fetchone()
    return (r["n"] or 0) > 0


def deliver(res):
    """-> (channel, error). Never raises: a delivery failure must not lose the recorded status."""
    text = (f"[liftlab {res['gw']}] {datetime.fromtimestamp(res['ts'], IST):%Y-%m-%d %H:%M} IST\n"
            f"{res['line']}")
    if WEBHOOK_URL:
        try:
            if WEBHOOK_FIELD:
                body = json.dumps({WEBHOOK_FIELD: text}).encode()
                hdr = {"Content-Type": "application/json"}
            else:
                body, hdr = text.encode(), {"Content-Type": "text/plain"}
            req = urllib.request.Request(WEBHOOK_URL, data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return f"webhook HTTP {r.status}", None
        except Exception as e:
            return None, f"webhook failed: {type(e).__name__}: {str(e)[:160]}"
    if SMTP_HOST and SMTP_TO:
        try:
            import smtplib
            from email.message import EmailMessage
            m = EmailMessage()
            m["Subject"] = f"liftlab {res['gw']}: {'OK' if res['ok'] else 'CAMERA SILENT'}"
            m["From"] = os.environ.get("HEALTH_SMTP_FROM", f"liftlab@{os.uname().nodename}")
            m["To"] = SMTP_TO
            m.set_content(text)
            port = int(os.environ.get("HEALTH_SMTP_PORT", "587"))
            with smtplib.SMTP(SMTP_HOST, port, timeout=30) as s:
                if os.environ.get("HEALTH_SMTP_STARTTLS", "1") == "1":
                    s.starttls()
                user = os.environ.get("HEALTH_SMTP_USER", "")
                if user:
                    s.login(user, os.environ.get("HEALTH_SMTP_PASS", ""))
                s.send_message(m)
            return f"smtp {SMTP_HOST} -> {SMTP_TO}", None
        except Exception as e:
            return None, f"smtp failed: {type(e).__name__}: {str(e)[:160]}"
    # NO CHANNEL. Not an error, and not silence either: it is recorded as a fact so the dashboard
    # banner and the journal can both say that nothing was pushed anywhere.
    return None, ("no push channel configured (set HEALTH_WEBHOOK_URL or HEALTH_SMTP_HOST/TO) — "
                  "this line exists only on the dashboard, in the journal, and in health_status")


def record(db, res, channel, err, sent=False):
    db.execute("INSERT INTO health_status (gateway_id, ts, ok, n_cams, n_bad, line, detail, "
               "sent, delivered, delivery_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (res["gw"], res["ts"], int(res["ok"]), res["n_cams"], len(res["bad"]),
                res["line"], json.dumps(res["detail"]), int(bool(sent)),
                channel or "", err or ""))
    db.commit()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    force = "--daily" in argv or "--force" in argv
    args = [a for a in argv if not a.startswith("-")]
    gw = args[0] if args else os.environ.get("GATEWAY_ID", "site-A")

    db = _db()
    try:
        res = evaluate(db, gw)
        send, why = should_send(db, res)
        if force:
            send, why = True, (why if why in ("breach", "recovered", "first run") else "forced")
        channel, err = (deliver(res) if send else (None, None))
        record(db, res, channel, err if send else None, sent=send)
    finally:
        db.close()

    if as_json:
        print(json.dumps({**res, "sent": bool(send), "why": why,
                          "channel": channel, "delivery_error": err}, indent=1))
    else:
        print(res["line"])
        print(f"  cameras from {res['cam_source']}; sent={bool(send)} ({why})"
              + (f"; channel={channel}" if channel else "")
              + (f"; DELIVERY: {err}" if err else ""))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
