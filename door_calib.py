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


def _lit_mask(crop_gray, abs_floor):
    """Binary mask of LIT LED pixels in a panel crop. Otsu split (LED is bright, panel dark), clamped
    to >= half-max so a dim smear background can't pass; None if the crop has no LED lit at all
    (max < abs_floor) so an all-dark/off panel contributes nothing rather than thresholding noise."""
    import cv2
    if int(crop_gray.max()) < abs_floor:
        return None
    t, _ = cv2.threshold(crop_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    t = max(float(t), 0.5 * float(crop_gray.max()))
    return crop_gray > t


def _runs(flags):
    """contiguous True runs in a 1-D bool array -> [(start, end_inclusive), ...]."""
    runs, s = [], None
    for i, v in enumerate(flags):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i - 1)); s = None
    if s is not None:
        runs.append((s, len(flags) - 1))
    return runs


def propose_cells(gw=None, cam=None, abs_floor=None, outdir=None):
    """MEASURE cell geometry from the collected panel0 crops instead of reading rulers by hand.
    Thresholds lit LED pixels across ALL crops, finds the main-row vertical band (bottom set ABOVE the
    destination-queue line), locates the arrow (rightmost gap-separated column run) and the digit block,
    derives the fixed pitch from single- vs double-digit block widths, and lays 3 right-aligned digit
    cells + 1 arrow cell (within-panel px). Reports horizontal jitter (angled panel) and sizes cells
    with tolerance if it exceeds 1px. Writes _calib_cells.jpg (overlay) + _calib_cells.json (result).
    WEB-CALLABLE: returns a JSON-able dict; raises CalibError (not SystemExit) if crops are missing."""
    import cv2
    import numpy as np
    gwid = gw or GW; cam = cam or CAM       # gwid = gateway id; the local 'gw' below is the glyph WIDTH
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    abs_floor = int(abs_floor if abs_floor is not None else os.environ.get("CELLS_ABS_FLOOR", "80"))
    paths = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not paths:
        raise CalibError("no _calib_crop_*.png — run collect first")
    raw = [(p, cv2.imread(p, cv2.IMREAD_GRAYSCALE)) for p in paths]
    raw = [(p, im) for p, im in raw if im is not None]
    Hp = min(im.shape[0] for _, im in raw); Wp = min(im.shape[1] for _, im in raw)
    imgs = [(p, im[:Hp, :Wp]) for p, im in raw]                       # unify size (share the panel ROI)
    masks = [(p, im, m) for p, im in imgs for m in [_lit_mask(im, abs_floor)] if m is not None and m.any()]
    n_lit = len(masks)
    if n_lit < 3:
        raise CalibError(f"only {n_lit}/{len(imgs)} crops have a lit panel (max<{abs_floor}) — check PANEL_ROIS / abs_floor")
    stack = np.stack([m for _, _, m in masks]).astype(np.int32)       # (n, Hp, Wp)

    # ---- vertical: main row band, bottom kept ABOVE the destination-queue line ----
    row_occ = stack.sum(axis=(0, 2)).astype(float)                   # lit-pixel count per row, all crops
    row_runs = _runs(row_occ > 0.30 * row_occ.max())
    if not row_runs:
        raise CalibError("no lit rows — threshold/ROI problem")
    y0, y1 = row_runs[0]                                              # topmost band = the main floor row
    queue = next(((a, b) for (a, b) in row_runs[1:] if a > y1 + 1), None)
    if queue:
        y1 = min(y1, queue[0] - 1)                                    # never let a cell reach into the queue
    cell_y, cell_h = int(y0), int(y1 - y0 + 1)

    # ---- horizontal: arrow = rightmost gap-separated run; digits are everything left of that gap ----
    band = stack[:, y0:y1 + 1, :]
    col_occ = band.sum(axis=(0, 1)).astype(float)
    col_runs = _runs(col_occ > 0.20 * col_occ.max())
    if not col_runs:
        raise CalibError("no lit columns in the main band")
    arrow_run = None; digit_runs = col_runs
    if len(col_runs) >= 2 and (col_runs[-1][0] - col_runs[-2][1] - 1) >= 1:
        arrow_run, digit_runs = col_runs[-1], col_runs[:-1]
    gap_start = arrow_run[0] if arrow_run else Wp                     # digit columns live left of this
    x_dr = digit_runs[-1][1]                                          # aggregate right edge of the digit block
    # cap the per-crop measurement at the aggregate digit edge (+2 for right-wobble). Without this, an
    # arrow that jitters LEFT of gap_start leaks into the "digit region" and fakes a huge right-edge jitter.
    dig_hi = min(gap_start, x_dr + 2)

    # ---- per-crop digit block: right edge (right-aligned anchor) + width (single vs multi digit) ----
    r_edges, widths = [], []
    for _, _, m in masks:
        cols = np.where(m[y0:y1 + 1, :dig_hi].any(axis=0))[0]
        if cols.size:
            r_edges.append(int(cols.max())); widths.append(int(cols.max() - cols.min() + 1))
    if len(r_edges) < 3:
        raise CalibError("too few crops with digits to measure pitch")
    r_edges = np.array(r_edges); widths = np.array(widths)
    C = int(round(np.median(r_edges)))                               # units-digit right edge (px, in-panel)
    jitter = float(r_edges.max() - r_edges.min())                    # horizontal wobble of that edge

    uniq = np.sort(np.unique(widths))
    split = None
    if uniq.size >= 2:
        gaps = np.diff(uniq); gi = int(np.argmax(gaps))
        if gaps[gi] >= 2:                                            # a real jump in block width = digit-count change
            split = (uniq[gi] + uniq[gi + 1]) / 2.0
    narrow = widths[widths < split] if split is not None else widths
    wideg = widths[widths >= split] if split is not None else np.array([], int)
    gw = float(np.median(narrow)) if narrow.size else float(np.median(widths))   # single-digit width ~ cell glyph width
    if wideg.size >= 3:
        pitch = float(np.median(wideg)) - gw                        # 2-digit block = pitch + glyph_width => pitch = Wd - gw
        pitch_src = f"width-diff (single~{gw:.0f}px×{narrow.size}, double~{np.median(wideg):.0f}px×{wideg.size})"
    else:
        pitch = gw + 1.0
        pitch_src = f"FALLBACK gw+1 (only {wideg.size} multi-digit crops — pitch UNVERIFIED, approve visually)"
    pitch = max(1.0, pitch)

    # tolerance: an angled panel wobbles the digit x; if the right edge moves >1px, pad cells (capped so
    # neighbours don't overlap: at most half the free space between cells, i.e. (pitch - gw)/2).
    tol = int(np.ceil(jitter / 2.0)) if jitter > 1 else 0
    tol = int(min(tol, max(0, (pitch - gw) / 2.0)))
    w = int(round(gw)) + 2 * tol
    cells = []                                                       # 3 digit cells, LEFT-TO-RIGHT (i=0 leftmost)
    for i in range(3):
        Ri = C - (2 - i) * pitch
        x = int(round(Ri - gw + 1)) - tol
        cells.append((max(0, x), cell_y, w, cell_h))
    if arrow_run:
        ax = max(0, arrow_run[0] - tol)
        arrow_cell = (ax, cell_y, int(arrow_run[1] - arrow_run[0] + 1) + 2 * tol, cell_h)
        arrow_src = f"measured run x[{arrow_run[0]}..{arrow_run[1]}]"
    else:
        ax = int(round(C + max(2.0, pitch - gw)))                   # estimate: one gap right of the units digit
        arrow_cell = (ax, cell_y, int(round(gw)) + 2 * tol, cell_h)
        arrow_src = "ESTIMATED (no gap-separated run right of digits — verify)"

    # ---- overlay on a TWO-DIGIT crop (prefer one with the arrow lit) so all four boxes have content ----
    best, best_score = None, -1.0
    for p, im, m in masks:
        cols = np.where(m[y0:y1 + 1, :dig_hi].any(axis=0))[0]
        if cols.size == 0:
            continue
        bw = int(cols.max() - cols.min() + 1)
        if split is not None and bw < split:                        # want a multi-digit tile
            continue
        arrow_lit = int(m[y0:y1 + 1, gap_start:].sum()) if arrow_run else 0
        score = bw + (5 if arrow_lit > 2 else 0)
        if score > best_score:
            best, best_score = (p, im, m), score
    if best is None:
        best = masks[len(masks) // 2]
    F = 10
    big = cv2.resize(cv2.cvtColor(best[1], cv2.COLOR_GRAY2BGR), (Wp * F, Hp * F), interpolation=cv2.INTER_NEAREST)
    palette = [(0, 180, 255), (0, 220, 120), (255, 160, 0)]         # BGR: cell0,1,2
    for i, (cx, cy, cw, ch) in enumerate(cells):
        cv2.rectangle(big, (cx * F, cy * F), ((cx + cw) * F, (cy + ch) * F), palette[i], 2)
        cv2.putText(big, f"d{i}", (cx * F + 2, cy * F + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, palette[i], 1)
    axx, ayy, aww, ahh = arrow_cell
    cv2.rectangle(big, (axx * F, ayy * F), ((axx + aww) * F, (ayy + ahh) * F), (200, 0, 200), 2)
    cv2.putText(big, "arrow", (axx * F + 2, ayy * F + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (200, 0, 200), 1)
    if queue:
        qy = queue[0] * F
        cv2.line(big, (0, qy), (Wp * F, qy), (0, 0, 255), 1)
        cv2.putText(big, f"queue y={queue[0]}", (2, qy - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 0, 255), 1)
    cv2.imwrite(str(outdir / "_calib_cells.jpg"), big)

    dc = ";".join(f"{x},{y},{cw},{ch}" for (x, y, cw, ch) in cells)
    ac = ",".join(str(v) for v in arrow_cell)
    result = {"gw": gwid, "cam": cam, "digit_cells": dc, "arrow_cell": ac,
              "pitch": round(float(pitch), 2), "glyph_w": round(float(gw), 2), "C": int(C),
              "jitter": float(jitter), "tol": int(tol), "cell_y": int(cell_y), "cell_h": int(cell_h),
              "queue_y": (int(queue[0]) if queue else None), "n_lit": int(n_lit), "n_crops": len(imgs),
              "panel_wh": [int(Wp), int(Hp)], "pitch_src": pitch_src, "arrow_src": arrow_src,
              "overlay_url": _url(gwid, cam, "_calib_cells.jpg"),
              "note": "measured on panel0 only; panel1 in-panel digit x differs a few px — spot-check p1"}
    _write_result(outdir, "_calib_cells.json", result)
    print(f"[cells] {n_lit}/{len(imgs)} lit crops; panel {Wp}x{Hp}")
    print(f"[cells] main row y[{cell_y}..{cell_y + cell_h - 1}] h={cell_h}"
          + (f"; QUEUE line detected at y={queue[0]} — cell bottom kept above it" if queue
             else "; NO queue band detected in aggregate — verify bottom against the 52-over-queue crop"))
    print(f"[cells] units right edge C={C}px; pitch={pitch:.1f}px [{pitch_src}]; glyph width~{gw:.0f}px")
    if jitter > 1:
        print(f"[cells] JITTER: units right edge varies {jitter:.0f}px across crops (panel is angled) "
              f"-> cells padded ±{tol}px (width {int(round(gw))}->{w})")
    else:
        print(f"[cells] jitter {jitter:.0f}px (<=1) — cells sized tight, no tolerance pad")
    print(f"[cells] arrow: {arrow_src}")
    print(f"[cells] PROPOSED (within-panel px, left-to-right):")
    print(f"    DIGIT_CELLS='{dc}'")
    print(f"    ARROW_CELL='{ac}'")
    print(f"[cells] APPROVE VISUALLY: {result['overlay_url']}  (boxes on a two-digit crop)")
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
    ap.add_argument("--cells", action="store_true", help="MEASURE DIGIT_CELLS + ARROW_CELL from collected crops (no rulers)")
    ap.add_argument("--build", action="store_true", help="build templates.npz from collected crops + --labels")
    ap.add_argument("--labels", default="", help="row-major floor labels, e.g. '7^,8^,12^,...,P3v,6v'")
    ap.add_argument("--fresh", action="store_true", help="clear accumulated crops before collecting (default = append)")
    a = ap.parse_args()
    try:
        if a.cells:
            propose_cells()                                # prints + writes _calib_cells.json
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
