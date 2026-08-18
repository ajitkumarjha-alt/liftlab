#!/usr/bin/env python3
"""Derive each camera's valid-floor whitelist FROM ITS OWN READS, and show it before anything applies.

WHY DERIVED, NOT TYPED. A hand-typed list encodes what someone believes the building has; this
encodes what the camera has actually resolved. The two differ in both directions — a floor the lift
serves but the reader never resolves would be typed in and then reject nothing, and a floor the
reader produces constantly but the building lacks is exactly what we are trying to exclude.

WHAT IT IS FOR. FloorReader already has `valid_floors` and already flags `off_alphabet`; the
mechanism is built and simply unconfigured, so today every assembled string is accepted as a good
read. Measured on ch16: 85 distinct floor strings over 19,424 attributed reads, 20.4% implausible by
shape — '1G' alone 2,902 rows — all carrying reason='single_panel'.

TWO FILTERS, IN THIS ORDER, because they answer different questions:
  SHAPE   is this string a floor at all? (numeric within the tower, or a known special)
  COUNT   has this camera resolved it often enough to be believed?

Shape does most of the work and is the honest one; count removes the long tail of one-off
assemblies. A real floor that is rarely visited can be lost to the count filter, which is the whole
reason this prints the list and applies nothing.

  python3 tools/derive_floor_alphabet.py --db /var/lib/liftlab/gateway.db
  python3 tools/derive_floor_alphabet.py --db ... --cam ch16 --min-count 20 --max-floor 78
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter

# The display's own vocabulary, per camera evidence. ch16 shows the P-series ('P4 OUT' verified on
# screen); ch29 produces P7 and an M prefix, both real possibilities in these towers. Admitted
# PROVISIONALLY and flagged — the MEP-02 lift schedule settles them.
SPECIALS = ("G", "LG", "UG", "B", "B1", "B2")
PARKING = re.compile(r"^P[1-9]$")                 # P1-P9, provisional above P5
MEZZ = re.compile(r"^M([1-9][0-9]?)?$")           # M, M1-M99 — an M PREFIX, provisional
NUMERIC = re.compile(r"^[1-9][0-9]?$")


def shape_ok(f, max_floor, prov_max):
    """(admit?, provisional_reason_or_'', rejection_reason).

    REJECT BY SHAPE ONLY. Count is not a discriminator: a rarely-visited real floor and a one-off
    misread produce the same number, so rejecting on frequency throws away real floors to remove
    noise it cannot identify. ch29's floor 1 (16 reads) and ch16's 48/38/42 are exactly that case.

    A REAL FLOOR THROWN AWAY IS WORSE THAN A MISREAD ADMITTED. Above the confirmed top floor but
    still plausible in shape, a value is ADMITTED and flagged rather than dropped: ch16's '79' has
    360 reads, which is not noise-shaped.
    """
    if f in SPECIALS:
        return True, "", ""
    if PARKING.match(f):
        return True, ("" if f <= "P5" else "parking level above P5 — confirm against schedule"), ""
    if MEZZ.match(f):
        return True, "M-prefix (mezzanine?) — confirm against schedule", ""
    if NUMERIC.match(f):
        n = int(f)
        if 1 <= n <= max_floor:
            return True, "", ""
        if n <= prov_max:
            return True, (f"above the confirmed top floor {max_floor} — confirm against schedule"), ""
        return False, "", f"numeric but outside 1-{prov_max}"
    return False, "", "not numeric and not a known special"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cam", action="append")
    ap.add_argument("--max-floor", type=int, default=78,
                    help="the CONFIRMED top floor; numerics above it are admitted as provisional")
    ap.add_argument("--prov-max", type=int, default=99,
                    help="numerics above this are rejected by shape (3-digit assemblies)")
    ap.add_argument("--era", default="", help="restrict to one door_version (full string)")
    ap.add_argument("--hours", type=float, default=0.0, help="0 = all history")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    cams = a.cam or [r[0] for r in db.execute(
        "SELECT DISTINCT cam FROM gw_door_event WHERE floor IS NOT NULL ORDER BY cam")]
    print("DERIVED FLOOR WHITELIST — proposed, NOT applied.")
    print(f"shape: 1-{a.max_floor} confirmed ({a.max_floor + 1}-{a.prov_max} provisional), "
          f"{', '.join(SPECIALS)}, P1-P9, M-prefix. REJECT BY SHAPE ONLY — never by count.\n")
    out = {}
    for cam in cams:
        w, args = "", [cam]
        if a.hours:
            import time as _t
            w += " AND ts >= ?"; args.append(_t.time() - a.hours * 3600)
        if a.era:
            w += " AND door_version = ?"; args.append(a.era)
        rows = db.execute("SELECT floor, COUNT(*) n FROM gw_door_event WHERE cam=? AND floor IS NOT "
                          "NULL" + w + " GROUP BY floor ORDER BY n DESC", args).fetchall()
        if not rows:
            print(f"=== {cam}: no attributed reads ===\n")
            continue
        tot = sum(r["n"] for r in rows)
        keep, by_shape, prov = [], [], []
        for r in rows:
            f, n = r["floor"], r["n"]
            ok, why_prov, why_rej = shape_ok(f, a.max_floor, a.prov_max)
            if not ok:
                by_shape.append((f, n, why_rej))
            else:
                keep.append((f, n))
                if why_prov:
                    prov.append((f, n, why_prov))

        def _key(f):
            return (0, int(f)) if NUMERIC.match(f) else (1, f)

        alphabet = sorted((f for f, _n in keep), key=_key)
        rej_shape = sum(n for _f, n, _w in by_shape)
        print(f"=== {cam} — {len(rows)} distinct strings over {tot} attributed reads ===")
        print(f"  WHITELIST ({len(alphabet)}): {','.join(alphabet)}")
        print(f"  would flag off_alphabet: {rej_shape} reads "
              f"({100.0 * rej_shape / tot:.1f}%) — ALL by shape; nothing rejected by count")
        if by_shape:
            print("  rejected BY SHAPE (the garbage assemblies):")
            for f, n, why in by_shape[:8]:
                print(f"    {f!r:8s} {n:>6}  {why}")
            if len(by_shape) > 8:
                print(f"    … and {len(by_shape) - 8} more, {rej_shape - sum(n for _f, n, _w in by_shape[:8])} reads")
        if prov:
            print("  ADMITTED BUT PROVISIONAL — CONFIRM AGAINST THE MEP-02 LIFT SCHEDULE:")
            for f, n, why in prov[:10]:
                print(f"    {f!r:8s} {n:>6}  {why}")
            if len(prov) > 10:
                print(f"    … and {len(prov) - 10} more provisional entries")
        # PATTERN CHECK ON THE M-PREFIX. One or two mezzanines is a building; fifteen, with their
        # numeric parts scattered across the whole tower, is a LEADING-CELL MISREAD wearing a
        # plausible shape. The entries are still admitted per the decision — a real floor thrown
        # away is worse — but the shape of the set is evidence and belongs on the page, not in
        # someone's head when they reach the schedule.
        mez = [(f, n) for f, n, _w in prov if MEZZ.match(f) and len(f) > 1]
        if len(mez) >= 3:
            nums = sorted(int(f[1:]) for f, _n in mez)
            print(f"    ^ NOTE: {len(mez)} M-prefix entries spanning M{nums[0]}-M{nums[-1]}, "
                  f"{sum(n for _f, n in mez)} reads total. A tower has one or two mezzanines, not "
                  f"{len(mez)} scattered across its full height — this pattern is what a misread of "
                  f"the LEADING CELL looks like. Admitted as decided; treat the schedule check as "
                  f"the one that matters.")
        print()
        out[cam] = alphabet
    print("REGISTRY VALUES, once you have checked the lists above:")
    for cam, al in out.items():
        print(f'  {cam}: {{"floor_alphabet": "{",".join(al)}"}}')
    print("\nNothing has been applied. Every rejection is by SHAPE; nothing was dropped for being")
    print("rare, because a rarely-visited real floor and a one-off misread produce the same count.")
    print("The PROVISIONAL entries above are admitted deliberately — a real floor thrown away is")
    print("worse than a misread admitted — and want the MEP-02 lift schedule to settle them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
