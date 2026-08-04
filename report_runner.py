#!/usr/bin/env python3
"""Export worker — runs ONE report job, in its OWN process, and records the outcome.

WHY A SEPARATE PROCESS AND NOT A THREAD. liftlab-cloud is a 2-vCPU e2-small carrying seven live
streams. A report build walks hundreds of thousands of rows and holds a workbook in memory; run in a
uvicorn worker thread it competes for the GIL with every ingest request, and if it OOMs it takes the
gateway's event loop with it. As a niced child it is preemptible, its RSS is its own, and the kernel
kills it alone. The 2026-08-03 OOM is the reason this is not negotiable.

CONTRACT. Invoked as `report_runner.py <job_id>`; reads the job from the jobs DB (never gateway.db's
schema), stamps running/pid, builds, then stamps done|failed exactly once. stdout/stderr are captured
by the caller into the job row, so a failure surfaces as its real error text rather than "failed".

The supervisor owns the hard timeout and the kill — a worker cannot be trusted to time out its own
wedge. This process only has to be honest about what it finished.
"""
import os
import sys
import time
import sqlite3
import traceback
from pathlib import Path

JOBS_DB = os.environ.get("REPORTS_DB", "/var/lib/liftlab/reports.db")
OUT_DIR = Path(os.environ.get("REPORTS_DIR", "/var/lib/liftlab/reports"))


def _jobs():
    db = sqlite3.connect(JOBS_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _finish(job_id, status, **cols):
    cols["status"] = status
    cols["finished_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in cols)
    db = _jobs()
    db.execute(f"UPDATE report_job SET {sets} WHERE id=?", (*cols.values(), job_id))
    db.commit(); db.close()


def main(argv):
    if len(argv) < 2:
        print("usage: report_runner.py <job_id>", file=sys.stderr)
        return 2
    job_id = int(argv[1])

    db = _jobs()
    row = db.execute("SELECT id, requested_from, requested_to, population, status "
                     "FROM report_job WHERE id=?", (job_id,)).fetchone()
    if row is None:
        print(f"job {job_id} not found", file=sys.stderr); db.close(); return 2
    _id, r_from, r_to, population, status = row
    if status not in ("queued", "running"):
        # Already terminal. Refuse rather than produce a second workbook for the same row: a job
        # that reports 'done' twice with different files is worse than one that never started.
        print(f"job {job_id} is {status}, not runnable", file=sys.stderr); db.close(); return 2
    db.execute("UPDATE report_job SET status='running', started_at=?, pid=? WHERE id=?",
               (time.time(), os.getpid(), job_id))
    db.commit(); db.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT_DIR, 0o750)          # resident movement data — never world-readable
    out = OUT_DIR / f"liftlab-report-{job_id}-{r_from[:10]}_{r_to[:10]}.xlsx"

    try:
        # Imported HERE, not at module top: an import error must be reported against the job (with
        # its traceback) rather than killing the process before the row says 'running'.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from liftlab_report import cli as report_cli

        argv_cli = ["--from", r_from, "--to", r_to, "--out", str(out), "--format", "xlsx"]
        if population is not None:
            argv_cli += ["--population", str(int(population))]

        # run_all(), NOT main(). main() catches ReportError and collapses it to `return 2` with the
        # message on stderr, so the job row would read "exited 2" and the user would be told nothing.
        # The whole point of storing error_text is that a failure explains itself.
        try:
            report_cli.run_all(argv_cli)
        except report_cli.ReportError as e:
            _finish(job_id, "failed", error_text=str(e))
            print(f"report error: {e}", file=sys.stderr)
            return 2

        if not out.exists():
            _finish(job_id, "failed",
                    error_text=f"CLI reported success but {out.name} was not written")
            return 1
        os.chmod(out, 0o640)

        # Row count is reported from the SAME read-only path the report uses, so it cannot disagree
        # with the workbook, and it is labelled as what it is: transits in range, the demand basis.
        n = None
        try:
            from liftlab_report import reader
            gdb = reader.open_ro(os.environ.get("GATEWAY_DB", "/var/lib/liftlab/gateway.db"))
            t0, t1 = report_cli.parse_ts(r_from), report_cli.parse_ts(r_to)
            n = gdb.execute("SELECT COUNT(*) FROM transit_event WHERE ts>=? AND ts<?",
                            (t0, t1)).fetchone()[0]
            gdb.close()
        except Exception:
            pass                                  # a missing count must not fail a good workbook

        _finish(job_id, "done", output_path=str(out), row_count=n)
        print(f"job {job_id} done: {out} ({out.stat().st_size} bytes, transits={n})")
        return 0

    except Exception:
        tb = traceback.format_exc()
        # Keep the tail: the useful line is the exception, not the frames above it.
        _finish(job_id, "failed", error_text=tb[-4000:])
        print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
