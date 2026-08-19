#!/usr/bin/env python3
"""Precompute everything /dash used to derive on the request path. RUNS OFF THE REQUEST PATH.

Three stages, and the ORDER IS LOAD-BEARING:
  1. floor alphabet   -> floor_alphabet   (the all-era admission evidence)
  2. per-camera aggregate -> door_aggregate (door_gpu + tier2 for the default window)
  3. trends payload   -> trends_cache     (per camera AND the fleet; reads what 1-2 just wrote)

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
        stages = {"alphabet": 0.0, "aggregate": 0.0, "trends_cams": 0.0, "trends_fleet": 0.0}
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

        # ── stage 3: the trends payload, per camera AND for the fleet ──
        # LAST, because it reads the aggregates stages 1-2 just wrote: a trends payload built before
        # them would cache the "not yet computed" tier2/RTT states for a whole timer interval.
        for cam in cams + ([""] if not only_cam else []):
            t0 = time.time()
            try:
                m = D.trends_refresh(db, gw, cam)
                print(f"[precompute] trends {gw}/{m['gw'] and (cam or 'fleet')}: "
                      f"{m['bytes']//1024}KB in {time.time()-t0:.2f}s "
                      f"(dv={(m['door_version'] or '-')[:24]})", flush=True)
                ok += 1; gw_ok += 1
            except Exception as e:
                err += 1; gw_err += 1
                print(f"[precompute] trends {gw}/{cam or 'fleet'}: FAILED {type(e).__name__}: {e}",
                      flush=True)
            # SPLIT DELIBERATELY. The fleet payload and the per-camera payloads read the SAME era
            # rows, so these two numbers are the evidence for the shared-read lever (~2x) that comes
            # BEFORE incremental fill. A single trends total would hide the duplication entirely.
            stages["trends_fleet" if not cam else "trends_cams"] += time.time() - t0

        m = D.precompute_run_record(db, gw, t_gw, stages, gw_ok, gw_err)
        print(f"[precompute] {gw} sweep: {m['total_s']:.1f}s total "
              f"(alphabet {stages['alphabet']:.1f}s, aggregate {stages['aggregate']:.1f}s, "
              f"trends {stages['trends_cams']:.1f}s per-camera + {stages['trends_fleet']:.1f}s fleet)"
              f" — recorded to precompute_run", flush=True)

    db.close()
    print(f"[precompute] done: {ok} ok, {err} failed, {time.time()-t_all:.1f}s total", flush=True)
    return 1 if (err and not ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
