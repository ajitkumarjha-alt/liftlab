#!/usr/bin/env python3
"""Move main.py's root page from "/" to "/fleet", so dash_api's / -> /dash redirect goes live.

main.py is not in this repo, so this cannot be an anchored text patch like the others — it finds the
route DECORATOR by pattern and rewrites only its path string. Everything else in the file is left
byte-identical, the original is backed up, and the result is AST-parsed before it is kept.

Handles the shapes a root route is actually written in:
    @app.get("/")                      @app.get("/", response_class=HTMLResponse)
    @app.route("/")                    @app.get('/')
and refuses rather than guesses if it finds none, or more than one.
"""
import ast
import pathlib
import re
import shutil
import sys
import time

APP = "/opt/liftlab-b3/cloud"
MAIN = pathlib.Path(f"{APP}/main.py")
NEW_PATH = "/fleet"

# @app.get("/")  /  @app.route('/', ...)  — the path is the FIRST string argument.
ROOT_DEC = re.compile(r'(@app\.(?:get|route)\(\s*)(["\'])/\2', re.M)


def main():
    if not MAIN.exists():
        print(f"  main.py not at {MAIN} — nothing to do"); return 2
    s = MAIN.read_text()

    if re.search(r'@app\.(?:get|route)\(\s*["\']/fleet["\']', s):
        print("  already moved: main.py serves /fleet — skip"); return 0

    hits = list(ROOT_DEC.finditer(s))
    if not hits:
        print('  NO root route found in main.py (no @app.get("/")).')
        print("  Nothing to move. If the fleet page is served another way (a mount, a static file,")
        print("  or a router), move it by hand and set DASH_FLEET_URL to wherever it lands.")
        return 0
    if len(hits) > 1:
        print(f"  {len(hits)} root-route decorators found — REFUSING to guess which is the fleet page.")
        for h in hits:
            line = s[:h.start()].count("\n") + 1
            print(f"    main.py:{line}: {s[h.start():h.end()+40].splitlines()[0]}")
        return 1

    h = hits[0]
    line = s[:h.start()].count("\n") + 1
    bak = f"{MAIN}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy(MAIN, bak)
    out = s[:h.start()] + f'{h.group(1)}{h.group(2)}{NEW_PATH}{h.group(2)}' + s[h.end():]
    try:
        ast.parse(out)
    except SyntaxError as e:
        print(f"  patch produced invalid Python ({e}) — NOT written; backup at {bak}")
        return 1
    MAIN.write_text(out)
    print(f"  main.py:{line}: root route \"/\" -> \"{NEW_PATH}\"  (backup {bak})")
    print("  dash_api's / -> /dash redirect is now the only handler for /.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
