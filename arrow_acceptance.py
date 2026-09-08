#!/usr/bin/env python3
"""Arrow acceptance: does the reader READ, and does it read STABLY? Both, or neither counts.

WHY THIS FILE EXISTS, in one paragraph. On 2026-08-13 a51b5a6 ("arrow reader: margin +
hysteresis") replaced the stack-aware `_match` with `_match2`, which passed a (k,H,W) exemplar
stack to cv2.matchTemplate, threw, and was swallowed by `except Exception: continue`. The reader
stopped emitting direction on every camera that hour and did not emit another for 26 days. It was
ACCEPTED because the acceptance criterion was "953 direction changes fell to 2" -- a STABILITY
measure, and a reader that emits nothing at all is perfectly stable. The criterion could not
distinguish the fix working from the reader dying, and it chose wrong.

So this measures three things and requires all three, because no two of them can tell those apart:

  1. VOLUME    non-null direction reads per hour, per camera. A silent reader scores 0 here and
               cannot hide behind a good stability number.
  2. SPLIT     up vs down. A reader stuck on one arrow scores well on volume AND on stability --
               ch27 and ch30 genuinely only HAVE one arrow template, so their split is expected to
               be one-sided and that is stated rather than failed.
  3. FLIP RATE direction changes as a fraction of consecutive direction-bearing reads. This is the
               number a51b5a6 was actually trying to reduce, and it is still the right thing to
               want -- just never on its own.

A camera PASSES when volume > 0 AND flip rate is below --max-flip. Volume alone is a reader that
flaps; flip rate alone is the 2026-08-13 mistake.

usage:
  arrow_acceptance.py --window 6                     # last 6 h, every camera
  arrow_acceptance.py --window 6 --label BEFORE      # tag the run
  arrow_acceptance.py --since '2026-09-08 14:00'     # explicit IST start
"""
import argparse
import calendar
import json
import os
import sqlite3
import statistics
import sys
import time

_IST = 19800


def _epoch(s):
    """IST wall time -> epoch, timezone-independent (timegm, not mktime -- see
    bootstrap_exemplars._epoch for why that distinction has already bitten once)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s, fmt)) - _IST
        except ValueError:
            continue
    raise SystemExit(f"unparseable time {s!r} — use 'YYYY-MM-DD [HH:MM]' (IST)")


def _ist(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S IST", time.gmtime(ts + _IST)) if ts else "?"


def measure(db, t0, t1, cams=None):
    """-> {cam: metrics}. Reads the rows once per camera, in ts order, so the flip count is over
    CONSECUTIVE direction-bearing reads rather than a self-join."""
    where = "ts >= ? AND ts < ?"
    args = [t0, t1]
    if cams:
        where += f" AND cam IN ({','.join('?' * len(cams))})"
        args += list(cams)
    rows = db.execute(f"SELECT cam, ts, direction, n_arrow_labels FROM gw_door_event "
                      f"WHERE {where} ORDER BY cam, ts", args).fetchall()
    per = {}
    for r in rows:
        d = per.setdefault(r["cam"], {"rows": 0, "dir_rows": 0, "up": 0, "down": 0,
                                      "other": 0, "flips": 0, "_prev": None,
                                      "n_arrow_labels": set()})
        d["rows"] += 1
        if r["n_arrow_labels"] is not None:
            d["n_arrow_labels"].add(r["n_arrow_labels"])
        v = (r["direction"] or "").strip()
        if not v:
            continue                      # a null direction breaks no run: it is absence of a read,
                                          # not a reading of "no direction"
        d["dir_rows"] += 1
        if v == "up":
            d["up"] += 1
        elif v == "down":
            d["down"] += 1
        else:
            d["other"] += 1
        if d["_prev"] is not None and v != d["_prev"]:
            d["flips"] += 1
        d["_prev"] = v
    hours = max((t1 - t0) / 3600.0, 1e-9)
    for cam, d in per.items():
        d.pop("_prev", None)
        d["n_arrow_labels"] = sorted(x for x in d["n_arrow_labels"] if x is not None)
        d["hours"] = round(hours, 3)
        d["rows_per_h"] = round(d["rows"] / hours, 1)
        d["dir_per_h"] = round(d["dir_rows"] / hours, 2)
        d["dir_share_pct"] = round(100.0 * d["dir_rows"] / d["rows"], 2) if d["rows"] else 0.0
        # Flip rate is over TRANSITIONS between direction-bearing reads, so its denominator is
        # dir_rows-1, not dir_rows. With one read there is no transition and the rate is undefined
        # -- reported as None rather than 0, which would read as "perfectly stable".
        d["flip_rate"] = (round(d["flips"] / (d["dir_rows"] - 1), 4)
                          if d["dir_rows"] > 1 else None)
        tot = d["up"] + d["down"]
        d["up_pct"] = round(100.0 * d["up"] / tot, 1) if tot else None
    return per


def arrow_capability(templates_dir):
    """-> {cam: [arrow labels it has templates for]}. A camera cannot emit a direction it has no
    template for, and gpu_analyze WITHHOLDS direction entirely below two arrow labels (f3ed8e5,
    "withhold direction when the reader knows one arrow"). So ch27 (down only), ch30 (up only) and
    the door-only cameras are expected to stay at zero after any arrow fix, and scoring them FAIL
    would turn a correct deploy into a 1-of-7 that reads like a partial failure. Measured from the
    template files themselves rather than assumed.

    NO NUMPY. An .npz is a zip of .npy members, and the member NAMES are all this needs -- reading
    them with zipfile means this runs on the gateway, which has no numpy, instead of silently
    degrading there. It raises rather than returning {} when it cannot answer: a capability table
    that quietly turns into "no information" would score ch27/ch30 as FAIL and report a correct
    deploy as 1-of-7, which is the same silent-degradation trap this whole exercise is about."""
    import zipfile
    cap = {}
    if not templates_dir:
        return cap
    if not os.path.isdir(templates_dir):
        raise SystemExit(f"--templates-dir {templates_dir!r} is not a directory. Omit it, or point "
                         f"it at the <cam>.npz store — silently ignoring it would mis-score every "
                         f"one-arrow camera as a failure.")
    for fn in sorted(os.listdir(templates_dir)):
        if not fn.endswith(".npz"):
            continue
        path = os.path.join(templates_dir, fn)
        try:
            with zipfile.ZipFile(path) as z:
                names = {n[:-4] if n.endswith(".npy") else n for n in z.namelist()}
        except Exception as e:
            raise SystemExit(f"cannot read {path}: {type(e).__name__}: {e}. Refusing to guess "
                             f"arrow capability.")
        cap[fn[:-4]] = sorted(k for k in names if k in ("up", "down"))
    if not cap:
        raise SystemExit(f"--templates-dir {templates_dir!r} holds no .npz files.")
    return cap


def verdict(d, max_flip, min_per_h, arrows=None):
    # EXPECTATION FIRST. A camera with fewer than two arrow templates is not capable of emitting a
    # direction at all; calling that a failure of an arrow fix is the same category error as the
    # 2026-08-13 acceptance, just pointed the other way.
    if arrows is not None and len(arrows) < 2:
        return "N/A", (f"only {arrows or 'no'} arrow template(s) — direction is withheld at source "
                       f"below two, so zero is CORRECT here and says nothing about the fix")
    if d["dir_rows"] == 0:
        return "FAIL", "SILENT — zero direction reads. This is the 2026-08-13 state."
    why = []
    ok = True
    if d["dir_per_h"] < min_per_h:
        ok = False
        why.append(f"only {d['dir_per_h']}/h (< {min_per_h})")
    if d["flip_rate"] is not None and d["flip_rate"] > max_flip:
        ok = False
        why.append(f"flip rate {d['flip_rate']} (> {max_flip}) — flapping")
    if d["up"] == 0 or d["down"] == 0:
        # NOT a failure by itself: ch27 has only a 'down' template and ch30 only 'up', so a
        # one-sided split is the honest output of a one-arrow reader. Say which it is.
        why.append(f"one-sided split (up={d['up']} down={d['down']}) — expected if this camera "
                   f"has one arrow template, a defect if it has two")
    return ("PASS" if ok else "FAIL"), "; ".join(why) or "volume and stability both in range"


def main():
    ap = argparse.ArgumentParser(description="arrow acceptance: reads AND stability, fleet-wide")
    ap.add_argument("--db", default=os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db"))
    ap.add_argument("--window", type=float, default=6.0, help="hours back from --until")
    ap.add_argument("--since", default=None, help="explicit IST start (overrides --window)")
    ap.add_argument("--until", default=None, help="explicit IST end (default: newest row)")
    ap.add_argument("--cams", default=None, help="comma list (default: every camera present)")
    ap.add_argument("--max-flip", type=float, default=0.35, dest="max_flip")
    ap.add_argument("--min-per-h", type=float, default=1.0, dest="min_per_h")
    ap.add_argument("--templates-dir", default=None, dest="templates_dir",
                    help="dir of <cam>.npz — cameras with <2 arrow templates are scored N/A, "
                         "not FAIL (direction is withheld at source below two)")
    ap.add_argument("--label", default=None, help="tag this run, e.g. BEFORE / AFTER")
    ap.add_argument("--json", default=None, help="also write the metrics here")
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    newest = db.execute("SELECT MAX(ts) m FROM gw_door_event").fetchone()["m"] or time.time()
    t1 = _epoch(a.until) if a.until else newest
    t0 = _epoch(a.since) if a.since else (t1 - a.window * 3600.0)
    cams = [c.strip() for c in a.cams.split(",")] if a.cams else None

    print(f"── arrow acceptance{' [' + a.label + ']' if a.label else ''} " + "─" * 40)
    print(f"  db      {a.db}")
    print(f"  window  {_ist(t0)}  ->  {_ist(t1)}   ({(t1 - t0) / 3600:.2f} h)")
    print(f"  newest row in db: {_ist(newest)}"
          + ("   ** the db is a replica; rows after its restore are not here yet **"
             if newest < time.time() - 900 else ""))
    print(f"  pass    dir_per_h >= {a.min_per_h} AND flip_rate <= {a.max_flip}\n")
    cap = arrow_capability(a.templates_dir)
    if cap:
        print(f"  arrow templates per camera (from {a.templates_dir}):")
        for c in sorted(cap):
            print(f"    {c}: {cap[c] or 'none'}"
                  + ("" if len(cap[c]) >= 2 else "   -> cannot emit direction, scored N/A"))
        print()
    per = measure(db, t0, t1, cams)
    if not per:
        raise SystemExit("  no rows in this window at all — nothing to accept or reject.")
    print(f"  {'cam':6} {'rows/h':>8} {'dir/h':>8} {'dir%':>6} {'up':>6} {'down':>6} "
          f"{'up%':>6} {'flips':>6} {'flip_rate':>9}  verdict")
    n_fail = n_na = 0
    for cam in sorted(per):
        d = per[cam]
        v, why = verdict(d, a.max_flip, a.min_per_h, cap.get(cam))
        n_fail += (v == "FAIL")
        n_na += (v == "N/A")
        print(f"  {cam:6} {d['rows_per_h']:>8} {d['dir_per_h']:>8} {d['dir_share_pct']:>6} "
              f"{d['up']:>6} {d['down']:>6} {str(d['up_pct']):>6} {d['flips']:>6} "
              f"{str(d['flip_rate']):>9}  {v}")
        print(f"         {why}")
    n_judged = len(per) - n_na
    print(f"\n  {n_judged - n_fail}/{n_judged} judged cameras pass"
          + (f"  ({n_na} scored N/A — fewer than two arrow templates)" if n_na else "") + ".")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"label": a.label, "t0": t0, "t1": t1, "t0_ist": _ist(t0),
                       "t1_ist": _ist(t1), "cams": per}, fh, indent=2, sort_keys=True)
        print(f"  metrics written to {a.json}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
