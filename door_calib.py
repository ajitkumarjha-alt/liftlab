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
DOOR_ROI = [int(x) for x in os.environ.get("DOOR_ROI", "450,0,568,900").split(",")]      # CALIB space
# panel ROIs are in FRAME (704x576) pixels — confirmed to read "25 v" on the sub
PANEL_ROIS_DEFAULT = "120,108,40,30;375,38,40,30"


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


def _ruler(crop_bgr, factor=12):
    """Upscale a panel crop and draw a labelled 5-px grid (in SOURCE coords) so the operator can read
    off digit-cell x/y boundaries to define digit_cells within the panel."""
    import cv2
    h, w = crop_bgr.shape[:2]
    big = cv2.resize(crop_bgr, (w * factor, h * factor), interpolation=cv2.INTER_NEAREST)
    for x in range(0, w + 1, 5):
        cv2.line(big, (x * factor, 0), (x * factor, h * factor), (0, 200, 0), 1)
        cv2.putText(big, str(x), (x * factor + 1, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 200, 0), 1)
    for y in range(0, h + 1, 5):
        cv2.line(big, (0, y * factor), (w * factor, y * factor), (0, 120, 0), 1)
    return big


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, default=0)
    a = ap.parse_args()
    outdir = SNAP_DIR / GW
    outdir.mkdir(parents=True, exist_ok=True)

    fr = newest_frame()
    if fr is None:
        raise SystemExit(f"no frame: no segments in {LIVE_DIR/GW/CAM} and HTTP fallback empty")
    H, W = fr.shape[:2]
    droi = gd.scale_roi(DOOR_ROI, CALIB_WH, (W, H))
    prois = _panel_rois()
    ann = gd.overlay_rois(fr, [droi] + list(prois), ["door"] + [f"p{i}" for i in range(len(prois))])
    cv2.imwrite(str(outdir / "_calib_frame.jpg"), ann)
    for i, pr in enumerate(prois):
        cv2.imwrite(str(outdir / f"_calib_p{i}.jpg"), _ruler(gd.crop(fr, pr)))
    print(f"[calib] frame {W}x{H}; door_roi(scaled)={droi}; panels(frame px)={prois}")
    print(f"[calib] view:  {CLOUD}/snap/{GW}/_calib_frame.jpg")
    for i in range(len(prois)):
        print(f"[calib]        {CLOUD}/snap/{GW}/_calib_p{i}.jpg   (ruler in SOURCE px -> read digit-cell x/y)")

    if a.collect:
        # montage of panel crops over time (upscaled) -> read floors off ONE viewable image
        tiles = {i: [] for i in range(len(prois))}
        seen = set()
        for _ in range(a.collect):
            seg = _newest_seg()
            key = seg
            if seg and key not in seen:
                seen.add(key)
                f2 = _decode_last_frame(seg)
                if f2 is not None:
                    for i, pr in enumerate(prois):
                        tiles[i].append(cv2.resize(gd.crop(f2, pr), (0, 0), fx=8, fy=8, interpolation=cv2.INTER_NEAREST))
            time.sleep(2)
        import numpy as np
        for i, ts in tiles.items():
            if not ts:
                continue
            cols = 5
            rows = [ts[k:k + cols] for k in range(0, len(ts), cols)]
            hh = max(t.shape[0] for t in ts); ww = max(t.shape[1] for t in ts)
            grid = []
            for row in rows:
                cells = [np.pad(t, ((0, hh - t.shape[0]), (0, ww - t.shape[1]), (0, 0))) for t in row]
                while len(cells) < cols:
                    cells.append(np.zeros((hh, ww, 3), np.uint8))
                grid.append(np.hstack(cells))
            cv2.imwrite(str(outdir / f"_calib_glyphs{i}.jpg"), np.vstack(grid))
            print(f"[calib] montage {len(ts)} crops -> {CLOUD}/snap/{GW}/_calib_glyphs{i}.jpg  (tell me the floors, row-major)")


if __name__ == "__main__":
    main()
