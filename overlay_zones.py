#!/usr/bin/env python3
"""Overlay the desk-rig ch29 zone_cabin (+ landing/door for context) onto the LIVE
/validation seed stills, to turn "very likely transfers if the camera hasn't moved"
into a FACT. Polygons are in the calibration resolution (1920x1080, drawn 2026-07-11);
they are SCALED to each frame's size (the /validation stills are downscaled to ~720w,
so raw coords would misalign). Run on ALL three stills — if zone_cabin fits all three,
the camera demonstrably hasn't moved across 16h; one frame could flatter.

Usage:  python overlay_zones.py ch29_20260714204500.jpg ch29_20260715110630.jpg ch29_20260715124743.jpg
Outputs one <name>.overlay.png per input. Then eyeball: COPY vs REDRAW.
"""
import sys

from PIL import Image, ImageDraw

CALIB_W, CALIB_H = 1920, 1080
ZONE_CABIN = [[630, 870], [932, 747], [1042, 733], [1308, 1056], [587, 1056], [548, 914]]
ZONE_LANDING = [[514, 394], [834, 322], [722, 529], [732, 684], [761, 776], [618, 827]]
DOOR_ROI = [450, 0, 568, 900]   # x, y, w, h


def scale(poly, sx, sy):
    return [(x * sx, y * sy) for x, y in poly]


if len(sys.argv) < 2:
    print("usage: python overlay_zones.py <seed1.jpg> [seed2.jpg] [seed3.jpg]")
    raise SystemExit(2)

for path in sys.argv[1:]:
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:
        print(f"  {path}: cannot open ({e})")
        continue
    w, h = im.size
    sx, sy = w / CALIB_W, h / CALIB_H
    d = ImageDraw.Draw(im, "RGBA")
    dx, dy, dw, dh = DOOR_ROI
    d.rectangle([dx * sx, dy * sy, (dx + dw) * sx, (dy + dh) * sy], outline=(80, 150, 230, 255), width=2)
    d.polygon(scale(ZONE_LANDING, sx, sy), outline=(230, 150, 60, 255), width=3)
    cab = scale(ZONE_CABIN, sx, sy)
    d.polygon(cab, outline=(40, 200, 90, 255), width=4, fill=(40, 200, 90, 60))
    d.text((6, 6), f"{path.split(chr(92))[-1].split('/')[-1]}  {w}x{h}  "
                   f"GREEN=zone_cabin  ORANGE=landing  BLUE=door", fill=(255, 255, 0, 255))
    inb = all(0 <= x <= w and 0 <= y <= h for x, y in cab)
    aspect_ok = abs(sx - sy) < 0.02
    out = path.rsplit(".", 1)[0] + ".overlay.png"
    im.save(out)
    print(f"  {path}: {w}x{h}  scale=({sx:.3f},{sy:.3f})  cabin_in_bounds={inb}  "
          f"aspect_match={aspect_ok}  -> {out}")

print("\nEYEBALL each .overlay.png:")
print("  Does GREEN zone_cabin enclose the CABIN floor (where riders stand), EXCLUDING")
print("  the ORANGE landing? Fits all THREE -> camera unmoved -> COPY the desk-rig polygon")
print("  into the Pi camera_zones.json. Shifted/clipped on any -> REDRAW on the current still.")
print("  (If cabin_in_bounds=False or aspect_match=False on a frame, the still isn't 16:9/1080-")
print("   derived and needs a full-res grab before deciding.)")
