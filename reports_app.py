"""dev-box entry point for /reports — the export UI, off the gateway.

Exists because the acceptance measurement on liftlab-cloud was unambiguous: a 30-day export pushed
/ops from ~6s to 26.3s and /dash past its 2s bar, on a 2-vCPU box carrying seven live streams. The
export is not the problem; the hardware is. Here there are 8GB and no streams to starve.

The database is an HOURLY LITESTREAM RESTORE, not the live one. Everything about the page that says
"data as of" exists because of that: an hourly snapshot presented as live data would be a confident
wrong answer, which is worse than no page.

dash_api is mounted only for /dash/{gw}/trends, which the reports page embeds to scope trends to the
selected range. Nothing else here is meant to be a dashboard — the operator dashboard stays on the
gateway, reading live data.
"""
import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

import dash_api
import reports_api

app = FastAPI(title="liftlab reports (dev-box)")
app.include_router(reports_api.reports_router)
app.include_router(dash_api.dash_router)
reports_api.start_supervisor()


@app.get("/healthz")
def healthz():
    st = reports_api.restore_state()
    return {"ok": True, "restore": {k: st.get(k) for k in
                                    ("ok", "restored_at_h", "data_max_ts_h", "stale", "error")}}


@app.get("/")
def root():
    return RedirectResponse("/reports")
