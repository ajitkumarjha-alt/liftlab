#!/usr/bin/env python3
"""Patch main.py (mount survey_router), gateway_api.py (survey job -> survey_ready),
and dashboard.html (state badge + Run channel survey button + Survey link + JS).
Anchored, idempotent, backup-first. Run as the liftlab owner."""
import pathlib
import shutil
import time

APP = "/opt/liftlab-b3/cloud"


def patch(path, edits, marker, label):
    p = pathlib.Path(path)
    s = p.read_text()
    if marker in s:
        print(f"  {label}: already patched — skip")
        return
    for old, _ in edits:
        if old not in s:
            print(f"  {label}: ANCHOR NOT FOUND {old[:46]!r} — SKIP (no write)")
            return
    shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
    for old, new in edits:
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"  {label}: patched")


# ---- main.py ----
patch(f"{APP}/main.py", [
    ("from events_api import events_router",
     "from events_api import events_router\nfrom survey_api import survey_router"),
    ("app.include_router(events_router)",
     "app.include_router(events_router)\napp.include_router(survey_router)"),
], "survey_router", "main.py")

# ---- gateway_api.py: on a survey job reporting done -> gateway state survey_ready ----
gw_old = '''        (s.state, s.progress, s.detail, time.time(), job_id, gateway_id)).rowcount
    db.commit(); db.close()'''
gw_new = '''        (s.state, s.progress, s.detail, time.time(), job_id, gateway_id)).rowcount
    if s.state == "done":
        _r = db.execute("SELECT type FROM job WHERE id=?", (job_id,)).fetchone()
        if _r and _r["type"] == "survey":
            db.execute("UPDATE gateway SET state='survey_ready' WHERE id=?", (gateway_id,))
    db.commit(); db.close()'''
patch(f"{APP}/gateway_api.py", [(gw_old, gw_new)], "survey_ready", "gateway_api.py")

# ---- dashboard.html: badge + button + link + JS + CSS ----
badge_old = '''<span class="name">${g.id}</span>'''
badge_new = '''<span class="name">${g.id}</span> <span class="gwstate s-${g.state||'installed'}">${(g.state||'installed').replace('_',' ')}</span>'''

reboot_old = '''<button onclick="if(confirm('Reboot ${g.id}?'))cmd('${g.id}','reboot')">Reboot Pi</button>'''
reboot_new = reboot_old + '''<button onclick="survey('${g.id}')">Run channel survey</button><a class="svlink" href="/survey/${g.id}">Survey ▸</a>'''

style_old = '''</style>'''
style_new = '''.gwstate{font-family:var(--mono);font-size:10px;letter-spacing:.05em;padding:1px 6px;border-radius:3px;background:#1b252b;text-transform:uppercase}.s-installed{color:#7a8b93}.s-survey_needed{color:#e3a53f}.s-survey_ready{color:#4a9eda}.s-channels_marked{color:#63a37e}.svlink{margin-left:4px;font-size:13px;color:#63a37e;text-decoration:none;align-self:center}</style>'''

js_old = '''refresh(); setInterval(refresh,5000);'''
js_new = '''async function survey(gw){if(!confirm("Run a channel survey on "+gw+"? (~1 snapshot/channel)"))return;const r=await fetch("/survey/"+gw+"/start",{method:"POST"});alert(r.ok?"Survey queued - the Pi grabs a snapshot per channel on its next poll (<=20s).":"Failed: "+(await r.text()));refresh();}
refresh(); setInterval(refresh,5000);'''

patch(f"{APP}/dashboard.html",
      [(badge_old, badge_new), (reboot_old, reboot_new),
       (style_old, style_new), (js_old, js_new)],
      "gwstate", "dashboard.html")

print("dashboard/gateway/main patches done")
