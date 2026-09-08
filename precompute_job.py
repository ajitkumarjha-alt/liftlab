#!/usr/bin/env python3
"""Precompute everything /dash used to derive on the request path. RUNS OFF THE REQUEST PATH.

Four stages, and the ORDER IS LOAD-BEARING:
  1. floor alphabet   -> floor_alphabet   (the all-era admission evidence)
  2. per-camera aggregate -> door_aggregate (door_gpu + tier2 for the default window)
  2b. RTT per window  -> rtt_window       (one entry per period the picker offers)
  3. trends payload   -> trends_cache     (per camera AND the fleet; reads what 1-2 just wrote)
  4. study matrices   -> study_matrix     (riders/lift/day + RTT/hour/lift; reads 2b, per period)

Stage 2 depends on stage 1: _tier2 reads the stored alphabet to decide which floor reads are
admissible. Running them as two independent timers would let an aggregate be computed against a
missing or stale alphabet and silently bake the wrong admissions into a cached result — which is
why this is ONE ordered job and not two units.

/dash NEVER calls any of this. A stale table serves reads; a read must never be able to trigger a
derivation, or the non-terminating request comes back under a new name.

usage:
  precompute_job.py                 # every gateway in channel_map
  precompute_job.py site-A          # one gateway
  precompute_job.py site-A ch29     # one camera
  ALPHABET_ONLY=1 precompute_job.py # stage 1 only
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.environ.get("LIFTLAB_APP", "/opt/liftlab-b3/cloud"))

import dash_api as D  # noqa: E402

# ── LOCK RETRY. Under WAL there is exactly ONE writer at a time, and busy_timeout only covers the
# wait INSIDE a single statement. A sweep stage that loses the race for 30 seconds still raises,
# and on 2026-09-07 every study stage did, 5m41s in, while seven workers posted their restart
# backlog. Retrying the STAGE is the missing layer: the work is idempotent (every refresh is an
# upsert keyed on the era), so a second attempt a few seconds later is exactly as correct as the
# first and is usually all it takes.
#
# BACKOFF, NOT A TIGHT LOOP. A retry that returns immediately joins the contention it is waiting
# out. 2s, 6s, 18s — bounded, and ~26s of extra waiting per stage in the worst case, against an
# hourly timer.
RETRY_DELAYS = [float(x) for x in os.environ.get("PRECOMPUTE_RETRY_S", "2,6,18").split(",") if x]
LOCK_STATS = {"n_locked": 0, "lock_wait_s": 0.0}
# WHERE THE MEMORY GOES, not just how much. The 2026-09-07 sweep peaked at 943 MB and nothing
# recorded which stage was holding it, so the only available answer was a guess. Profiled at
# fixture scale, ONE camera at 200,000 door rows peaks at 42 MB — 27 MB above baseline — so the
# live figure is not one camera's working set and the question is genuinely open. A high-water
# mark sampled after every stage, with the stage that set it, closes it from the next sweep on.
RSS_PEAK = {"mb": 0.0, "stage": None}


def _rss_mb():
    """Current resident set, MB. /proc first — it is the number the OOM killer reads."""
    try:
        with open("/proc/self/statm") as fh:
            return round(int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576.0, 1)
    except (OSError, ValueError, IndexError):
        try:
            import resource
            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
        except Exception:
            return None


def _note_rss(label):
    r = _rss_mb()
    if r is not None and r > RSS_PEAK["mb"]:
        RSS_PEAK.update({"mb": r, "stage": label})
    return r


def run_stage(label, fn, *args, **kwargs):
    """Call one refresh, retrying ONLY a held lock. -> (result, error_or_None).

    A lock is the one error worth retrying: it says nothing about the work, only about who else was
    writing. Everything else — a missing table, a bad era, a bug — reproduces exactly on the next
    attempt, so retrying it burns the timer and buries the real message under three copies.
    """
    for i, delay in enumerate([0.0] + RETRY_DELAYS):
        if delay:
            LOCK_STATS["lock_wait_s"] += delay
            time.sleep(delay)
        try:
            out = fn(*args, **kwargs)
            _note_rss(label)
            return out, None
        except Exception as e:
            if not D.is_locked(e) or i >= len(RETRY_DELAYS):
                return None, e
            LOCK_STATS["n_locked"] += 1
            nxt = RETRY_DELAYS[i]
            print(f"[precompute] {label}: DATABASE LOCKED ({e}) — retry {i + 1}/"
                  f"{len(RETRY_DELAYS)} in {nxt:g}s. {D.lock_diagnostics()}", flush=True)
    return None, RuntimeError("unreachable")


def gateways(db):
    rows = D._q(db, "SELECT DISTINCT gateway_id g FROM channel_map WHERE gateway_id IS NOT NULL")
    gws = [r["g"] for r in rows if r["g"]]
    if not gws:
        gws = [g.split(":", 1)[0]
               for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g]
    return gws


def main():
    argv = sys.argv[1:]
    only_gw = argv[0] if argv else None
    only_cam = argv[1] if len(argv) > 1 else None
    alphabet_only = bool(os.environ.get("ALPHABET_ONLY"))

    # THE TIMER'S BUSY TIMEOUT, not the request path's. This connection had none at all, which is
    # what killed the 2026-09-07 sweep: with busy_timeout=0 a writer that meets a held lock gives
    # up on contact, and seven workers were posting their restart backlog. 30s matches what the
    # ingest path already waits; nobody is watching this job, so waiting is free.
    db = D._db(busy_ms=D.PRECOMPUTE_BUSY_MS)
    D._alphabet_table(db)
    D._aggregate_table(db)
    t_all = time.time()
    ok = err = 0

    for gw in ([only_gw] if only_gw else gateways(db)):
        cams = [only_cam] if only_cam else [c["cam"] for c in D._cameras(db, gw)]
        # WALL TIME PER STAGE, RECORDED — not just printed. See precompute_run_record: the journal
        # is where this number went to be unread, and it is the one number that says whether the
        # sweep still fits inside its timer interval.
        t_gw = time.time()
        stages = {"alphabet": 0.0, "aggregate": 0.0, "rtt": 0.0,
                  "trends_cams": 0.0, "trends_fleet": 0.0, "study": 0.0}
        gw_ok = gw_err = 0

        # ── stage 1: alphabet (must complete before any aggregate for this camera) ──
        for cam in cams:
            t0 = time.time()
            m, e = run_stage(f"alphabet {gw}/{cam}", D.alphabet_refresh, db, gw, cam)
            if e is None:
                # RETAINED IS PRINTED, ALWAYS. The guard keeping a floor alive is exactly the
                # case where the underlying evidence broke — a silent save is a hidden fault.
                _ret = m.get("retained") or []
                print(f"[precompute] alphabet {gw}/{cam}: {m['n_admitted']} floors from "
                      f"{m['evidence_rows']} rows, era={m['era']} in {time.time()-t0:.2f}s"
                      + (f" — RETAINED {len(_ret)} from the stored alphabet "
                         f"({', '.join(_ret[:8])}{'...' if len(_ret) > 8 else ''}): this pass would "
                         f"have dropped them for lack of evidence" if _ret else ""),
                      flush=True)
                ok += 1; gw_ok += 1
            else:                                  # one bad camera must not stop the sweep
                err += 1; gw_err += 1
                print(f"[precompute] alphabet {gw}/{cam}: FAILED {type(e).__name__}: {e}", flush=True)
            stages["alphabet"] += time.time() - t0

        if alphabet_only:
            # DELIBERATELY NOT RECORDED. A stage-1-only run is not a sweep, and writing its wall
            # time to precompute_run would understate the duration the threshold is watching —
            # an alphabet-only pass would read as "the fill got faster" and mask real drift.
            continue

        # ── stage 2: aggregates ──
        for cam in cams:
            t0 = time.time()
            m, e = run_stage(f"aggregate {gw}/{cam}", D.aggregate_refresh, db, gw, cam)
            if e is None:
                if m.get("skipped"):
                    print(f"[precompute] aggregate {gw}/{cam}: skipped ({m['skipped']})", flush=True)
                else:
                    # RTT is reported SEPARATELY, with its own millisecond cost. It is the most
                    # expensive part of the sweep — the era filter is a LIKE no index covers, so it
                    # post-filters every row in the window — and it is the part that has to stay
                    # inside the timer interval. A line that hides it inside one total cannot show
                    # the sweep drifting towards its own period.
                    print(f"[precompute] aggregate {gw}/{cam}: {m['source_rows']} rows, "
                          f"tier2={'yes' if m['has_tier2'] else 'none'}, "
                          f"rtt={m.get('rtt_trips') if m.get('rtt_trips') is not None else '-'} trips"
                          f" in {(m.get('rtt_ms') or 0)/1000:.2f}s"
                          + (f" [{m['rtt_error']}]" if m.get("rtt_error") else "") + ", "
                          f"cv={m['counting_version'] or '-'} dv={m['door_version']} "
                          f"window={m['window_days']}d in {time.time()-t0:.2f}s", flush=True)
                ok += 1; gw_ok += 1
            else:
                err += 1; gw_err += 1
                print(f"[precompute] aggregate {gw}/{cam}: FAILED {type(e).__name__}: {e}", flush=True)
            stages["aggregate"] += time.time() - t0

        # ── stage 2b: RTT, one entry per PERIOD the picker offers ──
        # BEFORE stage 3, because the trends payload embeds the RTT summary: filling it afterwards
        # would cache "not yet computed" for a whole timer interval on every camera.
        #
        # UNCAPPED. The 120,000-row cap is a request-path guard and this is the timer; capping it
        # here is what left ch29 and ch27 showing the cap message on All and 7 days while Today and
        # 30 days said "not served for this range" — RTT unviewable on every button.
        for cam in cams:
            for wd in D.RTT_WINDOWS:
                t0 = time.time()
                m, e = run_stage(f"rtt {gw}/{cam} w={wd:g}d", D.rtt_refresh, db, gw, cam, wd)
                if e is None:
                    if m.get("skipped"):
                        print(f"[precompute] rtt {gw}/{cam} w={wd:g}d: skipped ({m['skipped']})",
                              flush=True)
                    else:
                        # n_trips_stored is reported separately from n_trips: the first is what
                        # the study bundle will be able to serve, the second is what the walk
                        # found, and they differ when RTT_STORE_MAX_TRIPS bites.
                        _nt = m.get("n_trips")
                        _ns = m.get("n_trips_stored")
                        print(f"[precompute] rtt {gw}/{cam} w={wd:g}d: "
                              f"{m.get('n_rows') if m.get('n_rows') is not None else '-'} rows, "
                              f"state={m.get('state')}, "
                              f"trips={_nt if _nt is not None else '-'} "
                              f"(stored {_ns if _ns is not None else '-'}) "
                              f"in {time.time()-t0:.2f}s"
                              + (f" [{m['error']}]" if m.get("error") else ""), flush=True)
                    ok += 1; gw_ok += 1
                else:
                    err += 1; gw_err += 1
                    print(f"[precompute] rtt {gw}/{cam} w={wd:g}d: FAILED {type(e).__name__}: {e}",
                          flush=True)
                stages["rtt"] += time.time() - t0

        # ── stage 3: the trends payload, per camera AND for the fleet ──
        # AFTER 1-2, because it reads the aggregates they just wrote: a trends payload built before
        # them would cache the "not yet computed" tier2/RTT states for a whole timer interval.
        for cam in cams + ([""] if not only_cam else []):
          # EVERY PERIOD THE PICKER OFFERS. Filling 'all' alone left Today / 7 days / 30 days on the
          # not-computed skeleton permanently, which reads as a broken deploy rather than as a
          # deliberate cache miss — and the request path will not derive, by design.
          for _period in D.TRENDS_FILL_PERIODS:
            t0 = time.time()
            m, e = run_stage(f"trends {gw}/{cam or 'fleet'} p={_period}",
                             D.trends_refresh, db, gw, cam, _period)
            if e is None:
                print(f"[precompute] trends {gw}/{m['gw'] and (cam or 'fleet')} p={_period}: "
                      f"{m['bytes']//1024}KB in {time.time()-t0:.2f}s "
                      f"(dv={(m['door_version'] or '-')[:24]})", flush=True)
                ok += 1; gw_ok += 1
            else:
                err += 1; gw_err += 1
                print(f"[precompute] trends {gw}/{cam or 'fleet'} p={_period}: "
                      f"FAILED {type(e).__name__}: {e}", flush=True)
            # SPLIT DELIBERATELY. The fleet payload and the per-camera payloads read the SAME era
            # rows, so these two numbers are the evidence for the shared-read lever (~2x) that comes
            # BEFORE incremental fill. A single trends total would hide the duplication entirely.
            stages["trends_fleet" if not cam else "trends_cams"] += time.time() - t0

        # ── stage 4: the two fleet study matrices, one entry per kind per period ──
        # AFTER stage 2b, because rtt_per_hour is a RE-SHAPE of what rtt_refresh stored — running
        # it first would cache "not yet computed" for every lift for a whole timer interval, which
        # is exactly the defect the trends stage carries a comment about one stage up.
        #
        # NOT SKIPPED WHEN only_cam IS SET. These are FLEET tables: computing one from a database
        # where a single camera was just refreshed is fine (they read every camera's stored state),
        # and skipping them would leave the fleet views describing the era before this run.
        for _kind in D.STUDY_KINDS:
            for _period in D.STUDY_FILL_PERIODS:
                t0 = time.time()
                m, e = run_stage(f"study {gw}/{_kind} p={_period}",
                                 D.study_refresh, db, gw, _kind, _period)
                if e is None:
                    print(f"[precompute] study {gw}/{_kind} p={_period}: {m['n_rows']} rows, "
                          f"{m['bytes']//1024}KB in {time.time()-t0:.2f}s "
                          f"rss={_rss_mb()}MB", flush=True)
                    ok += 1; gw_ok += 1
                else:
                    err += 1; gw_err += 1
                    print(f"[precompute] study {gw}/{_kind} p={_period}: "
                          f"FAILED {type(e).__name__}: {e}", flush=True)
                stages["study"] += time.time() - t0

        _rss = _note_rss("end of sweep")
        _lock = D.lock_diagnostics()
        m = D.precompute_run_record(db, gw, t_gw, stages, gw_ok, gw_err,
                                    extra={"n_locked": LOCK_STATS["n_locked"],
                                           "lock_wait_s": round(LOCK_STATS["lock_wait_s"], 1),
                                           "rss_peak_mb": RSS_PEAK["mb"] or _rss,
                                           "rss_peak_stage": RSS_PEAK["stage"],
                                           "wal_mb": _lock.get("wal_mb")})
        # THE CONTENTION AND THE FOOTPRINT, ON THE SAME LINE AS THE DURATION. A sweep that took an
        # hour because it spent 40 minutes waiting for a lock is a different problem from one that
        # spent 40 minutes computing, and until 2026-09-07 the line could not tell them apart.
        print(f"[precompute] {gw} contention: {LOCK_STATS['n_locked']} lock retr"
              f"{'y' if LOCK_STATS['n_locked'] == 1 else 'ies'}, "
              f"{LOCK_STATS['lock_wait_s']:.0f}s waited · rss now {_rss}MB, "
              f"peak {RSS_PEAK['mb']}MB at [{RSS_PEAK['stage']}] · "
              f"wal {_lock.get('wal_mb')}MB · litestream {_lock.get('litestream')}", flush=True)
        if m.get("record_error"):
            print(f"[precompute] {gw} sweep NOT recorded: {m['record_error']} — the work above "
                  f"still happened; only the durable row is missing", flush=True)
        print(f"[precompute] {gw} sweep: {m['total_s']:.1f}s total "
              f"(alphabet {stages['alphabet']:.1f}s, aggregate {stages['aggregate']:.1f}s, "
              f"rtt {stages['rtt']:.1f}s, "
              f"trends {stages['trends_cams']:.1f}s per-camera + {stages['trends_fleet']:.1f}s fleet, "
              f"study {stages['study']:.1f}s)"
              f" — recorded to precompute_run", flush=True)

    db.close()
    print(f"[precompute] done: {ok} ok, {err} failed, {time.time()-t_all:.1f}s total", flush=True)
    return 1 if (err and not ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
