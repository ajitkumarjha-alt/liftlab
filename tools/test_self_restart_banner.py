#!/usr/bin/env python3
"""A deliberate exit must not look like an unexplained crash in the journal.

WHY THIS EXISTS. ch29 exited rc=1 three times across two days. The first thing anyone runs is

    grep -iE 'traceback|error|exception' <crash window>

and it came back EMPTY, which reads as "rc=1 with no reason logged" — so the fault looked like a
crash nobody could see. It was not a crash at all: every deliberate exit in this worker calls
faulthandler.dump_traceback, whose output contains "Thread 0x..." and "  File ..." and NOT one of
the three words above. The reason line was in the journal the whole time, under a phrase nobody
searched for.

So every self-restart now prints one greppable banner containing the literal word TRACEBACK, and
this test asserts that no exit path can bypass it.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BANNER = "WORKER SELF-RESTART rc=1"


def main():
    fails = []

    print("=== 1. no raw os._exit(1) survives outside the banner helpers ===")
    for fn in ("gpu_analyze.py", "gpu_watchdog.py"):
        src = open(os.path.join(ROOT, fn)).read()
        # Allowed: inside the helper itself. Anywhere else is a path that exits without a reason.
        helper = re.search(r"def (?:self_restart|wd_self_restart)\(.*?(?=\ndef |\Z)", src, re.S)
        outside = src.replace(helper.group(0), "") if helper else src
        hits = [ln.strip() for ln in outside.splitlines() if "os._exit(1)" in ln]
        print(f"  {fn}: {len(hits)} raw exits outside the helper")
        for h in hits:
            fails.append(f"{fn}: a raw os._exit(1) bypasses the banner: {h!r}")
        n_calls = len(re.findall(r"(?:wd_)?self_restart\(", src))
        print(f"  {fn}: {n_calls} routed through the banner")

    print("\n=== 2. the banner is what an operator would actually grep for ===")
    src = open(os.path.join(ROOT, "gpu_watchdog.py")).read()
    for needle in (BANNER, "TRACEBACK (faulthandler, all threads)", "DELIBERATE exit"):
        ok = needle in src
        print(f"  {'present' if ok else 'MISSING'}: {needle}")
        if not ok:
            fails.append(f"the banner lacks {needle!r}")

    print("\n=== 3. it really fires: run it in a child and read the journal-shaped output ===")
    prog = (
        "import sys; sys.path.insert(0, %r)\n"
        "import gpu_watchdog as w\n"
        "w.self_restart('TEST REASON: synthetic', log=lambda m: print(m, flush=True))\n"
        "print('UNREACHABLE')\n" % ROOT)
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    print(f"  exit code: {r.returncode}")
    print("  " + "\n  ".join(out.strip().splitlines()[:4]))
    if r.returncode != 1:
        fails.append(f"self_restart exited {r.returncode}, not 1 — the supervisor keys on rc")
    if "UNREACHABLE" in out:
        fails.append("self_restart returned instead of exiting")
    if BANNER not in out:
        fails.append("the banner never reached the output")
    if "TEST REASON: synthetic" not in out:
        fails.append("the reason was not printed")
    # THE POINT: the operator's first grep must now hit.
    hit = [ln for ln in out.splitlines()
           if re.search(r"traceback|error|exception", ln, re.I)]
    print(f"  lines matching /traceback|error|exception/i: {len(hit)}")
    if not hit:
        fails.append("the first grep an operator runs STILL returns nothing — the whole defect")
    # and the stack dump itself must be there
    if "File " not in out:
        fails.append("no thread stacks were dumped")

    print("\n=== 4. it still works if the watchdog module is missing ===")
    # A guard that cannot fire because its logging helper failed to import is a guard that does not
    # exist, so gpu_analyze carries an inline fallback of the same shape.
    ga = open(os.path.join(ROOT, "gpu_analyze.py")).read()
    fb = re.search(r"def wd_self_restart\(.*?(?=\ndef )", ga, re.S)
    if not fb:
        fails.append("gpu_analyze has no wd_self_restart wrapper")
    else:
        body = fb.group(0)
        for needle in (BANNER, "TRACEBACK", "os._exit(1)"):
            if needle not in body:
                fails.append(f"the fallback path lacks {needle!r} — a missing watchdog would make "
                             "the guard silent again")
        print("  fallback carries the banner, the TRACEBACK word, and the exit")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK — every deliberate exit announces itself in words an operator greps for, exits rc=1, "
          "dumps stacks, and does so even without the watchdog module")
    return 0


if __name__ == "__main__":
    sys.exit(main())
