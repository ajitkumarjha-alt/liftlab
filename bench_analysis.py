#!/usr/bin/env python3
"""VM analysis BENCHMARK — decode ch29 HEVC + YOLO onnx, on 2 vCPU / no GPU. Measures decode fps,
inference fps @ 1 and 2 threads, combined end-to-end fps for ONE stream, CPU cost, and whether the
event ingest is disturbed (samples the cloud app's response latency under inference load). Decides
how many of the 7 cameras this e2-small carries, and whether a GPU is required or optional.

Run under the analysis venv:  sudo /opt/liftlab-analysis/.venv/bin/python /opt/liftlab-analysis/bench_analysis.py
The relay must be running (segments must exist at LIVE_DIR/GW/CH).
"""
import glob
import os
import statistics
import sys
import threading
import time
import urllib.request

import av
import cv2
import numpy as np
import onnxruntime as ort

LIVE = os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live")
GW = os.environ.get("GW", "site-A")
CH = os.environ.get("CH", "ch29")
MODEL = os.environ.get("MODEL", "/opt/liftlab-analysis/yolo11n.onnx")
NFRAMES = int(os.environ.get("NFRAMES", "150"))
PROBE_URL = os.environ.get("CLOUD_PROBE_URL", "http://127.0.0.1:9090/watchstatus/" + GW)
N_CAMS = int(os.environ.get("N_CAMS", "7"))


def newest_segs(camdir, k=6):
    segs = sorted(glob.glob(os.path.join(camdir, "*.ts")), key=os.path.getmtime, reverse=True)
    return list(reversed(segs[:k]))       # oldest-first of the newest k (all complete on the VM)


def decode_frames(segs, n):
    frames = []
    t0 = time.perf_counter()
    for seg in segs:
        try:
            c = av.open(seg)
        except Exception as e:
            print(f"  decode open failed {os.path.basename(seg)}: {e}")
            continue
        for fr in c.decode(video=0):
            frames.append(fr.to_ndarray(format="bgr24"))
            if len(frames) >= n:
                break
        c.close()
        if len(frames) >= n:
            break
    return frames, time.perf_counter() - t0


def sess(threads):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    return ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])


def preprocess(fr):
    x = cv2.cvtColor(cv2.resize(fr, (640, 640)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(x, (2, 0, 1))[None]


def probe_latencies(stop, out, period=0.4):
    while not stop.is_set():
        t = time.perf_counter()
        try:
            urllib.request.urlopen(PROBE_URL, timeout=5).read()
            out.append((time.perf_counter() - t) * 1000)
        except Exception:
            pass
        time.sleep(period)


def main():
    camdir = os.path.join(LIVE, GW, CH)
    segs = newest_segs(camdir)
    if not segs:
        print(f"NO SEGMENTS at {camdir} — is the relay running?")
        sys.exit(1)
    try:
        c = av.open(segs[-1]); vs = c.streams.video[0]
        W, H, FPS, CODEC = vs.width, vs.height, float(vs.average_rate or 0), vs.codec_context.name
        c.close()
    except Exception as e:
        print(f"cannot probe segment: {e}"); sys.exit(1)
    print(f"source: {camdir}  {len(segs)} segs  |  {CODEC} {W}x{H} @ {FPS:.1f}fps")
    print(f"model : {MODEL}")

    # ---- 1. DECODE ----
    frames, ddt = decode_frames(segs, NFRAMES)
    if not frames:
        print("decode produced NO frames — HEVC decode unavailable? (PyAV should handle it)"); sys.exit(1)
    dec_fps = len(frames) / ddt if ddt > 0 else 0
    print(f"\n[DECODE]      {len(frames)} frames in {ddt:.2f}s -> {dec_fps:.0f} fps  ({CODEC} {W}x{H})")

    t0 = time.perf_counter()
    blobs = [preprocess(f) for f in frames]
    print(f"[PREPROCESS]  resize+cvt: {(time.perf_counter()-t0)/len(frames)*1000:.1f} ms/frame")

    # ---- 2. INFERENCE @ 1 and 2 threads ----
    infer = {}
    for th in (1, 2):
        s = sess(th); name = s.get_inputs()[0].name
        for b in blobs[:3]:
            s.run(None, {name: b})
        c0 = time.process_time(); w0 = time.perf_counter()
        for b in blobs:
            s.run(None, {name: b})
        wall = time.perf_counter() - w0; cpu = time.process_time() - c0
        infer[th] = (len(blobs) / wall, wall / len(blobs) * 1000, cpu / wall)
        print(f"[INFER {th}t]     {infer[th][0]:.2f} fps  ({infer[th][1]:.0f} ms/frame, ~{infer[th][2]:.1f} cores)")
        del s

    # ---- 3. END-TO-END (decode+preproc+infer) @ 2 threads, WITH ingest probe ----
    base = []
    stop = threading.Event()
    th_p = threading.Thread(target=probe_latencies, args=(stop, base), daemon=True); th_p.start()
    time.sleep(3)                                 # idle baseline of the cloud's latency
    stop.set(); th_p.join()
    load = []
    stop = threading.Event()
    th_p = threading.Thread(target=probe_latencies, args=(stop, load), daemon=True); th_p.start()
    s = sess(2); name = s.get_inputs()[0].name
    n = 0; c0 = time.process_time(); w0 = time.perf_counter()
    for seg in segs * 3:                           # loop segs to sustain load through the probe
        try:
            c = av.open(seg)
        except Exception:
            continue
        for fr in c.decode(video=0):
            s.run(None, {name: preprocess(fr.to_ndarray(format="bgr24"))}); n += 1
            if n >= NFRAMES:
                break
        c.close()
        if n >= NFRAMES:
            break
    wall = time.perf_counter() - w0; cpu = time.process_time() - c0
    stop.set(); th_p.join()
    e2e = n / wall; e2e_cores = cpu / wall
    print(f"\n[END-TO-END 2t] {e2e:.2f} fps  ({wall/n*1000:.0f} ms/frame, ~{e2e_cores:.1f} cores) — one stream, decode+preproc+infer")

    def stat(x):
        return (statistics.median(x), max(x)) if x else (None, None)
    bm, bx = stat(base); lm, lx = stat(load)
    print(f"\n[INGEST PROBE] cloud latency ({PROBE_URL.split('/')[-2]}...): "
          f"idle med/max {bm:.0f}/{bx:.0f} ms -> under-load {lm:.0f}/{lx:.0f} ms" if bm else "[INGEST PROBE] no samples (probe URL unreachable)")

    # ---- VERDICT ----
    print("\n=== VERDICT (VM e2-small, 2 vCPU, no GPU) ===")
    print(f"  Pi 4 reference: onnx 1.97 fps @ 3 threads")
    print(f"  VM 1 stream end-to-end @ 2t: {e2e:.2f} fps using ~{e2e_cores:.1f} of 2 cores")
    need_full = e2e_cores * N_CAMS
    print(f"  {N_CAMS} cams at THIS (every-frame) rate: ~{need_full:.1f} cores vs 2 -> "
          + ("CPU-BOUND, will not fit" if need_full > 2 else "may fit"))
    # analysis realistically needs ~1-2 fps/stream (occupancy/door), not every frame:
    for target in (1, 2):
        agg = target * N_CAMS
        feasible = agg <= e2e * (2 / max(e2e_cores, 0.01)) if e2e_cores > 0 else False
        head = e2e / e2e_cores * 2 if e2e_cores > 0 else 0      # max aggregate fps if both cores used
        print(f"  at {target} fps/stream x {N_CAMS} = {agg} infer/s; VM ceiling ~{head:.0f} fps aggregate -> "
              + ("FITS" if agg <= head else "needs GPU or fewer cams/fps"))
    if bm and lm and lm > bm * 3 and lm > 50:
        print(f"  WARNING: cloud latency {bm:.0f}->{lm:.0f} ms under load — inference DID disturb the ingest. Nice/cap the analysis worker.")
    elif bm:
        print(f"  ingest latency held ({bm:.0f}->{lm:.0f} ms) — analysis did not starve it at 1-stream load.")


if __name__ == "__main__":
    main()
