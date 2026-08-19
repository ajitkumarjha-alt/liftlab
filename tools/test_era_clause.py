#!/usr/bin/env python3
"""The range predicate must select EXACTLY the rows `door_version LIKE prefix||'%'` selected.

WHY. Rewriting the era filter from LIKE to `>= p AND < p⁺` took /trends from 2.79s to 0.25s, but a
faster query that selects a different set is not an optimisation, it is a silent data change — and
this one touches every door-derived number on the dashboard: cycles, close travel, the funnel, the
floor table, RTT. A speedup is worth nothing without this test.

The two forms are NOT trivially identical. SQLite's LIKE is case-insensitive for ASCII, so
`LIKE 'abc%'` also matches 'ABC…'; the range form is case-sensitive. That difference is intended —
the prefix always comes from a door_version already in the table — but it must be VISIBLE, so this
reports any prefix where the two disagree rather than hiding it in a pass.

Runs against a real database when given one (the live copy is the interesting case), and against a
built fixture otherwise:
    python3 tools/test_era_clause.py --db /tmp/gw_copy.db
"""
import argparse
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Prefixes chosen to be nasty on purpose: shared stems, a case variant, a trailing-digit boundary,
# and the vestigial-levels-tag shape that made `LIKE prefix%` pool three live eras.
FIXTURE_VERSIONS = [
    "260d4a0f+495e8f48",
    "260d4a0fh2+495e8f48",
    "260d4a0fh2Laa52+495e8f48",
    "260d4a0fh3-stateL9f2T5471cb+495e8f48",
    "260d4a0fh3-stateT5471cb+495e8f48",
    "260d4a0FH3-STATET5471cb+495e8f48",   # case variant: LIKE folds it, the range does not
    "260d4a0g+495e8f48",                  # the byte AFTER 'f' — the upper bound must exclude it
    "260d4a0",                            # a strict prefix of the others, and a valid version
]


def build(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gw_door_event (id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT,"
                " cam TEXT, ts REAL, door_state TEXT, door_version TEXT)")
    for i, dv in enumerate(FIXTURE_VERSIONS):
        for k in range(7):
            con.execute("INSERT INTO gw_door_event (gateway_id,cam,ts,door_state,door_version) "
                        "VALUES ('site-A','ch29',?,?,?)", (1000.0 + i * 100 + k, "open", dv))
    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    a = ap.parse_args()

    tmp = None
    path = a.db
    if not path:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "gw.db")
        build(path)

    os.environ["GATEWAY_DB"] = path
    try:
        import dash_api  # noqa: F401
    except ModuleNotFoundError:
        from test_dash_occupancy import _stub
        _stub()
    import dash_api as D

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    pairs = db.execute("SELECT DISTINCT gateway_id, cam FROM gw_door_event "
                       "WHERE cam IS NOT NULL").fetchall()
    fails, checked = [], 0
    case_diffs = []

    print(f"db {path}")
    for gw, cam in pairs:
        vers = [r[0] for r in db.execute(
            "SELECT DISTINCT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? "
            "AND door_version IS NOT NULL AND door_version<>''", (gw, cam))]
        # Every prefix any consumer could resolve to: the part before '+', plus the full string.
        prefixes = sorted({v.split("+")[0] for v in vers} | set(vers))
        for p in prefixes:
            like = {r[0] for r in db.execute(
                "SELECT id FROM gw_door_event WHERE gateway_id=? AND cam=? AND door_version LIKE ?",
                (gw, cam, p + "%"))}
            frag, args = D._era_clause(p)
            rng = {r[0] for r in db.execute(
                f"SELECT id FROM gw_door_event WHERE gateway_id=? AND cam=?{frag}",
                (gw, cam, *args))}
            checked += 1
            if like == rng:
                continue
            # Disagreements are only acceptable when they are PURELY the case-folding difference:
            # every id in the symmetric difference must have a door_version that matches the prefix
            # case-insensitively but not case-sensitively.
            diff = like ^ rng
            qs = ",".join("?" * len(diff))
            bad = []
            for (dv,) in db.execute(
                    f"SELECT DISTINCT door_version FROM gw_door_event WHERE id IN ({qs})",
                    tuple(diff)):
                if not (dv.lower().startswith(p.lower()) and not dv.startswith(p)):
                    bad.append(dv)
            if bad:
                fails.append(f"{gw}/{cam} prefix {p!r}: LIKE={len(like)} range={len(rng)} rows and "
                             f"the difference is NOT case-folding — e.g. {bad[:3]}")
            else:
                case_diffs.append((gw, cam, p, len(like), len(rng)))

    db.close()
    print(f"  {len(pairs)} (gateway, cam) pair(s), {checked} era prefixes compared")

    if case_diffs:
        print("\n  CASE-FOLDING DIFFERENCES (intended: the range form is case-sensitive)")
        for gw, cam, p, nl, nr in case_diffs[:10]:
            print(f"    {gw}/{cam} {p!r}: LIKE {nl} rows, range {nr} rows")
        print("    LIKE pooled door_versions differing only in case. If any of those are REAL "
              "distinct\n    eras on this database, the old behaviour was pooling two instruments "
              "and the new one\n    is the correction — but look, because it changes numbers.")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — the range predicate selects exactly the LIKE row set on every era prefix "
          "(modulo the intended case-sensitivity, reported above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
