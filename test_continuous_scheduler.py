#!/usr/bin/env python3
"""Local synthetic proof of the scheduler's emit logic — NO Pi, NO network.
Drives _Runner.step()/maybe_detect() with a hand-built door signal and asserts:
 (1) a clean cycle emits exactly ONCE,
 (2) a partial cycle at the trailing edge does NOT emit until settled,
 (3) sliding the window does NOT re-emit (Q3 double-count guard),
 (4) a cycle straddling a capture hole is rejected.
Runs against the REAL liftlab detect_cycles / StitchedTimeline.
"""
import sys
import numpy as np

sys.path.insert(0, ".")
import continuous_scheduler as cs

HZ = cs.SIGNAL_HZ
DT = 1.0 / HZ
CROP = (8, 8)  # tiny ROI crop; baseline zeros


def crop_for(openness):
    """A crop whose mean |crop-baseline| ~= openness*40 gray levels (well above
    MOTION_FLOOR at the plateau). baseline is zeros."""
    return np.full(CROP, openness * 40.0, dtype=np.float32)


def make_runner():
    r = cs._Runner(29, (0, 0, 8, 8), "ch29", cloud=None, gw_id="site-A", headers={})
    r.baseline = np.zeros(CROP, dtype=np.float32)
    r.closed_floor = 0.0
    return r


def door_profile(t):
    """openness in [0,1] for a cycle: closed, 2s open-ramp, 4s plateau, 2s close-ramp.
    Cycle spans t in [10, 18]s; closed elsewhere."""
    if t < 10 or t > 18:
        return 0.0
    if t < 12:      # open ramp 10->12
        return (t - 10) / 2.0
    if t < 16:      # plateau
        return 1.0
    if t < 18:      # close ramp 16->18
        return 1.0 - (t - 16) / 2.0
    return 0.0


def drive(r, t_start, t_end, ts0=1_000_000.0, profile=door_profile, hole=None):
    """Feed samples [t_start, t_end); optionally skip a [hole) window to simulate a
    capture gap. Returns all newly-emitted rows across the drive."""
    emitted = []
    t = t_start
    while t < t_end:
        if hole and hole[0] <= t < hole[1]:
            t += DT; continue
        ts = ts0 + t
        r.step(crop_for(profile(t)), ts)
        rows = r.maybe_detect(r.offs[-1])
        emitted += rows
        t += DT
    return emitted


def test_clean_emits_once():
    r = make_runner()
    em = drive(r, 0, 30)          # full cycle + settle tail
    assert len(em) == 1, f"expected 1 emit, got {len(em)}"
    assert len(r.cycles) == 1
    c = r.cycles[0]
    # synthetic cycle: 2s open ramp, 4s plateau, 2s close ramp -> dwell ~8s
    assert 7.0 < c["dwell_s"] < 9.0, c
    assert 1.5 < c["open_travel_s"] < 2.5 and 1.5 < c["close_travel_s"] < 2.5, c
    print(f"  (1) clean cycle emits once: dwell={c['dwell_s']}s open_travel={c['open_travel_s']}s "
          f"close_travel={c['close_travel_s']}s OK")


def test_partial_holds_then_no_reemit():
    r = make_runner()
    # stop feeding right at close_full (18.0s) — no settle tail yet
    em_partial = drive(r, 0, 18.2)
    assert len(em_partial) == 0, f"partial should NOT emit yet, got {len(em_partial)}"
    # now feed the settle tail; should emit exactly once
    em_settled = drive(r, 18.2, 24, )
    assert len(em_settled) == 1, f"expected 1 emit after settle, got {len(em_settled)}"
    # keep sliding the window well past — must NOT re-emit
    em_more = drive(r, 24, 60)
    assert len(em_more) == 0, f"re-emit detected: {len(em_more)} extra (Q3 FAIL)"
    print("  (2)+(3) partial holds, emits once on settle, no re-emit across slides OK")


def test_hole_straddle_rejected():
    r = make_runner()
    # punch a capture hole through the plateau (13.0->15.0s): cycle straddles it
    em = drive(r, 0, 30, hole=(13.0, 15.0))
    assert len(em) == 0, f"hole-straddling cycle must be rejected, got {len(em)} emits"
    print("  (4) hole-straddling cycle rejected OK")


def test_rollups():
    r = make_runner()
    # two cycles: [10-18] and [40-48]
    def two(t):
        return door_profile(t) or door_profile(t - 30)
    drive(r, 0, 60, profile=two)
    ru = r.rollups()
    assert ru["stop_count"] == 2, ru
    assert ru["headway_median_s"] is not None
    print(f"  (5) rollups: stops={ru['stop_count']} headway_med={ru['headway_median_s']}s "
          f"dwell_med={ru['dwell_median_s']}s hourly={list(ru['hourly_profile'].values())} OK")


def test_params_nesting():
    # cloud enqueues params NESTED under job["params"]; stop/confirm must be read
    # from there, not collapsed to a default 'start'. With nothing registered, a
    # correctly-read stop/confirm returns not_running (and never spawns a runner).
    res = cs.run_watch({"type": "watch_channel", "params": {"channel": 97, "action": "stop"}})
    assert res == {"status": "not_running", "channel": 97}, res
    res = cs.run_watch({"type": "watch_channel", "params": {"channel": 96, "action": "confirm"}})
    assert res["status"] == "not_running" and res["channel"] == 96, res
    print("  (6) nested job params read correctly (stop/confirm NOT collapsed to start) OK")


if __name__ == "__main__":
    print("synthetic scheduler emit-logic proof:")
    test_clean_emits_once()
    test_partial_holds_then_no_reemit()
    test_hole_straddle_rejected()
    test_rollups()
    test_params_nesting()
    print("ALL PASS")
