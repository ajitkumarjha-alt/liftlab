#!/usr/bin/env python3
"""door/floor CALIBRATION (steps 1-2). RUNS ON liftlab-cloud (no token, no network hop): reads the
segments already in /dev/shm/liftlab-live/site-A/ch29/ and writes web-viewable crops into the snap
dir, so the output appears at lift.gargi.online/snap/site-A/_calib_*.jpg . Names are '_'-prefixed:
directly servable but EXCLUDED from the /ops grid. Read-only; posts nothing; never touches gw_event.

  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py                 # confirm ROIs (1 frame)
  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py --collect 40    # montage over 40 frames
Override boxes:  PANEL_ROIS="x,y,w,h;x,y,w,h"   DOOR_ROI="450,0,568,900"  (door_roi is CALIB space)
View:  https://lift.gargi.online/snap/site-A/_calib_frame.jpg  (and _calib_p0 / _calib_p1 / _calib_glyphs)
"""
import argparse
import glob
import io
import os
import time
from pathlib import Path

import gpu_door as gd

GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
SNAP_DIR = Path(os.environ.get("SNAP_DIR", "/run/liftlab-snap"))
CALIB_WH = (int(os.environ.get("CALIB_W", "1920")), int(os.environ.get("CALIB_H", "1080")))
DOOR_ROI = [int(x) for x in os.environ.get("DOOR_ROI", "450,0,568,900").split(",")]      # CALIB space (scaled)
# The scaled door_roi landed on the LEFT WALL (panel surface), not the leaf — the Pi's brightness
# scalar tolerates a correlated-but-wrong ROI; edge geometry does NOT. Override in FRAME px and eyeball:
DOOR_ROI_FRAME = os.environ.get("DOOR_ROI_FRAME", "")    # "x,y,w,h" in 704x576 frame px, used DIRECTLY
# panel ROIs in FRAME (704x576) px — confirmed to read "22 ^" and AGREE (nudged off the clipping boxes)
PANEL_ROIS_DEFAULT = "126,112,42,34;378,42,42,34"


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


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, default=0)
    ap.add_argument("--frames", type=int, default=0, help="collect N frames -> door + panel montages")
    a = ap.parse_args()
    nframes = max(a.collect, a.frames)
    outdir = SNAP_DIR / GW
    outdir.mkdir(parents=True, exist_ok=True)

    fr = newest_frame()
    if fr is None:
        raise SystemExit(f"no frame: no segments in {LIVE_DIR/GW/CAM} and HTTP fallback empty")
    H, W = fr.shape[:2]
    if DOOR_ROI_FRAME:
        droi = tuple(int(v) for v in DOOR_ROI_FRAME.split(","))    # frame px, used DIRECTLY (nudgeable)
        dsrc = f"DOOR_ROI_FRAME={droi}"
    else:
        droi = gd.scale_roi(DOOR_ROI, CALIB_WH, (W, H))            # scaled (lands on the wall — override it)
        dsrc = f"scaled from calib {DOOR_ROI} -> {droi}  (WRONG surface: set DOOR_ROI_FRAME to the leaf)"
    prois = _panel_rois()
    ann = _frame_grid(gd.overlay_rois(fr, [tuple(droi)] + list(prois),
                                      ["door"] + [f"p{i}" for i in range(len(prois))]))
    cv2.imwrite(str(outdir / "_calib_frame.jpg"), ann)
    cv2.imwrite(str(outdir / "_calib_door.jpg"),
                _ruler(gd.crop(fr, droi), factor=3, step=20, x0=droi[0], y0=droi[1]))
    for i, pr in enumerate(prois):
        cv2.imwrite(str(outdir / f"_calib_p{i}.jpg"), _ruler(gd.crop(fr, pr), x0=pr[0], y0=pr[1]))
    print(f"[calib] frame {W}x{H}; door_roi={droi}  [{dsrc}]; panels(frame px)={prois}")
    print(f"[calib] view:  {CLOUD}/snap/{GW}/_calib_frame.jpg   (40px grid -> find the leaf seam, read the door box)")
    print(f"[calib]        {CLOUD}/snap/{GW}/_calib_door.jpg    (door_roi crop, ruler in absolute frame px)")
    for i in range(len(prois)):
        print(f"[calib]        {CLOUD}/snap/{GW}/_calib_p{i}.jpg   (panel{i}, ruler in absolute frame px)")

    if nframes:
        # Time-ordered montages over N distinct segments. Panels -> read the floors. DOOR -> the leaf
        # edge sweeps across columns between shut and open frames; a montage spanning a cycle shows the
        # travel band (or, if the box is on a fixed jamb/wall, the edge NEVER moves — which is the test).
        panels = {i: [] for i in range(len(prois))}
        doors = []
        seen = set()
        for _ in range(nframes):
            seg = _newest_seg()
            if seg and seg not in seen:
                seen.add(seg)
                f2 = _decode_last_frame(seg)
                if f2 is not None:
                    for i, pr in enumerate(prois):
                        panels[i].append(gd.crop(f2, pr))
                    doors.append(gd.crop(f2, droi))
            time.sleep(2)          # ~one per 2s segment -> N*2s of coverage (catches a door cycle)
        for i, ts in panels.items():
            m = _montage(ts, cols=5, factor=8)
            if m is not None:
                cv2.imwrite(str(outdir / f"_calib_glyphs{i}.jpg"), m)
                print(f"[calib] panel{i}: {len(ts)} crops -> {CLOUD}/snap/{GW}/_calib_glyphs{i}.jpg  (floors row-major)")
        md = _montage(doors, cols=6, factor=2)
        if md is not None:
            cv2.imwrite(str(outdir / "_calib_doormap.jpg"), md)
            print(f"[calib] door: {len(doors)} crops -> {CLOUD}/snap/{GW}/_calib_doormap.jpg  "
                  f"(edge should SWEEP columns shut<->open; if it never moves, the box is on a fixed jamb/wall)")


if __name__ == "__main__":
    main()
