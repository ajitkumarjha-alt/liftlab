#!/usr/bin/env python3
"""STRIP the fabricated compliance verdict from events_api.py — surgical, keeps the
existing _db()/gw_source-JOIN/_esc()/aliases/styles/table/open_valid-flag/fleet link.
Five anchored edits: (1) docstring purpose, (2) delete DECISION_LINE_S, (3) replace the
verdict computation with per-camera physical-open clustering + singleton-only close
distribution, (4) muted <p> counts, (5) remove the decision-line header sentence.
Clusters on the already-selected `os` (sub-ms stable) so the SELECT is untouched.
Fail-CLOSED: any missing anchor aborts with no write; pre-write compile check; backup."""
import os
import pathlib
import shutil
import time

p = pathlib.Path(os.environ.get("EVENTS_API", "/opt/liftlab-b3/cloud/events_api.py"))
s = p.read_text(encoding="utf-8")
if "door-event log" in s or "close-travel withheld" in s:
    print("events_api.py already stripped to facts-only — skip")
    raise SystemExit

EDITS = [
    ("docstring purpose",
     "NOT under /api/gw/*). Per gateway/camera close-travel\n"
     "                        compliance vs the decision line. Events whose OPENING",
     "NOT under /api/gw/*). Per gateway/camera door-event log and\n"
     "                        close-travel distribution — no thresholds, no verdicts;\n"
     "                        the page reports observations, conclusions are the\n"
     "                        reader's. Events whose OPENING"),

    ("DECISION_LINE_S constant",
     'DECISION_LINE_S = float(os.environ.get("DECISION_LINE_S", "2.31"))\n',
     ''),

    ("verdict computation",
     '        cts = [r["ct"] for r in rs if r["ct"] is not None]\n'
     '        med = statistics.median(cts) if cts else None\n'
     '        n_open_clipped = sum(1 for r in rs if not r["ov"])\n'
     '        compliant = (med is not None and med < DECISION_LINE_S)\n'
     '        if med is None:\n'
     '            verdict, vcol = "no close measured", "#7a8b93"\n'
     '        elif compliant:\n'
     '            verdict, vcol = f"{med:.2f}s &lt; {DECISION_LINE_S}s — COMPLIANT", "#63a37e"\n'
     '        else:\n'
     '            verdict, vcol = f"{med:.2f}s &ge; {DECISION_LINE_S}s — NON-COMPLIANT", "#d0574d"\n',
     '        n_open_clipped = sum(1 for r in rs if not r["ov"])\n'
     '        # Cluster emissions of ONE physical opening: rows within 0.5s on the\n'
     '        # sub-ms-stable open edge are the SAME open re-measured (pairing bug\n'
     '        # pending). Opens = clusters; distribution over SINGLETONS only, so a\n'
     '        # cluster\'s disagreeing close-travels are withheld, not averaged.\n'
     '        def _pt(v):\n'
     '            try:\n'
     '                from datetime import datetime as _dt\n'
     '                return _dt.fromisoformat(v)\n'
     '            except Exception:\n'
     '                return None\n'
     '        _rr = sorted(rs, key=lambda r: (r["os"] or ""))\n'
     '        _cl, _cur, _prev = [], [], None\n'
     '        for _r in _rr:\n'
     '            _t = _pt(_r["os"])\n'
     '            if _cur and _prev and _t and (_t - _prev).total_seconds() < 0.5:\n'
     '                _cur.append(_r)\n'
     '            else:\n'
     '                if _cur:\n'
     '                    _cl.append(_cur)\n'
     '                _cur = [_r]\n'
     '            _prev = _t\n'
     '        if _cur:\n'
     '            _cl.append(_cur)\n'
     '        opens = len(_cl)\n'
     '        dupe = sum(1 for g in _cl if len(g) > 1)\n'
     '        _singles = [g[0] for g in _cl if len(g) == 1]\n'
     '        cts = sorted(float(r["ct"]) for r in _singles if r["ct"] is not None)\n'
     '        n = len(cts)\n'
     '        if cts:\n'
     '            p85 = cts[min(n - 1, int(0.85 * n))]\n'
     '            verdict = ("%d opens · %d closes · median %.2fs · p85 %.2fs · min %.2fs · max %.2fs"\n'
     '                       % (opens, n, statistics.median(cts), p85, cts[0], cts[-1]))\n'
     '        else:\n'
     '            verdict = "%d opens · no clean close measured" % opens\n'
     '        dqnote = (" · %d openings close-travel withheld (pairing fix pending)" % dupe) if dupe else ""\n'
     '        vcol = "#7a8b93"\n'),

    ("muted counts line",
     'n={len(rs)} events · {n_open_clipped} with clipped opening (close still counted)</p>',
     '{opens} openings · {n_open_clipped} with clipped opening (close still counted){dqnote}</p>'),

    ("decision-line header sentence",
     'only derived rows arrive here. Decision line: close travel &lt; {DECISION_LINE_S}s.\n',
     'only derived rows arrive here.\n'),
]

missing = [name for name, old, new in EDITS if old not in s]
if missing:
    print("ANCHOR(S) NOT FOUND:", missing, "— NO write. Paste the exact lines so anchors can be fixed.")
    raise SystemExit

for name, old, new in EDITS:
    s = s.replace(old, new, 1)

try:
    compile(s, str(p), "exec")
except SyntaxError as e:
    print(f"patched content does NOT compile ({e}) — aborting, NO write")
    raise SystemExit

shutil.copy(p, f"{p}.bak.{time.strftime('%Y%m%d-%H%M%S')}")
p.write_text(s, encoding="utf-8")
print("events_api.py stripped to facts-only (verdict/DECISION_LINE_S removed, distribution added); backup written")
