#!/usr/bin/env python3
"""ROUND TRIP TIME derivation — ONE implementation, used by tools/rtt.py and by the dash.

A number computed in two places is a number that can disagree with itself, and RTT is the figure the
whole MEP-02 sheet resolves to. So the derivation lives here and both callers import it.

THE CLAIM RULE IS THE PANEL'S OWN: a cycle is a transition INTO 'closed' from 'closing' or 'open' —
dash_api._h3_cycle_ts, verbatim — so cycles and RTT cannot disagree about what a cycle is.

DWELL = 0, ON EVIDENCE. tools/dwell_validation.py graded 0/1/2/3s against the frame-anchored hand
truth: ZERO phantoms at every threshold (no claimed cycle falls inside a verified door-CLOSED
window, so the truth offers no evidence any is false) and every threshold above 0 destroyed REAL
closes. Do not change DWELL_S without re-running that validation.

AND THE DIRECTION OF THE RESIDUAL BIAS IS KNOWN. With no dwell filter, sub-second G chatter can
SPLIT one real round trip into two — a spurious closed/open pair at G ends a trip early and starts
the next — so the median is biased LOW, not high. That is why the caveat says "measured, under dwell
validation" rather than "approximate": the number is a real measurement whose error has a known
sign, and STOPS_PER_TRIP is reported beside it because a fragmented trip shows up there first.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DWELL_S = 0.0
RTT_MIN_S, RTT_MAX_S = 30.0, 600.0
PEAK_WINDOWS = {"AM peak": (8, 10), "PM peak": (18, 20)}
SHEET_OPEN_S, SHEET_CLOSE_S = 3.00, 2.00

RTT_CAVEAT = ("measured, under dwell validation — no minimum dwell is applied (every threshold "
              "tested destroyed real closes), so sub-second chatter at the home floor can fragment "
              "a round trip and the median is biased LOW")
TRAVEL_GAP = ("travel term absent — LEG 2 has never run, h3 emits close_travel_s=NULL by design, "
              "and the h2 travel figures were invalidated 2026-08-05, so RTT cannot be checked "
              "against the sum of its parts")


def cycles_with_floor(rows):
    """[(ts_closed, floor)] — the floor the car was AT while its doors were open."""
    out, prev, cur = [], None, None
    for r in rows:
        st, fl = r["door_state"], r["floor"]
        if fl is not None:
            cur = fl
        if st == "closed" and prev in ("closing", "open"):
            out.append((r["ts"], cur))
            cur = None
        if st != prev:
            prev = st
    return out


def opens_with_floor(rows):
    """[(ts_open, floor)] for transitions INTO 'open'."""
    out, prev, cur = [], None, None
    for r in rows:
        st, fl = r["door_state"], r["floor"]
        if fl is not None:
            cur = fl
        if st == "open" and prev != "open":
            out.append((r["ts"], cur))
        if st != prev:
            prev = st
    return out


def trips(rows, home="G"):
    """[(t_start, t_end, seconds, n_stops)] — door CLOSED at home to the next door OPEN at home.

    BOTH ENDS MUST BE STOPS AT HOME. A car passing the ground floor reads 'G' on the panel without
    its doors opening; counting that as an endpoint would shorten every trip containing one.

    n_stops is the cycle count strictly inside the trip. A round trip on a 70-floor tower that
    reports 2 stops is either a very short journey or a FRAGMENT of a longer one, which is why this
    is carried per trip rather than averaged away.
    """
    closes = cycles_with_floor(rows)
    opens = opens_with_floor(rows)
    cyc = [t for t, _f in closes]
    out, oi = [], 0
    for t0, f0 in closes:
        if f0 != home:
            continue
        while oi < len(opens) and opens[oi][0] <= t0:
            oi += 1
        for j in range(oi, len(opens)):
            t1, f1 = opens[j]
            if f1 == home:
                n_stops = sum(1 for c in cyc if t0 < c <= t1)
                out.append((t0, t1, t1 - t0, n_stops))
                break
    return out


def classify(dt):
    if dt < RTT_MIN_S:
        return "short (<30s) — likely a phantom G, or the doors reopened at G"
    if dt > RTT_MAX_S:
        return "long (>600s) — likely a missed G read, or the car was parked"
    return None


def summarise(rows, home="G"):
    """Everything a surface needs: plausible trips, anomalies, per-hour, windows, stops-per-trip."""
    tr = trips(rows, home)
    good, anom = [], {}
    per_hour = {h: {"rtt": [], "anom": 0} for h in range(24)}
    for ts, _te, dt, ns in tr:
        why = classify(dt)
        h = datetime.fromtimestamp(ts, IST).hour
        if why:
            anom[why] = anom.get(why, 0) + 1
            per_hour[h]["anom"] += 1
        else:
            good.append({"ts": ts, "rtt_s": dt, "stops": ns})
            per_hour[h]["rtt"].append(dt)

    def st(v):
        v = sorted(v)
        if not v:
            return {"n": 0, "median": None, "p85": None}
        return {"n": len(v), "median": round(statistics.median(v), 1),
                "p85": round(v[min(len(v) - 1, int(0.85 * len(v)))], 1)}

    stops = [g["stops"] for g in good]
    stops_hist = {}
    for s_ in stops:
        k = str(s_) if s_ <= 9 else "10+"
        stops_hist[k] = stops_hist.get(k, 0) + 1
    n_all = len(tr)
    return {
        "n_trips": n_all, "n_plausible": len(good), "n_anomalies": n_all - len(good),
        "anomaly_rate": (round(100.0 * (n_all - len(good)) / n_all, 1) if n_all else None),
        "anomalies": anom,
        "all_day": st([g["rtt_s"] for g in good]),
        "windows": {k: st([g["rtt_s"] for g in good
                           if lo <= datetime.fromtimestamp(g["ts"], IST).hour < hi])
                    for k, (lo, hi) in PEAK_WINDOWS.items()},
        "by_hour": [{"hour": h, **st(per_hour[h]["rtt"]), "anom": per_hour[h]["anom"]}
                    for h in range(24)],
        "stops": {**st([float(s_) for s_ in stops]),
                  "hist": dict(sorted(stops_hist.items(),
                                      key=lambda kv: (len(kv[0]), kv[0])))},
        "caveat": RTT_CAVEAT, "travel_gap": TRAVEL_GAP, "dwell_s": DWELL_S,
        "home": home,
    }


# ── ERA RESOLUTION — the other half of "one derivation" ──────────────────────────────────────
# Sharing the WALK was not enough. The CLI scoped on the FULL door_version with `=`; the dash used
# _era_for, which returns everything before the '+' (templates hash + engine tag + levels tag, but
# NOT the geometry signature) — a PREFIX, which every other dash consumer matches with LIKE. I wrote
# `= prefix`, which matches nothing, so ch29 reported no rows while the CLI found 1569 trips on the
# same database. Two callers, one walk, and they still disagreed, because era selection is part of
# the derivation and I had left it outside.
#
# So both callers now resolve the era HERE and match it the same way.

def newest_era(version_rows):
    """The full door_version to scope by: newest by EVENT TIME, complete string.

    `version_rows` is [(door_version, last_ts)]. Returns None when there are none.

    FULL, not a prefix. The prefix is the templates hash and the engine tag follows it, so
    '260d4a0f%' matched three eras on ch29 — untagged, h2, and h2 with recalibrated levels — and
    would pool h3 in too. An era that pools instruments is not an era.
    """
    best, best_ts = None, None
    for dv, ts in version_rows:
        if not dv:
            continue
        if best_ts is None or (ts is not None and ts > best_ts):
            best, best_ts = dv, ts
    return best


def expand_era(version_rows, spec):
    """A user-supplied era (possibly a prefix) -> (full_version, error).

    A prefix that matches more than one full version is REFUSED rather than silently resolved: those
    are different instruments and picking one for the operator would be a guess wearing a number.
    """
    if not spec:
        return newest_era(version_rows), None
    matches = [dv for dv, _ts in version_rows if dv and dv.startswith(spec)]
    if len(matches) > 1:
        return None, ("era %r matches %d different door_versions (%s) — the prefix is the templates "
                      "hash and the engine tag follows it, so this spans instruments; pass one in "
                      "full" % (spec, len(matches), ", ".join(sorted(matches)[:3])))
    if not matches:
        return None, "era %r matches no door_version for this camera" % (spec,)
    return matches[0], None
