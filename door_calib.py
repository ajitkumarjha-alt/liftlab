#!/usr/bin/env python3
"""door/floor CALIBRATION (steps 1-2). RUNS ON liftlab-cloud (no token, no network hop): reads the
segments already in /dev/shm/liftlab-live/site-A/ch29/ and writes crops + renders to the DURABLE calib
dir (/var/lib/liftlab/calib, NOT /run tmpfs — the --build source crops must survive a service restart),
served read-only at lift.gargi.online/calib/site-A/ch29/_calib_*.jpg . Posts nothing; never touches gw_event.

  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py                 # confirm ROIs (1 frame)
  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py --collect 40    # montage over 40 frames
Override boxes:  PANEL_ROIS="x,y,w,h;x,y,w,h"   DOOR_ROI="450,0,568,900"  (door_roi is CALIB space)
View:  https://lift.gargi.online/calib/site-A/ch29/_calib_frame.jpg  (and _calib_p0 / _calib_p1 / _calib_glyphs)
"""
import argparse
import glob
import io
import json
import os
import time
from pathlib import Path

import gpu_door as gd


class CalibError(Exception):
    """A recoverable calibration failure (bad input, nothing collected yet, geometry not measurable).
    EVERY library function below raises THIS, never SystemExit — a web endpoint maps it to a 4xx, the
    CLI prints it and exits 2. SystemExit would kill a uvicorn worker, so it's confined to __main__."""


# --- WEB-CALLABLE contract -------------------------------------------------------------------------
# door_calib is a LIBRARY first, a CLI second: render_rois / collect_crops / propose_cells /
# build_from_crops each take explicit params (env only as a default), return a JSON-able dict, write
# their artifacts to the served calib dir, and raise CalibError (not SystemExit) on bad input. main()
# is a thin argparse shell over them so the same code path backs a future /calibrate web action.


def _calib_dir(gw, cam):
    d = CALIB_DIR / gw / cam                 # DURABLE (survives restart); served at /calib/{gw}/{cam}/
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url(gw, cam, fname):
    return f"{CLOUD}/calib/{gw}/{cam}/{fname}"


def _write_result(outdir, name, result):
    """Persist a step's structured result as JSON next to its images so a web poll can read the
    outcome (+ artifact URLs) without re-running the step."""
    try:
        (outdir / name).write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return result

GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
# DURABLE calibration dir — NOT /run (tmpfs): the --build source crops must survive a service restart
# (an apply_* deploy wiped them from RAM once). Served (read-only) at /calib/{gw}/{cam}/ by ops_api.
CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
CALIB_WH = (int(os.environ.get("CALIB_W", "1920")), int(os.environ.get("CALIB_H", "1080")))
DOOR_ROI = [int(x) for x in os.environ.get("DOOR_ROI", "450,0,568,900").split(",")]      # CALIB space (scaled)
# The scaled door_roi landed on the LEFT WALL (panel surface), not the leaf — the Pi's brightness
# scalar tolerates a correlated-but-wrong ROI; edge geometry does NOT. Override in FRAME px and eyeball:
DOOR_ROI_FRAME = os.environ.get("DOOR_ROI_FRAME", "")    # "x,y,w,h" in 704x576 frame px, used DIRECTLY
# panel ROIs in FRAME (704x576) px — CONFIRMED tight, both agree frame-for-frame.
PANEL_ROIS_DEFAULT = "124,116,51,92;379,44,40,89"
# FIXED-PITCH cells WITHIN the panel0 crop (x relative to the panel's left edge). No gap segmentation:
# HEVC smears the 1-2px inter-digit gap, so we crop known cell positions instead. Measure off the
# _calib_p0 ruler (absolute frame px) minus the panel origin. Set DIGIT_CELLS + ARROW_CELL for --build.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "/var/lib/liftlab/templates")


def _parse_cells(s):
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out


def _panel_rois():
    out = []
    for part in os.environ.get("PANEL_ROIS", PANEL_ROIS_DEFAULT).split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out


def _newest_seg():
    segs = sorted(glob.glob(str(LIVE_DIR / GW / CAM / "*.ts")), key=os.path.getmtime)
    return segs[-1] if segs else None


def _decode_last_frame(seg_path):
    import av
    with open(seg_path, "rb") as fh:
        data = fh.read()
    c = av.open(io.BytesIO(data))
    fr = None
    for f in c.decode(video=0):
        fr = f.to_ndarray(format="bgr24")
    c.close()
    return fr


def _newest_frame_http():                        # fallback if run somewhere without the local segments
    import requests
    tok = os.environ.get("ANALYSIS_TOKEN", "").split(":")[-1]
    base = f"{CLOUD}/api/gw/{GW}/live/{CAM}"
    h = {"Authorization": "Bearer " + tok}
    pl = requests.get(f"{base}/index.m3u8", headers=h, timeout=15).text
    segs = [ln.strip() for ln in pl.splitlines() if ln.strip().endswith(".ts")]
    if not segs:
        return None
    import av
    data = requests.get(f"{base}/{segs[-1]}", headers=h, timeout=15).content
    c = av.open(io.BytesIO(data)); fr = None
    for f in c.decode(video=0):
        fr = f.to_ndarray(format="bgr24")
    c.close()
    return fr


def newest_frame():
    seg = _newest_seg()
    if seg:
        return _decode_last_frame(seg)
    return _newest_frame_http()


def _ruler(crop_bgr, factor=12, step=5, x0=0, y0=0):
    """Upscale a crop + draw a labelled grid in SOURCE (frame) coords so the operator reads exact
    x/y boundaries. x0/y0 offset the labels to the crop's position in the full frame (so the numbers
    are absolute frame px, ready to type into DOOR_ROI_FRAME / PANEL_ROIS)."""
    import cv2
    h, w = crop_bgr.shape[:2]
    big = cv2.resize(crop_bgr, (w * factor, h * factor), interpolation=cv2.INTER_NEAREST)
    for x in range(0, w + 1, step):
        cv2.line(big, (x * factor, 0), (x * factor, h * factor), (0, 200, 0), 1)
        cv2.putText(big, str(x0 + x), (x * factor + 1, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 200, 0), 1)
    for y in range(0, h + 1, step):
        cv2.line(big, (0, y * factor), (w * factor, y * factor), (0, 120, 0), 1)
        cv2.putText(big, str(y0 + y), (1, y * factor + 9), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 120, 0), 1)
    return big


def _frame_grid(img, step=40):
    """Faint labelled coordinate grid on the full frame, so the leaf seam (~x340-380) and the door
    box can be read off directly."""
    import cv2
    out = img.copy()
    H, W = out.shape[:2]
    for x in range(0, W, step):
        cv2.line(out, (x, 0), (x, H), (60, 60, 60), 1)
        cv2.putText(out, str(x), (x + 1, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 60), 1)
    for y in range(0, H, step):
        cv2.line(out, (0, y), (W, y), (60, 60, 60), 1)
        cv2.putText(out, str(y), (1, y + 9), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 60), 1)
    return out


def _montage(crops, cols, factor):
    import cv2
    import numpy as np
    if not crops:
        return None
    ups = [cv2.resize(c, (0, 0), fx=factor, fy=factor, interpolation=cv2.INTER_NEAREST) for c in crops]
    hh = max(u.shape[0] for u in ups); ww = max(u.shape[1] for u in ups)
    rows = []
    for k in range(0, len(ups), cols):
        row = ups[k:k + cols]
        cells = [np.pad(u, ((0, hh - u.shape[0]), (0, ww - u.shape[1]), (0, 0))) for u in row]
        while len(cells) < cols:
            cells.append(np.zeros((hh, ww, 3), np.uint8))
        rows.append(np.hstack(cells))
    return np.vstack(rows)


def render_index(gw=None, cam=None, outdir=None, cols=6):
    """Montage of every collected crop with its INDEX drawn on it, so the operator can pick a good
    ANCHOR crop (a two-digit + arrow + queue tile) by number for --anchor. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    paths = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not paths:
        raise CalibError("no _calib_crop_*.png — run collect first")
    F = 8
    tiles = []
    for p in paths:
        idx = int(Path(p).stem.split("_")[-1])
        im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        big = cv2.resize(cv2.cvtColor(im, cv2.COLOR_GRAY2BGR),
                         (im.shape[1] * F, im.shape[0] * F), interpolation=cv2.INTER_NEAREST)
        cv2.rectangle(big, (0, 0), (big.shape[1] - 1, big.shape[0] - 1), (0, 120, 0), 1)
        cv2.putText(big, str(idx), (2, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        tiles.append(big)
    cv2.imwrite(str(outdir / "_calib_index.jpg"), _montage(tiles, cols=cols, factor=1))
    result = {"gw": gwid, "cam": cam, "n": len(paths), "index_url": _url(gwid, cam, "_calib_index.jpg")}
    return _write_result(outdir, "_calib_index.json", result)


def render_anchor(crop, gw=None, cam=None, outdir=None):
    """Enlarge one crop with a FINE within-panel-px ruler so the operator can READ the anchor coords off
    it: tens-digit left, units-digit left, digit top, digit bottom, arrow left. Pick a two-digit + arrow
    (+ queue) crop by number from --index. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    p = outdir / f"_calib_crop_{int(crop):03d}.png"
    if not p.exists():
        raise CalibError(f"no {p.name} — pick an index shown by --index")
    im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    ruled = _ruler(cv2.cvtColor(im, cv2.COLOR_GRAY2BGR), factor=14, step=2)   # labels in WITHIN-PANEL px
    cv2.imwrite(str(outdir / "_calib_anchor.jpg"), ruled)
    result = {"gw": gwid, "cam": cam, "crop": int(crop),
              "anchor_url": _url(gwid, cam, "_calib_anchor.jpg"),
              "read": "tens_left, units_left, digit_top, digit_bottom, arrow_left  (within-panel px off the ruler)"}
    return _write_result(outdir, "_calib_anchor.json", result)


def propose_cells(gw=None, cam=None, outdir=None, anchors=None, anchor_crop=None, arrow_w=None):
    """DETERMINISTIC cells from human-read anchors — NO auto-detection. The ch29 panel ROI also sees the
    moving door/lobby, so every auto approach (brightness v1, temporal-variance v2) FAILED: the door
    sweep, people and changing light give the WHOLE ROI variance, so nothing isolates the LEDs. Instead
    the operator reads 5 coords ONCE off the --anchor ruler (from a two-digit + arrow crop); fixed-pitch
    geometry does the rest, exactly and repeatably. Auto-detection can return later as a wizard, VALIDATED
    against these human anchors as fixtures.

    anchors = tens_left, units_left, digit_top, digit_bottom, arrow_left  (within-panel px; str or seq)
      pitch  = units_left - tens_left                         (an LED matrix is fixed-pitch)
      cell_w = pitch, but never past the arrow: min(pitch, arrow_left - units_left)
      3 digit cells RIGHT-ALIGNED on the units cell (d0 = one pitch left of tens: the 3rd char / P-prefix)
      arrow cell one cell wide at arrow_left; height = digit_bottom - digit_top (bottom set above the queue
      by the operator's eye). Writes _calib_cells.jpg (cells on the anchor crop) + _calib_cells.json. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    if anchors is None:
        anchors = os.environ.get("CELLS_ANCHORS", "")
    if isinstance(anchors, str):
        vals = [int(v) for v in anchors.replace(";", ",").split(",") if v.strip() != ""]
    else:
        vals = [int(v) for v in anchors]
    if len(vals) != 5:
        raise CalibError("need 5 anchors: tens_left,units_left,digit_top,digit_bottom,arrow_left (within-panel "
                         "px). Run `--index` to pick a two-digit+arrow crop, then `--anchor N` to read them off the ruler.")
    tens_left, units_left, top, bottom, arrow_left = vals
    pitch = units_left - tens_left
    if pitch <= 0:
        raise CalibError(f"units_left ({units_left}) must be > tens_left ({tens_left}); got pitch={pitch}")
    if bottom <= top:
        raise CalibError(f"digit_bottom ({bottom}) must be > digit_top ({top})")
    if arrow_left < units_left:
        raise CalibError(f"arrow_left ({arrow_left}) should be to the RIGHT of units_left ({units_left})")
    height = bottom - top

    paths = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not paths:
        raise CalibError("no _calib_crop_*.png — run collect first")
    if anchor_crop is not None:
        cp = outdir / f"_calib_crop_{int(anchor_crop):03d}.png"
        if not cp.exists():
            raise CalibError(f"no {cp.name} — that anchor-crop index wasn't collected")
    else:
        cp = Path(paths[len(paths) // 2])                    # any crop for the confirm overlay if none named
    base = cv2.imread(str(cp), cv2.IMREAD_GRAYSCALE)
    Hp, Wp = base.shape[:2]

    w = pitch if (arrow_left - units_left) >= pitch else max(1, arrow_left - units_left)   # never overlap arrow
    warn = []

    def _fit(x, cw, tag):
        if x < 0:
            warn.append(f"{tag} left {x}<0 -> clamped to 0"); cw += x; x = 0
        if x + cw > Wp:
            warn.append(f"{tag} right {x + cw}>{Wp} -> clamped"); cw = Wp - x
        return int(x), int(max(1, cw))

    lefts = [tens_left - pitch, tens_left, units_left]       # d0 (3rd char / P-prefix), d1 (tens), d2 (units)
    cells = []
    for i, l in enumerate(lefts):
        cx, cw = _fit(int(l), int(w), f"d{i}")
        cells.append((cx, int(top), cw, int(height)))
    aw = int(arrow_w if arrow_w is not None else os.environ.get("CELLS_ARROW_W", int(w)))
    ax, aw = _fit(int(arrow_left), aw, "arrow")
    arrow_cell = (ax, int(top), aw, int(height))

    # confirm overlay — draw the derived cells on the anchor crop so they visibly land on the real glyphs
    F = 12
    big = cv2.resize(cv2.cvtColor(base, cv2.COLOR_GRAY2BGR), (Wp * F, Hp * F), interpolation=cv2.INTER_NEAREST)
    palette = [(0, 180, 255), (0, 220, 120), (255, 160, 0)]  # BGR: d0, d1, d2
    for i, (cx, cy, cw, ch) in enumerate(cells):
        cv2.rectangle(big, (cx * F, cy * F), ((cx + cw) * F, (cy + ch) * F), palette[i], 2)
        cv2.putText(big, f"d{i}", (cx * F + 2, cy * F + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, palette[i], 1)
    cv2.rectangle(big, (ax * F, top * F), ((ax + aw) * F, (top + height) * F), (200, 0, 200), 2)
    cv2.putText(big, "arrow", (ax * F + 2, top * F + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 0, 200), 1)
    cv2.imwrite(str(outdir / "_calib_cells.jpg"), big)

    dc = ";".join(f"{x},{y},{cw},{ch}" for (x, y, cw, ch) in cells)
    ac = ",".join(str(v) for v in arrow_cell)
    result = {"gw": gwid, "cam": cam, "digit_cells": dc, "arrow_cell": ac, "pitch": int(pitch),
              "cell_w": int(w), "cell_y": int(top), "cell_h": int(height),
              "anchors": {"tens_left": tens_left, "units_left": units_left, "digit_top": top,
                          "digit_bottom": bottom, "arrow_left": arrow_left},
              "anchor_crop": (int(anchor_crop) if anchor_crop is not None else None),
              "panel_wh": [int(Wp), int(Hp)], "warnings": warn,
              "overlay_url": _url(gwid, cam, "_calib_cells.jpg"),
              "note": "deterministic from human anchors; panel1 in-panel digit x differs a few px — spot-check p1"}
    _write_result(outdir, "_calib_cells.json", result)
    print(f"[cells] anchors tens_left={tens_left} units_left={units_left} top={top} bottom={bottom} arrow_left={arrow_left}")
    print(f"[cells] pitch={pitch}px cell_w={w}px height={height}px  (fixed-pitch, right-aligned)")
    for wln in warn:
        print(f"[cells] WARN {wln}")
    print(f"[cells] DIGIT_CELLS='{dc}'")
    print(f"[cells] ARROW_CELL='{ac}'")
    print(f"[cells] CONFIRM: {result['overlay_url']}  (cells drawn on {cp.name})")
    print(f"[cells] NOTE: {result['note']}")
    return result


def _resolve_geometry(fr, door_roi_frame=None, panel_rois=None):
    """(door_roi, dsrc, panel_rois) for a decoded frame. door_roi_frame='x,y,w,h' (str or seq) is frame
    px used DIRECTLY (nudged onto the leaf); else the calib-space DOOR_ROI is scaled (lands on the wall)."""
    H, W = fr.shape[:2]
    drf = door_roi_frame if door_roi_frame is not None else DOOR_ROI_FRAME
    if drf:
        seq = drf.split(",") if isinstance(drf, str) else drf
        droi = tuple(int(v) for v in seq)
        dsrc = f"DOOR_ROI_FRAME={droi}"
    else:
        droi = gd.scale_roi(DOOR_ROI, CALIB_WH, (W, H))
        dsrc = f"scaled from calib {DOOR_ROI} -> {droi}  (WRONG surface: set door_roi_frame to the leaf)"
    if panel_rois is None:
        prois = _panel_rois()
    elif isinstance(panel_rois, str):
        prois = [tuple(int(v) for v in part.split(",")) for part in panel_rois.split(";") if part.strip()]
    else:
        prois = [tuple(p) for p in panel_rois]
    return droi, dsrc, prois


def _as_cells(v, envkey):
    """Normalise cells from a web param OR env into [(x,y,w,h), ...]. Accepts a 'x,y,w,h;...' string,
    a list of cells, or a single (x,y,w,h)."""
    if v is None:
        v = os.environ.get(envkey, "")
    if isinstance(v, str):
        return _parse_cells(v)
    if v and isinstance(v[0], (list, tuple)):
        return [tuple(c) for c in v]
    return [tuple(v)] if v else []


def render_rois(gw=None, cam=None, door_roi_frame=None, panel_rois=None):
    """Decode the newest frame + write the ROI-overlay and ruler renders (frame/door/panels). Returns
    geometry + artifact URLs. WEB-CALLABLE (returns JSON-able dict; raises CalibError if no frame)."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    fr = newest_frame()
    if fr is None:
        raise CalibError(f"no frame: no segments in {LIVE_DIR/gw/cam} and HTTP fallback empty")
    H, W = fr.shape[:2]
    droi, dsrc, prois = _resolve_geometry(fr, door_roi_frame, panel_rois)
    ann = _frame_grid(gd.overlay_rois(fr, [tuple(droi)] + list(prois),
                                      ["door"] + [f"p{i}" for i in range(len(prois))]))
    cv2.imwrite(str(outdir / "_calib_frame.jpg"), ann)
    cv2.imwrite(str(outdir / "_calib_door.jpg"),
                _ruler(gd.crop(fr, droi), factor=3, step=20, x0=droi[0], y0=droi[1]))
    for i, pr in enumerate(prois):
        cv2.imwrite(str(outdir / f"_calib_p{i}.jpg"), _ruler(gd.crop(fr, pr), x0=pr[0], y0=pr[1]))
    artifacts = [_url(gw, cam, "_calib_frame.jpg"), _url(gw, cam, "_calib_door.jpg")] + \
                [_url(gw, cam, f"_calib_p{i}.jpg") for i in range(len(prois))]
    result = {"gw": gw, "cam": cam, "frame_wh": [W, H], "door_roi": list(droi), "door_src": dsrc,
              "panels": [list(p) for p in prois], "artifacts": artifacts}
    return _write_result(outdir, "_calib_rois.json", result)


def collect_crops(gw=None, cam=None, nframes=40, fresh=False, door_roi_frame=None, panel_rois=None):
    """Collect one panel0 crop per NEW segment (dedup by MTIME — the relay reuses seg filenames, so the
    PATH repeats while content changes; a path-keyed dedup was the old 1-crop bug). APPEND across runs
    (durable-dir promise); fresh=True starts over. Rebuilds the cumulative panel0 montage (matches what
    build reads, 1:1) + this-run door montage. Returns counts + URLs. WEB-CALLABLE — but BLOCKS ~2s ×
    nframes, so a web caller must run it as a background job, not inline in the request."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    fr0 = newest_frame()
    if fr0 is None:
        raise CalibError(f"no frame: no segments in {LIVE_DIR/gw/cam} and HTTP fallback empty")
    droi, _dsrc, prois = _resolve_geometry(fr0, door_roi_frame, panel_rois)
    existing = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if fresh:
        for gp in existing:
            os.remove(gp)
        existing = []
    n = 1 + max([int(Path(g).stem.split("_")[-1]) for g in existing], default=-1)   # continue numbering
    doors = []; last_mtime = None; added = 0
    for _ in range(nframes):
        seg = _newest_seg()
        if seg:
            mt = os.path.getmtime(seg)
            if mt != last_mtime:              # new CONTENT (path may repeat), decode + save
                last_mtime = mt
                f2 = _decode_last_frame(seg)
                if f2 is not None:
                    doors.append(gd.crop(f2, droi))
                    cv2.imwrite(str(outdir / f"_calib_crop_{n:03d}.png"),
                                cv2.cvtColor(gd.crop(f2, prois[0]), cv2.COLOR_BGR2GRAY))
                    n += 1; added += 1
        time.sleep(2)                         # ~one per 2s segment
    allc = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    pmont = _montage([cv2.cvtColor(cv2.imread(c, cv2.IMREAD_GRAYSCALE), cv2.COLOR_GRAY2BGR) for c in allc], cols=5, factor=8)
    if pmont is not None:
        cv2.imwrite(str(outdir / "_calib_glyphs0.jpg"), pmont)
    md = _montage(doors, cols=6, factor=2)
    if md is not None:
        cv2.imwrite(str(outdir / "_calib_doormap.jpg"), md)
    result = {"gw": gw, "cam": cam, "added": added, "total": len(allc),
              "glyphs_url": _url(gw, cam, "_calib_glyphs0.jpg"),
              "doormap_url": (_url(gw, cam, "_calib_doormap.jpg") if md is not None else None)}
    return _write_result(outdir, "_calib_collect.json", result)


def build_from_crops(gw=None, cam=None, labels=None, digit_cells=None, arrow_cell=None, align=None, out_path=None):
    """Build templates.npz from collected crops + row-major labels + fixed cells (from cells step / env /
    param). labels: comma-string or list; cells: 'x,y,w,h;...' string or list. Returns stats + the fetch
    URL the GPU pulls. WEB-CALLABLE (raises CalibError on missing crops/labels/cells)."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    if labels is None:
        labels = os.environ.get("LABELS", "")
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    crops = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not crops or not labels:
        raise CalibError("need _calib_crop_*.png (collect first) and labels '7^,8^,...'")
    if len(crops) != len(labels):
        raise CalibError(f"{len(crops)} crops but {len(labels)} labels — must be 1:1 row-major (relabel the montage)")
    dcells = _as_cells(digit_cells, "DIGIT_CELLS")
    acell = _as_cells(arrow_cell, "ARROW_CELL")
    if not dcells or not acell:
        raise CalibError("fixed cells required (no segmentation): digit_cells 'x,y,w,h;x,y,w,h;x,y,w,h' + "
                         "arrow_cell 'x,y,w,h' (within-panel px — run the cells step to measure them)")
    labeled = [(cv2.imread(c, cv2.IMREAD_GRAYSCALE), lab) for c, lab in zip(crops, labels)]
    tpl, stats = gd.build_templates(labeled, dcells, acell[0], align=(align or os.environ.get("ALIGN", "right")))
    outp = out_path or os.environ.get("TEMPLATES_OUT", os.path.join(TEMPLATES_DIR, gw, f"{cam}.npz"))
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    gd.save_templates(tpl, outp)
    result = {"gw": gw, "cam": cam, "stats": stats, "n_templates": len(tpl), "out_path": outp,
              "fetch_url": f"{CLOUD}/api/gw/{gw}/templates/{cam}"}
    return _write_result(outdir, "_calib_build.json", result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, default=0)
    ap.add_argument("--frames", type=int, default=0, help="collect N frames -> door + panel montages")
    ap.add_argument("--index", action="store_true", help="montage of crops WITH indices, to pick an anchor crop")
    ap.add_argument("--anchor", type=int, default=None, metavar="N", help="enlarge crop N with a fine ruler to read anchor px")
    ap.add_argument("--cells", action="store_true", help="DETERMINISTIC cells from --anchors (no auto-detect)")
    ap.add_argument("--anchors", default="", help="tens_left,units_left,digit_top,digit_bottom,arrow_left (within-panel px)")
    ap.add_argument("--anchor-crop", type=int, default=None, dest="anchor_crop", help="crop index to draw the derived cells on")
    ap.add_argument("--build", action="store_true", help="build templates.npz from collected crops + --labels")
    ap.add_argument("--labels", default="", help="row-major floor labels, e.g. '7^,8^,12^,...,P3v,6v'")
    ap.add_argument("--fresh", action="store_true", help="clear accumulated crops before collecting (default = append)")
    a = ap.parse_args()
    try:
        if a.index:
            r = render_index()
            print(f"[index] {r['n']} crops -> {r['index_url']}")
            print(f"[index] pick a two-digit + arrow (+ queue) tile; note its number for `--anchor N`")
            return
        if a.anchor is not None:
            r = render_anchor(a.anchor)
            print(f"[anchor] crop {r['crop']} -> {r['anchor_url']}")
            print(f"[anchor] read off the ruler: {r['read']}")
            print(f"[anchor] then: door_calib.py --cells --anchors 'tens_left,units_left,digit_top,digit_bottom,arrow_left' --anchor-crop {r['crop']}")
            return
        if a.cells:
            propose_cells(anchors=a.anchors, anchor_crop=a.anchor_crop)   # prints + writes _calib_cells.json
            return
        if a.build:
            r = build_from_crops(labels=a.labels)
            print(f"[build] {r['stats']}")
            print(f"[build] wrote {r['n_templates']} templates -> {r['out_path']}")
            print(f"[build] the GPU fetches it (no scp needed): GET {r['fetch_url']}  (Bearer analysis token)")
            return
        r = render_rois()
        print(f"[calib] frame {r['frame_wh'][0]}x{r['frame_wh'][1]}; door_roi={tuple(r['door_roi'])}  "
              f"[{r['door_src']}]; panels(frame px)={[tuple(p) for p in r['panels']]}")
        for u in r["artifacts"]:
            print(f"[calib]   {u}")
        nframes = max(a.collect, a.frames)
        if nframes:
            c = collect_crops(nframes=nframes, fresh=a.fresh)
            print(f"[calib] +{c['added']} new crops this run -> {c['total']} total. Label ALL row-major:")
            print(f"[calib]   {c['glyphs_url']}")
            if c["doormap_url"]:
                print(f"[calib] door montage -> {c['doormap_url']}  "
                      f"(edge should SWEEP shut<->open; if it never moves, the box is on a fixed jamb/wall)")
    except CalibError as e:
        raise SystemExit(f"[calib] {e}")                   # CLI-only: catchable error -> stderr + nonzero exit


if __name__ == "__main__":
    main()
