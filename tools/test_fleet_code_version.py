#!/usr/bin/env python3
"""gpu_fleet cycles a worker whose code differs from disk, and only then.

WHY. A worker is a separate process started from WORKER_SCRIPT. Replacing that file changes what the
NEXT spawn runs and nothing about the ones already running, so a deploy can leave old code executing
with no outward sign. On 2026-08-10 a deploy needed stop + pkill + start to actually take effect.
"""
import os, sys, tempfile

def main():
    d = tempfile.mkdtemp()
    w = os.path.join(d, "gpu_analyze.py")
    open(w, "w").write("import time\ntime.sleep(60)\n")
    os.environ.update({"WORKER_SCRIPT": w, "WORKER_PY": sys.executable, "STATE_DIR": d,
                       "ANALYSIS_TOKEN": "stub", "FLEET_TRANSIT_FLOOR_S": "0"})
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import gpu_fleet as gf

    cfg = {"stride": 2, "analyze_fps": 0, "geometry": {}, "door_levels": {}, "door_tracker": "h2"}
    fails = []
    gf.start("chTEST", cfg)
    rec = gf._procs["chTEST"]
    if not rec.get("code_md5"):
        fails.append("no code_md5 recorded at spawn")
    old_pid = rec["proc"].pid

    gf.cycle_stale_code()                                  # nothing changed on disk
    if gf._procs["chTEST"]["proc"].pid != old_pid:
        fails.append("cycled a worker whose code had NOT changed (would restart-loop the fleet)")

    open(w, "a").write("# deployed change\n")              # the deploy
    gf.cycle_stale_code()
    new_pid = gf._procs["chTEST"]["proc"].pid
    if new_pid == old_pid:
        fails.append("stale worker was NOT cycled — a deploy would leave old code running")
    if gf._procs["chTEST"].get("code_md5") != gf._worker_md5():
        fails.append("the replacement worker did not record the new md5 (would cycle forever)")

    gf.cycle_stale_code()                                  # now in sync again
    if gf._procs["chTEST"]["proc"].pid != new_pid:
        fails.append("cycled again after the code matched — restart loop")

    gf.stop("chTEST", "test done")
    print("FAIL: " + "; ".join(fails) if fails else "FLEET CODE-VERSION CYCLE: ALL ASSERTIONS PASS")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
