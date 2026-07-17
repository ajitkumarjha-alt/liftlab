#!/usr/bin/env python3
"""door/floor CALIBRATION (steps 1-2), on the GPU box. Pull a live ch29 sub frame, overlay door_roi
(scaled from calib space) + candidate indicator-panel ROIs, and dump upscaled crops so the operator
CONFIRMS the ROIs sit on the LED panels (same eyeball approach as zone_verify.py), then COLLECTS
labelled glyph crops for the templates. Read-only: pulls with the read-only analysis token, posts
NOTHING, never touches gw_event or the counting path.

  # 1. confirm ROIs on a live frame (panel ROIs are in FRAME pixels, read off the annotated image):
  PANEL_ROIS="x,y,w,h;x,y,w,h" python3 door_calib.py --live --out /tmp/calib
  # 2. collect panel crops over N frames of real traffic, to label into digit/arrow templates:
  PANEL_ROIS="..." python3 door_calib.py --collect 300 --out /tmp/glyphs
"""
import argparse
import io
import os
import time

import gpu_door as gd

CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
TOKEN = os.environ.get("ANALYSIS_TOKEN", "").split(":")[-1]
BASE = f"{CLOUD}/api/gw/{GW}/live/{CAM}"
HDRS = {"Authorization": "Bearer " + TOKEN}
CALIB_WH = (int(os.environ.get("CALIB_W", "1920")), int(os.environ.get("CALIB_H", "1080")))
DOOR_ROI = [int(x) for x in os.environ.get("DOOR_ROI", "450,0,568,900").split(",")]


def _get(url):
    import requests
    r = requests.get(url, headers=HDRS, timeout=15)
    r.raise_for_status()
    return r.content


def newest_frame():
    import av
    pl = _get(f"{BASE}/index.m3u8").decode("utf-8", "ignore")
    segs = [ln.strip() for ln in pl.splitlines() if ln.strip().endswith(".ts")]
    if not segs:
        raise SystemExit("no segments in playlist")
    c = av.open(io.BytesIO(_get(f"{BASE}/{segs[-1]}")))
    fr = None
    for f in c.decode(video=0):
        fr = f.to_ndarray(format="bgr24")
    c.close()
    if fr is None:
        raise SystemExit("decode produced no frame")
    return fr


def _panel_rois():
    out = []
    for part in os.environ.get("PANEL_ROIS", "").split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out               # in FRAME pixels — the operator reads them off the saved annotated frame


def main():
    import cv2
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--frame", default="")
    ap.add_argument("--collect", type=int, default=0)
    ap.add_argument("--out", default="/tmp/door_calib")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    def up(img, f=10):
        return cv2.resize(img, (img.shape[1] * f, img.shape[0] * f), interpolation=cv2.INTER_NEAREST)

    fr = cv2.imread(a.frame) if a.frame else newest_frame()
    H, W = fr.shape[:2]
    droi = gd.scale_roi(DOOR_ROI, CALIB_WH, (W, H))
    prois = _panel_rois()
    ann = gd.overlay_rois(fr, [droi] + prois, ["door_roi"] + [f"panel{i}" for i in range(len(prois))])
    cv2.imwrite(f"{a.out}/frame_annotated.png", ann)
    cv2.imwrite(f"{a.out}/door_roi.png", up(gd.crop(fr, droi), 4))
    for i, pr in enumerate(prois):
        cv2.imwrite(f"{a.out}/panel{i}.png", up(gd.crop(fr, pr)))
    print(f"[calib] frame {W}x{H}; door_roi(scaled to frame)={droi}; panels(frame px)={prois}")
    print(f"[calib] wrote {a.out}/frame_annotated.png + door_roi.png + panel*.png — eyeball, adjust PANEL_ROIS, re-run.")

    if a.collect:
        n = 0
        for _ in range(a.collect):
            try:
                f2 = newest_frame()
            except Exception as e:
                print("[calib] skip:", e); time.sleep(1); continue
            for i, pr in enumerate(prois):
                cv2.imwrite(f"{a.out}/glyph_p{i}_{n:04d}.png", up(gd.crop(f2, pr)))
            n += 1
            time.sleep(2)          # ~one per 2s segment
        print(f"[calib] collected {n} panel crop sets -> label the digits/arrows into templates.")


if __name__ == "__main__":
    main()
