#!/usr/bin/env python3
"""Pi-side thermal probe for the occupancy LOAD path. Runs the REAL CabinCounter at a
chosen thread count + sample rate and logs SoC temp + throttle every 10s, so the redeploy
decision rests on a MEASURED Pi temperature — not an x86 guess.

Why synthetic frames: YOLO inference cost is frame-content-independent (same 640x640
conv), so a black/synthetic frame heats the SoC identically to a live cabin frame. This
isolates the YOLO thermal contribution WITHOUT needing NVR/ONVIF access.

RUN IT ALONGSIDE THE LIVE DOOR SERVICE for the true COMBINED load (occupancy shares the
SoC with the door loop). If it stays cool solo but you want the real answer, leave
liftlab-watch running and run this in parallel.

  # candidate config (the proposed fix): 1 thread, ~0.67 Hz, 15 min
  sudo -u askjitk /home/askjitk/liftlab-b4/.venv/bin/python occ_thermal_probe.py \
       --threads 1 --hz 0.67 --minutes 15

  # reproduce the failure (what threw 81C): 3 threads, 2 Hz
  sudo -u askjitk /home/askjitk/liftlab-b4/.venv/bin/python occ_thermal_probe.py \
       --threads 3 --hz 2.0 --minutes 15

PASS = soc_temp stays under ~70C and NO live throttle bit (freqcap/throttled/templimit)
sets during the run. FAIL at threads=1/0.67Hz => the LOAD path needs a fan, full stop.
"""
import argparse
import os
import subprocess
import sys
import time

BITS = {0: "undervolt", 1: "freqcap", 2: "throttled", 3: "templimit"}


def _vcgencmd(arg):
    try:
        out = subprocess.run(["vcgencmd", arg], capture_output=True, text=True, timeout=5).stdout.strip()
        return out
    except Exception as e:
        return f"err:{type(e).__name__}"


def temp_c():
    s = _vcgencmd("measure_temp")           # temp=62.3'C
    try:
        return float(s.split("=")[1].split("'")[0])
    except Exception:
        return -1.0


def throttle_live():
    s = _vcgencmd("get_throttled")          # throttled=0x0
    try:
        v = int(s.split("=")[1], 16)
    except Exception:
        return ["parse_err"], "?"
    live = [name for bit, name in BITS.items() if v & (1 << bit)]
    return live, hex(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--hz", type=float, default=0.67, help="YOLO inferences per second")
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--model", default="/home/askjitk/liftlab-b4/yolo11n.onnx")
    ap.add_argument("--sample-s", type=float, default=10.0, help="temp log period")
    a = ap.parse_args()

    # import the SAME counter the scheduler uses (must run in B4_DIR / B4 venv)
    sys.path.insert(0, os.path.dirname(os.path.abspath(a.model)))
    sys.path.insert(0, "/home/askjitk/liftlab-b4")
    import numpy as np
    import occupancy

    # a dummy zone that accepts everything — count value is irrelevant, only the compute matters
    zone = [[0, 0], [1919, 0], [1919, 1079], [0, 1079]]
    occ = occupancy.CabinCounter(a.model, zone, threads=a.threads)
    frame = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)

    t0 = temp_c()
    live0, hex0 = throttle_live()
    print(f"probe: threads={a.threads} hz={a.hz} minutes={a.minutes}")
    print(f"  start  temp={t0:.1f}C  throttle_live={live0 or '[]'}  ({hex0})")
    print(f"  {'t_s':>6} {'temp_C':>7} {'infers':>7} {'infer_ms':>9}  throttle_live")

    period = 1.0 / a.hz if a.hz > 0 else 0.0
    end = a.minutes * 60.0
    start = time.monotonic()
    next_sample = start
    next_infer = start
    infers = 0
    last_ms = 0.0
    tmax = t0
    hit = set(live0)

    while True:
        now = time.monotonic()
        if now - start >= end:
            break
        if now >= next_infer:
            w = time.perf_counter()
            occ.count(frame)
            last_ms = (time.perf_counter() - w) * 1000.0
            infers += 1
            next_infer = now + period
        if now >= next_sample:
            t = temp_c()
            tmax = max(tmax, t)
            live, _ = throttle_live()
            hit |= set(live)
            print(f"  {now - start:6.0f} {t:7.1f} {infers:7d} {last_ms:9.1f}  {live or '[]'}")
            next_sample = now + a.sample_s
        time.sleep(0.02)

    live_end, hex_end = throttle_live()
    print(f"\n  RESULT  peak_temp={tmax:.1f}C  infers={infers}  live_infer_ms~{last_ms:.0f}")
    bad = hit - set(live0)   # bits that turned ON during the run
    if tmax < 70.0 and not bad:
        print(f"  VERDICT: PASS — peak {tmax:.1f}C < 70C, no NEW throttle bit. Safe to enable at these settings.")
    else:
        why = []
        if tmax >= 70.0:
            why.append(f"peak {tmax:.1f}C >= 70C")
        if bad:
            why.append(f"throttle bits set during run: {sorted(bad)}")
        print(f"  VERDICT: FAIL — {'; '.join(why)}. This config heats the SoC; drop threads/hz or add a fan.")


if __name__ == "__main__":
    main()
