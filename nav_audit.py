#!/usr/bin/env python3
"""Audit: every HTML page renders the shared header, and every installer that ships a page module
also ships nav_common.py.

    python nav_audit.py          # exits non-zero if anything is missing

WHY THIS EXISTS. The header was added to "every operator page" three times, and each time a page was
missed — /calib-cells, then survey_api's four, then /events — because the page list was built by
hand from the pages already in mind. A hand-built list of pages cannot find the page you forgot.
This derives the list from the source instead: every route declared with response_class=HTMLResponse
is a page, whether or not anyone remembered it.

It also checks the OTHER half, which is what actually broke floorcheck: a module can render the
header perfectly and still show up bare, because the installer that ships it was not in the deploy
set — or ships it without nav_common, which is an ImportError that takes the whole UI down rather
than one page.

Run it in the repo before a deploy. Zero output plus rc=0 means the invariant holds.
"""
import glob
import os
import re
import sys

PAGE_RE = re.compile(r'@\w+\.get\(\s*["\']([^"\']+)["\'][^)]*response_class=HTMLResponse')
GUARD_MARK = "nav_common.py is a HARD"


def main(root="."):
    os.chdir(root)
    mods = sorted(f for f in glob.glob("*.py")
                  if not f.startswith(("apply_", "test_")) and f != "nav_audit.py")
    pages = {}
    for m in mods:
        s = open(m, encoding="utf-8").read()
        routes = PAGE_RE.findall(s)
        if routes:
            pages[m] = {"routes": routes,
                        "nav": ("__NAV__" in s or "nc.header" in s),
                        "imports": "import nav_common" in s}

    applies = {}
    for a in sorted(glob.glob("apply_*.sh")):
        s = open(a, encoding="utf-8").read()
        applies[a] = {"guarded": GUARD_MARK in s,
                      "mods": sorted({m for m in pages if m in s})}

    problems = []
    print(f"{'module':<22} {'routes':<3} {'renders nav':<12} installers")
    print("-" * 86)
    for m, info in sorted(pages.items()):
        owners = [a for a, v in applies.items() if m in v["mods"]]
        if not info["nav"]:
            problems.append(f"{m}: serves {info['routes']} but never renders the shared header")
        if info["nav"] and not info["imports"]:
            problems.append(f"{m}: uses the header but does not import nav_common")
        if not owners:
            problems.append(f"{m}: NO apply_*.sh installs it — it can never be deployed")
        for a in owners:
            if not applies[a]["guarded"]:
                problems.append(f"{a}: installs {m} but does not ship/require nav_common.py")
        print(f"{m:<22} {len(info['routes']):<3} {str(info['nav']):<12} "
              f"{', '.join(o.replace('apply_', '').replace('.sh', '') for o in owners) or 'NONE'}")
    print("-" * 86)
    all_routes = sorted(r for i in pages.values() for r in i["routes"])
    print(f"{len(pages)} page modules, {len(all_routes)} HTML routes: {' '.join(all_routes)}")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  -", p)
        return 1
    print("\nOK — every page renders the shared header, and every installer that ships one ships "
          "nav_common.py too.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
