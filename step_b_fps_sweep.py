#!/usr/bin/env python3
"""STEP B — frame-rate vs ByteTrack, on the surviving 17-clip night. LOCAL desk rig.
Same detector/config as the 71.4% bench (yolo11n.pt, conf=0.35, bytetrack.yaml, [0],
imgsz 640). SCOPED to the doors-OPEN transfer window per cycle (where people move) —
faithful to the bench AND fast (the clips are ~9-min continuous, mostly-empty overnight;
only real cycles cost anything). Decimates each window to 25/6/4/2 fps and runs the
EXACT counter (ZoneCounter on landing/cabin). Isolates the frame-rate effect on
ByteTrack ID persistence -> transit COUNT. Ground truth is gone, so this is the
count-vs-fps collapse signal; absolute precision needs a 7-event re-adjudication.
Run with cwd = the liftlab repo."""
import glob
import json
import sys
import time

import av
import numpy as np
from ultralytics import YOLO

from liftlab.counting import ZoneCounter, Detection
from liftlab.timestamps import build_model
from liftlab.doors import openness_signal_2pass, detect_cycles

Z = json.load(open("camera_zones.json"))["ch29"]
DOOR_ROI = tuple(Z["door_roi"])
FPS_LIST = [25, 6, 4, 2]
PAD_S = 3.0
CLIPS = sorted(glob.glob("clips/ch29_2026070[56]*.mp4"))
model = YOLO("yolo11n.pt")


def transfer_windows(clip):
    """Doors-open [open_full_s, close_start_s] windows (+/-pad) via the door pipeline."""
    m = build_model(clip)
    sig = openness_signal_2pass(clip, DOOR_ROI, m)
    wins = []
    for c in detect_cycles(sig, m):
        if c.transfer_s >= 0:
            wins.append((max(0.0, c.open_full_s - PAD_S), c.close_start_s + PAD_S))
    return m, wins


def count_window(clip, m, w_lo, w_hi, step):
    """Run the counter over one transfer window at the given decimation step."""
    zc = ZoneCounter(Z["zone_landing"], Z["zone_cabin"])
    offs = np.array([m.offset_at(i) for i in range(m.n_frames)], dtype=np.float64)
    cont = av.open(clip)
    s = next(v for v in cont.streams if v.type == "video")
    s.thread_type = "AUTO"
    tb = float(s.time_base)
    p0 = next(float(x.pts * s.time_base) for x in cont.decode(s) if x.pts is not None)
    cont.seek(int(max(0.0, w_lo + p0) / tb), stream=s)
    first, k = True, 0
    for fr in cont.decode(s):
        if fr.pts is None:
            continue
        off = float(fr.pts * s.time_base) - p0
        if off < w_lo:
            continue
        if off > w_hi:
            break
        if k % step:
            k += 1
            continue
        k += 1
        r = model.track(fr.to_ndarray(format="bgr24"), classes=[0], persist=not first,
                        conf=0.35, verbose=False, tracker="bytetrack.yaml")[0]
        first = False
        dets = []
        if r.boxes is not None and r.boxes.id is not None:
            for box, tid in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.id.cpu().numpy().astype(int)):
                dets.append(Detection(*box.tolist(), track_id=int(tid)))
        zc.update(dets, off)
    cont.close()
    return zc.counts()


print(f"STEP B — {len(CLIPS)} clips, scoped to doors-open windows", flush=True)
# 1) find transfer windows once (door detection is fps-independent)
windows = []
for clip in CLIPS:
    try:
        m, wins = transfer_windows(clip)
        for lo, hi in wins:
            windows.append((clip, m, lo, hi))
        if wins:
            print(f"  {clip.split(chr(92))[-1].split('/')[-1]}: {len(wins)} transfer window(s)", flush=True)
    except Exception as e:
        print(f"  ! detect {clip}: {type(e).__name__}: {str(e)[:60]}", file=sys.stderr, flush=True)
print(f"  total transfer windows across the night: {len(windows)}", flush=True)

print(f"\n{'fps':>4} {'step':>5} {'boarded':>8} {'alighted':>9} {'total':>6} {'wall_s':>7}", flush=True)
results = {}
for target in FPS_LIST:
    step = max(1, round(25 / target))
    t0 = time.time(); tb = ta = 0
    for clip, m, lo, hi in windows:
        try:
            b, a = count_window(clip, m, lo, hi, step)
            tb += b; ta += a
        except Exception as e:
            print(f"   ! count {clip} [{lo:.0f},{hi:.0f}] step{step}: {type(e).__name__}: {str(e)[:50]}", file=sys.stderr, flush=True)
    results[target] = (tb, ta)
    print(f"{target:>4} {step:>5} {tb:>8} {ta:>9} {tb+ta:>6} {round(time.time()-t0):>7}", flush=True)

base = results[25]; b0 = base[0] + base[1]
print("\nCOLLAPSE vs 25fps baseline (transit total):", flush=True)
for target in FPS_LIST:
    tb, ta = results[target]
    frac = (tb + ta) / b0 if b0 else 0
    print(f"  {target:>2}fps: boarded {tb} alighted {ta} total {tb+ta}  ({frac*100:.0f}% of 25fps)")
print("\nREAD: total CRATERS below ~6fps -> ByteTrack can't hold IDs on sparse frames ->")
print("Pi 4 (~2fps YOLO) can't do occupancy regardless of runtime (Hailo justified). HOLDS ->")
print("re-adjudicate the 7 events for the absolute precision-vs-71.4% number.")
