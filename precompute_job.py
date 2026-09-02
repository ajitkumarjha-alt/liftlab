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
import sys
import time

sys.path.insert(0, os.environ.get("LIFTLAB_APP", "/opt/liftlab-b3/cloud"))

import dash_api as D  # noqa: E402


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

    db = D._db()
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
            try:
                m = D.alphabet_refresh(db, gw, cam)
                print(f"[precompute] alphabet {gw}/{cam}: {m['n_admitted']} floors from "
                      f"{m['evidence_rows']} rows, era={m['era']} in {time.time()-t0:.2f}s",
                      flush=True)
                ok += 1; gw_ok += 1
            except Exception as e:                 # one bad camera must not stop the sweep
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
            try:
                m = D.aggregate_refresh(db, gw, cam)
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
            except Exception as e:
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
                try:
                    m = D.rtt_refresh(db, gw, cam, wd)
                    if m.get("skipped"):
                        print(f"[precompute] rtt {gw}/{cam} w={wd:g}d: skipped ({m['skipped']})",
                              flush=True)
                    else:
                        print(f"[precompute] rtt {gw}/{cam} w={wd:g}d: "
                              f"{m.get('n_rows') if m.get('n_rows') is not None else '-'} rows, "
                              f"state={m.get('state')}, "
                              f"trips={m.get('n_trips') if m.get('n_trips') is not None else '-'} "
                              f"in {time.time()-t0:.2f}s"
                              + (f" [{m['error']}]" if m.get("error") else ""), flush=True)
                    ok += 1; gw_ok += 1
                except Exception as e:
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
            try:
                m = D.trends_refresh(db, gw, cam, _period)
                print(f"[precompute] trends {gw}/{m['gw'] and (cam or 'fleet')} p={_period}: "
                      f"{m['bytes']//1024}KB in {time.time()-t0:.2f}s "
                      f"(dv={(m['door_version'] or '-')[:24]})", flush=True)
                ok += 1; gw_ok += 1
            except Exception as e:
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
                try:
                    m = D.study_refresh(db, gw, _kind, _period)
                    print(f"[precompute] study {gw}/{_kind} p={_period}: {m['n_rows']} rows, "
                          f"{m['bytes']//1024}KB in {time.time()-t0:.2f}s", flush=True)
                    ok += 1; gw_ok += 1
                except Exception as e:
                    err += 1; gw_err += 1
                    print(f"[precompute] study {gw}/{_kind} p={_period}: "
                          f"FAILED {type(e).__name__}: {e}", flush=True)
                stages["study"] += time.time() - t0

        m = D.precompute_run_record(db, gw, t_gw, stages, gw_ok, gw_err)
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
