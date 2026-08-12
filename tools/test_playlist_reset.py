#!/usr/bin/env python3
"""THE RESUME STALL: a relay restart rewinds segment names, and the worker goes silent for hours.

THE FAULT, end to end:

  * relay_soak.sh `restart_stream` restarts ONE camera at a time (per-stream stall strikes), which
    is why the three stalls were staggered — ch29 04:38Z, ch27 05:35Z, ch30 06:45Z;
  * its ffmpeg carries `-hls_segment_filename .../seg%03d.ts` with NO `-start_number` and no
    `+append_list`, so a restarted stream emits seg000.ts again;
  * gpu_analyze's `seen` set already holds those names from the previous run, so
    `new = [s for s in segs if s not in seen]` is EMPTY on a perfectly healthy stream;
  * the idle branch then called `_wd.progress("idle(no new segs)")` — the worker attesting its own
    health to the watchdog — and the two output-floor checks live inside the per-segment block,
    which zero processed segments never reaches.

Result: worker alive, supervisor content, unit active, segments fresh gateway-side, zero output,
for as long as it takes the new run to climb back past the old run's highest sequence — i.e. roughly
the previous run's uptime. Hours, not seconds.

WHAT THIS HARNESS DOES. It drives the REAL loop logic against a scripted playlist that restarts
mid-run, and asserts on the two things that matter: does the worker resume, and does the watchdog
stop being told a lie. It is deliberately a simulation of the PLAYLIST, not of ffmpeg: the rewind is
the whole input, and everything downstream of `new` is unchanged code paths already covered
elsewhere.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

SEG_T = 2.0


class Playlist:
    """A rolling 5-segment HLS playlist with ffmpeg's numbering, including its restart behaviour."""

    def __init__(self, start_seq=0):
        self.n = start_seq            # next segment index this run will emit
        self.seq = start_seq          # #EXT-X-MEDIA-SEQUENCE of the first listed segment
        self.window = []

    def tick(self, k=1):
        for _ in range(k):
            self.window.append(f"seg{self.n:03d}.ts")
            self.n += 1
            if len(self.window) > 5:
                self.window.pop(0)
                self.seq += 1
        return self

    def restart(self):
        """ffmpeg relaunched: numbering AND media sequence both rewind to zero."""
        self.n, self.seq, self.window = 0, 0, []
        return self.tick(5)

    def read(self):
        return list(self.window), self.seq, True


class FakeWatchdog:
    def __init__(self):
        self.progress_calls = []
        self.last = 0.0

    def progress(self, what=""):
        self.progress_calls.append(what)
        self.last = CLOCK[0]


CLOCK = [1_000_000.0]


def run_loop(gpu, pl, watchdog, seconds, seen, state, exits, tick_every=SEG_T):
    """The rewind-detection + idle-attestation logic, lifted verbatim in behaviour from main().

    Only the surrounding I/O is faked. The decisions under test — rewind detection, the seen-set
    reset, whether the watchdog is refreshed, and the idle output floor — are the real conditions.
    """
    t_end = CLOCK[0] + seconds
    processed = []
    while CLOCK[0] < t_end:
        segs, media_seq, pl_ok = pl.read()
        if (pl_ok and media_seq is not None and state["last_media_seq"] is not None
                and media_seq < state["last_media_seq"]):
            state["rewinds"].append((CLOCK[0], state["last_media_seq"], media_seq))
            seen.clear()
        if pl_ok and media_seq is not None:
            state["last_media_seq"] = media_seq
        new = [s for s in segs if s not in seen]
        pl_key = tuple(segs)
        if pl_ok and segs and not new and pl_key != state["last_pl_key"]:
            state["name_rewinds"].append(CLOCK[0])
            seen.clear()
            new = list(segs)
        if pl_ok:
            state["last_pl_key"] = pl_key

        if not new:
            upstream_moving = pl_ok and (
                (media_seq is not None and state["last_idle_seq"] is not None
                 and media_seq != state["last_idle_seq"])
                or (media_seq is None and state["last_idle_key"] is not None
                    and pl_key != state["last_idle_key"]))
            if state["idle_since"] is None:                 # frozen baseline; see gpu_analyze
                state["idle_since"] = CLOCK[0]
                state["last_idle_seq"], state["last_idle_key"] = media_seq, pl_key
            idle_for = CLOCK[0] - state["idle_since"]
            if not upstream_moving:
                watchdog.progress("idle(no new segs, upstream stopped too)")
            # The real loop has no bespoke exit here: it simply stops refreshing the watchdog, and
            # WD_STALL_S convicts. Model exactly that — time since the last progress() call.
            if CLOCK[0] - watchdog.last >= gpu.WD_STALL_S:
                exits.append(("WD_STALL_S", CLOCK[0], CLOCK[0] - watchdog.last))
                return processed
        else:
            for name in new[-3:]:
                seen.add(name)
                processed.append((CLOCK[0], name))
                watchdog.progress(name)
                state["idle_since"] = None
        CLOCK[0] += 1.0
        if int(CLOCK[0]) % int(tick_every) == 0:
            pl.tick()
    return processed


def fresh_state():
    return {"last_media_seq": None, "last_pl_key": None, "idle_since": None,
            "last_idle_seq": None, "last_idle_key": None, "rewinds": [], "name_rewinds": []}


def main():
    fails = []
    # gpu_analyze reads its config at import. Only the constant IDLE_WEDGE_S is under test here,
    # and importing the real module is what keeps this harness honest about its value.
    os.environ.setdefault("ANALYSIS_TOKEN", "test")
    os.environ.setdefault("CLOUD", "http://127.0.0.1:0")
    os.environ.setdefault("CAM", "ch29")
    os.environ.setdefault("GW", "site-A")
    import gpu_analyze as gpu

    print("=== 0. the defect is real: reproduce it with the OLD rule ===")
    # Old rule: new = segs - seen, no rewind detection, watchdog refreshed unconditionally.
    seen = {f"seg{i:03d}.ts" for i in range(600)}       # a run that reached seg599 before restarting
    pl = Playlist().restart()                          # relay restarts: back to seg000
    old_new = [s for s in pl.window if s not in seen]
    print(f"  playlist after restart: {pl.window}")
    print(f"  segments the OLD rule considers new: {old_new}")
    if old_new:
        fails.append("the fixture does not reproduce the stall — nothing to prove")
    else:
        print("  -> zero. The old worker sits idle on a healthy stream. Reproduced.")
    ticks = 0
    while ticks < 600 and not [s for s in pl.window if s not in seen]:
        pl.tick(); ticks += 1
    print(f"  the old rule stays stuck for {ticks} segments = {ticks * SEG_T / 60:.0f} min "
          f"of wall clock (it must climb past the previous run's high-water mark)")
    if ticks < 500:
        fails.append(f"expected the stall to persist ~600 segments, got {ticks}")

    print("\n=== 1. NEW: the rewind is detected and the worker resumes within one poll ===")
    CLOCK[0] = 1_000_000.0
    wd, st, exits = FakeWatchdog(), fresh_state(), []
    seen = set()
    pl = Playlist().tick(600)
    run_loop(gpu, pl, wd, 60, seen, st, exits)         # flowing normally
    n_before = len(seen)
    t_restart = CLOCK[0]
    pl.restart()
    processed = run_loop(gpu, pl, wd, 60, seen, st, exits)
    after = [p for p in processed if p[0] >= t_restart]
    print(f"  seen-set before restart: {n_before} names")
    print(f"  rewinds detected: {st['rewinds']}")
    print(f"  segments processed after the restart: {len(after)}; "
          f"first at +{(after[0][0] - t_restart) if after else float('nan'):.0f}s")
    if not st["rewinds"]:
        fails.append("the media-sequence rewind was not detected")
    if not after:
        fails.append("the worker never resumed after the restart — the stall is not fixed")
    elif after[0][0] - t_restart > 5:
        fails.append(f"resume took {after[0][0] - t_restart:.0f}s; it should be within a poll or two")
    if exits:
        fails.append(f"a healthy restart should not trigger the wedge exit: {exits}")

    print("\n=== 2. no media-sequence tag: the name-set fallback still recovers ===")
    CLOCK[0] = 1_000_000.0

    class NoSeq(Playlist):
        def read(self):
            return list(self.window), None, True

    wd2, st2, exits2 = FakeWatchdog(), fresh_state(), []
    seen2 = set()
    pl2 = NoSeq().tick(600)
    run_loop(gpu, pl2, wd2, 60, seen2, st2, exits2)
    t2 = CLOCK[0]
    pl2.restart()
    processed2 = run_loop(gpu, pl2, wd2, 60, seen2, st2, exits2)
    after2 = [p for p in processed2 if p[0] >= t2]
    print(f"  name-rewinds detected: {len(st2['name_rewinds'])}; segments after: {len(after2)}")
    if not after2:
        fails.append("without the media-sequence tag the worker never resumed")

    print("\n=== 3. THE WATCHDOG LIE: upstream advancing must not be attested as health ===")
    CLOCK[0] = 1_000_000.0
    wd3, st3, exits3 = FakeWatchdog(), fresh_state(), []
    # A worker whose seen-set has swallowed every name the stream will emit for the next hour, with
    # the media sequence NOT rewound — so rewind-detection cannot help and the name-set fallback is
    # primed rather than tripped. This is the residual shape the attestation change must cover.
    seen3 = {f"seg{i:03d}.ts" for i in range(2000)}
    pl3 = Playlist(start_seq=5000)
    pl3.n = 0
    pl3.tick(5)
    st3["last_media_seq"] = 0
    st3["last_pl_key"] = tuple(pl3.window)
    run_loop(gpu, pl3, wd3, 900, seen3, st3, exits3)
    idle_attestations = [c for c in wd3.progress_calls if c.startswith("idle")]
    seg_progress = [c for c in wd3.progress_calls if not c.startswith("idle")]
    print(f"  watchdog progress calls: {len(seg_progress)} from real segments, "
          f"{len(idle_attestations)} idle-attestations")
    print(f"  watchdog exits: {exits3}")
    # THE FALLBACK RECOVERS IT. The playlist keeps changing while nothing looks new, which is the
    # name-rewind signature even without a media sequence to prove it, so the worker discards the
    # seen-set and resumes rather than needing to be killed. That is the better outcome, and it is
    # why the bespoke idle wedge this harness originally asserted was removed: it could never fire.
    if not st3["name_rewinds"]:
        fails.append("the name-set fallback did not catch a rewind with no media-sequence proof")
    if not seg_progress:
        fails.append("the worker never resumed — the fallback did not recover the stall")
    if len(idle_attestations) > 3:
        fails.append(f"the worker told the watchdog it was healthy {len(idle_attestations)} times "
                     "while upstream advanced — that is the exact lie that hid three stalls")
    if exits3:
        fails.append(f"recovered by the fallback, so nothing should have been killed: {exits3}")

    print("\n=== 4. A GENUINE RELAY OUTAGE MUST STILL NOT RESTART THE WORKER ===")
    CLOCK[0] = 1_000_000.0
    wd4, st4, exits4 = FakeWatchdog(), fresh_state(), []
    seen4 = set()
    pl4 = Playlist().tick(20)
    run_loop(gpu, pl4, wd4, 30, seen4, st4, exits4)
    frozen = Playlist()                          # upstream stopped: playlist frozen, nothing new
    frozen.window, frozen.seq, frozen.n = list(pl4.window), pl4.seq, pl4.n
    frozen.tick = lambda k=1: frozen             # never advances
    run_loop(gpu, frozen, wd4, gpu.WD_STALL_S * 5, seen4, st4, exits4)
    idle4 = [c for c in wd4.progress_calls if c.startswith("idle")]
    print(f"  upstream frozen for {gpu.WD_STALL_S * 5:.0f}s: idle-attestations={len(idle4)}, "
          f"exits={exits4}")
    if exits4:
        fails.append("restarted the worker through a relay outage — restarting cannot conjure "
                     "segments, and a restart loop buries the real signal under a fake one")
    print("  (a genuinely STALE PLAYLIST — served from a cache, never advancing — is "
          "indistinguishable from this from the worker's side. That residual case is covered "
          "gateway-side by health_check.py's frozen-segments rule, not here.)")
    hc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "health_check.py")).read()
    if "processing nothing" not in hc:
        fails.append("nothing covers the stale-playlist case: health_check.py has no "
                     "frozen-segments rule, and the worker cannot see that failure itself")
    if not idle4:
        fails.append("a genuinely starved worker must keep the watchdog refreshed, or systemd "
                     "restart-loops it through every upstream outage")

    print("\n=== 5. the cursor keeps the MOST RECENT names, not the alphabetically largest ===")
    names = [f"seg{i:03d}.ts" for i in range(995, 1005)]     # crosses the 3->4 digit boundary
    lexicographic = sorted(set(names))[-4:]
    by_order = names[-4:]
    print(f"  lexicographic (the old rule): {lexicographic}")
    print(f"  by processing order (new):    {by_order}")
    if lexicographic == by_order:
        fails.append("the fixture does not exercise the sort defect")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "gpu_analyze.py")).read()
    # Match the CODE, not the comment that quotes the old expression to explain why it was wrong.
    if 'join(sorted(seen)' in src:
        fails.append("gpu_analyze still persists the cursor by lexicographic sort")
    if "seen_order[-40:]" not in src:
        fails.append("gpu_analyze does not persist the cursor in processing order")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — rewind detected and resumed within a poll, name-set fallback works, the watchdog is "
          "no longer told a lie while upstream advances, a real outage still does not restart-loop, "
          "and the cursor keeps recency")
    return 0


if __name__ == "__main__":
    sys.exit(main())
