#!/usr/bin/env python3
"""Why does closing exceed opening on three cameras at once? Walk the stream and count.

The dash funnel's PAIRING SUSPECT fires on began_closing > opened — but a LEGITIMATE cycle with an
obstruction-reopen (closing -> open -> closing) has one opening and two closings by design, and the
flag never subtracts reopens. Cross-camera consistency (ch16/ch27/ch29 all ~1.5-1.7x) fits either a
shared miscalibrated flag OR shared threshold flapping in DoorTracker. This census discriminates,
read-only, per camera and era:

  1. transition counts: opened / confirmed / began_closing / reopened / completed / aborted
     -> VERDICT: excess ~= reopens  => flag miscalibrated (fix the dash condition, tracker fine)
                excess >> reopens   => spurious closing entries (threshold flapping, tune tracker)
  2. oscillation chains: maximal open<->closing alternation runs — length histogram + intra-chain
     gap seconds (sub-second alternation = hysteresis flap; multi-second = real reopens)
  3. close_travel by cycle type: clean cycles (no reopen inside) vs reopened cycles — medians/p85.
     If reopened cycles carry the long tail, the quotable compliance number is the CLEAN median,
     and the 59%-exceed headline is an artifact of pooling flapped cycles.

    sudo python3 door_pairing_census.py            # ch16 ch27 ch29
    sudo python3 door_pairing_census.py ch29       # one camera
"""
import os
import sqlite3
import sys

DB = os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db")
GW = os.environ.get("GW", "site-A")
GUARD_TS = float(os.environ.get("DASH_DOOR_GUARD_TS_EPOCH", "0") or 0)   # optional post-guard cut


def pctl(v, q):
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 2)


def era_for(db, cam):
    r = db.execute("SELECT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? "
                   "AND door_version IS NOT NULL AND door_version<>'' AND ts IS NOT NULL "
                   "ORDER BY ts DESC LIMIT 1", (GW, cam)).fetchone()
    return r[0].split("+")[0] if r else None


def census(db, cam):
    era = era_for(db, cam)
    if not era:
        print(f"\n== {cam}: no door rows in any era")
        return
    rows = db.execute(
        "SELECT ts, door_state, close_travel_s FROM gw_door_event WHERE gateway_id=? AND cam=? "
        "AND door_version LIKE ? AND door_state IS NOT NULL AND ts>=? ORDER BY ts, id",
        (GW, cam, era + "%", GUARD_TS)).fetchall()
    # collapse heartbeat re-emits of the same state, keeping first ts + any close_travel seen
    seq = []
    for ts, st, ct in rows:
        if not seq or seq[-1][1] != st:
            seq.append([ts, st, ct])
        elif ct is not None and seq[-1][2] is None:
            seq[-1][2] = ct
    trans = {}
    for (t1, a, _), (t2, b, _) in zip(seq, seq[1:]):
        trans[f"{a}->{b}"] = trans.get(f"{a}->{b}", 0) + 1
    opened = trans.get("closed->opening", 0)
    confirmed = trans.get("opening->open", 0)
    began = trans.get("open->closing", 0)
    reopened = trans.get("closing->open", 0)
    completed = trans.get("closing->closed", 0)
    aborted = trans.get("opening->closed", 0)
    excess = began - opened
    print(f"\n== {cam} era {era} rows {len(rows)} runs {len(seq)}")
    print(f"   opened {opened}  confirmed {confirmed}  began_closing {began}  "
          f"reopened {reopened}  completed {completed}  aborted_opening {aborted}")
    print(f"   closing excess over opening: {excess}  vs reopens: {reopened}", end="")
    if reopened and excess > 0:
        share = reopened / excess
        verdict = ("~= FLAG MISCALIBRATED (reopens account for the excess; tracker fine)"
                   if 0.8 <= share <= 1.25 else
                   "reopens PARTIAL — mixed: some flap on top of real reopens" if share >= 0.4 else
                   "reopens SMALL — spurious closings dominate: threshold flapping, tune tracker")
        print(f"  ({share:.0%} of excess)  -> {verdict}")
    else:
        print(f"  -> {'no excess' if excess <= 0 else 'NO reopens: pure spurious closings'}")

    # oscillation chains: consecutive open<->closing alternations
    chains, gaps = [], []
    i = 0
    while i < len(seq) - 1:
        if seq[i][1] == "open" and seq[i + 1][1] == "closing":
            j = i + 1
            n = 1
            while j + 1 < len(seq) and ((seq[j][1] == "closing" and seq[j + 1][1] == "open")
                                        or (seq[j][1] == "open" and seq[j + 1][1] == "closing")):
                gaps.append(seq[j + 1][0] - seq[j][0])
                n += 1
                j += 1
            if n >= 3:
                chains.append(n)
            i = j
        else:
            i += 1
    print(f"   open<->closing chains (len>=3): {len(chains)}  longest {max(chains) if chains else 0}"
          f"  intra-chain gap p50 {pctl(gaps, .5)}s p85 {pctl(gaps, .85)}s"
          f"  ({sum(1 for g in gaps if g < 1.0)}/{len(gaps)} gaps sub-second)" if gaps else
          "   open<->closing chains: none")

    # close_travel split, FLAP-AWARE (2026-07-29: the reopen-split alone let flap-born cycles
    # poison CLEAN — ch29 "clean" median 0.08s): FLAP = sub-1s closing->open bounce in-window OR
    # open dwell < 1.5s before the closing (never really open); REOPENED = real (>=1s) reopen;
    # CLEAN = the quotable pool.
    FLAP_GAP, MIN_DWELL = 1.0, 1.5
    clean, dirty, flap = [], [], []
    open_since = None
    flap_w = reopen_w = False
    for k, (t, st, ct) in enumerate(seq):
        prev = seq[k - 1][1] if k else None
        if st == "opening" and prev == "closed":
            flap_w = reopen_w = False
            open_since = None
        elif st == "open":
            open_since = t
            if prev == "closing":
                if (t - seq[k - 1][0]) < FLAP_GAP:
                    flap_w = True
                else:
                    reopen_w = True
        elif st == "closing" and prev == "open":
            if open_since is not None and (t - open_since) < MIN_DWELL:
                flap_w = True
        elif st == "closed" and prev == "closing":
            v = seq[k - 1][2] if seq[k - 1][2] is not None else ct
            if v is not None and v > 0:
                (flap if flap_w else dirty if reopen_w else clean).append(float(v))
            flap_w = reopen_w = False
    print(f"   close_travel CLEAN: n {len(clean)} median {pctl(clean, .5)}s p85 {pctl(clean, .85)}s")
    print(f"   close_travel REOPENED (real, >=1s): n {len(dirty)} median {pctl(dirty, .5)}s p85 {pctl(dirty, .85)}s")
    print(f"   close_travel FLAP-EXCLUDED: n {len(flap)} median {pctl(flap, .5)}s")
    if clean:
        over = sum(1 for v in clean if v > 2.31)
        print(f"   CLEAN pct>2.31s: {round(100.0 * over / len(clean))}%  <- the quotable compliance shape")


cams = sys.argv[1:] or ["ch16", "ch27", "ch29"]
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
print(f"door pairing census — {DB} gw {GW}" + (f" post-guard>{GUARD_TS}" if GUARD_TS else ""))
for cam in cams:
    census(db, cam)
