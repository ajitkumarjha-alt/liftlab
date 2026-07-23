"""Cell drawing wizard — /calib-cells/{gw}/{cam}. The THIRD web wizard piece.

Ends the anchor-number loop. The operator drags TENS, UNITS and ARROW (optionally HUNDREDS) on an
enlarged panel crop instead of reading five within-panel coordinates off a ruler render and typing
them into --anchors. Cells land in roi.json beside the ROIs; door_calib reads them when
DIGIT_CELLS / ARROW_CELL are absent (env still wins).

COORDINATE SPACE — different from /calib-roi, and the reason this is a separate page. ROIs are FRAME
px; cells are WITHIN-PANEL px, measured from the panel crop's top-left. The drawing surface is a
collected panel0 crop (_calib_crop_NNN.png), whose pixel dimensions ARE the panel ROI's dimensions,
so a box drawn on it at 1:1 is already in the right space. The crops are PNG, so the header parse
here reads IHDR rather than a JPEG SOF.

ONE DERIVATION, SERVER-SIDE. The page never computes cells. It POSTs the drawn boxes to /derive and
draws back exactly what the server returns, so the preview the operator approves is the same
arithmetic that gets saved — there is no second implementation in JavaScript to drift from this one.
That matters because the geometry is normalised, not taken literally: an LED matrix is fixed-pitch
and shares one baseline, so hand-drawn boxes are reduced to (pitch, cell_w, top, height) exactly as
door_calib.propose_cells does from typed anchors. The page shows what was normalised.

NO cv2 (this app has none): the confirm overlay is drawn in the browser over the same crop at the
same enlargement, live while dragging and again on the derived result. That is strictly more useful
than door_calib's post-hoc _calib_cells.jpg — it updates as you drag — but it is a DIFFERENT
renderer, so the arithmetic behind it is deliberately the server's, not the browser's.

Routes:
  GET  /calib-cells/{gw}/{cam}          the drawing page
  GET  /calib-cells/{gw}/{cam}/state    crops (+labels), panel dims, saved cells
  POST /calib-cells/{gw}/{cam}/derive   drawn boxes -> normalised cells + warnings (NO write)
  POST /calib-cells/{gw}/{cam}/save     derive + persist into roi.json (atomic)
"""
import json
import os
import re
import time
from pathlib import Path

import nav_common as nc
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
# The panel ROI extends below the digits to the queue/status line. Cells reaching into the bottom of
# the panel usually mean the box was dragged past the digit baseline onto that line, which poisons
# every template built from it. A HEURISTIC (this module cannot see where the line actually is), so
# it warns and never blocks.
QUEUE_ZONE_FRAC = float(os.environ.get("CELLS_QUEUE_ZONE_FRAC", "0.80"))
MIN_SIDE = 3                                     # a 2px cell is a misclick
# How far a drawn digit may sit off perfect pitch and still be kept as a real residual. Small, because
# the pitch itself is not in doubt — only where each glyph sits within its step.
X_RESIDUAL = int(os.environ.get("CELLS_X_RESIDUAL", "2"))
# Past this, an offset from the uniform grid is more likely a bad drag than a slant worth encoding.
OFFSET_WARN = int(os.environ.get("CELLS_OFFSET_WARN", "4"))

calib_cells_router = APIRouter()


def _safe(*p):
    for x in p:
        if not x or not _SAFE.match(x):
            raise HTTPException(400, "bad name")


def _dir(gw, cam):
    _safe(gw, cam)
    return CALIB_DIR / gw / cam


def _png_wh(path):
    """(w, h) from a PNG IHDR — stdlib only, same reason as the JPEG parse in calib_roi_api."""
    try:
        b = path.read_bytes()[:33]
    except OSError:
        return None
    if len(b) < 24 or b[:8] != b"\x89PNG\r\n\x1a\n" or b[12:16] != b"IHDR":
        return None
    w = int.from_bytes(b[16:20], "big")
    h = int.from_bytes(b[20:24], "big")
    return (w, h) if w and h else None


def _crops(d):
    return sorted(p.name for p in d.glob("_calib_crop_*.png")) if d.exists() else []


def _labels(d):
    """Labels from /calib-label, if it has run — shown beside each crop so the operator can pick a
    two-digit + arrow crop (the one this geometry needs) instead of hunting blind."""
    try:
        v = json.loads((d / "labels.json").read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _roi_path(d):
    return d / "roi.json"


def _load_roi(d):
    try:
        v = json.loads(_roi_path(d).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _rect(v, what):
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise HTTPException(400, f"{what}: expected [x,y,w,h]")
    try:
        x, y, w, h = (int(round(float(n))) for n in v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what}: non-numeric value")
    if w < MIN_SIDE or h < MIN_SIDE:
        raise HTTPException(400, f"{what}: {w}x{h} is too small (min {MIN_SIDE}px)")
    return [x, y, w, h]


def _derive_cells(tens, units, arrow, hundreds, panel_wh):
    """Drawn boxes -> the cell geometry door_calib will crop with.

    NORMALISE WHAT IS PHYSICALLY FIXED, PRESERVE WHAT IS PHYSICALLY VARIABLE.

    Fixed: the pitch and the cell size. An LED matrix has one character pitch and one glyph box, so
    the pitch (measured over the tens->units span, the longest baseline available) is a better
    measure of both than any single hand-drawn edge.

    NOT fixed: the vertical position of each cell. These displays SLANT — fitcells measured a [1,3]
    offset on ch29's tens cell, and ch16 shows the same tilt. An earlier version of this function
    forced one baseline across all cells, which erased exactly that slant and left fitcells to
    rediscover it from labelled crops after the build. So each cell keeps ITS OWN y as drawn, and a
    small x residual (±{X_RESIDUAL}px) off perfect pitch is kept too. The operator's drags encode the
    slant directly.

    Output is the same structure fitcells emits: per-cell [x,y,w,h] with independent x/y and shared
    w/h, plus `offsets` = per-cell [dx,dy] from the uniform grid — the identical meaning and sign
    convention as fitcells' `moves`, so the two are directly comparable.
    """
    Wp, Hp = panel_wh
    warn = []
    kept, normalised = [], []
    tens_left, units_left, arrow_left = tens[0], units[0], arrow[0]

    if units_left <= tens_left:
        raise HTTPException(400, f"UNITS must be to the RIGHT of TENS (tens x={tens_left}, "
                                 f"units x={units_left}) — did the two boxes get swapped?")
    if arrow_left < units_left:
        raise HTTPException(400, f"ARROW must be to the RIGHT of UNITS (units x={units_left}, "
                                 f"arrow x={arrow_left})")
    pitch = units_left - tens_left

    # The uniform-grid REFERENCE the offsets are measured against — the geometry the old normalising
    # version would have produced. Keeping it explicit is what makes `offsets` mean the same thing
    # as fitcells' `moves` (both are "how far from the uniform grid").
    ref_y = int(round((tens[1] + units[1]) / 2))
    height = int(round((tens[3] + units[3]) / 2))
    normalised.append(f"height {height}px (shared — one glyph box)")
    if abs(tens[3] - units[3]) > 2:
        warn.append(f"TENS and UNITS heights differ by {abs(tens[3] - units[3])}px — shared height "
                    f"{height}px used (the glyph box is one size; only position varies)")

    cell_w = pitch if (arrow_left - units_left) >= pitch else max(1, arrow_left - units_left)
    normalised.append(f"cell width {cell_w}px (from pitch {pitch}px)")
    if cell_w < pitch:
        warn.append(f"cell width trimmed {pitch}->{cell_w}px so the units cell cannot reach the arrow")
    drawn_w = int(round((tens[2] + units[2]) / 2))
    if abs(drawn_w - cell_w) > 2:
        warn.append(f"drawn digit width ~{drawn_w}px replaced by the pitch-derived {cell_w}px "
                    f"(fixed-pitch matrix — the pitch is the reliable measure, not one drawn edge)")

    # ── per-cell x: pitch position, plus the drawn residual within ±X_RESIDUAL ──────────────
    def _x_for(ideal, drawn, tag):
        if drawn is None:
            return ideal, 0
        res = drawn - ideal
        if abs(res) > X_RESIDUAL:
            clamped = max(-X_RESIDUAL, min(X_RESIDUAL, res))
            warn.append(f"{tag} x is {res:+d}px off perfect pitch — kept {clamped:+d}px "
                        f"(beyond ±{X_RESIDUAL}px contradicts a fixed-pitch matrix; check the drag)")
            res = clamped
        return ideal + res, res

    ideal_x = [units_left - 2 * pitch, units_left - pitch, units_left]
    xs, x_res = [], []
    for i, (ide, drawn) in enumerate(zip(ideal_x, (hundreds[0] if hundreds else None, tens_left, units_left))):
        x, r = _x_for(ide, drawn, f"d{i}")
        xs.append(x)
        x_res.append(r)

    # ── per-cell y: AS DRAWN. This is the slant. ────────────────────────────────────────────
    # d0 is usually not drawn (it holds a 3rd char / P-prefix that rarely lights), so extrapolate the
    # slant the drawn pair establishes rather than dropping it onto the shared baseline: one more
    # pitch to the left is one more step of the same tilt.
    slant = units[1] - tens[1]
    if hundreds is not None:
        ys = [hundreds[1], tens[1], units[1]]
    else:
        ys = [tens[1] - slant, tens[1], units[1]]
        if slant:
            normalised.append(f"d0 y extrapolated from the {slant:+d}px tens→units tilt")
    kept.append(f"per-cell y as drawn (tilt {slant:+d}px across one pitch)")
    # The tilt itself, checked directly. Per-cell offsets alone can miss this: with HUNDREDS drawn,
    # a steep tilt spreads into several offsets that are each under the threshold while the slope is
    # plainly implausible. A real display slant is a pixel or three per pitch (fitcells measured 3 on
    # ch29); anything much steeper is a drag that wandered.
    if abs(slant) > OFFSET_WARN:
        warn.append(f"tilt is {slant:+d}px per {pitch}px of pitch — that is a very steep slant for an "
                    f"LED panel. Real ones run a few px; check TENS and UNITS are on the same row.")
    if any(r for r in x_res):
        kept.append("x residual " + ", ".join(f"d{i}{r:+d}" for i, r in enumerate(x_res) if r))

    def _fit(x, y, cw, ch, tag):
        if x < 0:
            warn.append(f"{tag} left {x}<0 -> clamped to 0 (cell narrowed)")
            cw += x
            x = 0
        if x + cw > Wp:
            warn.append(f"{tag} right {x + cw}>{Wp} -> clamped to the panel edge")
            cw = Wp - x
        return int(x), int(y), int(max(1, cw)), int(ch)

    # fitcells-compatible per-cell offsets from the uniform grid (same sign convention as `moves`).
    # Measured on the INTENDED geometry, before the panel-edge clamp: d0's ideal x is routinely
    # negative (one pitch left of tens falls off a tight panel), and clamping it to 0 would otherwise
    # register as a large spurious offset and fire the bad-drag warning on a perfectly good drag.
    # The clamp is already reported on its own.
    offsets = {f"d{i}": [xs[i] - ideal_x[i], ys[i] - ref_y] for i in range(3)}
    offsets["arrow"] = [0, arrow[1] - ref_y]
    for tag, (dx, dy) in offsets.items():
        if abs(dx) > OFFSET_WARN or abs(dy) > OFFSET_WARN:
            warn.append(f"{tag} sits [{dx:+d},{dy:+d}]px off the uniform grid — beyond ±{OFFSET_WARN}px "
                        f"that is more likely a bad drag than a real slant. Check the overlay.")

    cells = [list(_fit(xs[i], ys[i], cell_w, height, f"d{i}")) for i in range(3)]
    arrow_cell = list(_fit(arrow_left, arrow[1], arrow[2], height, "arrow"))
    top = ref_y

    # Overlap checks (requirement: warn on overlap). The pitch derivation makes digit-vs-digit
    # overlap impossible, but a hand-placed HUNDREDS or a narrow panel can still produce one.
    boxes = [(f"d{i}", c) for i, c in enumerate(cells)] + [("arrow", arrow_cell)]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (n1, a), (n2, b) = boxes[i], boxes[j]
            if a[0] < b[0] + b[2] and b[0] < a[0] + a[2]:
                warn.append(f"{n1} and {n2} OVERLAP horizontally ({n1} x={a[0]}..{a[0]+a[2]}, "
                            f"{n2} x={b[0]}..{b[0]+b[2]}) — templates from overlapping cells confuse glyphs")

    if top + height > Hp * QUEUE_ZONE_FRAC:
        warn.append(f"cells reach y={top + height} of a {Hp}px panel (past {int(QUEUE_ZONE_FRAC * 100)}%) "
                    f"— the queue/status line usually sits along the bottom; if it is inside the cells "
                    f"every template learns it. Check the overlay.")
    if top < 0 or top + height > Hp:
        warn.append(f"cells fall outside the panel vertically (y={top}..{top + height}, panel {Hp}px)")

    return {"digit_cells": cells, "arrow_cell": arrow_cell,
            "pitch": int(pitch), "cell_w": int(cell_w), "cell_y": int(top), "cell_h": int(height),
            "panel_wh": [int(Wp), int(Hp)], "warnings": warn,
            "hundreds_drawn": hundreds is not None,
            # Same shape and meaning as fitcells' `moves`: per-cell [dx,dy] from the uniform grid.
            # A build made from these should leave fitcells with little left to move.
            "offsets": offsets, "slant_px_per_pitch": int(slant),
            "kept": kept, "normalised": normalised,
            # The equivalent --anchors line, so the wizard and the CLI remain mutually intelligible.
            "anchors": {"tens_left": int(tens_left), "units_left": int(units_left),
                        "digit_top": int(top), "digit_bottom": int(top + height),
                        "arrow_left": int(arrow_left)},
            "digit_cells_str": ";".join(f"{x},{y},{w},{h}" for x, y, w, h in cells),
            "arrow_cell_str": ",".join(str(v) for v in arrow_cell)}


def _panel_wh(d, crop):
    p = d / crop
    wh = _png_wh(p)
    if not wh:
        raise HTTPException(404, f"{crop}: not a readable PNG (collect crops first)")
    return wh


@calib_cells_router.get("/calib-cells/{gw}/{cam}/state")
def cells_state(gw: str, cam: str):
    d = _dir(gw, cam)
    crops = _crops(d)
    labels = _labels(d)
    saved = _load_roi(d).get("cells") or {}
    wh = None
    if crops:
        wh = _png_wh(d / crops[len(crops) // 2])
    return JSONResponse({
        "gw": gw, "cam": cam,
        "crops": crops,
        "labels": {c: labels.get(c) for c in crops if labels.get(c)},
        "n_crops": len(crops),
        "panel_wh": list(wh) if wh else None,
        "saved": saved,
        "min_side": MIN_SIDE,
        "roi_saved": bool(_load_roi(d).get("panel_rois")),
    })


async def _drawn(request, d, crop):
    body = await request.json()
    panel_wh = _panel_wh(d, crop)
    tens = _rect(body.get("tens"), "TENS")
    units = _rect(body.get("units"), "UNITS")
    arrow = _rect(body.get("arrow"), "ARROW")
    hundreds = _rect(body.get("hundreds"), "HUNDREDS") if body.get("hundreds") else None
    return _derive_cells(tens, units, arrow, hundreds, panel_wh), (tens, units, arrow, hundreds)


@calib_cells_router.post("/calib-cells/{gw}/{cam}/derive")
async def cells_derive(gw: str, cam: str, request: Request, crop: str = ""):
    """Normalise drawn boxes into cells WITHOUT writing. The page calls this on every drag so the
    preview it shows is the server's arithmetic, not a JS copy of it."""
    d = _dir(gw, cam)
    _safe(crop)
    res, _ = await _drawn(request, d, crop)
    return res


@calib_cells_router.post("/calib-cells/{gw}/{cam}/save")
async def cells_save(gw: str, cam: str, request: Request, crop: str = ""):
    d = _dir(gw, cam)
    _safe(crop)
    res, drawn = await _drawn(request, d, crop)
    roi = _load_roi(d)
    roi["cells"] = {
        "digit_cells": res["digit_cells"], "arrow_cell": res["arrow_cell"],
        "pitch": res["pitch"], "cell_w": res["cell_w"], "panel_wh": res["panel_wh"],
        "anchors": res["anchors"],
        # Provenance: which crop these were drawn on, and the raw boxes before normalisation. The raw
        # boxes are what makes a later "why is d1 2px left?" answerable without redrawing.
        "anchor_crop": crop, "drawn": {"tens": drawn[0], "units": drawn[1], "arrow": drawn[2],
                                       "hundreds": drawn[3]},
        "warnings": res["warnings"],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    d.mkdir(parents=True, exist_ok=True)
    tmp = _roi_path(d).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roi, indent=2))
    os.replace(tmp, _roi_path(d))
    return {"ok": True, **res, "anchor_crop": crop}


@calib_cells_router.get("/calib-cells/{gw}/{cam}", response_class=HTMLResponse)
def cells_page(gw: str, cam: str):
    _safe(gw, cam)
    nav = nc.header('', gw, cam) + nc.cam_bar(gw, cam, 'cells')
    page = (_PAGE.replace("__GW__", gw).replace("__CAM__", cam)
                 .replace("__NAV__", nav)
                 .replace("</style>", nc.NAV_CSS + "</style>", 1))
    page += nc.switcher_js(gw, cam, f"/calib-cells/{gw}/__C__")
    return HTMLResponse(page)


_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Cells __CAM__ @ __GW__</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--line:#e3e8ec;--fg:#1c2429;--mut:#6b7a84;--ok:#2f9e5f;--warn:#d98a1f;--bad:#d4483b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e1418;--card:#161d22;--line:#243038;--fg:#d6dee3;--mut:#7f9099}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h1{font:600 15px var(--mono);margin:0}
.pill{font:11px var(--mono);padding:2px 8px;border-radius:10px;background:var(--line);color:var(--mut)}
a.pill{text-decoration:none;color:#4c8bf5}
.wrap{padding:14px;display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.stage{position:relative;display:inline-block;background:#000;border:1px solid var(--line);border-radius:8px;overflow:hidden;line-height:0}
.stage img{display:block;image-rendering:pixelated;user-select:none;-webkit-user-drag:none}
.box{position:absolute;border:2px solid;pointer-events:none;font:10px var(--mono);color:#fff}
.box b{position:absolute;top:-1px;left:-1px;padding:0 3px;background:inherit;line-height:13px}
.box.d0{border-color:#ffb400}.box.d0 b{background:#ffb400}
.box.d1{border-color:#00dc78}.box.d1 b{background:#00dc78}
.box.d2{border-color:#00b4ff}.box.d2 b{background:#00b4ff}
.box.arrow{border-color:#c800c8}.box.arrow b{background:#c800c8}
.box.live{border-style:dashed;border-color:#d98a1f}
.side{min-width:320px;flex:1;max-width:520px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
.card h3{margin:0 0 8px;font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}
button{font:600 13px system-ui;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);cursor:pointer;margin:0 6px 6px 0}
button.on{background:#4c8bf5;border-color:#4c8bf5;color:#fff}
button.pri{background:#2f9e5f;border-color:#2f9e5f;color:#fff}
button:disabled{opacity:.4;cursor:default}
.kv{display:flex;justify-content:space-between;gap:10px;padding:2px 0;border-bottom:1px solid var(--line);font:12px var(--mono)}
.kv:last-child{border-bottom:0}
.msg{font:12px var(--mono);min-height:18px;margin-top:6px}
.msg.err{color:var(--bad)}.msg.ok{color:var(--ok)}
.note{color:var(--mut);font:12px/1.5 var(--mono)}
.warnbox{background:#fff8ec;border:1px solid #e8c88a;color:#7a5200;border-radius:8px;padding:8px 10px;font:12px/1.6 var(--mono);margin-bottom:10px}
@media(prefers-color-scheme:dark){.warnbox{background:#2a2113;border-color:#5c4a24;color:#e8c88a}}
select,input[type=range]{font:12px var(--mono);padding:4px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
code{font:11px var(--mono);background:var(--line);padding:1px 4px;border-radius:4px;word-break:break-all}

#runlog{background:#0e1418;color:#cfe3d6;font:11px/1.45 var(--mono);padding:8px;border-radius:6px;
  max-height:230px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:8px 0 0}
#runlog:empty{display:none}
.runbtn{font:600 12px system-ui;padding:7px 11px;border:1px solid var(--line);border-radius:8px;
  background:var(--card);color:var(--fg);cursor:pointer;margin:0 6px 6px 0}
.runbtn:disabled{opacity:.45;cursor:default}

</style></head><body>
__NAV__
<header>
  <h1>cells · __CAM__ @ __GW__</h1>
  <span class=pill id=cropcount>—</span>
  <span class=pill id=panelwh>—</span>
  <a class=pill href="/calib-roi/__GW__/__CAM__">← ROIs</a>
  <a class=pill href="/calib-label/__GW__/__CAM__">labels</a>
</header>
<div class=wrap>
  <div>
    <div class=stage id=stage><img id=crop alt="panel crop"><div id=boxes></div></div>
    <div class=note id=hint style="margin-top:6px">pick a crop showing TWO digits and an arrow, then drag the boxes</div>
  </div>
  <div class=side>
    <div id=warnings></div>
    <div class=card>
      <h3>1 · crop</h3>
      <div class=kv><span>crop</span><b><select id=sel></select></b></div>
      <div class=kv><span>zoom</span><b><button id=zo>−</button><span id=zval>8x</span><button id=zi>+</button></b></div>
      <div class=note>Choose one with a two-digit floor AND an arrow — that is the geometry these cells must fit. Labels are shown when /calib-label has run.</div>
    </div>
    <div class=card>
      <h3>2 · drag the boxes</h3>
      <button id=bT class=on>TENS</button>
      <button id=bU>UNITS</button>
      <button id=bA>ARROW</button>
      <button id=bH>HUNDREDS <span class=note>(optional)</span></button>
      <div class=note style="margin-top:6px">HUNDREDS is optional — left out, it is derived one pitch left of TENS and clamped to the panel.</div>
    </div>
    <div class=card>
      <h3>3 · derived cells</h3>
      <div class=kv><span>pitch</span><b id=vPitch>—</b></div>
      <div class=kv><span>cell w × h</span><b id=vWH>—</b></div>
      <div class=kv><span>tilt / pitch</span><b id=vSlant>—</b></div>
      <div class=kv><span>offsets [dx,dy]</span><b id=vOff>—</b></div>
      <div class=note id=vKept style="margin:4px 0 0"></div>
      <div class=kv><span>DIGIT_CELLS</span><b><code id=vDC>—</code></b></div>
      <div class=kv><span>ARROW_CELL</span><b><code id=vAC>—</code></b></div>
      <div style="margin-top:8px">
        <button id=clear>Clear</button>
        <button id=save class=pri disabled>Save cells</button>
      </div>
      <div class=msg id=msg></div>
    </div>
<div class=card id=fitcard style="display:none">
  <h3>last fitcells result <span class=note style="font-weight:400" id=fitwhen></span></h3>
  <div id=fitscores></div>
  <img id=fitoverlay style="max-width:100%;margin-top:6px;image-rendering:pixelated;border:1px solid var(--line);border-radius:6px" alt="fitcells overlay">
  <div class=note id=fitnote style="margin-top:4px"></div>
</div>
<div class=card id=runcard>
  <h3>run <span class=note style="font-weight:400" id=runwhy></span></h3>
  <div id=runbtns></div>
  <div class=msg id=runmsg></div>
  <pre id=runlog></pre>
</div>
<script>
// ---- run buttons (wizard piece 4) ----
// Shared by /calib-roi, /calib-cells and /calib-label. Starts a door_calib subprocess on the cloud
// and polls its output; polling rather than SSE so a reload or a dropped lobby connection does not
// lose the run. Buttons disable while anything is running for THIS camera — two collects at once
// would interleave crops.
// Self-contained: this block sits ABOVE the page's main <script>, so it cannot rely on that
// script's GW/CAM having been evaluated yet. The template placeholders are substituted
// server-side, so reading them here is order-independent.
var RGW="__GW__", RCAM="__CAM__";
var RUN={since:0,timer:null,busy:false};
function runBtnHtml(jobs){
  return (jobs||[]).map(function(j){
    return '<button class=runbtn data-job="'+j.id+'" title="'+escR(j.help)+'">'+escR(j.label)+'</button>';
  }).join('')+'<button class=runbtn id=runstop style="display:none">Stop</button>';
}
function escR(s){return s==null?'':(''+s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function runPaint(d){
  var btns=document.querySelectorAll('.runbtn');
  for(var i=0;i<btns.length;i++){
    if(btns[i].id==='runstop'){btns[i].style.display=d.running?'':'none';continue;}
    btns[i].disabled=d.running||!(d.runner&&d.runner.ok);
  }
  document.getElementById('runwhy').textContent=(d.runner&&d.runner.ok)?'':(d.runner?d.runner.reason:'');
  var m=document.getElementById('runmsg');
  if(d.running){m.className='msg';m.textContent=d.job+' running… '+Math.round(d.elapsed_s)+'s';}
  else if(d.rc===0){m.className='msg ok';m.textContent=d.job+' finished OK in '+Math.round(d.elapsed_s)+'s';}
  else if(d.rc!=null){m.className='msg err';m.textContent=d.job+' FAILED rc='+d.rc+' — see the output';}
}
function runPoll(){
  fetch('/calib-run/'+RGW+'/'+RCAM+'/status?since='+RUN.since).then(function(r){return r.json()}).then(function(d){
    if(!document.getElementById('runbtns').innerHTML){document.getElementById('runbtns').innerHTML=runBtnHtml(d.jobs);}
    if(d.lines&&d.lines.length){
      var pre=document.getElementById('runlog');
      pre.textContent+=(pre.textContent?'\n':'')+d.lines.join('\n');
      pre.scrollTop=pre.scrollHeight;
      RUN.since=d.next_since;
    }
    var wasBusy=RUN.busy; RUN.busy=!!d.running;
    runPaint(d);
    if(d.running){ if(!RUN.timer)RUN.timer=setInterval(runPoll,1500); }
    else if(RUN.timer){ clearInterval(RUN.timer); RUN.timer=null;
      // A finished job usually changed what this page shows (a new frame, new crops, new cells) —
      // reload the page's own state rather than making the operator guess whether it took.
      if(wasBusy&&typeof runFinished==='function')runFinished(d);
    }
  }).catch(function(){});
}
document.addEventListener('click',function(e){
  var b=e.target; if(!b.classList||!b.classList.contains('runbtn')||b.disabled)return;
  if(b.id==='runstop'){fetch('/calib-run/'+RGW+'/'+RCAM+'/stop',{method:'POST'}).then(runPoll);return;}
  var job=b.getAttribute('data-job'); if(!job)return;
  b.disabled=true;
  fetch('/calib-run/'+RGW+'/'+RCAM+'/'+job,{method:'POST'}).then(function(r){
    return r.json().then(function(j){
      if(!r.ok){document.getElementById('runmsg').className='msg err';
                document.getElementById('runmsg').textContent=j.detail||'could not start';}
      runPoll();
    });
  }).catch(function(){runPoll()});
});
runPoll();
</script>
    <div class=card>
      <h3>next</h3>
      <div class=note>flow: <a href="/calib-roi/__GW__/__CAM__">draw ROIs</a> → collect → <a href="/calib-label/__GW__/__CAM__">label</a> → draw cells → <code>door_calib --build</code>.
      Cells are used only when <code>DIGIT_CELLS</code>/<code>ARROW_CELL</code> are unset — env still wins.</div>
    </div>
  </div>
</div>
<script>
var GW="__GW__", CAM="__CAM__";
var $=function(id){return document.getElementById(id)};
function esc(s){return s==null?'':(''+s).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
var S=null, zoom=8, mode="tens", crop=null;
var D={tens:null,units:null,arrow:null,hundreds:null};   // drawn, WITHIN-PANEL px
var DERIVED=null, drag=null, pending=false;

function nat(){return (S&&S.panel_wh)?S.panel_wh:[1,1]}
function disp(){var i=$("crop");return [i.clientWidth||1,i.clientHeight||1]}
function toPanel(x,y,w,h){var n=nat(),dd=disp();
  return [Math.round(x/dd[0]*n[0]),Math.round(y/dd[1]*n[1]),Math.round(w/dd[0]*n[0]),Math.round(h/dd[1]*n[1])];}
function toDisp(r){var n=nat(),dd=disp();
  return [r[0]/n[0]*dd[0],r[1]/n[1]*dd[1],r[2]/n[0]*dd[0],r[3]/n[1]*dd[1]];}

function drawBoxes(live){
  var h="";
  function add(r,cls,lab){if(!r)return;var d=toDisp(r);
    h+='<div class="box '+cls+'" style="left:'+d[0]+'px;top:'+d[1]+'px;width:'+d[2]+'px;height:'+d[3]+'px"><b>'+lab+'</b></div>';}
  if(DERIVED){                                   // approve the SERVER's cells, not the raw drag
    add(DERIVED.digit_cells[0],"d0","d0"); add(DERIVED.digit_cells[1],"d1","d1");
    add(DERIVED.digit_cells[2],"d2","d2"); add(DERIVED.arrow_cell,"arrow","arw");
  } else {
    add(D.hundreds,"d0","H"); add(D.tens,"d1","T"); add(D.units,"d2","U"); add(D.arrow,"arrow","A");
  }
  if(live){h+='<div class="box live" style="left:'+live[0]+'px;top:'+live[1]+'px;width:'+live[2]+'px;height:'+live[3]+'px"></div>';}
  $("boxes").innerHTML=h;
}
function setMode(m){mode=m;
  ["tens","units","arrow","hundreds"].forEach(function(k){
    $({tens:"bT",units:"bU",arrow:"bA",hundreds:"bH"}[k]).className=(k===m)?"on":"";});
  $("hint").textContent="drag the "+m.toUpperCase()+" box";
}
function msg(t,c){$("msg").textContent=t;$("msg").className="msg "+(c||"")}
function applyZoom(){var n=nat();$("crop").style.width=(n[0]*zoom)+"px";$("zval").textContent=zoom+"x";drawBoxes(null);}

function derive(then){
  if(!(D.tens&&D.units&&D.arrow)){DERIVED=null;show();drawBoxes(null);return;}
  if(pending)return; pending=true;
  fetch("/calib-cells/"+GW+"/"+CAM+"/derive?crop="+encodeURIComponent(crop),
    {method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify({tens:D.tens,units:D.units,arrow:D.arrow,hundreds:D.hundreds})})
    .then(function(r){return r.json().then(function(j){
      pending=false;
      if(!r.ok){DERIVED=null;msg(j.detail||"derive failed","err");show();drawBoxes(null);return;}
      DERIVED=j; msg(""); show(); drawBoxes(null); if(then)then();
    })}).catch(function(){pending=false;msg("derive failed","err")});
}
function show(){
  var j=DERIVED;
  $("vPitch").textContent=j?(j.pitch+"px"):"—";
  $("vWH").textContent=j?(j.cell_w+" × "+j.cell_h+"px"):"—";
  $("vSlant").textContent=j?((j.slant_px_per_pitch>0?"+":"")+j.slant_px_per_pitch+"px"):"—";
  // The per-cell offsets from the uniform grid — the same numbers, sign convention and meaning as
  // door_calib --fitcells reports as `moves`. Near-zero after a build means the drag already
  // encoded the slant and fitcells has nothing left to find.
  $("vOff").textContent=j?["d0","d1","d2","arrow"].map(function(k){
    var o=(j.offsets||{})[k]; return o?(k+" ["+(o[0]>0?"+":"")+o[0]+","+(o[1]>0?"+":"")+o[1]+"]"):"";
  }).filter(Boolean).join("  "):"—";
  $("vKept").innerHTML=j?("<b>kept</b> "+(j.kept||[]).map(esc).join("; ")
    +"<br><b>normalised</b> "+(j.normalised||[]).map(esc).join("; ")):"";
  $("vDC").textContent=j?j.digit_cells_str:"—";
  $("vAC").textContent=j?j.arrow_cell_str:"—";
  $("save").disabled=!j;
  var w=(j&&j.warnings)||[];
  $("warnings").innerHTML=w.length?('<div class=warnbox><b>'+w.length+' thing'+(w.length>1?'s':'')+' to check</b><br>• '+w.join("<br>• ")+'</div>'):"";
}

$("stage").addEventListener("mousedown",function(e){
  if(e.target.id!=="crop")return;
  var b=$("crop").getBoundingClientRect(); drag={x0:e.clientX-b.left,y0:e.clientY-b.top}; e.preventDefault();});
window.addEventListener("mousemove",function(e){
  if(!drag)return; var b=$("crop").getBoundingClientRect();
  var x=Math.max(0,Math.min(b.width,e.clientX-b.left)), y=Math.max(0,Math.min(b.height,e.clientY-b.top));
  drag.x1=x;drag.y1=y;
  drawBoxes([Math.min(drag.x0,x),Math.min(drag.y0,y),Math.abs(x-drag.x0),Math.abs(y-drag.y0)]);});
window.addEventListener("mouseup",function(){
  if(!drag)return;
  if(drag.x1!=null){
    var x=Math.min(drag.x0,drag.x1),y=Math.min(drag.y0,drag.y1);
    var p=toPanel(x,y,Math.abs(drag.x1-drag.x0),Math.abs(drag.y1-drag.y0));
    if(p[2]>=(S.min_side||3)&&p[3]>=(S.min_side||3)){
      D[mode]=p; DERIVED=null;
      if(mode==="tens")setMode("units"); else if(mode==="units")setMode("arrow");
      derive();
    } else { msg("too small ("+p[2]+"x"+p[3]+" panel px) — ignored","err"); }
  }
  drag=null; if(!DERIVED)drawBoxes(null);});

$("bT").onclick=function(){setMode("tens")}; $("bU").onclick=function(){setMode("units")};
$("bA").onclick=function(){setMode("arrow")}; $("bH").onclick=function(){setMode("hundreds")};
$("clear").onclick=function(){D={tens:null,units:null,arrow:null,hundreds:null};DERIVED=null;msg("");show();drawBoxes(null);setMode("tens")};
$("zi").onclick=function(){zoom=Math.min(20,zoom+2);applyZoom()};
$("zo").onclick=function(){zoom=Math.max(2,zoom-2);applyZoom()};
$("sel").onchange=function(){crop=$("sel").value;$("crop").src="/calib/"+GW+"/"+CAM+"/"+crop+"?t="+Date.now();};
$("save").onclick=function(){
  msg("saving…");
  fetch("/calib-cells/"+GW+"/"+CAM+"/save?crop="+encodeURIComponent(crop),
    {method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify({tens:D.tens,units:D.units,arrow:D.arrow,hundreds:D.hundreds})})
    .then(function(r){return r.json().then(function(j){
      if(!r.ok){msg(j.detail||"save failed","err");return;}
      msg("saved into roi.json ✓ — cells are live for door_calib --build","ok");
    })}).catch(function(){msg("save failed","err")});
};

function loadCells(){
 return fetch("/calib-cells/"+GW+"/"+CAM+"/state").then(function(r){return r.json()}).then(function(d){
  S=d;
  $("cropcount").textContent=d.n_crops+" crops";
  $("panelwh").textContent=d.panel_wh?("panel "+d.panel_wh[0]+"×"+d.panel_wh[1]+"px"):"panel size unknown";
  if(!d.n_crops){
    $("warnings").innerHTML='<div class=warnbox>No crops collected yet. Run <code>door_calib --collect</code> for '+CAM+' first — cells are drawn on a real panel crop.'
      +(d.roi_saved?'':'<br><br>ROIs are not saved either — start at <a href="/calib-roi/'+GW+'/'+CAM+'">draw ROIs</a>.')+'</div>';
    return;
  }
  $("sel").innerHTML=d.crops.map(function(c){
    var lab=d.labels[c]?(" — "+d.labels[c]):"";
    return '<option value="'+c+'">'+c.replace("_calib_crop_","").replace(".png","")+lab+'</option>';}).join("");
  crop=d.crops[Math.floor(d.crops.length/2)]; $("sel").value=crop;
  $("crop").onload=function(){applyZoom()};
  $("crop").src="/calib/"+GW+"/"+CAM+"/"+crop+"?t="+Date.now();
  if(d.saved&&d.saved.drawn){                     // reload what was drawn last time: redraw = correction
    D=d.saved.drawn; derive();
    msg("loaded the boxes saved "+(d.saved.saved_at||"earlier"),"ok");
  }
  setMode("tens"); show(); loadFit();
}).catch(function(){$("cropcount").textContent="state failed"});
}
// new crops or refitted cells change what this page shows
function loadFit(){
  // Durable fitcells score, read from the json door_calib writes. cell_ncc near 1.0 = well aligned;
  // a low cell is the one still mis-fit. Persists regardless of what scrolled out of the run log.
  fetch('/calib/'+GW+'/'+CAM+'/_calib_fitcells.json?t='+Date.now()).then(function(r){
    if(!r.ok)return null; return r.json();
  }).then(function(j){
    if(!j){document.getElementById('fitcard').style.display='none';return;}
    document.getElementById('fitcard').style.display='';
    var sc=j.cell_ncc||{};
    document.getElementById('fitscores').innerHTML=Object.keys(sc).map(function(k){
      var v=sc[k], bad=(v!=null&&v<0.6);
      return '<span class="kv" style="display:inline-flex;gap:4px;margin-right:10px"><span class=mut>'+k+'</span><b class="'+(bad?'bad':'ok')+'">'+v+'</b></span>';
    }).join('')||'<span class=mut>no per-cell scores</span>';
    document.getElementById('fitoverlay').src=(j.overlay_url||('/calib/'+GW+'/'+CAM+'/_calib_fitcells.jpg'))+'?t='+Date.now();
    document.getElementById('fitwhen').textContent='('+(j.n_samples||0)+' crops, radius '+(j.radius||'?')+', '+(j.iters||'?')+' iters)';
    document.getElementById('fitnote').textContent=j.note||'';
  }).catch(function(){document.getElementById('fitcard').style.display='none';});
}
function runFinished(){loadCells();loadFit();}
loadCells();
</script></body></html>"""
