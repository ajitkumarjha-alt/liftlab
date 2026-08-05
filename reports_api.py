"""/reports — pick a date range, see trends for it, download the workbook.

A thin wrapper around the liftlab-report CLI. The CLI is unchanged and remains the source of truth;
this module only decides WHEN it runs, WHERE the file lands, and WHO may fetch it.

THE ONE RULE: NEVER BLOCK THE GATEWAY.
liftlab-cloud is a 2-vCPU e2-small carrying seven live streams, and this week established exactly
what happens when work runs on the request path — /dash queries that never terminated, then an OOM.
So the export runs in a NICED CHILD PROCESS, never in a uvicorn worker:

    request handler   ->  INSERT a queued row, return immediately.        (milliseconds)
    supervisor thread ->  spawn report_runner.py when nothing is running. (sqlite poll + Popen)
    report_runner.py  ->  the actual build, in its own address space.     (minutes)

The supervisor is a thread, but it never builds anything: it polls a small sqlite file, spawns, kills
on timeout, and sweeps old files. If the build OOMs, the kernel takes the child and the gateway keeps
serving. That separation is the entire design.

DATABASE DISCIPLINE. Job state lives in its OWN sqlite file (REPORTS_DB). gateway.db is
Litestream-replicated and its schema is the instrument's record — job bookkeeping does not belong in
it, and adding tables there would put operational churn into the replicated stream. The report path
opens gateway.db read-only (`mode=ro` + `PRAGMA query_only`); the deploy gate re-checks its md5 after
a full export.

FILES. Workbooks contain resident movement data. They are written to a 0750 directory, served only
through an authenticated route that streams them, and deleted after RETENTION_DAYS. Never /tmp: it is
tmpfs (so a workbook competes with RAM the streams need) and world-readable. Both were real problems
this week.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

import nav_common
from liftlab_report import eras

reports_router = APIRouter()

REPORTS_DB = os.environ.get("REPORTS_DB", "/var/lib/liftlab/reports.db")
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "/var/lib/liftlab/reports"))
RUNNER = Path(__file__).resolve().parent / "report_runner.py"
GW = os.environ.get("DASH_GW", "site-A")

JOB_TIMEOUT_S = float(os.environ.get("REPORT_TIMEOUT_S", "900"))     # 15 min
QUEUE_CAP = int(os.environ.get("REPORT_QUEUE_CAP", "3"))             # queued, excluding running
RETENTION_DAYS = float(os.environ.get("REPORT_RETENTION_DAYS", "7"))
MIN_FREE_GB = float(os.environ.get("REPORT_MIN_FREE_GB", "2"))
NICE = int(os.environ.get("REPORT_NICE", "10"))
POLL_S = 2.0

# DEV-BOX DEPLOYMENT. On the gateway these are unset and everything below is inert: the DB is live,
# there is no restore, and the page says so. On dev-box the database is an hourly Litestream RESTORE
# from GCS, which makes two things mandatory:
#   1. the page must show the RESTORE POINT, never "now" — an hourly snapshot presented as live data
#      is a wrong answer delivered confidently, which is worse than no page at all;
#   2. the refresh must not swap the file under a running export, hence a lock both sides take.
DB_LOCK = os.environ.get("LIFTLAB_DB_LOCK", "")          # flock path; "" disables locking
RESTORE_STATE = os.environ.get("LIFTLAB_RESTORE_STATE", "")   # json written by the refresh job


def restore_state():
    """What the refresh job last did. A FAILED refresh is not a cosmetic problem: this database is a
    continuous restore test of the backup chain, so a refresh that stops working is a backup alarm.
    It is surfaced on the page rather than logged quietly."""
    if not RESTORE_STATE:
        return {"mode": "live", "note": "reading the live gateway database"}
    try:
        with open(RESTORE_STATE) as fh:
            st = json.load(fh)
    except FileNotFoundError:
        return {"mode": "restore", "ok": False, "stale": True,
                "error": "no restore has completed yet — the page is showing nothing, or an old file"}
    except Exception as e:
        return {"mode": "restore", "ok": False, "error": f"restore state unreadable: {e!r}"}
    st["mode"] = "restore"
    ra = st.get("restored_at")
    if ra:
        age = time.time() - float(ra)
        st["age_s"] = age
        # Two missed hourly refreshes. One can be a blip; two is a pattern worth a red banner.
        st["stale"] = age > 2 * 3600
    return st

IST = eras.IST


# ───────────────────────── jobs db ─────────────────────────
def _jobs():
    Path(REPORTS_DB).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(REPORTS_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS report_job (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      requested_from TEXT, requested_to TEXT, population INTEGER,
      status TEXT,                       -- queued|running|done|failed|expired
      created_at REAL, started_at REAL, finished_at REAL,
      output_path TEXT, error_text TEXT, row_count INTEGER, requested_by TEXT,
      pid INTEGER)""")
    # `pid` is the one column beyond the specified list. It is not bookkeeping for its own sake: the
    # hard timeout has to KILL the subprocess, and reaping a worker that died without recording an
    # outcome needs to ask whether that pid still exists. Without it a wedged job pins the queue.
    db.execute("CREATE INDEX IF NOT EXISTS ix_job_status ON report_job (status, created_at)")
    return db


def _row(r):
    (jid, rf, rt, pop, st, ca, sa, fa, out, err, n, by, pid) = r
    size = None
    if out and st == "done":
        try:
            size = os.path.getsize(out)
        except OSError:
            size = None
    return {"id": jid, "requested_from": rf, "requested_to": rt, "population": pop,
            "status": st, "created_at": ca, "started_at": sa, "finished_at": fa,
            "output_path": out, "error_text": err, "row_count": n, "requested_by": by,
            "size": size,
            "elapsed_s": ((fa or time.time()) - sa) if sa else None}


_COLS = ("id, requested_from, requested_to, population, status, created_at, started_at, "
         "finished_at, output_path, error_text, row_count, requested_by, pid")


# ───────────────────────── supervisor ─────────────────────────
# One per box. A second uvicorn worker starting its own supervisor would double-spawn jobs, so the
# thread takes an exclusive flock and simply does not run if it loses the race.
_sup_started = False
_sup_lock = threading.Lock()
_procs = {}          # job_id -> Popen, so children are reaped rather than left as zombies


def _is_dead(pid):
    """True if the pid is gone OR is a zombie awaiting reaping.

    Reading /proc rather than trusting signal 0: a zombie accepts signals and would otherwise be
    mistaken for a running export, pinning the queue for everyone behind it."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # comm can contain spaces and parentheses; state is the field after the last ')'
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True
    except Exception:
        return False


def _spawn(job_id):
    env = dict(os.environ)
    env["REPORTS_DB"] = REPORTS_DB
    env["REPORTS_DIR"] = str(REPORTS_DIR)
    if DB_LOCK:
        env["LIFTLAB_DB_LOCK"] = DB_LOCK      # the worker holds it for the whole build
    log = REPORTS_DIR / f"job-{job_id}.log"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(REPORTS_DIR, 0o750)
    fh = open(log, "wb")
    # nice: the streams and the request path must always win a CPU contest against a report.
    # start_new_session: the child gets its own process group, so killing it on timeout cannot
    # signal uvicorn, and a stray Ctrl-C in a console cannot reach the build.
    return subprocess.Popen(
        [sys.executable, str(RUNNER), str(job_id)],
        stdout=fh, stderr=subprocess.STDOUT, env=env,
        preexec_fn=(lambda: os.nice(NICE)), start_new_session=True,
        cwd=str(Path(__file__).resolve().parent))


def _sweep(db):
    """Retention: delete workbooks past RETENTION_DAYS and mark the row expired.

    The row is KEPT. A user who exported something last month should see that it existed and that we
    removed it — 'expired' is an answer; a job vanishing from the list is not."""
    cutoff = time.time() - RETENTION_DAYS * 86400.0
    for jid, out in db.execute(
            "SELECT id, output_path FROM report_job WHERE status='done' AND finished_at < ?",
            (cutoff,)).fetchall():
        try:
            if out and os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        db.execute("UPDATE report_job SET status='expired' WHERE id=?", (jid,))
    db.commit()


def _supervise_once(db):
    now = time.time()
    # 1. time out a wedged job. The worker cannot be trusted to time out its own hang.
    for jid, sa, pid in db.execute(
            "SELECT id, started_at, pid FROM report_job WHERE status='running'").fetchall():
        if sa and now - sa > JOB_TIMEOUT_S:
            if pid:
                try:
                    os.killpg(os.getpgid(pid), 9)
                except (ProcessLookupError, PermissionError):
                    pass
            db.execute("UPDATE report_job SET status='failed', finished_at=?, error_text=? "
                       "WHERE id=?",
                       (now, f"timed out after {JOB_TIMEOUT_S:.0f}s — subprocess killed", jid))
            db.commit()

    # 2. reap a job whose process died without recording an outcome (OOM kill, SIGKILL, crash).
    #    Without this the row sits 'running' forever and the queue never moves again.
    #
    #    ZOMBIES. os.kill(pid, 0) is NOT a liveness test for our own children. A killed child that
    #    nobody has wait()ed on stays in the process table as a zombie, keeps its pid, and answers
    #    signal 0 quite happily — so this check called a dead worker alive and the job sat 'running'
    #    until the 30-minute timeout. Measured on dev-box 2026-08-05: kill -9 the worker, job stayed
    #    'running' past 400s. Reap the Popen first (which clears the zombie and yields an exit code),
    #    and treat state Z as dead for anything we no longer hold a handle to.
    for jid, proc in list(_procs.items()):
        rc = proc.poll()
        if rc is not None:
            _procs.pop(jid, None)
    for jid, pid, sa in db.execute(
            "SELECT id, pid, started_at FROM report_job WHERE status='running'").fetchall():
        if pid and sa and now - sa > 10:
            try:
                if _is_dead(pid):
                    raise ProcessLookupError
                os.kill(pid, 0)
            except ProcessLookupError:
                log = REPORTS_DIR / f"job-{jid}.log"
                tail = ""
                try:
                    tail = log.read_text(errors="replace")[-2000:]
                except OSError:
                    pass
                db.execute("UPDATE report_job SET status='failed', finished_at=?, error_text=? "
                           "WHERE id=?",
                           (now, "worker exited without recording an outcome (killed or crashed). "
                                 "Tail of its log:\n" + tail, jid))
                db.commit()
            except PermissionError:
                pass

    # 3. start the next queued job, only if nothing is running (global one-at-a-time lock).
    #
    # CLAIM BEFORE SPAWN. The job must be flipped out of 'queued' by THIS function, atomically,
    # before the worker is started. Letting the worker set its own status='running' looks tidier and
    # is a double-spawn bug: python takes ~1s to boot, the supervisor ticks every 2s, so the next
    # tick still sees the row as 'queued' with nothing 'running' and starts a SECOND worker for the
    # same job — two processes writing one output path. The UPDATE ... WHERE status='queued' is the
    # lock; rowcount tells us whether we won it.
    running = db.execute("SELECT COUNT(*) FROM report_job WHERE status='running'").fetchone()[0]
    if running == 0:
        nxt = db.execute("SELECT id FROM report_job WHERE status='queued' "
                         "ORDER BY created_at LIMIT 1").fetchone()
        if nxt:
            jid = nxt[0]
            cur = db.execute("UPDATE report_job SET status='running', started_at=? "
                             "WHERE id=? AND status='queued'", (now, jid))
            db.commit()
            if cur.rowcount != 1:
                return                      # someone else claimed it; nothing to do this tick
            try:
                proc = _spawn(jid)
                _procs[jid] = proc          # keep the handle so poll() can reap it
                db.execute("UPDATE report_job SET pid=? WHERE id=?", (proc.pid, jid))
                db.commit()
            except Exception as e:
                db.execute("UPDATE report_job SET status='failed', finished_at=?, error_text=? "
                           "WHERE id=?", (time.time(), f"could not start worker: {e!r}", jid))
                db.commit()


def _supervisor():
    last_sweep = 0.0
    while True:
        try:
            db = _jobs()
            _supervise_once(db)
            if time.time() - last_sweep > 3600:
                _sweep(db); last_sweep = time.time()
            db.close()
        except Exception:
            pass                          # a supervisor that dies stops every future export
        time.sleep(POLL_S)


def start_supervisor():
    global _sup_started
    with _sup_lock:
        if _sup_started:
            return
        try:
            import fcntl
            Path(REPORTS_DIR).mkdir(parents=True, exist_ok=True)
            f = open(REPORTS_DIR / ".supervisor.lock", "w")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            globals()["_sup_lockfile"] = f          # held for process lifetime
        except Exception:
            return                                   # another worker owns it
        _sup_started = True
        threading.Thread(target=_supervisor, daemon=True, name="report-supervisor").start()


# ───────────────────────── range analysis ─────────────────────────
def _parse(s):
    """Naive input is IST. The instrument split sits at 2026-07-21 05:30 IST, so an off-by-one
    timezone here silently moves a range across an era boundary."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def range_notes(t0, t1):
    """Era boundaries and declared gaps inside [t0, t1). The workbook handles both correctly — this
    exists so the user is not surprised by what it says, not to block the export."""
    notes = []
    if t0 < eras.INSTRUMENT_SPLIT_EPOCH < t1:
        notes.append({
            "kind": "era",
            "at": datetime.fromtimestamp(eras.INSTRUMENT_SPLIT_EPOCH, IST).strftime("%Y-%m-%d %H:%M"),
            "text": "Range crosses the INSTRUMENT SPLIT (2026-07-21 05:30 IST). Door timings before "
                    "and after come from different instruments (Pi doorwatch vs GPU door engine) and "
                    "are reported separately, never pooled."})
    for cam, ts, ver in eras._EPOCHS_PARSED:
        if t0 < ts < t1:
            notes.append({
                "kind": "era",
                "at": datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M"),
                "text": f"Counting version changes to {ver}"
                        f"{'' if cam is None else ' on ' + cam} inside this range. Transit counts "
                        f"either side are not pooled."})
    for g in eras.DATA_GAPS:
        a, b = g["start_epoch"], g["end_epoch"]
        if a < t1 and b > t0:
            cams = "all cameras" if g["cams"] is None else ", ".join(g["cams"])
            notes.append({
                "kind": "gap",
                "at": datetime.fromtimestamp(a, IST).strftime("%Y-%m-%d %H:%M"),
                "text": f"Declared data gap ({cams}): {g['reason']} — excluded from rates so it "
                        f"cannot deflate averages."})
    return notes


@reports_router.get("/reports/range-check")
def range_check(from_ts: str, to_ts: str):
    try:
        a, b = _parse(from_ts), _parse(to_ts)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": f"bad timestamp: {e}"}, status_code=400)
    if b <= a:
        return JSONResponse({"ok": False, "error": "end must be after start"}, status_code=400)
    return {"ok": True, "notes": range_notes(a.timestamp(), b.timestamp()),
            "days": round((b - a).total_seconds() / 86400.0, 2)}


# ───────────────────────── routes ─────────────────────────
@reports_router.post("/reports/submit")
async def submit(request: Request):
    start_supervisor()
    body = await request.json()
    f, t = str(body.get("from") or ""), str(body.get("to") or "")
    pop = body.get("population")
    try:
        a, b = _parse(f), _parse(t)
    except ValueError as e:
        raise HTTPException(400, f"bad timestamp: {e}")
    if b <= a:
        raise HTTPException(400, "end must be after start")
    if pop in ("", None):
        pop = None                       # blank is valid. Never guess a population.
    else:
        try:
            pop = int(pop)
            if pop <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "population must be a positive whole number, or blank")

    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < MIN_FREE_GB:
        raise HTTPException(507, f"refusing to start: only {free_gb:.1f} GB free on / "
                                 f"(need {MIN_FREE_GB:.0f} GB). Old workbooks are swept after "
                                 f"{RETENTION_DAYS:g} days.")

    db = _jobs()
    n_q = db.execute("SELECT COUNT(*) FROM report_job WHERE status='queued'").fetchone()[0]
    if n_q >= QUEUE_CAP:
        db.close()
        raise HTTPException(429, f"queue is full ({n_q} waiting, cap {QUEUE_CAP}). One export runs "
                                 f"at a time so the live streams are never starved — try again when "
                                 f"the current one finishes.")
    who = request.headers.get("x-forwarded-user") or request.headers.get("x-remote-user") or "operator"
    cur = db.execute("INSERT INTO report_job (requested_from, requested_to, population, status, "
                     "created_at, requested_by) VALUES (?,?,?,'queued',?,?)",
                     (a.isoformat(), b.isoformat(), pop, time.time(), who))
    db.commit()
    jid = cur.lastrowid
    db.close()
    return {"ok": True, "job_id": jid, "queued_ahead": n_q}


@reports_router.get("/reports/data-as-of")
def data_as_of():
    """The restore point. Deliberately its own endpoint so the page can poll it independently of the
    job list — a stale-data banner must keep updating even when nothing is exporting."""
    return restore_state()


@reports_router.get("/reports/jobs")
def jobs(limit: int = 10):
    start_supervisor()
    db = _jobs()
    rows = db.execute(f"SELECT {_COLS} FROM report_job ORDER BY created_at DESC LIMIT ?",
                      (max(1, min(int(limit), 50)),)).fetchall()
    db.close()
    return {"jobs": [_row(r) for r in rows]}


@reports_router.get("/reports/job/{job_id}")
def job(job_id: int):
    db = _jobs()
    r = db.execute(f"SELECT {_COLS} FROM report_job WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not r:
        raise HTTPException(404, "no such job")
    return _row(r)


@reports_router.get("/reports/download/{job_id}")
def download(job_id: int):
    """Streams the workbook through the app so it inherits the same auth as every other human page.

    The output directory is deliberately NOT exposed via a Caddy file_server: a static mount would
    serve resident movement data to anyone who learned a filename, and filenames are guessable."""
    db = _jobs()
    r = db.execute("SELECT status, output_path FROM report_job WHERE id=?", (job_id,)).fetchone()
    db.close()
    if not r:
        raise HTTPException(404, "no such job")
    status, out = r
    if status == "expired":
        raise HTTPException(410, f"this workbook was deleted by the {RETENTION_DAYS:g}-day "
                                 f"retention sweep. Re-run the export for the same range.")
    if status != "done" or not out:
        raise HTTPException(409, f"job is {status}, nothing to download")
    p = Path(out)
    if not p.exists():
        raise HTTPException(410, "the file is gone from disk though the job says done — it was "
                                 "removed outside the retention sweep. Re-run the export.")
    return FileResponse(str(p), filename=p.name,
                        media_type="application/vnd.openxmlformats-officedocument."
                                   "spreadsheetml.sheet")


# ───────────────────────── the page ─────────────────────────
_PAGE = """<!doctype html><meta charset=utf-8><title>liftlab — reports</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f8fa;--card:#fff;--fg:#1c2429;--mut:#6b7a84;--line:#e3e8ec;--warn:#8a6100;
 --warnbg:#fff8e1;--err:#a11;--errbg:#fdecea;--ok:#186a3b}
@media(prefers-color-scheme:dark){:root{--bg:#11161a;--card:#182027;--fg:#e6edf3;--mut:#8b98a5;
 --line:#242f38;--warnbg:#2b2410;--warn:#e3b341;--errbg:#3a1d1b;--err:#ff7b72;--ok:#3fb950}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:0 0 14px}
h2{margin:0 0 4px;font-size:15px}
.sub{color:var(--mut);font-size:12px;margin:0 0 12px}
label{display:block;font-size:12px;color:var(--mut);margin:0 0 3px}
input,button{font:13px ui-monospace,Menlo,monospace;padding:7px 9px;border:1px solid var(--line);
 border-radius:7px;background:var(--card);color:var(--fg)}
button{cursor:pointer;background:#4c8bf5;color:#fff;border-color:#4c8bf5;font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.note{border-radius:7px;padding:8px 10px;margin:8px 0 0;font-size:12px}
.note.warn{background:var(--warnbg);color:var(--warn);border:1px solid rgba(138,97,0,.25)}
.note.err{background:var(--errbg);color:var(--err);border:1px solid rgba(170,17,17,.25)}
.note.ok{color:var(--ok)}
.asof{border-radius:10px;padding:12px 14px;margin:0 0 14px;border:1px solid var(--line);
 background:var(--card);font-size:13px}
.asof b{font-size:15px}
.asof.stale{background:var(--warnbg);color:var(--warn);border-color:rgba(138,97,0,.35)}
.asof.broken{background:var(--errbg);color:var(--err);border-color:rgba(170,17,17,.35)}
.asof .sub2{font-size:12px;opacity:.85;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}
.mut{color:var(--mut)}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.s-done{background:#e6f4ea;color:#186a3b}.s-failed{background:#fdecea;color:#a11}
.s-running{background:#e8f0fe;color:#1a56c4}.s-queued{background:#eee;color:#555}
.s-expired{background:#f3e8fd;color:#6b21a8}
@media(prefers-color-scheme:dark){.s-done{background:#0f2e1c;color:#3fb950}
 .s-failed{background:#3a1d1b;color:#ff7b72}.s-running{background:#16243c;color:#79a9ff}
 .s-queued{background:#222c33;color:#9aa}.s-expired{background:#2a1b3d;color:#c4a2f0}}
pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:11px;color:var(--err)}
__NAVCSS__
</style>
__NAV__
<div class=wrap>

<div id=asof></div>

<div class=card>
  <h2>Export a report</h2>
  <p class=sub>Builds the Excel workbook for a date range. Runs as a background job — one at a time,
  niced, in its own process, so it cannot slow the live streams.</p>
  <div class=row>
    <div><label>From (IST)</label><input type=date id=f></div>
    <div><label>To (IST, exclusive)</label><input type=date id=t></div>
    <div><label>Population (optional)</label><input type=number id=pop min=1 placeholder="blank = omit"></div>
    <div><button id=go onclick=submit()>Build workbook</button></div>
  </div>
  <p class=sub style="margin:10px 0 0">Dates are <b>IST</b>. The range is <b>[from, to)</b> — the end
  day is excluded. Population unlocks peak demand as a % of tower population, to compare against the
  8% handling-capacity assumption; leave it blank and that comparison is simply omitted, never
  guessed.</p>
  <div id=notes></div>
  <div id=sub></div>
</div>

<div class=card>
  <h2>Trends for this range</h2>
  <p class=sub id=trsub>Scoped to the dates above — the same view as the dashboard, restricted to
  what you are about to export.</p>
  <div id=trends class=mut>pick a range…</div>
</div>

<div class=card>
  <h2>Recent exports</h2>
  <p class=sub>Last 10 jobs. Workbooks are deleted after __RET__ days — the row stays, marked
  expired, so you can see it existed.</p>
  <div id=jobs class=mut>loading…</div>
</div>
</div>

<script>
var GW=__GW__, POLL=null, WATCH=null;
function iso(d){return d.toISOString().slice(0,10)}
(function(){var now=new Date();var a=new Date(now.getTime()-7*86400000);
 document.getElementById('f').value=iso(a);document.getElementById('t').value=iso(now);})();

function fmtBytes(n){if(n==null)return '—';if(n<1024)return n+' B';
 if(n<1048576)return (n/1024).toFixed(0)+' KB';return (n/1048576).toFixed(1)+' MB'}
function fmtDur(s){if(s==null)return '—';s=Math.round(s);
 return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'}

function range(){return {f:document.getElementById('f').value,t:document.getElementById('t').value}}

function checkRange(){
  var r=range(); if(!r.f||!r.t){return}
  fetch('/reports/range-check?from_ts='+r.f+'&to_ts='+r.t)
   .then(function(x){return x.json()}).then(function(j){
    var el=document.getElementById('notes');
    if(!j.ok){el.innerHTML='<div class="note err">'+j.error+'</div>';return}
    var h='';
    if(j.notes.length){
      h+='<div class="note warn"><b>This range crosses '+j.notes.length+
         ' boundary/boundaries.</b> The workbook handles these correctly — it reports either side '+
         'separately and excludes declared gaps. Listed so nothing in it surprises you:<ul style="margin:6px 0 0;padding-left:18px">';
      j.notes.forEach(function(n){h+='<li><b>'+n.at+'</b> — '+n.text+'</li>'});
      h+='</ul></div>';
    }
    el.innerHTML=h;
   }).catch(function(){});
  loadTrends();
}

function loadTrends(){
  var r=range(); if(!r.f||!r.t){return}
  var el=document.getElementById('trends'); el.className='mut'; el.textContent='loading…';
  fetch('/dash/'+GW+'/trends?from_d='+r.f+'&to_d='+r.t)
   .then(function(x){return x.json()}).then(function(t){renderTrends(t)})
   .catch(function(e){el.textContent='trends unavailable: '+e});
}

function renderTrends(t){
  var el=document.getElementById('trends'); el.className='';
  var prof=t.profile||[];
  // profile is ALWAYS 24 hours, zero-filled, so emptiness is never signalled by length. Ask the
  // data: no contributing days, or nothing counted in any hour.
  var boards=0, cycles=0;
  prof.forEach(function(p){boards+=(p.boarded||0)+(p.alighted||0); cycles+=(p.cycles||0)});
  if(!t.n_days || (boards===0 && cycles===0)){
    el.className='mut';
    el.innerHTML='No data in this range'+(t.n_days?'':' (0 contributing days)')+
      '. The export will still build, and the workbook states the zero explicitly rather than '+
      'rendering an empty chart.';
    return;
  }
  // Prefer boardings; fall back to door cycles where a camera counts doors but not transits.
  var useCycles = (boards===0);
  var val=function(p){return useCycles?(p.cycles||0):((p.boarded||0)+(p.alighted||0))};
  var max=0; prof.forEach(function(p){if(val(p)>max)max=val(p)});
  var h='<div class=mut style="margin-bottom:6px">n_days='+t.n_days+
        '  ·  hour-of-day profile over the selected range · '+
        (useCycles?'door cycles (no transits counted in this range)':'boardings + alightings')+
        '</div>';
  h+='<div style="display:flex;align-items:flex-end;gap:2px;height:110px">';
  prof.forEach(function(p){
    var v=val(p);
    var pct=max?Math.max(2,Math.round(v/max*100)):2;
    h+='<div title="'+(p.hour!=null?p.hour:'')+':00 — '+v+'" style="flex:1;background:#4c8bf5;'+
       'height:'+pct+'%;border-radius:2px 2px 0 0"></div>';
  });
  h+='</div><div style="display:flex;gap:2px;font-size:9px;color:var(--mut)">';
  prof.forEach(function(p){h+='<div style="flex:1;text-align:center">'+
    ((p.hour%6===0)?p.hour:'')+'</div>'});
  h+='</div>';
  el.innerHTML=h;
}

function submit(){
  var r=range(); var pop=document.getElementById('pop').value;
  var btn=document.getElementById('go'); btn.disabled=true;
  document.getElementById('sub').innerHTML='<div class="note">submitting…</div>';
  fetch('/reports/submit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({from:r.f,to:r.t,population:pop})})
   .then(function(x){return x.json().then(function(j){return {ok:x.ok,st:x.status,j:j}})})
   .then(function(res){
     btn.disabled=false;
     if(!res.ok){
       document.getElementById('sub').innerHTML='<div class="note err"><b>Not submitted.</b> '+
         ((res.j&&(res.j.detail||res.j.error))||('HTTP '+res.st))+'</div>';
       return;
     }
     WATCH=res.j.job_id;
     document.getElementById('sub').innerHTML='<div class="note">job #'+WATCH+' queued'+
       (res.j.queued_ahead?(' — '+res.j.queued_ahead+' ahead of it'):'')+'</div>';
     if(POLL)clearInterval(POLL); POLL=setInterval(poll,3000); poll();
   })
   .catch(function(e){btn.disabled=false;
     document.getElementById('sub').innerHTML='<div class="note err">'+e+'</div>'});
}

function poll(){
  loadJobs();
  if(WATCH==null)return;
  fetch('/reports/job/'+WATCH).then(function(x){return x.json()}).then(function(j){
    var el=document.getElementById('sub'), h='';
    if(j.status==='queued'){h='<div class="note">job #'+j.id+' <b>queued</b> — waiting for the '+
      'current export to finish.</div>'}
    else if(j.status==='running'){h='<div class="note">job #'+j.id+' <b>running</b> — '+
      fmtDur(j.elapsed_s)+' elapsed.</div>'}
    else if(j.status==='done'){h='<div class="note ok">job #'+j.id+' <b>done</b> in '+
      fmtDur(j.elapsed_s)+' · '+fmtBytes(j.size)+
      (j.row_count!=null?(' · '+j.row_count+' transits in range'):'')+
      ' — <a href="/reports/download/'+j.id+'">download workbook</a></div>';
      clearInterval(POLL);POLL=null;WATCH=null}
    else if(j.status==='failed'){h='<div class="note err"><b>job #'+j.id+' failed.</b>'+
      '<pre>'+(j.error_text||'(no error text recorded)')+'</pre></div>';
      clearInterval(POLL);POLL=null;WATCH=null}
    else{h='<div class="note">job #'+j.id+' — '+j.status+'</div>';
      clearInterval(POLL);POLL=null;WATCH=null}
    el.innerHTML=h;
  }).catch(function(){});
}

function loadJobs(){
  fetch('/reports/jobs?limit=10').then(function(x){return x.json()}).then(function(d){
    var el=document.getElementById('jobs');
    if(!d.jobs.length){el.className='mut';el.textContent='no exports yet';return}
    el.className='';
    var h='<table><tr><th>#</th><th>range (IST)</th><th>status</th><th>elapsed</th>'+
          '<th>size</th><th></th></tr>';
    d.jobs.forEach(function(j){
      var dl='';
      if(j.status==='done')dl='<a href="/reports/download/'+j.id+'">download</a>';
      else if(j.status==='expired')dl='<span class=mut>deleted by retention</span>';
      else if(j.status==='failed')dl='<span class=mut title="'+
        (j.error_text||'').replace(/"/g,'&quot;').slice(0,300)+'">see error</span>';
      h+='<tr><td>'+j.id+'</td><td>'+j.requested_from.slice(0,10)+' → '+j.requested_to.slice(0,10)+
         (j.population?(' <span class=mut>pop '+j.population+'</span>'):'')+
         '</td><td><span class="pill s-'+j.status+'">'+j.status+'</span></td><td>'+
         fmtDur(j.elapsed_s)+'</td><td>'+fmtBytes(j.size)+'</td><td>'+dl+'</td></tr>';
      if(j.status==='failed'&&j.error_text){
        h+='<tr><td></td><td colspan=5><pre>'+
           j.error_text.replace(/</g,'&lt;').slice(-1200)+'</pre></td></tr>';
      }
    });
    el.innerHTML=h+'</table>';
  }).catch(function(){});
}

function loadAsOf(){
  fetch('/reports/data-as-of').then(function(x){return x.json()}).then(function(a){
    var el=document.getElementById('asof'), h='';
    if(a.mode==='live'){
      h='<div class=asof><b>Live data.</b> <span class=sub2>This page reads the gateway database '+
        'directly; figures are current as of the moment you export.</span></div>';
    } else if(a.ok===false){
      h='<div class="asof broken"><b>The hourly refresh is FAILING.</b>'+
        '<div class=sub2>'+(a.error||'unknown error')+
        '</div><div class=sub2>This database is restored from the backup chain, so a refresh that '+
        'stops working is a <b>backup alarm</b>, not just a stale report. Anything you export below '+
        'is from the last good restore'+(a.restored_at_h?(' — '+a.restored_at_h):'')+'.</div></div>';
    } else {
      var cls = a.stale ? 'asof stale' : 'asof';
      h='<div class="'+cls+'"><b>Data as of '+(a.data_max_ts_h||a.restored_at_h||'unknown')+' IST</b>'+
        '<div class=sub2>Restored from the backup chain at '+(a.restored_at_h||'?')+
        (a.age_h?(' · '+a.age_h+' ago'):'')+'. <b>This is not live data.</b> The gateway keeps '+
        'collecting; anything after the timestamp above is not in this copy.'+
        (a.stale?' <b>The refresh is overdue — more than two hours old.</b>':'')+'</div></div>';
    }
    el.innerHTML=h;
  }).catch(function(){});
}
loadAsOf(); setInterval(loadAsOf,60000);

document.getElementById('f').addEventListener('change',checkRange);
document.getElementById('t').addEventListener('change',checkRange);
checkRange(); loadJobs(); setInterval(loadJobs,10000);
</script>
"""


@reports_router.get("/reports", response_class=HTMLResponse)
def reports_page():
    start_supervisor()
    html = (_PAGE
            .replace("__NAVCSS__", nav_common.NAV_CSS)
            .replace("__NAV__", nav_common.header("reports"))
            .replace("__RET__", f"{RETENTION_DAYS:g}")
            .replace("__GW__", repr(GW)))
    return HTMLResponse(html)
