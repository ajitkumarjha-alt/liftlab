#!/usr/bin/env python3
"""Occupancy columns at the gateway: BOTH ingest paths, plus a static arity check on every INSERT.

TWO KINDS OF CHECK, DELIBERATELY.

1. STATIC ARITY. Every `INSERT INTO validation_item` in validation_api.py is parsed out of the AST and
   its column count, placeholder count and VALUES-tuple length are compared. This is the check that
   catches an insert extended in three places and finished in two — the exact shape of the floor_age_s
   off-by-one, and of the half-applied edit that produced 20 placeholders against 16 values here. It
   needs no database and no request, so it cannot be dodged by a test that only exercises one path.

2. BOTH PATHS RUN. `validation_item` has two inserts — 'auto' for a LIVE camera and the reviewable
   row for a VALIDATING one — and the occupancy fields go into both. Per the lesson recorded in
   INCIDENT_live_episode_gate.md ("a gate must be tested in every mode it fires in"), each mode is
   exercised against a real SQLite file, not just the one the feature was written against.

THE NULL RULE IS ASSERTED, NOT ASSUMED. A worker that predates occupancy sends no occupancy fields.
Those must land as NULL. A 0 would read downstream as "an empty cabin was measured" and would drag
every fleet aggregate toward zero with fabricated observations.
"""
import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _stub_web_framework():
    """Minimal fastapi/starlette stand-ins so this runs on a box without the web stack installed.

    The gateway modules import fastapi at module scope; the analysis box does not have it. Stubbing is
    honest here because nothing under test is framework behaviour — the handlers are plain async
    functions and the assertions are about SQL and coercion.
    """
    try:
        import fastapi  # noqa: F401
        from starlette.concurrency import run_in_threadpool  # noqa: F401
        return
    except Exception:
        pass

    fa = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            super().__init__(f"{status_code} {detail}")
            self.status_code, self.detail = status_code, detail

    class APIRouter:
        def __init__(self, *a, **k):
            pass

        def _noop(self, *a, **k):
            return lambda f: f

        get = post = put = delete = patch = _noop

    class Request:
        pass

    class UploadFile:
        pass

    fa.APIRouter, fa.HTTPException, fa.Request, fa.UploadFile = APIRouter, HTTPException, Request, UploadFile
    fa.Header = lambda default="", **k: default
    fa.Form = lambda default=None, **k: default
    fa.File = lambda default=None, **k: default
    fa.Query = lambda default=None, **k: default
    resp = types.ModuleType("fastapi.responses")
    for nm in ("HTMLResponse", "JSONResponse", "PlainTextResponse", "FileResponse",
               "StreamingResponse", "RedirectResponse", "Response"):
        setattr(resp, nm, type(nm, (), {"__init__": lambda s, *a, **k: None}))
    fa.responses = resp
    sys.modules["fastapi"] = fa
    sys.modules["fastapi.responses"] = resp

    st = types.ModuleType("starlette")
    conc = types.ModuleType("starlette.concurrency")

    async def run_in_threadpool(fn, *a, **k):
        return fn(*a, **k)

    conc.run_in_threadpool = run_in_threadpool
    st.concurrency = conc
    sys.modules["starlette"], sys.modules["starlette.concurrency"] = st, conc


def check_insert_arity(path, table):
    """Column count == placeholder count == len(VALUES tuple), for every insert into `table`."""
    out = []
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and len(node.args) >= 1):
            continue
        sql = node.args[0]
        if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
            continue
        s = " ".join(sql.value.split())
        m = re.match(rf"INSERT(?: OR \w+)? INTO {table}\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*$", s, re.I)
        if not m:
            continue
        cols = [c.strip() for c in m.group(1).split(",") if c.strip()]
        vals_sql = [v.strip() for v in m.group(2).split(",") if v.strip()]
        n_ph = sum(1 for v in vals_sql if v == "?")          # literals in VALUES take no tuple slot
        n_tuple = None
        if len(node.args) >= 2 and isinstance(node.args[1], (ast.Tuple, ast.List)):
            n_tuple = len(node.args[1].elts)
        out.append({"line": node.lineno, "cols": len(cols), "vals_sql": len(vals_sql),
                    "placeholders": n_ph, "tuple": n_tuple, "col_names": cols})
    return out


def main():
    fails = []
    _stub_web_framework()

    # ---------------- 1. static arity over every validation_item insert ----------------
    print("=== static arity: INSERT INTO validation_item ===")
    rows = check_insert_arity(os.path.join(ROOT, "validation_api.py"), "validation_item")
    if not rows:
        fails.append("no INSERT INTO validation_item found — the check is not looking at anything")
    for r in rows:
        ok = (r["cols"] == r["vals_sql"] and r["placeholders"] == r["tuple"])
        print(f"  line {r['line']:>4}: {r['cols']} cols / {r['vals_sql']} values "
              f"({r['placeholders']} placeholders) / tuple {r['tuple']}  {'OK' if ok else 'MISMATCH'}")
        if r["cols"] != r["vals_sql"]:
            fails.append(f"line {r['line']}: {r['cols']} columns but {r['vals_sql']} VALUES entries")
        if r["placeholders"] != r["tuple"]:
            fails.append(f"line {r['line']}: {r['placeholders']} placeholders but {r['tuple']} values supplied")
        missing = [c for c in ("occupancy_max", "occupancy_frames", "occupancy_degraded",
                               "analysed_frames") if c not in r["col_names"]]
        if missing:
            fails.append(f"line {r['line']}: occupancy fields absent from this path: {missing}")

    # ---------------- 2. both ingest paths, real sqlite ----------------
    tmp = tempfile.mkdtemp()
    os.environ["GATEWAY_DB"] = os.path.join(tmp, "gw.db")
    os.environ.setdefault("VALIDATION_IMG_DIR", os.path.join(tmp, "img"))
    import validation_api as V
    V.GATEWAY_TOKENS["site-A"] = "tok"

    db = V._db()
    cols = {r[1] for r in db.execute("PRAGMA table_info(validation_item)")}
    for c in ("occupancy_max", "occupancy_frames", "occupancy_degraded", "analysed_frames",
              "human_occupancy"):
        if c not in cols:
            fails.append(f"migration did not add {c}")
    db.close()

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    def post(body):
        return asyncio.run(V.validation_item("site-A", "ch30", Req(body), "Bearer tok"))

    def last(*fields):
        con = sqlite3.connect(os.environ["GATEWAY_DB"])
        try:
            return con.execute(f"SELECT {','.join(fields)} FROM validation_item "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            con.close()

    base = {"ts_start": 1.0, "ts_end": 19.0, "machine_boarded": 2, "machine_alighted": 0,
            "counting_version": "v1", "frames": 140}
    occ = {"occupancy_max": 3, "occupancy_frames": 140, "occupancy_degraded": 0,
           "analysed_frames": 140}
    OCC_COLS = ("status", "occupancy_max", "occupancy_frames", "occupancy_degraded", "analysed_frames")

    print("\n=== path A: camera LIVE -> 'auto' row ===")
    con = sqlite3.connect(os.environ["GATEWAY_DB"])
    # counting_version must equal CURRENT_COUNTING_VERSION. A live row with NULL/stale version is
    # REOPENED to validating by _reopen_if_stale — correct behaviour, and it silently converts this
    # into a second test of path B if left unset.
    con.execute("INSERT INTO camera_validation (gateway_id,cam,state,counting_version) "
                "VALUES ('site-A','ch30','live',?)", (V.CURRENT_COUNTING_VERSION,))
    con.commit(); con.close()
    post({**base, **occ})
    got = last(*OCC_COLS)
    print(f"  {OCC_COLS} = {got}")
    if got[0] != "auto":
        fails.append(f"live camera did not take the auto path (status={got[0]})")
    if tuple(got[1:]) != (3, 140, 0, 140):
        fails.append(f"auto path lost the occupancy fields: {got[1:]}")

    print("  legacy worker (no occupancy fields at all):")
    post(base)
    got = last(*OCC_COLS)
    print(f"  {OCC_COLS} = {got}")
    if any(v is not None for v in got[1:]):
        fails.append(f"missing occupancy became non-NULL on the auto path: {got[1:]} "
                     "— a 0 here claims an empty cabin was measured")

    print("\n=== path B: camera VALIDATING -> reviewable row ===")
    con = sqlite3.connect(os.environ["GATEWAY_DB"])
    con.execute("UPDATE camera_validation SET state='validating' WHERE cam='ch30'")
    con.commit(); con.close()
    post({**base, **occ, "det_max": 5, "det_mean": 3.2, "distinct_ids": 6, "det_frames": 140,
          "conf_min": 0.31, "conf_mean": 0.62, "conf_max": 0.91, "occupancy_degraded": 1})
    got = last(*OCC_COLS)
    print(f"  {OCC_COLS} = {got}")
    if got[0] == "auto":
        fails.append("validating camera took the auto path")
    if tuple(got[1:]) != (3, 140, 1, 140):
        fails.append(f"validating path lost the occupancy fields: {got[1:]}")
    det = last("det_max", "distinct_ids", "conf_mean")
    if det != (5, 6, 0.62):
        fails.append(f"validating path corrupted the detection audit alongside occupancy: {det}")

    print("  legacy worker (no occupancy fields at all):")
    post({**base, "det_max": 5, "det_frames": 140})
    got = last(*OCC_COLS)
    print(f"  {OCC_COLS} = {got}")
    if any(v is not None for v in got[1:]):
        fails.append(f"missing occupancy became non-NULL on the validating path: {got[1:]}")

    print("\n=== _i(): garbage coerces to NULL, never to 0 ===")
    for v, want in ((None, None), ("", None), ("abc", None), ("4", 4), (3.9, 3), (0, 0), (7, 7)):
        got_i = V._i(v)
        print(f"  _i({v!r}) = {got_i!r}")
        if got_i != want:
            fails.append(f"_i({v!r}) = {got_i!r}, expected {want!r}")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print(f"OK — {len(rows)} inserts arity-checked, both ingest paths carry occupancy, "
          "missing fields stay NULL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
