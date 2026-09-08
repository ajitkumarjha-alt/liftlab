#!/usr/bin/env python3
"""Propose SINGLE-CHARACTER panel frames for operator confirmation — the only source of blank_1.

WHY THIS IS ITS OWN TOOL. `blank_1` is taught only by single-character floors: build_templates
right-aligns a label into the fixed cells, so a 1-char label pads BOTH left cells with blank while
a 2-char label pads only one. And single-character floors are exactly what a reader with a failing
tens cell cannot produce — the empty cell is where the junk digit appears. The fault starves its
own repair evidence, so the frames have to be found by other means and confirmed by a human.

TWO TESTS, AND NEITHER MAY CONSULT THE MATCHER. The templates are what is broken; letting them
choose their own replacements is circular twice over — ranking on `blank_1` similarity selects the
frames the failed template already recognises, and ranking on "no glyph matches the tens cell"
selects frames whose tens digit the DEGRADED reader merely cannot name. Both were tried on ch29 and
both were wrong. So the decision is physical:

  1. CONTRAST GAP   units cell clearly lit (range >= --units-ctr) and tens cell measurably dimmer
                    (gap >= --min-gap). Cheap, and it does most of the work.
  2. TENS EDGE ENERGY   variance of the Laplacian over the tens cell, <= --max-tens-lap.
                    THIS IS THE ONE THAT MATTERS. A dim digit still has EDGES; an empty cell has
                    only the panel's glow. Test 1 alone cannot tell them apart.

CALIBRATED, NOT GUESSED. Against 60 candidates the operator adjudicated on 2026-09-08 (55 genuine
single-character panels, 5 two-digit floors with a dim tens digit):

    feature            accepts (n=55)              rejects (n=5)          separates?
    contrast gap       24 .. 57  (p50 28)          26, 27, 27, 29, 39     NO — 36 accepts overlap
    tens edge energy   3053 .. 11657 (p50 9828)    10960 .. 23603         nearly — 7 overlap

  t_lap <= 10000   keeps 30/55 (55%)   admits 0/5 rejects
  t_lap <= 10500   keeps 43/55 (78%)   admits 0/5 rejects     <- default
  t_lap <= 10900   keeps 48/55 (87%)   admits 0/5 rejects
  t_lap <= 11000   keeps 51/55 (93%)   admits 1/5 rejects

Default 10500 rather than the 87%-recall 10900: with only five adjudicated rejects, a threshold 60
units under the lowest of them is fitted to noise. 10500 leaves ~460 units of margin and still
keeps three-quarters. Recall is the cheap side of this trade — a missed frame costs one exemplar,
an admitted two-digit frame teaches blank_1 that a LIT cell is blank, which is the misreading that
caused the incident.

PREFER floor_sample OVER VIDEO. Single-character floors are ~11.7% of ch29's confident reads and
effectively only two floors (G 8686, 7 2393 over nine pre-incident days; everything else is a long
tail). A 25-minute clip yields ~9 usable frames; the 5-day floor_sample export yielded 55.

usage:
  find_single_char.py --crops /tmp/fs_ch29 --meta /tmp/fs_ch29/meta.csv --out /var/tmp/confirm
  find_single_char.py --video ch29_mon.mp4 --out /var/tmp/confirm     # fallback, far thinner
"""
import argparse
import collections
import csv
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_door as gd  # noqa: E402


def tens_edge_energy(gray_cell):
    """Variance of the Laplacian — edge energy. float64 in, because OpenCV 5 refuses a float32
    source against a CV_64F destination and the resulting error is not obviously about dtype."""
    return float(cv2.Laplacian(gray_cell.astype(np.float64), cv2.CV_64F).var())


def score(pg, cells, units_ctr, min_gap, max_tens_lap):
    """-> dict of measurements plus `ok`, or None if the panel is unusable. NO TEMPLATE IS READ."""
    u = gd.crop(pg, cells[2])
    t = gd.crop(pg, cells[1])
    if u.size == 0 or t.size == 0:
        return None
    uc = int(u.max()) - int(u.min())
    tc = int(t.max()) - int(t.min())
    lap = tens_edge_energy(t)
    return {"u_ctr": uc, "t_ctr": tc, "gap": uc - tc, "t_lap": round(lap, 1),
            "ok": uc >= units_ctr and (uc - tc) >= min_gap and lap <= max_tens_lap}


def main():
    ap = argparse.ArgumentParser(description="propose single-character panels for confirmation")
    ap.add_argument("--crops", default=None, help="dir of <id>.jpg (preferred source)")
    ap.add_argument("--meta", default=None, help="csv describing --crops (default <crops>/meta.csv)")
    ap.add_argument("--video", default=None, help="fallback source; far fewer usable frames")
    ap.add_argument("--roi", default=None)
    ap.add_argument("--cam", default="ch29")
    ap.add_argument("--units-ctr", type=int, default=150, dest="units_ctr")
    ap.add_argument("--min-gap", type=int, default=12, dest="min_gap")
    ap.add_argument("--max-tens-lap", type=float, default=10500.0, dest="max_tens_lap")
    ap.add_argument("--per-hour", type=int, default=3, dest="per_hour",
                    help="cap per capture-hour so one long dwell cannot fill the shortlist")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--exclude", default=None, help="json with an 'accept'/'reject' map of ids already judged")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if not (a.crops or a.video):
        raise SystemExit("pass --crops (preferred) or --video")

    roi = json.load(open(a.roi or f"{os.path.dirname(os.path.abspath(__file__))}/{a.cam}_roi.json"))
    cells = [tuple(x) for x in roi["cells"]["digit_cells"]]
    panel = tuple(roi["panel_rois"][0])
    seen = set()
    if a.exclude and os.path.exists(a.exclude):
        j = json.load(open(a.exclude))
        seen = {str(k) for k in list(j.get("accept", {})) + list(j.get("reject", {}))}
        print(f"  excluding {len(seen)} already-judged frames")

    cand = []
    if a.crops:
        meta = a.meta or os.path.join(a.crops, "meta.csv")
        rows = list(csv.DictReader(open(meta)))
        for r in rows:
            if str(r["id"]) in seen:
                continue
            fp = os.path.join(a.crops, f"{r['id']}.jpg")
            if not os.path.exists(fp):
                continue
            img = cv2.imdecode(np.frombuffer(open(fp, "rb").read(), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            m = score(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cells,
                      a.units_ctr, a.min_gap, a.max_tens_lap)
            if m and m["ok"]:
                cand.append((m, str(r["id"]), float(r["ts"]), fp, None))
        src = f"{a.crops} ({len(rows)} crops)"
    else:
        cap = cv2.VideoCapture(a.video)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            n += 1
            if n % 5:
                continue
            m = score(cv2.cvtColor(gd.crop(fr, panel), cv2.COLOR_BGR2GRAY), cells,
                      a.units_ctr, a.min_gap, a.max_tens_lap)
            if m and m["ok"]:
                cand.append((m, f"f{n}", n / fps, None, n))
        cap.release()
        src = f"{a.video} ({n} frames, every 5th)"

    cand.sort(key=lambda c: -c[0]["gap"])
    picked, per_hour = [], collections.Counter()
    for m, sid, ts, fp, frame in cand:
        hr = int((ts + 19800) // 3600) if a.crops else int(ts // 60)
        if per_hour[hr] >= a.per_hour:
            continue
        per_hour[hr] += 1
        picked.append((m, sid, ts, fp, frame))
        if len(picked) >= a.limit:
            break

    print(f"source {src}")
    print(f"gates  units_ctr>={a.units_ctr}  gap>={a.min_gap}  tens_edge<={a.max_tens_lap:g}")
    print(f"{len(cand)} candidates -> {len(picked)} selected (max {a.per_hour} per hour)\n")
    print(f"  {'when':>19} {'gap':>5} {'t_edge':>9} {'u_ctr':>6} {'t_ctr':>6}  id")
    for m, sid, ts, fp, frame in picked:
        when = (time.strftime('%m-%d %H:%M:%S', time.gmtime(ts + 19800)) if a.crops
                else f"{int(ts//60):02d}:{ts%60:05.2f}")
        print(f"  {when:>19} {m['gap']:5d} {m['t_lap']:9.0f} {m['u_ctr']:6d} {m['t_ctr']:6d}  {sid}")
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        cap = cv2.VideoCapture(a.video) if a.video else None
        for r, (m, sid, ts, fp, frame) in enumerate(picked):
            if fp:
                img = cv2.imdecode(np.frombuffer(open(fp, "rb").read(), np.uint8), cv2.IMREAD_COLOR)
            else:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame - 1)
                ok, fr = cap.read()
                img = gd.crop(fr, panel) if ok else None
            if img is None:
                continue
            cv2.imwrite(f"{a.out}/S_{r:02d}_id{sid}_gap{m['gap']}_edge{int(m['t_lap'])}.png",
                        cv2.resize(img, (img.shape[1] * 4, img.shape[0] * 4),
                                   interpolation=cv2.INTER_NEAREST))
        if cap:
            cap.release()
        print(f"\n  crops (4x) -> {a.out}")
    print("\n  Every frame here is a CANDIDATE. Read the panel, not the filename: the tool never "
          "names\n  the glyph, because the matcher that would name it is the one being repaired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
