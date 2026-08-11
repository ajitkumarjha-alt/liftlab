"""Evidence gate: the 05:24:41 fabrication stays refused, and a LIVE episode is NOT.

THE SECOND HALF EXISTS BECAUSE THE FIRST VERSION BROKE IT. The gate shipped in aa39eb0 keyed on
det_counts/ids, which gpu_analyze collects ONLY while validating — so in live mode both are empty
by design and EVERY live episode was refused, silently ending the live audit trail. The
denominator is now `frames`, counted in both modes."""
import sys, types, io, contextlib
sys.path.insert(0, "/home/ajit_kumarjha_lodhagroup_com/projects/liftlab")
import os
os.environ.setdefault("ANALYSIS_TOKEN", "stub"); os.environ.setdefault("GW", "site-A")
os.environ.setdefault("CLOUD_URL", "http://127.0.0.1:9"); os.environ.setdefault("CAM", "ch29")
import gpu_analyze as ga

posted = []
ga.http_post_json = lambda url, payload, what="": (posted.append((url, payload)) or 200)
logs = []
ga.log = lambda m: logs.append(m)

def run(ep, label):
    logs.clear(); posted.clear()
    ga.post_episode(ep, label)
    return list(posted), list(logs)

fails = []
# 1) THE REAL ONE, from the journal: boarded=0 alighted=1, imgs=0, span=0s, 0 frames, 0 ids
churn = {"b": 0, "a": 1, "imgs": [], "ts_start": 1786339472.0, "ts_end": 1786339472.0,
         "det_counts": [], "ids": set(), "confs": [], "frames": 0, "occ_max": 0, "occ_frames": 0}
p, l = run(churn, "gap-between-segments")
print("churn episode -> posted:", len(p))
if p: fails.append("FABRICATED episode was POSTED — the gate did not hold")
if not any("REFUSED" in m for m in l): fails.append("no REFUSED log line for the churn episode")

# 2) a REAL episode with evidence must still post
real = {"b": 1, "a": 0, "imgs": [1, 2], "ts_start": 1786339472.0, "ts_end": 1786339490.0,
        "det_counts": [1, 2, 2, 1], "ids": {7, 9}, "confs": [0.71, 0.83], "frames": 140, "occ_max": 4, "occ_frames": 140}
p, l = run(real, "live")
print("real episode  -> posted:", len(p))
if len(p) != 1: fails.append("a well-evidenced episode was refused — the gate is too tight")

# 3) frames but no tracks (detector saw boxes, tracker established nothing) -> refuse
noids = {"b": 0, "a": 1, "imgs": [], "ts_start": 1.0, "ts_end": 5.0,
         "det_counts": [1, 1], "ids": set(), "confs": [0.6], "frames": 60, "occ_max": 0, "occ_frames": 60}
p, l = run(noids, "gap")
print("frames,no ids -> posted:", len(p))
if p: fails.append("episode with 0 distinct track_ids was posted")

# 4) the pre-existing empty guard still fires first
p, l = run({"b": 0, "a": 0, "imgs": [], "ts_start": 1.0, "ts_end": 2.0,
            "det_counts": [], "ids": set(), "confs": [], "frames": 30}, "gap")
if p: fails.append("0-transit episode posted")
if not any("EMPTY" in m for m in l): fails.append("0-transit episode lost its EMPTY log line")


# 5) THE REGRESSION: a LIVE episode collects no detection audit by design. It must POST.
live = {"b": 1, "a": 0, "imgs": [], "ts_start": 100.0, "ts_end": 118.0,
        "det_counts": [], "ids": set(), "confs": [], "frames": 140, "occ_max": 5, "occ_frames": 140}
p, l = run(live, "live")
print("live episode  -> posted:", len(p), "(detection audit empty by design in live mode)")
if not p:
    fails.append("LIVE episode refused — aa39eb0 regression is back; the live audit trail dies")
elif p[0][1].get("occupancy_max") != 5 or p[0][1].get("occupancy_frames") != 140:
    fails.append("occupancy fields not carried on the posted payload")

# 6) occupancy_degraded rides through
p, l = run(dict(live, occ_degraded=True), "live")
print("degraded flag ->", p[0][1].get("occupancy_degraded") if p else "not posted")
if not p or p[0][1].get("occupancy_degraded") != 1:
    fails.append("occupancy_degraded not carried")

print()
print("FAIL: " + "; ".join(fails) if fails else "ALL GATE ASSERTIONS PASS")
sys.exit(1 if fails else 0)
