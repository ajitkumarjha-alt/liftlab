#!/usr/bin/env python3
"""The precompute sweep survives a contended database, and its bookkeeping cannot kill it.

THE FAILURE, 2026-09-07. The sweep died 5m41s in with OperationalError("database is locked") —
first in every study stage, then FATALLY in precompute_run_record, which took the whole job down
after most of its work had succeeded. The fleet had been restarted 30 minutes earlier and seven
workers were posting their backlog.

Three causes, and only the first is about SQLite:

  1. dash_api._db() SET NO BUSY TIMEOUT AT ALL. The default is 0: a writer that meets a held lock
     gives up on contact. Under WAL there is exactly one writer at a time, so a burst of ingest is
     enough. door_event_api already carries this exact reasoning beside its own PRAGMA — the dash
     connection was simply never given one.
  2. busy_timeout only covers the wait INSIDE one statement. A stage that loses the race for the
     whole timeout still raises, and nothing retried it, so a transient became a failed stage.
  3. THE RECORD OF THE RUN COULD DESTROY THE RUN. precompute_run_record met the same lock and
     raised, and the sweep's completed work went unreported because the bookkeeping failed.

What is asserted here is behaviour under a REAL held lock, not a mocked one: a second connection
takes the write lock with BEGIN IMMEDIATE and lets go on a timer, which is exactly the shape of a
busy ingest path.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import shutil
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
IST = timezone(timedelta(hours=5, minutes=30))


def _hold_write_lock(path, seconds, started):
    """Take the single WAL write lock and hold it, the way a burst of ingest does."""
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("BEGIN IMMEDIATE")
    con.execute("CREATE TABLE IF NOT EXISTS _lockprobe (x INTEGER)")
    con.execute("INSERT INTO _lockprobe VALUES (1)")
    started.set()
    time.sleep(seconds)
    con.rollback()
    con.close()


def main():
    fails = []
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "gw.db")
    from test_dash_occupancy import _stub
    _stub()
    from test_study_views import build
    build(path, datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
          - timedelta(days=3))
    # WAL, as the live gateway runs (litestream requires it).
    c = sqlite3.connect(path)
    print("  journal_mode ->", c.execute("PRAGMA journal_mode=WAL").fetchone()[0])
    c.close()
    os.environ["GATEWAY_DB"] = path
    import dash_api as D
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pj", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "precompute_job.py"))
    PJ = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(PJ)

    # ══ 1. the connection carries a busy timeout at all, and the timer's is longer ═══════
    print("\n=== 1. busy_timeout ===")
    a, b = D._db(), D._db(busy_ms=D.PRECOMPUTE_BUSY_MS)
    ta = a.execute("PRAGMA busy_timeout").fetchone()[0]
    tb = b.execute("PRAGMA busy_timeout").fetchone()[0]
    a.close(); b.close()
    print(f"  request path {ta}ms · timer {tb}ms · data budget {D.DATA_BUDGET_S:g}s")
    if not ta:
        fails.append("the dash connection still has NO busy timeout — a writer that meets a held "
                     "lock gives up on contact, which is how the sweep died")
    if tb < ta:
        fails.append("the timer waits less than a request does, which is backwards: nobody is "
                     "waiting on the timer and a user is waiting on the request")
    # A BUSY WAIT SLEEPS INSIDE SQLITE, where the request budget's progress handler does not fire.
    # A request-path timeout longer than the budget would silently outlast the thing bounding it.
    if ta / 1000.0 >= D.DATA_BUDGET_S:
        fails.append(f"the request-path busy timeout ({ta}ms) is not inside the request budget "
                     f"({D.DATA_BUDGET_S:g}s) — a busy wait sleeps inside SQLite where the budget's "
                     f"progress handler never fires, so the request would outlast its own bound")

    # ══ 2. is_locked discriminates — a real bug must not be retried forever ══════════════
    print("\n=== 2. only a LOCK is retried ===")
    db = D._db(busy_ms=50)
    try:
        db.execute("SELECT * FROM definitely_not_a_table")
    except Exception as e:
        missing = e
    print(f"  missing table -> is_locked={D.is_locked(missing)} (must be False)")
    if D.is_locked(missing):
        fails.append("a missing table is treated as a lock — a bug would be retried three times "
                     "and its message buried under three copies")
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: whatever")
    _r, err = PJ.run_stage("boom", boom)
    print(f"  a non-lock error was attempted {calls['n']}x (must be 1) and returned {type(err).__name__}")
    if calls["n"] != 1:
        fails.append(f"a non-lock error was retried {calls['n']}x — it reproduces exactly, so "
                     f"retrying burns the timer and triples the noise")
    if err is None:
        fails.append("a failing stage reported success")

    # ══ 3. a REAL held lock is waited out, and the stage then succeeds ═══════════════════
    print("\n=== 3. a held write lock is retried, not failed ===")
    PJ.RETRY_DELAYS = [1.0, 2.0, 4.0]
    PJ.LOCK_STATS.update({"n_locked": 0, "lock_wait_s": 0.0})
    started = threading.Event()
    # Held for longer than the connection will wait, so the FIRST attempt genuinely fails.
    t = threading.Thread(target=_hold_write_lock, args=(path, 2.5, started), daemon=True)
    t.start()
    started.wait(5)
    db2 = D._db(busy_ms=200)                 # a short wait, so the lock is really met
    t0 = time.monotonic()
    m, e = PJ.run_stage("study", D.study_refresh, db2, "site-A", "riders_per_day", "all")
    dt = time.monotonic() - t0
    t.join(10)
    print(f"  result={'ok' if e is None else type(e).__name__} in {dt:.1f}s · "
          f"retries={PJ.LOCK_STATS['n_locked']} · waited {PJ.LOCK_STATS['lock_wait_s']:.0f}s")
    if e is not None:
        fails.append(f"a stage that met a held lock FAILED instead of retrying: {e}")
    if not PJ.LOCK_STATS["n_locked"]:
        fails.append("the lock was never actually met — this section is not exercising the retry")
    if m is None or not m.get("n_rows"):
        fails.append("the retried stage produced no result")
    db2.close()

    # ══ 4. the bookkeeping write can NEVER be fatal ══════════════════════════════════════
    print("\n=== 4. a failed run-record is logged, not raised ===")
    dead = D._db()
    dead.close()                             # any use of this raises ProgrammingError
    try:
        out = D.precompute_run_record(dead, "site-A", time.time() - 5,
                                      {"alphabet": 1.0}, 3, 0)
        raised = None
    except Exception as ex:
        out, raised = None, ex
    print(f"  raised={type(raised).__name__ if raised else None} · "
          f"record_error={str((out or {}).get('record_error'))[:48]!r}")
    if raised is not None:
        fails.append(f"precompute_run_record RAISED ({type(raised).__name__}) — a record of what "
                     f"happened must never be able to destroy the thing it is recording")
    if not (out or {}).get("record_error"):
        fails.append("the failure was swallowed silently: the caller cannot tell the sweep went "
                     "unrecorded")
    if (out or {}).get("total_s") is None:
        fails.append("the return lost the sweep's own timings, which are the caller's actual "
                     "subject")

    # and the same under a REAL lock, which is how it failed live
    started2 = threading.Event()
    t2 = threading.Thread(target=_hold_write_lock, args=(path, 2.0, started2), daemon=True)
    t2.start(); started2.wait(5)
    db3 = D._db(busy_ms=100)
    try:
        out2 = D.precompute_run_record(db3, "site-A", time.time() - 5, {"study": 2.0}, 5, 0)
        raised2 = None
    except Exception as ex:
        out2, raised2 = None, ex
    t2.join(10); db3.close()
    print(f"  under a real lock: raised={type(raised2).__name__ if raised2 else None} · "
          f"locked={(out2 or {}).get('record_error_locked')}")
    if raised2 is not None:
        fails.append(f"under a real held lock the run record raised {type(raised2).__name__} — "
                     f"this is exactly how the 2026-09-07 sweep died")

    # ══ 5. the diagnostics say something without needing the database ════════════════════
    print("\n=== 5. lock diagnostics ===")
    d = D.lock_diagnostics()
    print(f"  {d}")
    for k in ("db_mb", "wal_mb", "litestream"):
        if k not in d:
            fails.append(f"lock_diagnostics does not report {k} — when the lock is the problem, "
                         f"anything that needs the database is unavailable by definition")

    # ══ 6. one write lock per refresh, not two ══════════════════════════════════════════
    # Counted at the source: each refresh used to INSERT+commit then DELETE+commit, taking the
    # WAL's single write lock twice per unit of work for a DELETE that almost never removes a row.
    print("\n=== 6. one transaction per refresh ===")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "dash_api.py")).read()
    import re
    for fn in ("aggregate_refresh", "rtt_refresh", "trends_refresh", "study_refresh"):
        m = re.search(r"\ndef " + fn + r"\(.*?(?=\ndef )", src, re.S)
        n = m.group(0).count("db.commit()") if m else -1
        print(f"  {fn:20s} {n} commit(s)")
        if n != 1:
            fails.append(f"{fn} takes the write lock {n} times per call — under WAL there is one "
                         f"writer, so each extra commit is another chance to meet a busy one")
    # ══ 7. WHO CAN ACTUALLY STARVE THE WRITER — demonstrated, not assumed ═══════════════
    # The question asked after the failure was whether litestream checkpointing or a long-running
    # read (a study-bundle build) could be holding the lock. This section answers it by
    # reproducing the mechanics on a scratch database, so the answer is a measurement rather than
    # a recollection of what WAL is supposed to do.
    print("\n=== 7. what a long read does, and what it does not ===")
    pp = os.path.join(tmp, "probe.db")
    w = sqlite3.connect(pp)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    w.commit()
    pay = "x" * 4000

    def wal_kb():
        try:
            return os.stat(pp + "-wal").st_size / 1024.0
        except OSError:
            return 0.0
    for _ in range(1500):
        w.execute("INSERT INTO t (blob) VALUES (?)", (pay,)); w.commit()
    free = wal_kb()
    rdr = sqlite3.connect(pp)
    rdr.execute("BEGIN")
    rdr.execute("SELECT COUNT(*) FROM t").fetchone()      # a snapshot, pinned open
    wrote_ok = True
    try:
        for _ in range(1500):
            w.execute("INSERT INTO t (blob) VALUES (?)", (pay,)); w.commit()
    except sqlite3.OperationalError:
        wrote_ok = False
    pinned = wal_kb()
    ck = w.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    print(f"  WAL after 1500 writes, no reader open : {free:8.0f} KB")
    print(f"  WAL after 1500 more, ONE reader pinned: {pinned:8.0f} KB  "
          f"(writes succeeded: {wrote_ok})")
    print(f"  checkpoint while pinned: {ck}  -> reclaimed {ck[2]} of {ck[1]} pages")
    # THE FINDING: a long read does NOT block the writer. It blocks the CHECKPOINT, so the WAL
    # grows — and the checkpoint that eventually runs is bigger, and a checkpoint DOES pause
    # writers while it runs. A long-running read is an amplifier, never the direct cause.
    if not wrote_ok:
        fails.append("a pinned reader blocked the writer — that would contradict WAL's whole "
                     "point and would make the bundle build a direct cause")
    if pinned <= free:
        fails.append("a pinned reader did not hold the WAL open, so this section is not "
                     "demonstrating the mechanism it describes")
    if ck[2] >= ck[1]:
        fails.append("the checkpoint reclaimed everything despite a pinned reader — the "
                     "starvation mechanism is not being reproduced")
    rdr.rollback(); rdr.close()
    after = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"  once the reader lets go: {after} -> wal {wal_kb():.0f} KB")

    # AND WHAT THE SWEEP ACTUALLY MET: another WRITER, with no busy timeout.
    a = sqlite3.connect(pp); a.execute("PRAGMA busy_timeout=0")
    b2 = sqlite3.connect(pp); b2.execute("PRAGMA busy_timeout=0")
    a.execute("BEGIN IMMEDIATE"); a.execute("INSERT INTO t (blob) VALUES ('held')")
    t0 = time.monotonic()
    gave_up_ms = None
    try:
        b2.execute("INSERT INTO t (blob) VALUES ('other')"); b2.commit()
    except sqlite3.OperationalError:
        gave_up_ms = 1000 * (time.monotonic() - t0)
    print(f"  a second WRITER with busy_timeout=0 gave up after {gave_up_ms:.1f} ms"
          if gave_up_ms is not None else "  second writer unexpectedly succeeded")
    if gave_up_ms is None:
        fails.append("a second writer was not blocked at all — the fixture is not reproducing "
                     "the single-writer rule this whole change is about")
    elif gave_up_ms > 50:
        fails.append(f"a writer with no busy timeout waited {gave_up_ms:.0f} ms; the point of the "
                     f"fix is that it waits ~0 and fails on contact")
    # a READER is never blocked by that writer
    c3 = sqlite3.connect(pp); c3.execute("PRAGMA busy_timeout=0")
    try:
        c3.execute("SELECT COUNT(*) FROM t").fetchone()
        read_ok = True
    except sqlite3.OperationalError:
        read_ok = False
    print(f"  a READER while that writer holds the lock: {'not blocked' if read_ok else 'BLOCKED'}")
    if not read_ok:
        fails.append("a reader was blocked by a writer, which would make every long read a "
                     "suspect; under WAL it is not")
    a.rollback()
    for h in (a, b2, c3, w):
        h.close()

    # ══ 8. the RTT walk is no longer QUADRATIC, and still returns the same trips ════════
    # Found while profiling the sweep's memory: rtt_core.trips() counted n_stops with
    # `sum(1 for c in cyc if t0 < c <= t1)` — a full pass over every cycle in the era, per trip.
    # That is O(trips x cycles): on a 200,000-row era, ~360 million comparisons for a number each
    # trip needs once, and it is why the RTT stage grows faster than the row count. Bisection is
    # exact on a non-decreasing list, so the answer cannot change — which is asserted here against
    # the scan it replaced, on random eras WITH TIES, because a tie is where an off-by-one in a
    # half-open interval would hide.
    print("\n=== 8. n_stops by bisection == n_stops by scan ===")
    import random
    import rtt_core

    def slow_trips(rows, home="G"):
        closes = rtt_core.cycles_with_floor(rows)
        opens = rtt_core.opens_with_floor(rows)
        cyc = [t for t, _f in closes]
        out, oi = [], 0
        for t0, f0 in closes:
            if f0 != home:
                continue
            while oi < len(opens) and opens[oi][0] <= t0:
                oi += 1
            for j in range(oi, len(opens)):
                t1, f1 = opens[j]
                if f1 == home:
                    out.append((t0, t1, t1 - t0, sum(1 for c in cyc if t0 < c <= t1)))
                    break
        return out
    random.seed(7)
    mismatch, n_trips = 0, 0
    for _ in range(200):
        n, t, rws = random.randint(5, 400), 0.0, []
        for _i in range(n):
            t += random.choice([0.0, 0.0, 0.1, 1.0, 5.0, 30.0])     # ties, deliberately
            rws.append({"ts": t, "floor": random.choice(["G", "G", "3", "7", None]),
                        "door_state": random.choice(["open", "closing", "closed", None])})
        a, b_ = slow_trips(rws), rtt_core.trips(rws)
        n_trips += len(a)
        if a != b_:
            mismatch += 1
    print(f"  200 random eras, {n_trips} trips: "
          f"{'identical' if not mismatch else str(mismatch) + ' MISMATCHED'}")
    if mismatch:
        fails.append(f"{mismatch} random eras produced different trips after the rewrite — the "
                     f"fast path is not the same measurement")
    # and it must actually be sub-quadratic: 4x the rows must not be ~16x the time
    def timed(nrows):
        t, rws = 0.0, []
        for i in range(nrows):
            t += 1.0
            rws.append({"ts": t, "floor": ("G" if i % 6 < 3 else "7"),
                        "door_state": ("open", "closing", "closed")[i % 3]})
        t0 = time.monotonic(); rtt_core.trips(rws); return time.monotonic() - t0
    t1x, t4x = timed(6000), timed(24000)
    ratio = t4x / max(t1x, 1e-6)
    print(f"  6k rows {t1x * 1000:.0f} ms · 24k rows {t4x * 1000:.0f} ms · 4x rows = {ratio:.1f}x time")
    if ratio > 9:
        fails.append(f"4x the rows cost {ratio:.1f}x the time — still superlinear enough to be "
                     f"the quadratic scan; on a 400k-row era that is the sweep's whole budget")

    db.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK — the connection waits, a locked stage is retried and only a locked one, the run\n"
          "     record cannot kill the run, and each refresh takes the write lock once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
