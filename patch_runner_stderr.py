#!/usr/bin/env python3
"""
Close the diagnostic gap: make analyze_local self-report on every run.

Anchored, idempotent, backup-first. Patches:
  * B4 runner (analyze_runner.py): emit a one-line integrity+signal+cycle
    summary to STDERR right before the ok-emit.
  * agent (agent.py): log that summary even when the job SUCCEEDS (today the
    agent only reads runner stderr on a non-zero exit).

Runner change is picked up per-job (no restart). agent.py change needs:
    systemctl restart liftlab-agent
"""
import pathlib
import shutil
import time

RUNNER = "/home/askjitk/liftlab-b4/analyze_runner.py"
AGENT = "/home/askjitk/liftlab-b3/pi-agent/agent.py"


def patch(path, old, new, label):
    p = pathlib.Path(path)
    s = p.read_text()
    if new.strip().splitlines()[0] in s:
        print(f"{label}: already patched — skip")
        return
    if old not in s:
        print(f"{label}: ANCHOR NOT FOUND -> aborting, no write")
        return
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    p.write_text(s.replace(old, new, 1))
    print(f"{label}: patched; backup written")


r_old = '        _emit({"status": "ok", "payload": payload, "footage_path": str(local),'
r_new = (
    '        import sys as _sys\n'
    '        _sig = sr.timeline.signal\n'
    '        _sys.stderr.write("DIAG %s span=%.1fs fps=%.2f pts=%s mono=%s '
    'signal_n=%d std=%.3f raw=%d clean=%d\\n" % (camera, m.duration_actual_s, '
    'm.effective_fps, m.pts_source, m.monotonic, len(_sig), float(_sig.std()), '
    'len(sr.raw), len(sr.clean)))\n'
    '        _sys.stderr.flush()\n'
    '        _emit({"status": "ok", "payload": payload, "footage_path": str(local),'
)

a_old = '    payload = out["payload"]\n    footage = out.get("footage_path")'
a_new = (
    '    for _l in (proc.stderr or "").splitlines():\n'
    '        if _l.startswith("DIAG"):\n'
    '            log("analyze " + _l)\n'
    '    payload = out["payload"]\n'
    '    footage = out.get("footage_path")'
)

patch(RUNNER, r_old, r_new, "runner")
patch(AGENT, a_old, a_new, "agent")
print("\nDone. If 'agent: patched' above, restart:  systemctl restart liftlab-agent")
