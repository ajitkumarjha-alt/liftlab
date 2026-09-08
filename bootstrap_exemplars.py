#!/usr/bin/env python3
"""Bootstrap a recalibration exemplar set from reads the OLD templates still get RIGHT.

THE PROBLEM THIS SOLVES. After the 2026-09-02 camera image-profile change
(INCIDENT_decode_regression_0902.md) every NCC score fell ~0.06 and the templates no longer
describe the image. The templates must be rebuilt against the CURRENT image — but a rebuild needs
LABELLED crops, and the reader that would label them is the degraded one. Labelling from its
output would bake today's misreads into tomorrow's templates: 'G' would be learned as '6G', and
ch29's phantom floor 67 (66 reads/day in August, 1,457 on 09-04) would be learned as a real floor.

THE WAY OUT IS THE MARGIN, NOT THE MEAN. Degradation did not make every read wrong; it moved the
whole distribution down until its lower tail crossed the gates. The reads in the UPPER tail are
still correct — merely closer to the edge than they used to be. So: score current-image crops with
the OLD templates and keep only those where EVERY glyph clears --min-score (default 0.85, i.e.
comfortably above the September mean of ~0.80 and inside the August range). Those crops carry the
new image and a label that the old instrument is still entitled to assert. Everything else is
rejected, counted, and reported by reason.

WHAT THIS DELIBERATELY WILL NOT DO.
  * It will not label a non-numeric floor. 'G' currently scores ~0.79-0.83 and cannot clear the
    bar; the lobby has to come from operator-confirmed windows instead (--lobby-from). Admitting
    it here on a lower threshold would be exactly the circularity the whole design avoids.
  * It will not accept a floor outside the camera's stored alphabet. Floor 67 on ch29 assembles
    from two glyphs that each score well; only the alphabet knows the tower has no 67 that busy.
  * It will not accept a read whose cells disagree with the emitted floor string, or one carrying
    a nonzero shift — a crop that only reads correctly after a shift search is not the fixed-pitch
    exemplar the builder assumes.

OUTPUT is exactly what door_calib.build_from_crops consumes, so the rebuild is the normal path:
    <out>/_calib_crop_NNNNN.png    the crop, unmodified pixels from floor_sample.crop_jpeg
    <out>/labels.json              {filename: label}
    <out>/labels_bind.json         {filename: sha16(png)} — the CONTENT binding
The bind file is not optional. build_from_crops excludes any label with no bind entry unless the
camera is on LABEL_BIND_LEGACY_CAMS, because filenames are reused and a re-collect can otherwise
re-attach old labels to new pixels (the 2026-07-30 ch16 label-inheritance postmortem). Writing it
here means these crops are content-verified from the moment they land.

TWO CROP SOURCES, ONE PATH. By default the crops come from floor_sample.crop_jpeg. --crops/--meta
reads them from an exported directory of <id>.jpg plus a csv instead, for running where the blobs
have been carried rather than where the DB is. The bytes are the same either way -- crop_jpeg IS
the panel crop and an export of it is a copy, not a rendering -- and everything after loading is
identical, so the file mode cannot drift into a second behaviour. The DB is STILL REQUIRED either
way: the pre-incident label gate is a question about gw_door_event and has no file equivalent.

MIND THE WINDOW when exporting. A SQL export written as `ts > '2026-09-03'` is a UTC midnight,
while --since is IST -- a 5.5 h difference at the start of the range. Both are safely after the
2026-09-02 17:50 IST cutover so neither contaminates the corpus with old-image crops, but the two
sources will not report the same candidate count and that is not a fault.

usage:
  bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --out /tmp/boot_ch29
  bootstrap_exemplars.py --cam ch29 --crops /tmp/fs_ch29 --meta /tmp/fs_ch29/meta.csv \
                         --since '2026-09-03' --min-score 0.80 --out /tmp/boot_ch29
  bootstrap_exemplars.py --cam ch29 --since '2026-09-03' --out /tmp/boot_ch29 --min-score 0.88
  bootstrap_exemplars.py --cam ch29 --dry-run            # census only, writes nothing
"""
import argparse
import collections
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_door as gd  # noqa: E402


def _epoch(s):
    """'2026-09-03' or '2026-09-03 17:50' in IST -> epoch. Naive input is IST because every window
    in this incident is quoted on the building's clock, and the report says so.

    timegm, NOT mktime. mktime reads the struct as the BOX's local time, so the same command would
    select a different set of crops on the UTC cloud VM and on an IST box — a silent 5h30m shift in
    which image era the exemplars come from. timegm is absolute: it reads the struct as UTC, and
    one subtraction turns that into "this wall time, in IST"."""
    import calendar
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s, fmt)) - 19800
        except ValueError:
            continue
    raise SystemExit(f"unparseable time {s!r} — use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (IST)")


def _ist(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S IST", time.gmtime(ts + 19800)) if ts else "?"


def load_geometry(cam, roi_path, panel_arg, cells_arg, arrow_arg):
    cells = arrow = None
    p = roi_path or f"{os.path.dirname(os.path.abspath(__file__))}/{cam}_roi.json"
    if os.path.exists(p):
        j = json.load(open(p))
        c = (j.get("cells") or {})
        cells = [tuple(x) for x in c.get("digit_cells") or []] or None
        arrow = tuple(c["arrow_cell"]) if c.get("arrow_cell") else None
    if cells_arg:
        cells = [tuple(int(v) for v in q.split(",")) for q in cells_arg.split(";") if q]
    if arrow_arg:
        arrow = tuple(int(v) for v in arrow_arg.split(","))
    if not (cells and arrow):
        raise SystemExit(
            f"no cell geometry for {cam}. floor_sample.crop_jpeg is already the PANEL crop, so a "
            f"panel ROI is not needed — but digit_cells/arrow_cell are. Pass --cells/--arrow, or "
            f"put {cam}_roi.json beside this script.")
    return cells, arrow


# ── the label gate ─────────────────────────────────────────────────────────────────────────────
# DO NOT USE THE STORED ALPHABET FOR THIS. It was the obvious choice and it is actively wrong here,
# because the alphabet is DERIVED FROM THE READS and the reads are what broke. Measured on ch29,
# stored row derived 2026-09-08:
#
#   floor  Aug 10-19   since Sep 3   stored verdict
#     10       7,039         4,064   quarantine:glyph_shadow_of_18 (31 impossible-speed flips)
#     21       6,250         3,600   quarantine:glyph_shadow_of_27 (59 flips)
#     77      18,075         4,955   quarantine:glyph_shadow_of_17 (95 flips)
#     67       1,293         7,742   ADMITTED (labeled)
#
# Every real floor there is being convicted by flip evidence the degradation manufactures — a
# single-glyph confusion at impossible speed is exactly what a reader produces when its scores sit
# on a gate. Meanwhile 67, up 6x on a camera whose total reads FELL, walks through because labeled
# floors bypass every quarantine rule. Gating on that set would drop real digits from the training
# corpus and admit the phantom: precisely inverted.
#
# THE GATE IS PRE-INCIDENT EVIDENCE, and it is a direct empirical claim rather than a derivation:
#   (a) the floor was read confidently at least --min-pre times BEFORE the cutover — this tower
#       has that floor, observed by an instrument that was working; and
#   (b) its per-day rate has not INFLATED since — a real floor's traffic did not change when the
#       image did, so a floor that got busier is the signature of a phantom being manufactured.
# Neither test needs labels.json, the stored alphabet, or anything the incident touched.
def _days_with_data(db, gw, cam, before, n_days):
    """The newest n_days IST days that actually carry confident reads before `before`.

    DAYS WITH DATA, NOT CALENDAR DAYS — the same trap the health check fell into. This gateway has
    NO rows at all between 2026-08-19 and 2026-09-02, so `cutover - 10*86400` lands entirely inside
    a 13-day ingest gap and the pre-window comes back nearly empty: the first version of this gate
    admitted 1 floor of 68 on ch29 and 0 of 1 on ch30, and would have rejected every real floor as
    "never observed". Reaching back over days that HAVE data is the only version of "before the
    incident" that survives an outage. -> (t_from, n_days_found)."""
    rows = db.execute(
        "SELECT CAST(strftime('%Y%m%d', ts + 19800, 'unixepoch') AS INTEGER) d, MIN(ts) lo, "
        "COUNT(*) n FROM gw_door_event WHERE gateway_id=? AND cam=? AND floor IS NOT NULL "
        "AND reason IN ('ok','single_panel') AND ts < ? GROUP BY d HAVING n > 0 ORDER BY d DESC "
        "LIMIT ?", (gw, cam, before, int(n_days))).fetchall()
    if not rows:
        return None, 0
    return min(r["lo"] for r in rows), len(rows)


def label_gate(db, gw, cam, cutover, pre_days, min_pre, max_inflation):
    """-> (admitted:set, report:dict). Floors this camera demonstrably had before the image
    changed, minus any whose rate inflated after it."""
    pre, post = {}, {}
    t_from, n_pre_days = _days_with_data(db, gw, cam, cutover, pre_days)
    if t_from is None:
        return None, {"n_pre_floors": 0, "admitted": [], "rejected": {},
                      "note": "no confident reads before the cutover at all — this gate cannot "
                              "run, and NOTHING should be bootstrapped for this camera"}
    q = ("SELECT floor f, COUNT(*) n FROM gw_door_event WHERE gateway_id=? AND cam=? "
         "AND floor IS NOT NULL AND reason IN ('ok','single_panel') AND ts>=? AND ts<? "
         "GROUP BY floor")
    for f, n in db.execute(q, (gw, cam, t_from, cutover)):
        pre[str(f)] = n
    for f, n in db.execute(q, (gw, cam, cutover, 9e18)):
        post[str(f)] = n
    # Rates per DAY-WITH-DATA on each side, so an outage on either side cannot fake an inflation.
    # COUNTED AFTER THE CUTOVER DIRECTLY, not as (all days - pre days): the camera also has history
    # older than the pre-window, so the subtraction over-counts the post side and divides the post
    # rate down. On ch29 that understated 67's inflation as 3.6x when it is ~12x — the test still
    # fired, but a weaker phantom would have slipped through a check that looked like it worked.
    n_post_days = max((db.execute(
        "SELECT COUNT(DISTINCT CAST(strftime('%Y%m%d', ts + 19800, 'unixepoch') AS INTEGER)) d "
        "FROM gw_door_event WHERE gateway_id=? AND cam=? AND floor IS NOT NULL "
        "AND reason IN ('ok','single_panel') AND ts >= ?",
        (gw, cam, cutover)).fetchone()["d"] or 0), 1)
    pre_days_n = max(n_pre_days, 1)
    admitted, rejected = set(), {}
    for f, n in sorted(pre.items()):
        if n < min_pre:
            rejected[f] = f"only {n} pre-incident reads (<{min_pre})"
            continue
        pre_rate = n / pre_days_n
        post_rate = post.get(f, 0) / n_post_days
        infl = (post_rate / pre_rate) if pre_rate else 0.0
        if infl > max_inflation:
            rejected[f] = (f"rate INFLATED {infl:.1f}x since the cutover "
                           f"({pre_rate:.0f}/day -> {post_rate:.0f}/day) — phantom signature")
            continue
        admitted.add(f)
    # A floor that appears ONLY after the cutover was never observed by a working instrument.
    for f in sorted(set(post) - set(pre)):
        rejected[f] = f"{post[f]} reads, NONE before the cutover — not an observed floor"
    return admitted, {"n_pre_floors": len(pre), "admitted": sorted(admitted),
                      "rejected": rejected, "pre_days_with_data": n_pre_days,
                      "post_days_with_data": n_post_days,
                      "pre_window_from_ist": _ist(t_from)}


def load_candidates(a, db, t0):
    """-> (list of {id, ts, blob}, source_description). Two sources, ONE downstream path.

    The crops are the same bytes either way -- floor_sample.crop_jpeg IS the panel crop, and an
    export of it is a copy, not a rendering. So the file source exists purely so this can run where
    the blobs have been carried to rather than where the DB is, and it must not become a second
    code path with its own behaviour: everything after this function is identical.

    WHAT IT WILL NOT DO IS INFER. A meta row whose JPEG is missing is COUNTED and reported, never
    skipped quietly -- a corpus silently short by the rows that failed to export is exactly the
    kind of gap that reads as "the camera had less data" months later.

    NOTE the gate still needs the DB. --crops changes where the CROPS come from; the pre-incident
    label gate is a question about gw_door_event and has no file equivalent.
    """
    if not a.crops:
        rows = db.execute("SELECT id, ts, crop_jpeg, floor, reason FROM floor_sample "
                          "WHERE gateway_id=? AND cam=? AND ts>=? AND crop_jpeg IS NOT NULL "
                          "ORDER BY ts", (a.gw, a.cam, t0)).fetchall()
        return ([{"id": r["id"], "ts": r["ts"], "blob": r["crop_jpeg"]} for r in rows],
                f"floor_sample in {a.db}")
    import csv as _csv
    meta = a.meta or os.path.join(a.crops, "meta.csv")
    if not os.path.exists(meta):
        raise SystemExit(f"no meta csv at {meta} -- pass --meta")
    out, n_before, n_nofile, n_badcam = [], 0, 0, 0
    with open(meta, newline="") as fh:
        for r in _csv.DictReader(fh):
            if (r.get("cam") or a.cam) != a.cam:
                n_badcam += 1
                continue
            try:
                ts = float(r["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts < t0:
                n_before += 1
                continue
            fp = os.path.join(a.crops, f"{r['id']}.jpg")
            if not os.path.exists(fp):
                n_nofile += 1
                continue
            out.append({"id": r["id"], "ts": ts, "blob": open(fp, "rb").read()})
    out.sort(key=lambda x: x["ts"])
    note = (f"{a.crops} + {os.path.basename(meta)}"
            + (f" [{n_before} before --since, excluded]" if n_before else "")
            + (f" [** {n_nofile} meta rows with NO jpg on disk **]" if n_nofile else "")
            + (f" [{n_badcam} rows for another camera]" if n_badcam else ""))
    if n_nofile:
        print(f"  ** WARNING: {n_nofile} meta rows have no matching .jpg. The export is INCOMPLETE; "
              f"this run sees a smaller corpus than the export claims.", flush=True)
    return out, note


def main():
    ap = argparse.ArgumentParser(description="bootstrap recalibration exemplars from high-margin reads")
    ap.add_argument("--gw", default=os.environ.get("GW", "site-A"))
    ap.add_argument("--cam", required=True)
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db"))
    ap.add_argument("--templates", default=None, help="the CURRENT live templates.npz (default <cam>_templates.npz)")
    ap.add_argument("--roi", default=None)
    ap.add_argument("--cells", default=None, help="x,y,w,h;... digit cells within the panel crop")
    ap.add_argument("--arrow", default=None, help="x,y,w,h arrow cell within the panel crop")
    ap.add_argument("--since", default="2026-09-03",
                    help="only crops after this IST time — must be AFTER the image change, or the "
                         "exemplars carry the old rendering (default 2026-09-03)")
    ap.add_argument("--min-score", type=float, default=0.85, dest="min_score")
    ap.add_argument("--cutover", default="2026-09-02 17:50",
                    help="IST instant the image changed — the pre/post boundary for the label gate")
    ap.add_argument("--pre-days", type=float, default=10.0, dest="pre_days",
                    help="days of PRE-cutover evidence defining what floors this tower has")
    ap.add_argument("--min-pre", type=int, default=200, dest="min_pre",
                    help="confident pre-cutover reads a floor needs to be a usable label")
    ap.add_argument("--max-inflation", type=float, default=2.0, dest="max_inflation",
                    help="reject a floor whose per-day rate rose more than this since the cutover")
    ap.add_argument("--max-per-glyph", type=int, default=40, dest="max_per_glyph",
                    help="cap exemplars per GLYPH so one busy floor cannot dominate the set")
    ap.add_argument("--crops", default=None,
                    help="directory of <id>.jpg panel crops (instead of reading floor_sample). "
                         "The DB is still required for the pre-incident label gate.")
    ap.add_argument("--meta", default=None,
                    help="csv with id,cam,ts,... describing --crops (default <crops>/meta.csv)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    a = ap.parse_args()

    tpl_path = a.templates or f"{os.path.dirname(os.path.abspath(__file__))}/{a.cam}_templates.npz"
    z = np.load(tpl_path)
    tpl = {k: z[k] for k in z.files}
    cells, arrow = load_geometry(a.cam, a.roi, None, a.cells, a.arrow)
    R = gd.FloorReader(tpl, cells, arrow)
    t0 = _epoch(a.since)

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cutover = _epoch(a.cutover)
    alpha, gate = label_gate(db, a.gw, a.cam, cutover, a.pre_days, a.min_pre, a.max_inflation)

    print(f"[bootstrap] {a.gw}/{a.cam}")
    print(f"  templates      {tpl_path}  hash={gd.templates_hash(tpl)[:16]}")
    print(f"  cells          {cells}  arrow={arrow}")
    print(f"  since          {_ist(t0)}  (crops before this carry the OLD image — excluded)")
    print(f"  min-score      {a.min_score}")
    print(f"  label gate     PRE-INCIDENT EVIDENCE, not the stored alphabet (see label_gate)")
    print(f"                 cutover {_ist(cutover)}, {gate.get('pre_days_with_data')} "
          f"day(s)-with-data before it (from {gate.get('pre_window_from_ist')}), "
          f"min {a.min_pre} reads, max {a.max_inflation:g}x inflation")
    print(f"                 admitted {len(alpha or ())} of {gate['n_pre_floors']} floors seen "
          f"pre-cutover")
    if alpha is None:
        raise SystemExit(f"  ABORT: {gate.get('note')}")
    _infl = {f: w for f, w in gate["rejected"].items() if "INFLATED" in w or "NONE before" in w}
    for f, w in sorted(_infl.items()):
        print(f"                 ** {f}: {w}")

    rows, src = load_candidates(a, db, t0)
    print(f"  crop source    {src}")
    print(f"  candidates     {len(rows)} crops\n")

    census = collections.Counter()
    per_glyph = collections.Counter()
    curve = []          # (min_cell_score, floor) for every gate-passing crop — see below
    accepted = []       # (id, ts, png_bytes, label)
    for r in rows:
        arr = cv2.imdecode(np.frombuffer(r["blob"], np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            census["undecodable crop"] += 1
            continue
        pg = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr
        out = R.read_panel(pg)
        floor, dbg = out.get("floor"), (out.get("cells") or [])
        if not floor:
            census[f"no floor ({out.get('status')})"] += 1
            continue
        if out.get("shift") not in ([0, 0], (0, 0), None):
            # A crop that only reads after a shift is not a fixed-pitch exemplar; learning from it
            # bakes the shift into the template.
            census["nonzero shift"] += 1
            continue
        if not floor.isdigit():
            # 'G'/'P1' cannot clear the bar in the current image and must not be admitted here.
            census[f"non-numeric ({floor}) — needs operator-confirmed windows"] += 1
            continue
        if alpha is not None and floor not in alpha:
            census[f"not a pre-incident floor ({floor})"] += 1
            continue
        # EVERY glyph-bearing cell must clear the bar — not the mean, and not just the weakest
        # named one. A two-digit floor with one strong and one marginal glyph teaches the marginal
        # one as if it were confirmed.
        scores = [c[0][1] for c in dbg
                  if c and c[0] and c[0][0] not in ("blank", "flat") and c[0][1] is not None]
        # THE YIELD CURVE, recorded for every crop that clears every gate EXCEPT the score. One
        # pass then answers "what threshold gives full digit coverage", instead of re-scoring 3,849
        # crops per candidate threshold. --min-score is a judgement call about how much label trust
        # to trade for corpus coverage, and it cannot be made without seeing the trade.
        if scores:
            curve.append((min(scores), floor))
        if not scores or min(scores) < a.min_score:
            census[f"below min-score (min {min(scores):.3f})" if scores else "no scored cell"] += 1
            continue
        glyphs = tuple(sorted(set(floor)))
        if all(per_glyph[g] >= a.max_per_glyph for g in glyphs):
            census["glyph quota full"] += 1
            continue
        for g in glyphs:
            per_glyph[g] += 1
        census[f"ACCEPTED {floor}"] += 1
        accepted.append((r["id"], r["ts"], r["blob"], floor))

    print("  ── census " + "─" * 60)
    for k, v in census.most_common():
        print(f"    {v:6d}  {k}")
    print(f"\n  accepted {len(accepted)} crops covering glyphs: "
          f"{dict(sorted(per_glyph.items()))}")
    missing = sorted(set("0123456789") - set(per_glyph))
    if missing:
        print(f"  ** NO EXEMPLAR for digit(s) {','.join(missing)} — the rebuild would LOSE them. "
              f"Widen --since, lower --min-score, or supply them from confirmed windows.")
    print(f"  ** the lobby 'G' is NOT in this set by design — supply it from operator-confirmed "
          f"windows before building.")

    # ── yield curve: coverage vs label trust ───────────────────────────────────────────────────
    print("\n  ── digit coverage by --min-score (gate-passing crops only) " + "─" * 12)
    print(f"    {'thresh':>7} {'crops':>6}  {'missing digits':<16} per-digit")
    for th in (0.90, 0.88, 0.85, 0.82, 0.80, 0.78, 0.75, 0.70, 0.65):
        pg = collections.Counter()
        n = 0
        for sc, fl in curve:
            if sc >= th:
                n += 1
                for g in set(fl):
                    pg[g] += 1
        miss = sorted(set("0123456789") - set(pg))
        thin = sorted(g for g in "0123456789" if 0 < pg[g] < 5)
        print(f"    {th:>7.2f} {n:>6}  {(','.join(miss) or 'none'):<16} "
              + " ".join(f"{g}:{pg[g]}" for g in sorted(pg))
              + (f"   [thin: {','.join(thin)}]" if thin else ""))
    print("    A threshold with a missing digit is unbuildable; one with a THIN digit (<5) builds a "
          "template from too few\n    exemplars to be robust. Pick the highest threshold that is "
          "neither, and supply the rest from confirmed windows.")

    if a.dry_run or not a.out:
        print("\n  dry run — nothing written." if a.dry_run else
              "\n  no --out given — nothing written.")
        return 0

    out = Path(a.out)
    (out).mkdir(parents=True, exist_ok=True)
    labels, bind = {}, {}
    for i, (sid, ts, blob, floor) in enumerate(accepted):
        name = f"_calib_crop_{i:05d}.png"
        arr = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        cv2.imwrite(str(out / name), arr)
        labels[name] = floor
        bind[name] = hashlib.sha256((out / name).read_bytes()).hexdigest()[:16]
    (out / "labels.json").write_text(json.dumps(labels, indent=2, sort_keys=True))
    (out / "labels_bind.json").write_text(json.dumps(bind, indent=2, sort_keys=True))
    # Provenance beside the crops: which instrument selected them, on what rule, from what window.
    (out / "_bootstrap_provenance.json").write_text(json.dumps({
        "gw": a.gw, "cam": a.cam, "built_at": time.time(), "built_at_ist": _ist(time.time()),
        "selected_by_templates_hash": gd.templates_hash(tpl), "templates_path": tpl_path,
        "min_score": a.min_score, "since_ist": _ist(t0), "cells": cells, "arrow_cell": arrow,
        "label_gate": gate, "cutover_ist": _ist(cutover),
        "gate_params": {"pre_days": a.pre_days, "min_pre": a.min_pre,
                        "max_inflation": a.max_inflation},
        "n_accepted": len(accepted), "n_candidates": len(rows), "crop_source": src,
        "per_glyph": dict(per_glyph), "census": dict(census),
        "note": "Exemplars carry the POST-2026-09-02 image and labels asserted by the PRE-change "
                "templates at a margin they still clear. Non-numeric floors are absent by design.",
    }, indent=2, sort_keys=True))
    print(f"\n  wrote {len(accepted)} crops + labels.json + labels_bind.json to {out}")
    print(f"  next: copy into the calib dir, add the lobby crops, then\n"
          f"        door_calib.py --build   (labels.json is picked up automatically)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
