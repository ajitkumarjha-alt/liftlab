#!/usr/bin/env python3
"""Do the cameras' door= costs move TOGETHER (a shared resource) or independently (per-camera cause)?

Feed it journalctl output containing BOTH the fleet's `started pid=` lines (which map pid -> camera)
and the workers' `seg timing` lines (which carry the pid and door=):

  journalctl -u liftlab-gpu-fleet --since '90 min ago' --no-pager \\
    | python3 tools/door_covariance.py

WHAT IT ANSWERS, AND WHAT IT CANNOT.

It answers: are the per-minute door= series correlated across cameras?

It does NOT answer WHY, and the distinction matters because two different faults produce the same
correlation:

  * CPU COUPLING — door passes are CPU work (Sobel, per-cell NCC) and the box has few cores, so one
    camera's spike steals cycles from the rest.
  * SHARED CLOUD LATENCY — door_ms as currently instrumented INCLUDES post_door_event and
    post_floorcheck, which are synchronous HTTP. A slow cloud inflates every door=ON camera at once,
    with the workers BLOCKED ON I/O rather than burning CPU. `SLOW POST floorcheck: 5237ms` is
    already in the journal.

High correlation is consistent with both. Only per-process CPU (pidstat: %CPU near a core = compute;
low %CPU with high wall = blocked on I/O) separates them. Run both; this tool is half the evidence.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

RE_START = re.compile(r"([A-Za-z0-9_]+): started pid=(\d+)")
RE_PID = re.compile(r"python\[(\d+)\]")
RE_DOOR = re.compile(r"door=(\d+)")
RE_RATIO = re.compile(r"-> ([0-9.]+)x")
RE_TIME = re.compile(r"^\w+\s+\d+\s+(\d{2}):(\d{2}):\d{2}")


def parse(lines):
    cam_of, series = {}, defaultdict(lambda: defaultdict(list))
    for ln in lines:
        m = RE_START.search(ln)
        if m:
            cam_of[m.group(2)] = m.group(1)
            continue
        if "seg timing" not in ln:
            continue
        p, d, t = RE_PID.search(ln), RE_DOOR.search(ln), RE_TIME.search(ln)
        if not (p and d and t):
            continue
        cam = cam_of.get(p.group(1), f"pid{p.group(1)}")
        series[cam][f"{t.group(1)}:{t.group(2)}"].append(int(d.group(1)))
    return cam_of, series


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    if saa < 1e-9 or sbb < 1e-9:
        return None
    return sab / (saa * sbb) ** 0.5


def main():
    cam_of, series = parse(sys.stdin)
    if not series:
        print("no `seg timing` lines with door= found on stdin")
        return 2
    if not cam_of:
        print("WARNING: no `started pid=` lines — cameras shown as pids. Widen the --since window "
              "so the fleet's start banner is included, or the mapping is a guess.\n")

    minutes = sorted({m for c in series for m in series[c]})
    cams = sorted(series)
    print(f"{len(cams)} camera(s), {len(minutes)} minute bucket(s)\n")
    print("per-minute mean door= (ms), '.' = no sample that minute")
    print(f"{'minute':>7} " + " ".join(f"{c:>7}" for c in cams))
    for mi in minutes:
        cells = []
        for c in cams:
            v = series[c].get(mi)
            cells.append(f"{sum(v)/len(v):7.0f}" if v else "      .")
        print(f"{mi:>7} " + " ".join(cells))

    # Correlate only on minutes where BOTH cameras produced a sample — pairing across gaps would
    # correlate the sampling pattern instead of the load.
    print("\npairwise correlation of per-minute door= (only cameras with door>0 are meaningful)")
    print(f"{'pair':>17} {'n':>4} {'r':>7}")
    strong = []
    for i, a in enumerate(cams):
        for b in cams[i + 1:]:
            common = [m for m in minutes if series[a].get(m) and series[b].get(m)]
            if len(common) < 3:
                continue
            va = [sum(series[a][m]) / len(series[a][m]) for m in common]
            vb = [sum(series[b][m]) / len(series[b][m]) for m in common]
            r = pearson(va, vb)
            if r is None:
                continue
            print(f"{a + '~' + b:>17} {len(common):>4} {r:>+7.3f}")
            if r >= 0.6:
                strong.append((a, b, r))

    print()
    if strong:
        print(f"{len(strong)} pair(s) at r >= +0.60 — CONSISTENT WITH a shared resource, and equally")
        print("consistent with shared cloud latency, because door= includes the synchronous POSTs.")
        print("Settle it with per-process CPU, not with this number alone:")
    else:
        print("No strongly correlated pair — the cameras' door costs move INDEPENDENTLY, which points")
        print("at per-camera causes rather than a shared resource. Confirm with:")
    print("  pidstat -u -p ALL 5 12 | grep -E 'gpu_analyze|UID'   # %CPU per worker")
    print("  grep -c 'SLOW POST' <the same journal window>        # blocked-on-cloud evidence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
