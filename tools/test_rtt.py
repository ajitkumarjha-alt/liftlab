#!/usr/bin/env python3
"""RTT: the definition, the fragmentation bias, and the absences that must name themselves.

RTT is the figure the whole MEP-02 sheet resolves to, so the things that can go wrong with it are
worth stating: a G READ is not a G STOP, dwell=0 lets chatter SPLIT a trip, and a camera with no
floor attribution has no RTT at all rather than an RTT of zero.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rtt_core


def R(ts, state, floor=None):
    return {"ts": float(ts), "door_state": state, "floor": floor}


def trip_seq(t0, stops, gap=60.0, home="G"):
    """closed at home -> `stops` intermediate stops -> open at home."""
    rows = [R(t0 - 6, "open", home), R(t0 - 3, "closing", home), R(t0, "closed", home)]
    t = t0
    for k in range(stops):
        t += gap
        rows += [R(t, "open", str(10 + k)), R(t + 8, "closing"), R(t + 10, "closed")]
    t += gap
    rows += [R(t, "open", home)]
    return rows, t - t0


def main():
    fails = []

    print("=== 1. the definition: closed at home -> next open at home ===")
    rows, want = trip_seq(1000.0, 3)
    tr = rtt_core.trips(rows, "G")
    print(f"  trips={len(tr)}  rtt={tr[0][2]:.0f}s (expected {want:.0f})  stops={tr[0][3]}")
    if len(tr) != 1 or abs(tr[0][2] - want) > 0.01:
        fails.append(f"trip not measured correctly: {tr}")
    if tr[0][3] != 3:
        fails.append(f"stops counted {tr[0][3]}, expected 3")

    print("\n=== 2. a G READ during travel is not a G STOP ===")
    # the car passes the ground floor: the panel reads G, the doors never open
    # A REAL pass-through: the panel reads G while door_state stays 'closed' throughout. My first
    # fixture put the G read straight after an 'open', which manufactures a closing transition that
    # a passing car cannot produce — it modelled a pass-through as something that cannot happen, and
    # then the code "failed" for behaving correctly.
    rows2, _ = trip_seq(2000.0, 2)
    rows2.insert(6, R(2075.0, "closed", "G"))          # doors shut before and after: a pass
    tr2 = rtt_core.trips(rows2, "G")
    print(f"  trips={len(tr2)} (a pass-through must not end the trip early)")
    if len(tr2) != 1:
        fails.append(f"a G pass-through split the trip: {len(tr2)} trips — every RTT containing one "
                     "would be shortened")

    print("\n=== 3. THE FRAGMENTATION BIAS: chatter at home splits one trip into two ===")
    rows3, whole = trip_seq(3000.0, 4)
    # a sub-second open/closed pair at G partway through — exactly what dwell=0 admits
    mid = 3000.0 + 2 * 60.0 + 20.0
    rows3 += [R(mid, "open", "G"), R(mid + 0.3, "closing", "G"), R(mid + 0.5, "closed", "G")]
    rows3.sort(key=lambda r: r["ts"])
    tr3 = rtt_core.trips(rows3, "G")
    print(f"  one real {whole:.0f}s trip becomes {len(tr3)} trips: "
          + ", ".join(f"{t[2]:.0f}s/{t[3]}stops" for t in tr3))
    if len(tr3) < 2:
        fails.append("fixture error: the chatter did not fragment the trip")
    else:
        if max(t[2] for t in tr3) >= whole:
            fails.append("fragmentation did not shorten the trips — the bias claim is wrong")
        print("  -> every fragment is SHORTER than the real trip, so the median is biased LOW.")
        print("     That is why the caveat states a direction and stops-per-trip is reported.")
    if "biased LOW" not in rtt_core.RTT_CAVEAT:
        fails.append("the caveat does not state the direction of the bias")

    print("\n=== 4. the plausibility band reports, never drops ===")
    S = rtt_core.summarise(rows3 + trip_seq(9000.0, 1, gap=5.0)[0], "G")
    print(f"  trips={S['n_trips']} plausible={S['n_plausible']} anomalies={S['n_anomalies']} "
          f"rate={S['anomaly_rate']}%")
    if S["n_trips"] != S["n_plausible"] + S["n_anomalies"]:
        fails.append("trips are being dropped rather than bucketed")
    if not S["anomalies"]:
        fails.append("no anomaly reason recorded for an out-of-band trip")
    else:
        for why in S["anomalies"]:
            print(f"    {why}")
            if "likely" not in why:
                fails.append(f"anomaly reason does not offer a cause: {why!r}")

    print("\n=== 5. stops-per-trip is a distribution, not just a median ===")
    print(f"  {S['stops']}")
    if "hist" not in S["stops"]:
        fails.append("no stops-per-trip histogram — a reader cannot judge fragmentation from a "
                     "median alone")

    print("\n=== 6. the travel gap is on the payload, not in a release note ===")
    for needle in ("LEG 2 has never run", "close_travel_s=NULL"):
        if needle not in S["travel_gap"]:
            fails.append(f"travel_gap does not say {needle!r}")
    print(f"  {S['travel_gap'][:96]}…")

    print("\n=== 7. dwell is 0 and the code says why ===")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "rtt_core.py")).read()
    if rtt_core.DWELL_S != 0.0:
        fails.append(f"DWELL_S is {rtt_core.DWELL_S}, not 0 — was dwell_validation re-run?")
    if "ZERO phantoms at every threshold" not in src:
        fails.append("the evidence for dwell=0 is not recorded where the constant lives")
    print("  DWELL_S=0.0, with the validation result recorded beside it")

    print("\n=== 8. ONE derivation: the dash imports it, never re-implements it ===")
    dash = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                             "dash_api.py")).read()
    if "import rtt_core" not in dash:
        fails.append("dash_api does not import rtt_core")
    for banned in ("def _rtt_trips", "st == \"closed\" and prev in (\"closing\", \"open\")"):
        if banned in dash and "rtt" in dash[max(0, dash.find(banned) - 400):dash.find(banned)]:
            fails.append(f"dash_api appears to re-implement the trip walk ({banned!r})")
    print("  dash_api imports rtt_core.summarise; no second copy of the walk")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — a G read is not a G stop, fragmentation is demonstrated and its direction stated, "
          "anomalies are bucketed with causes, and one derivation serves both surfaces")
    return 0


if __name__ == "__main__":
    sys.exit(main())
