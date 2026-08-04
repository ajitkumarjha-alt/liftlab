"""Shared navigation — ONE definition of the header, used by every operator page.

The rule this exists to keep: any page reaches any other page family by clicking, and nobody types a
URL. That only stays true if the header is defined once. Copies drift — a link gets added to three
pages and forgotten on the fourth, and the fourth becomes the dead end everyone hits.

Every page renders `__NAV__`, and its route handler replaces that with header(...) + optionally
cam_bar(...). Pages carry no navigation markup of their own.

  header(active)            wordmark -> /dash, then Dash · Ops · Fleet · Floorcheck · Validate
  cam_bar(gw, cam, step)    per-camera context: which camera, back to its dash tab, the setup chain
                            with the current step marked, and a camera switcher
  NAV_CSS                   the styles, injected into each page's own <style>

The Fleet target is DASH_FLEET_URL because that page lives in main.py, not in this repo — a hardcoded
path here would be a guess, and a guess in a nav bar is a dead link on every page at once.
"""
import os

FLEET_URL = os.environ.get("DASH_FLEET_URL", "/fleet")
DASH_GW = os.environ.get("DASH_GW", "site-A")

# Top-level families. Order is fixed and identical everywhere: an operator learns the position of a
# link once. Floorcheck/Validate are gateway-wide entry points here; the camera bar narrows them.
_TOP = [
    ("dash", "Dash", "/dash"),
    ("ops", "Ops", "/ops/{gw}"),
    ("fleet", "Fleet", None),                 # resolved from DASH_FLEET_URL
    ("floorcheck", "Floorcheck", "/floorcheck/{gw}/{cam}"),
    ("validate", "Validate", "/validate"),
    ("reports", "Reports", "/reports"),
]

# The per-camera setup chain, in the order it is actually performed. `collect` is an ACTION (a button
# on the ROI page), not a page, so it renders as a step without a link rather than as a dead one.
_CHAIN = [
    ("rois", "ROIs", "/calib-roi/{gw}/{cam}"),
    ("collect", "collect", None),
    ("label", "label", "/calib-label/{gw}/{cam}"),
    ("cells", "cells", "/calib-cells/{gw}/{cam}"),
]

NAV_CSS = """
.lnav{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:8px 14px;
  border-bottom:1px solid var(--line,#e3e8ec);background:var(--card,#fff);
  font:12px ui-monospace,SFMono-Regular,Menlo,monospace}
.lnav .wm{font:700 14px ui-monospace,Menlo,monospace;color:var(--fg,#1c2429);text-decoration:none;letter-spacing:-.02em}
.lnav a{color:#4c8bf5;text-decoration:none}
.lnav a:hover{text-decoration:underline}
.lnav a.on{color:var(--fg,#1c2429);font-weight:600;text-decoration:none;cursor:default}
.lcam{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:6px 14px;
  border-bottom:1px solid var(--line,#e3e8ec);background:var(--bg,#f6f8fa);
  font:12px ui-monospace,Menlo,monospace}
.lcam .who{font-weight:600;color:var(--fg,#1c2429)}
.lcam a{color:#4c8bf5;text-decoration:none}.lcam a:hover{text-decoration:underline}
.lcam .step{color:var(--mut,#6b7a84)}
.lcam .step.on{color:var(--fg,#1c2429);font-weight:600;background:#e8f0fe;border-radius:10px;padding:1px 8px}
.lcam .arr{color:var(--mut,#6b7a84);opacity:.55}
.lcam select{font:12px ui-monospace,Menlo,monospace;padding:2px 5px;border:1px solid var(--line,#e3e8ec);
  border-radius:6px;background:var(--card,#fff);color:var(--fg,#1c2429)}
@media(prefers-color-scheme:dark){.lcam .step.on{background:#23324a}}
"""


def header(active="", gw=None, cam=None):
    """The same five links, in the same order, on every page. `active` is not a link."""
    gw = gw or DASH_GW
    out = [f'<a class=wm href="/dash">liftlab</a>']
    for key, label, tmpl in _TOP:
        if key == "floorcheck" and not cam:
            # Floorcheck is inherently per-camera. Pointing it at /dash would be a link labelled one
            # thing that lands on another — worse than absent, because the operator stops trusting
            # the header. Gateway-scoped pages pass a default camera so this link is REAL; if there
            # is genuinely no camera to name, the entry is omitted rather than faked.
            continue
        href = FLEET_URL if key == "fleet" else tmpl.format(gw=gw, cam=cam or "")
        if key == active:
            out.append(f'<a class=on href="{href}">{label}</a>')
        else:
            out.append(f'<a href="{href}">{label}</a>')
    return '<div class=lnav>' + "".join(out) + '</div>'


def cam_bar(gw, cam, step=""):
    """Per-camera context: who am I looking at, how do I get back to it on the dash, where am I in
    the setup chain, and how do I switch camera without going via the dash."""
    chain = []
    for key, label, tmpl in _CHAIN:
        if chain:
            chain.append('<span class=arr>→</span>')
        cls = "step on" if key == step else "step"
        if tmpl and key != step:
            chain.append(f'<a class="{cls}" href="{tmpl.format(gw=gw, cam=cam)}">{label}</a>')
        else:
            chain.append(f'<span class="{cls}">{label}</span>')
    return (
        '<div class=lcam>'
        f'<span class=who>{gw} · {cam}</span>'
        f'<a href="/dash?cam={cam}">dash tab</a>'
        f'<a href="/floorcheck/{gw}/{cam}">floorcheck</a>'
        f'<a href="/validate?cam={cam}">validate</a>'
        '<span class=arr>|</span>' + "".join(chain) +
        '<span class=arr>|</span>'
        '<label>camera <select id=lcamsel></select></label>'
        '</div>'
    )


def switcher_js(gw, cam, path_tmpl):
    """Populate the camera switcher from the dashboard's own camera list, and jump to the SAME page
    for the chosen camera — switching camera should not also change what you were looking at."""
    return f"""
<script>
(function(){{
  var sel=document.getElementById('lcamsel'); if(!sel)return;
  fetch('/dash/{gw}/cams').then(function(r){{return r.json()}}).then(function(d){{
    var cams=(d&&d.cameras)||[];
    if(!cams.length){{sel.innerHTML='<option>{cam}</option>';return;}}
    sel.innerHTML=cams.map(function(c){{
      var on=(c.cam==='{cam}')?' selected':'';
      return '<option value="'+c.cam+'"'+on+'>'+c.cam+(c.label?(' '+c.label):'')+(c.enabled?'':' (off)')+'</option>';
    }}).join('');
  }}).catch(function(){{sel.innerHTML='<option>{cam}</option>';}});
  sel.addEventListener('change',function(){{
    if(sel.value) location.href='{path_tmpl}'.replace('__C__', encodeURIComponent(sel.value));
  }});
}})();
</script>"""
