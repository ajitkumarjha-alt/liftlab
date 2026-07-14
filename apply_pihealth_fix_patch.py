#!/usr/bin/env python3
"""Fix the Pi-health card flicker: cache last-good HTML, re-inject it when the
5s card rebuild wipes the panel, never blank on an empty/racing fetch, refresh
~10s, and link the header to the live /pihealth graph. Anchored, idempotent."""
import pathlib
import shutil
import time

p = pathlib.Path("/opt/liftlab-b3/cloud/dashboard.html")
s = p.read_text()
if "_healthHTML" in s:
    print("dashboard flicker-fix already applied — skip")
    raise SystemExit

edits = [
    ("const _healthAt={};", "const _healthAt={},_healthHTML={};"),
    ("if(_healthAt[gw]&&now-_healthAt[gw]<25000)return;",
     "if(_healthAt[gw]&&now-_healthAt[gw]<9000)return;"),
    ('const rows=d.rows||[];if(!rows.length){el.innerHTML=\'<span class="phmuted">Pi health: no telemetry yet</span>\';return;}',
     'const rows=d.rows||[];if(!rows.length){if(!_healthHTML[gw])el.innerHTML=\'<span class="phmuted">Pi health: no telemetry yet</span>\';return;}'),
    ('el.innerHTML=\'<div class="phrow"><b>Pi health</b> \'+badge+',
     'el.innerHTML=_healthHTML[gw]=\'<div class="phrow"><b><a href="/pihealth/\'+gw+\'" style="color:#dbe3e6;text-decoration:none">Pi health &rsaquo;</a></b> \'+badge+'),
    ("st.gateways.forEach(g=>renderHealth(g.id));",
     "st.gateways.forEach(g=>{const _e=document.getElementById('ph-'+g.id);if(_e&&_healthHTML[g.id])_e.innerHTML=_healthHTML[g.id];renderHealth(g.id);});"),
]
for old, _ in edits:
    if old not in s:
        print(f"ANCHOR NOT FOUND: {old[:50]!r} — aborting, no write")
        raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
for old, new in edits:
    s = s.replace(old, new, 1)
p.write_text(s)
print("dashboard flicker-fix applied: cache+reinject, no-blank, 10s refresh, graph link")
