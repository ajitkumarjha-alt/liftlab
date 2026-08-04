#!/usr/bin/env python3
"""Derive the per-camera floor alphabet and persist it. RUNS OFF THE REQUEST PATH.

This is the 140k-row all-era walk that /dash used to do inline, per camera, on every page load
(247,019 rows per request across cameras). It is deliberately all-era — floors are physical, so a
template rebuild moves door_version but not the building — which means it cannot be windowed. So it
moved here instead: a scheduled job writes `floor_alphabet`, and /dash does a single indexed read.

/dash NEVER calls this. A stale table serves reads; a read must never be able to trigger a
derivation, or the hang returns under a new name.

usage:
  alphabet_job.py                 # every gateway in channel_map (falls back to GATEWAY_TOKENS)
  alphabet_job.py site-A          # one gateway
  alphabet_job.py site-A ch29     # one camera
"""
import os
import sys
import time

sys.path.insert(0, os.environ.get("LIFTLAB_APP", "/opt/liftlab-b3/cloud"))

import dash_api as D  # noqa: E402


def gateways(db):
    rows = D._q(db, "SELECT DISTINCT gateway_id g FROM channel_map WHERE gateway_id IS NOT NULL")
    gws = [r["g"] for r in rows if r["g"]]
    if not gws:                                    # channel_map unmarked -> the configured tokens
        gws = [g.split(":", 1)[0]
               for g in os.environ.get("GATEWAY_TOKENS", "site-A:devtoken").split(",") if ":" in g]
    return gws


def main():
    argv = sys.argv[1:]
    only_gw = argv[0] if argv else None
    only_cam = argv[1] if len(argv) > 1 else None

    db = D._db()
    D._alphabet_table(db)
    t_all = time.time()
    n_ok = n_err = 0

    for gw in ([only_gw] if only_gw else gateways(db)):
        cams = [only_cam] if only_cam else [c["cam"] for c in D._cameras(db, gw)]
        for cam in cams:
            t0 = time.time()
            try:
                m = D.alphabet_refresh(db, gw, cam)
                dt = time.time() - t0
                n_ok += 1
                print(f"[alphabet] {gw}/{cam}: {m['n_admitted']} floors from "
                      f"{m['evidence_rows']} rows, era={m['era']} in {dt:.2f}s", flush=True)
            except Exception as e:                 # one bad camera must not stop the sweep
                n_err += 1
                print(f"[alphabet] {gw}/{cam}: FAILED {type(e).__name__}: {e}", flush=True)

    db.close()
    print(f"[alphabet] done: {n_ok} ok, {n_err} failed, {time.time()-t_all:.1f}s total", flush=True)
    return 1 if (n_err and not n_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
