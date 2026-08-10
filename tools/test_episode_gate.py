"""Evidence gate: replays the 05:24:41 ch29 episode and asserts it is refused."""
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
         "det_counts": [], "ids": set(), "confs": []}
p, l = run(churn, "gap-between-segments")
print("churn episode -> posted:", len(p))
if p: fails.append("FABRICATED episode was POSTED — the gate did not hold")
if not any("REFUSED" in m for m in l): fails.append("no REFUSED log line for the churn episode")

# 2) a REAL episode with evidence must still post
real = {"b": 1, "a": 0, "imgs": [1, 2], "ts_start": 1786339472.0, "ts_end": 1786339490.0,
        "det_counts": [1, 2, 2, 1], "ids": {7, 9}, "confs": [0.71, 0.83]}
p, l = run(real, "live")
print("real episode  -> posted:", len(p))
if len(p) != 1: fails.append("a well-evidenced episode was refused — the gate is too tight")

# 3) frames but no tracks (detector saw boxes, tracker established nothing) -> refuse
noids = {"b": 0, "a": 1, "imgs": [], "ts_start": 1.0, "ts_end": 5.0,
         "det_counts": [1, 1], "ids": set(), "confs": [0.6]}
p, l = run(noids, "gap")
print("frames,no ids -> posted:", len(p))
if p: fails.append("episode with 0 distinct track_ids was posted")

# 4) the pre-existing empty guard still fires first
p, l = run({"b": 0, "a": 0, "imgs": [], "ts_start": 1.0, "ts_end": 2.0,
            "det_counts": [], "ids": set(), "confs": []}, "gap")
if p: fails.append("0-transit episode posted")
if not any("EMPTY" in m for m in l): fails.append("0-transit episode lost its EMPTY log line")

print()
print("FAIL: " + "; ".join(fails) if fails else "ALL GATE ASSERTIONS PASS")
sys.exit(1 if fails else 0)
