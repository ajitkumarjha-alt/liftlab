#!/usr/bin/env python3
"""Every rtt.state renders. A state the page has never seen must degrade to a panel, not a throw.

THE DEFECT THIS LOCKS DOWN, reported live on 2026-08-19. /trends returned 200 in 0.283s and the
page showed "TRENDS REQUEST FAILED — Cannot read properties of undefined (reading 'map')" on ch29.
Nothing was wrong with the request, the cache, or the payload: the cached rtt object was BYTE
IDENTICAL to a fresh derivation. The renderer guarded the RTT card with a whitelist of known-bad
states —

    if(!R||R.state==='no_floor'||R.state==='no_era'||R.state==='no_rows'){ ...absent panel... }
    var vals=R.by_hour.map(...)          // <-- anything else lands HERE

— and the server has a fourth state. The row cap returns state='too_many_rows' with no by_hour, so
ch27 and ch29 (both over the 120,000-row cap) fell through and threw, killing the WHOLE trends view
rather than one card. It was invisible until precompute landed because before that the endpoint
timed out at 37s and the page never rendered far enough to reach the card.

Three properties, and the third is the one that makes this a class fix rather than a patch:

  1. EVERY state renders — including a state string this build has never heard of. A whitelist of
     bad states drifts out of date the moment the server gains one; asking "do I have the array I
     am about to walk" cannot.
  2. THE REASON MATCHES THE FAULT. too_many_rows is a range too large to walk, NOT a lift without a
     floor read, and must not inherit the home-floor sentence — the same misdescription the health
     line was corrected for.
  3. A RENDER FAILURE IS NOT A REQUEST FAILURE. renderTrends() ran inside loadTrends's promise
     chain, so a TypeError while drawing was reported as a failed request on a 200. The two faults
     now carry different labels, and retry is offered only for the one retrying can fix.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOME_FLOOR_SENTENCE = "RTT is defined by the home floor"

# The states the server can put on rtt, plus one it cannot — because the point is that an UNKNOWN
# state renders too. 'no_rows'/'no_era'/'no_floor' carry no by_hour; 'ok' does.
STATES = [
    ("ok", True, False),
    ("no_floor", False, True),
    ("no_era", False, True),
    ("no_rows", False, True),
    ("too_many_rows", False, False),      # the one that threw
    ("a_state_from_a_future_build", False, False),
]


def _rtt(state, with_by_hour):
    r = {"state": state, "era": "260d4a0fh3-stateT5471cb+495e8f48", "n_rows": 377854,
         "note": f"synthetic note for {state}"}
    if with_by_hour:
        r.update({
            "by_hour": [{"hour": h, "median": 60 + h} for h in range(24)],
            "home": "G", "all_day": {"n": 120}, "anomaly_rate": 3.1, "n_anomalies": 4,
            "n_trips": 130, "n_plausible": 126, "stops": {"median": 2},
            "caveat": "synthetic", "travel_gap": "", "dwell_s": {"median": 8},
            "floor_attributed_pct": 91.2, "anomalies": [], "windows": {},
        })
    return r


def _render(*a, **kw):
    """render() raises SystemExit(1) when node throws. Catch it: a throw is the RESULT under test
    here, not a reason to abandon the remaining assertions."""
    from test_dash_render_audit import render
    try:
        return render(*a, **kw)
    except SystemExit:
        return None


def main():
    node = shutil.which("node")
    if not node:
        print("  SKIPPED — no node; nothing below was asserted.")
        return 0
    from test_dash_occupancy import _stub
    _stub()
    from test_dash_render_audit import build_db, _precompute_trends

    fails = []
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "gw.db")
    build_db(db_path)
    os.environ["GATEWAY_DB"] = db_path
    import dash_api as D

    db = D._db()
    data = D._dash_data_inner(db, "site-A").payload
    _precompute_trends(D, ["ch29"])
    base = D.dash_trends("site-A", cam="ch29", period="all").payload
    db.close()

    print("=== 1. every rtt.state renders — no state may take the view down ===")
    for state, with_bh, floorish in STATES:
        tr = {**base, "rtt": _rtt(state, with_bh), "rtt_state": "ok"}
        out = _render(tmp, D, data, tr, select="ch29", view="trends")
        if out is None:
            fails.append(f"rtt.state={state!r} THREW — the whole trends view is dead")
            print(f"  {state:28s} THREW  <-- the reported defect")
            continue
        html = out["trend"]
        drew_chart = "round trip time / hour-of-day (s) — median" in html
        absent = "RTT UNAVAILABLE" in html
        print(f"  {state:28s} rendered {len(html):>7,} chars  "
              f"{'chart' if drew_chart else ('absent-panel' if absent else 'NEITHER')}")
        if not (drew_chart or absent):
            fails.append(f"rtt.state={state!r} rendered neither a chart nor the absence panel")
        if with_bh and not drew_chart:
            fails.append(f"rtt.state={state!r} has by_hour but drew no chart")
        if not with_bh and not absent:
            fails.append(f"rtt.state={state!r} has no by_hour but drew no absence panel")

        # 2. THE REASON MUST MATCH THE FAULT.
        if absent:
            has_floor_sentence = HOME_FLOOR_SENTENCE in html
            if floorish and not has_floor_sentence:
                fails.append(f"{state!r} is a floor fault but dropped the home-floor explanation")
            if not floorish and has_floor_sentence:
                fails.append(f"{state!r} is NOT a floor fault but inherited the home-floor "
                             f"sentence — the panel misdescribes the fault")

    print("\n=== 2. too_many_rows explains the CAP, and does not blame the floor ===")
    tr = {**base, "rtt": _rtt("too_many_rows", False)}
    out = _render(tmp, D, data, tr, select="ch29", view="trends")
    txt = "" if out is None else re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", out["trend"]))
    if out is None:
        fails.append("too_many_rows THREW — cannot assert its wording")
    cap_named = "cap on what the request path will walk" in txt
    not_zero = "NOT a lift that made no journeys" in txt
    print(f"  names the cap: {cap_named} · says it is not a lift with no journeys: {not_zero}")
    print(f"  blames the floor: {HOME_FLOOR_SENTENCE in txt}  (must be False)")
    if not (cap_named and not_zero):
        fails.append("too_many_rows panel does not explain the row cap")
    if HOME_FLOOR_SENTENCE in txt:
        fails.append("too_many_rows panel blames the home floor")

    print("\n=== 3. a render fault is labelled a RENDER fault, not a failed request ===")
    out = _render(tmp, D, data, {"cam": "ch29", "render_error": "synthetic draw failure"},
                  select="ch29", view="trends")
    h = "" if out is None else out["trend"]
    print(f"  says RENDER FAILED: {'TRENDS RENDER FAILED' in h} · "
          f"says REQUEST FAILED: {'TRENDS REQUEST FAILED' in h} (must be False) · "
          f"offers retry: {'>retry<' in h} (must be False)")
    if "TRENDS RENDER FAILED" not in h:
        fails.append("a render fault is not labelled as one")
    if "TRENDS REQUEST FAILED" in h:
        fails.append("a render fault is still reported as a failed REQUEST")
    if ">retry<" in h:
        fails.append("retry offered for a deterministic render fault")

    out = _render(tmp, D, data, {"cam": "ch29", "fetch_error": "synthetic network failure"},
                  select="ch29", view="trends")
    h = "" if out is None else out["trend"]
    print(f"  a real fetch failure still says REQUEST FAILED: {'TRENDS REQUEST FAILED' in h} "
          f"· offers retry: {'>retry<' in h}")
    if "TRENDS REQUEST FAILED" not in h or ">retry<" not in h:
        fails.append("a genuine fetch failure lost its label or its retry button")

    print("\n=== 4. a structurally incomplete payload degrades to a sentence ===")
    out = _render(tmp, D, data, {k: v for k, v in base.items() if k != "profile"},
                  select="ch29", view="trends")
    h = "" if out is None else out["trend"]
    print(f"  says PAYLOAD INCOMPLETE: {'TRENDS PAYLOAD INCOMPLETE' in h}")
    if "TRENDS PAYLOAD INCOMPLETE" not in h:
        fails.append("a payload missing 'profile' did not render the incomplete-payload panel")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK — every rtt.state renders, each absence names its own cause, a render fault is not\n"
          "     reported as a request fault, and an incomplete payload degrades to one panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
