#!/usr/bin/env python3
"""ZONE VERIFICATION — the prerequisite that blocks counting. ch29's zone_cabin/zone_landing were
drawn on 1920x1080 desk-rig geometry; the relay carries the SUB stream (704x576 for ch29, a 4CIF
1.22 aspect vs main's 1.78). A per-axis scale (sx=subW/1920, sy=subH/1080) is only valid if the sub
is a full-frame ANAMORPHIC resample of main; if it's cropped or a different FOV, the scaled zones
land in the wrong place and every boarded/alighted count is garbage.

So: decode a REAL ch29 sub frame, overlay the per-axis-scaled zones, and EYEBALL whether they sit on
the cabin/landing. Run on the VM (local segments) under the analysis venv:
    sudo /opt/liftlab-analysis/.venv/bin/python zone_verify.py
Then open the printed URL. If GREEN=cabin / ORANGE=landing don't fit -> REDRAW on sub geometry.
"""
import glob
import json
import os
import sys

import av
import cv2
import numpy as np

LIVE = os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live")
GW = os.environ.get("GW", "site-A")
CH = os.environ.get("CH", "ch29")
OUT = os.environ.get("ZONE_OUT", "/run/liftlab-snap/" + GW + "/zonecheck.jpg")
ZONES_JSON = os.environ.get("ZONES_JSON", "")          # optional: read zones from a camera_zones.json
CALIB_W, CALIB_H = 1920, 1080

# desk-rig ch29 zones (1920x1080), geometry-verified 2026-07-15
ZONE_CABIN = [[630, 870], [932, 747], [1042, 733], [1308, 1056], [587, 1056], [548, 914]]
ZONE_LANDING = [[514, 394], [834, 322], [722, 529], [732, 684], [761, 776], [618, 827]]


def load_zones():
    if ZONES_JSON and os.path.exists(ZONES_JSON):
        try:
            z = json.load(open(ZONES_JSON)).get(CH, {})
            return z.get("zone_cabin", ZONE_CABIN), z.get("zone_landing", ZONE_LANDING)
        except Exception as e:
            print(f"  (zones json unreadable: {e}; using embedded desk-rig zones)")
    return ZONE_CABIN, ZONE_LANDING


def newest(camdir):
    segs = sorted(glob.glob(os.path.join(camdir, "*.ts")), key=os.path.getmtime, reverse=True)
    return segs[0] if segs else None


def grab_frame(seg):
    c = av.open(seg)
    try:
        for fr in c.decode(video=0):
            return fr.to_ndarray(format="bgr24")
    finally:
        c.close()
    return None


def scale(poly, sx, sy):
    return np.array([[int(round(x * sx)), int(round(y * sy))] for x, y in poly], np.int32)


def main():
    camdir = os.path.join(LIVE, GW, CH)
    seg = newest(camdir)
    if not seg:
        print(f"NO SEGMENTS at {camdir} — is the relay running?"); sys.exit(1)
    fr = grab_frame(seg)
    if fr is None:
        print("decode produced no frame"); sys.exit(1)
    H, W = fr.shape[:2]
    sx, sy = W / CALIB_W, H / CALIB_H
    asp_main, asp_sub = CALIB_W / CALIB_H, W / H
    cabin, landing = load_zones()
    cab, land = scale(cabin, sx, sy), scale(landing, sx, sy)

    ov = fr.copy()
    cv2.fillPoly(ov, [cab], (40, 200, 90))          # BGR green = cabin
    cv2.fillPoly(ov, [land], (60, 150, 230))        # BGR orange = landing
    fr = cv2.addWeighted(ov, 0.35, fr, 0.65, 0)
    cv2.polylines(fr, [cab], True, (40, 200, 90), 2)
    cv2.polylines(fr, [land], True, (60, 150, 230), 2)
    for pt in cab:
        cv2.circle(fr, tuple(pt), 3, (40, 200, 90), -1)
    for pt in land:
        cv2.circle(fr, tuple(pt), 3, (60, 150, 230), -1)
    inb = all(0 <= x < W and 0 <= y < H for x, y in np.vstack([cab, land]))
    cv2.putText(fr, f"{CH} SUB {W}x{H}  aspect {asp_sub:.2f} vs main {asp_main:.2f}  sx={sx:.3f} sy={sy:.3f}",
                (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(fr, "GREEN=cabin ORANGE=landing  (per-axis scaled from 1920x1080)",
                (6, H - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cv2.imwrite(OUT, fr)
    print(f"source seg : {os.path.basename(seg)}  ({W}x{H})")
    print(f"aspect     : sub {asp_sub:.2f}  vs main {asp_main:.2f}  (delta {abs(asp_sub-asp_main):.2f}) "
          f"-> {'SAME aspect, uniform-ish scale plausible' if abs(asp_sub-asp_main) < 0.05 else 'DIFFERENT aspect — anamorphic; per-axis scale only valid if sub is a full-frame resample'}")
    print(f"scaled pts in-bounds: {inb}")
    print(f"wrote {OUT}")
    print(f"VIEW: https://lift.gargi.online/snap/{GW}/zonecheck.jpg   (basicauth)")
    print("EYEBALL: does GREEN enclose the CABIN floor and ORANGE the LANDING threshold?")
    print("  fits  -> zones transfer; proceed to build the pipeline against these coords.")
    print("  shifted/clipped -> the sub is cropped/different-FOV; REDRAW zones on sub geometry first.")


if __name__ == "__main__":
    main()
