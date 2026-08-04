"""Gateway stub for /reports — the feature moved to dev-box, and says so.

A 404 where a working page used to be reads as breakage, and sends someone hunting through logs for
a fault that does not exist. This says where it went, why, and links it.

Why it moved is not a detail worth hiding: measured 2026-08-04, a 30-day export on this box took
/ops from a ~6s baseline to 26.3s and /dash past its 2s bar. The box has 2 vCPUs and seven live
streams; the export needs CPU they need. Nothing about the export was wrong — this hardware was.
"""
import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import nav_common

reports_moved_router = APIRouter()
REPORTS_URL = os.environ.get("LIFTLAB_REPORTS_URL", "https://dev.gargi.online/reports")

_PAGE = """<!doctype html><meta charset=utf-8><title>liftlab — reports moved</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f8fa;--card:#fff;--fg:#1c2429;--mut:#6b7a84;--line:#e3e8ec}
@media(prefers-color-scheme:dark){:root{--bg:#11161a;--card:#182027;--fg:#e6edf3;--mut:#8b98a5;--line:#242f38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:760px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px}
h2{margin:0 0 10px;font-size:16px}
a.btn{display:inline-block;margin:14px 0 4px;padding:9px 16px;background:#4c8bf5;color:#fff;
 border-radius:8px;text-decoration:none;font-weight:600}
.mut{color:var(--mut);font-size:12px}
table{border-collapse:collapse;margin:12px 0;font-size:12px}
td{padding:3px 12px 3px 0;vertical-align:top}
td:first-child{color:var(--mut);white-space:nowrap}
__NAVCSS__
</style>
__NAV__
<div class=wrap><div class=card>
  <h2>Reports moved to dev-box</h2>
  <p>Report exports are no longer built on this machine. They run on
  <b>dev.gargi.online</b>, and the page there works exactly the same way.</p>
  <a class=btn href="__URL__">Open reports &rarr;</a>
  <p class=mut>Same login as this dashboard.</p>

  <p style="margin-top:18px"><b>Why it moved.</b> Building a workbook here competed with the seven
  live camera streams for two CPUs. Measured on 4 August, during a 30-day export:</p>
  <table>
    <tr><td>/ops</td><td>~6 s normally &rarr; <b>26.3 s</b> during an export</td></tr>
    <tr><td>/dash</td><td>~1 s normally &rarr; <b>3.5 s</b> (its bar is 2 s)</td></tr>
    <tr><td>memory</td><td>never the problem — 394 MB peak against 898 MB free</td></tr>
  </table>
  <p class=mut>The export was not at fault; this hardware is. dev-box has four times the memory and
  no live streams to starve.</p>

  <p style="margin-top:14px"><b>One difference worth knowing.</b> Reports there read a copy of the
  database restored from backup <b>every hour</b>, not the live one. The page shows the exact
  restore point at the top. Data collected in the last hour may not be in it yet.</p>
</div></div>
"""


@reports_moved_router.get("/reports", response_class=HTMLResponse)
def reports_moved():
    return HTMLResponse(_PAGE
                        .replace("__NAVCSS__", nav_common.NAV_CSS)
                        .replace("__NAV__", nav_common.header("reports"))
                        .replace("__URL__", REPORTS_URL))
