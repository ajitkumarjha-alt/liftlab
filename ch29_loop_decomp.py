#!/usr/bin/env python3
"""READ-ONLY ch29 loop decomposition. Breaks the coupled decode+YOLO loop into two
ISOLATED measurements so we know which fix to build. Changes NOTHING (no agent,
pipeline, controller, or Gargi writes). Run with the B4 venv.

Confirmed upstream: stream delivers 25fps steady (bare ffmpeg 24.98, no decay).
The 0.9fps+decay is in our loop. This decomposes it.

Pipeline reality (read, not invented):
  * capture backend = PyAV (av.open, thread_type=AUTO) — same as the 0.9fps loop
    (load_probe.py) and the repo pipeline (liftlab/hwdecode.py). NO cv2.VideoCapture
    exists in the frame path; the cv2 path here is an EXTRA test of that hypothesis.
  * Pi 4 HEVC decode is software-only unless hevc_v4l2m2m is present (reported below).
  * inference = onnxruntime CPU, 1 thread, model resized to the ONNX input size.

1. DECODE-ONLY fps (no inference, no YOLO import in the loop): PyAV, then cv2.
2. INFERENCE-ONLY fps (no capture in the loop): ONE static frame, N=100, at the
   model's native input size AND (best-effort) ultralytics 640 vs 1088.
3. VERDICT table + which case.
"""
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")
import onvif_resolve  # noqa: E402

ENV = "/etc/liftlab-agent.env"
CH = 29
MODEL_CANDIDATES = ["/home/askjitk/liftlab-b4/yolo11n.onnx", "/home/askjitk/yolo11n.onnx"]
DECODE_SECONDS = 60
INFER_N = 100


def load_env(p):
    cfg = {}
    for line in Path(p).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


c = load_env(ENV)
USER, PW, HOST = c.get("NVR_USER", ""), c.get("NVR_PASS", ""), c.get("NVR_HOST", "")
GWID = c.get("GATEWAY_ID", "site-A")
MODEL = next((m for m in MODEL_CANDIDATES if Path(m).exists()), MODEL_CANDIDATES[0])


def ch29_url():
    cm = onvif_resolve.resolve_map(HOST, USER, PW, cache_path=f"/tmp/onvif_map_{GWID}.json")
    raw = onvif_resolve.uri_for(cm, CH, prefer=1)
    if not raw:
        return None
    return raw if urlparse(raw).username else raw.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


URL = ch29_url()
print(f"READ-ONLY ch29 loop decomposition — NVR {HOST}")
print(f"  ch{CH} URL (main, profile=vam), model={MODEL}\n")

# what decoders exist (confirms software-only HEVC on this Pi)
try:
    import av
    codecs = set(av.codecs_available)
    hw = "hevc_v4l2m2m" in codecs
    print(f"  PyAV codecs: hevc_v4l2m2m present = {hw}  (LIFTLAB_HWDEC={os.environ.get('LIFTLAB_HWDEC','')!r})")
except Exception as e:
    av = None
    print("  PyAV import failed:", e)


def buckets_report(label, samples, total_frames, total_t):
    print(f"  {label}: overall {round(total_frames/total_t,2)} fps ({total_frames} frames / {round(total_t,1)}s)")
    print("     end_s  frames  fps")
    for end_s, fr in samples:
        print(f"     {end_s:>5}  {fr:>6}  {round(fr/10.0,2)}")
    if len(samples) >= 2:
        first, last = samples[0][1] / 10.0, samples[-1][1] / 10.0
        print(f"     decay: {'YES' if first>0 and last<0.6*first else 'no'} (first {round(first,2)} -> last {round(last,2)} fps)")


# ---------- 1a. PyAV decode-only ----------
print("\n=== 1a. PyAV DECODE-ONLY (thread_type=AUTO, the pipeline's real backend) ===")
pyav_fps = None
if av and URL:
    try:
        cont = av.open(URL, options={"rtsp_transport": "tcp", "stimeout": "5000000"})
        s = next(x for x in cont.streams if x.type == "video")
        s.thread_type = "AUTO"
        print(f"  codec={s.codec_context.name} {s.codec_context.width}x{s.codec_context.height} "
              f"thread_type={s.thread_type} decoder={'hevc_v4l2m2m' if ('hevc_v4l2m2m' in codecs and s.codec_context.name in ('hevc','h265')) else 'software'}")
        t0, n, samples, nextb = time.time(), 0, [], 10
        for frame in cont.decode(s):
            n += 1
            el = time.time() - t0
            if el >= nextb:
                samples.append((nextb, n - sum(x[1] for x in samples)))
                nextb += 10
            if el >= DECODE_SECONDS:
                break
        tt = time.time() - t0
        cont.close()
        pyav_fps = round(n / tt, 2)
        buckets_report("PyAV decode-only", samples, n, tt)
    except Exception as e:
        print("  PyAV decode FAILED:", type(e).__name__, str(e)[:120])

# ---------- 1b. cv2 decode-only ----------
print("\n=== 1b. cv2.VideoCapture DECODE-ONLY (hypothesis test; NOT used by the pipeline) ===")
cv2_fps = None
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
try:
    import cv2
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("  cv2 could not open the stream")
    else:
        bs = cap.get(cv2.CAP_PROP_BUFFERSIZE)
        set_ok = cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        bs_after = cap.get(cv2.CAP_PROP_BUFFERSIZE)
        print(f"  backend={cap.getBackendName()} CAP_PROP_BUFFERSIZE default={bs} set(1)->{set_ok} now={bs_after}")
        t0, n, samples, nextb = time.time(), 0, [], 10
        while True:
            ok = cap.grab()
            if not ok:
                break
            cap.retrieve()
            n += 1
            el = time.time() - t0
            if el >= nextb:
                samples.append((nextb, n - sum(x[1] for x in samples)))
                nextb += 10
            if el >= DECODE_SECONDS:
                break
        tt = time.time() - t0
        cap.release()
        cv2_fps = round(n / tt, 2)
        buckets_report("cv2 decode-only", samples, n, tt)
except Exception as e:
    print("  cv2 decode FAILED:", type(e).__name__, str(e)[:120])

# ---------- grab ONE static frame for inference ----------
frame_bgr = None
try:
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(URL, cv2.CAP_FFMPEG)
    for _ in range(10):
        ok, f = cap.read()
        if ok and f is not None:
            frame_bgr = f
            break
    cap.release()
    if frame_bgr is not None:
        print(f"\n  static frame for inference: {frame_bgr.shape[1]}x{frame_bgr.shape[0]}")
except Exception as e:
    print("  frame grab FAILED:", str(e)[:100])

# ---------- 2. inference-only ----------
print("\n=== 2. YOLO INFERENCE-ONLY (static in-memory frame, N=%d, no capture in loop) ===" % INFER_N)
onnx_native_fps = onnx_640_fps = None
cur_imgsz = None
if frame_bgr is not None and Path(MODEL).exists():
    try:
        import numpy as np
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = so.inter_op_num_threads = 1
        sess = ort.InferenceSession(MODEL, sess_options=so, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        shp = inp.shape  # [1,3,H,W]
        Hd = shp[2] if isinstance(shp[2], int) else None
        Wd = shp[3] if isinstance(shp[3], int) else None
        cur_imgsz = f"{Wd}x{Hd}" if Hd and Wd else f"dynamic {shp}"
        print(f"  ONNX model input '{inp.name}' shape={shp} -> current imgsz = {cur_imgsz} (onnxruntime CPU, 1 thread)")

        def pre(img, size):
            import cv2
            x = cv2.cvtColor(cv2.resize(img, (size, size)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            return np.transpose(x, (2, 0, 1))[None]

        def time_onnx(size, n):
            blob = pre(frame_bgr, size)
            sess.run(None, {inp.name: blob})  # warmup
            t0 = time.time()
            for _ in range(n):
                sess.run(None, {inp.name: blob})
            return round(n / (time.time() - t0), 2)

        if Hd and Wd:  # fixed input
            onnx_native_fps = time_onnx(Hd, INFER_N)
            print(f"  a) native/current ({cur_imgsz}): {onnx_native_fps} fps")
            if Hd == 640:
                onnx_640_fps = onnx_native_fps
                print("  b) imgsz=640: SAME (model input is already 640 — resize is ALREADY in effect)")
            else:
                onnx_640_fps = time_onnx(640, INFER_N)
                print(f"  b) imgsz=640: {onnx_640_fps} fps")
        else:  # dynamic
            onnx_native_fps = time_onnx(1088, INFER_N // 2)
            onnx_640_fps = time_onnx(640, INFER_N)
            print(f"  a) imgsz=1088 (dynamic): {onnx_native_fps} fps")
            print(f"  b) imgsz=640: {onnx_640_fps} fps")
    except Exception as e:
        print("  ONNX inference FAILED:", type(e).__name__, str(e)[:120])

# best-effort ultralytics .pt native-vs-640 (the repo detector path)
print("\n  -- best-effort ultralytics .pt (repo YoloDetector path) resize sensitivity --")
pt_640 = pt_big = None
try:
    from ultralytics import YOLO
    ptp = next((p for p in ["/home/askjitk/liftlab-b4/yolo11n.pt", "/home/askjitk/yolo11n.pt",
                            str(Path.home() / "yolo11n.pt")] if Path(p).exists()), "yolo11n.pt")
    ym = YOLO(ptp)
    for _ in range(3):
        ym.predict(frame_bgr, imgsz=640, classes=[0], verbose=False)
    t0 = time.time()
    for _ in range(20):
        ym.predict(frame_bgr, imgsz=640, classes=[0], verbose=False)
    pt_640 = round(20 / (time.time() - t0), 2)
    t0 = time.time()
    for _ in range(20):
        ym.predict(frame_bgr, imgsz=1088, classes=[0], verbose=False)
    pt_big = round(20 / (time.time() - t0), 2)
    print(f"  ultralytics {Path(ptp).name}: imgsz=640 -> {pt_640} fps | imgsz=1088 -> {pt_big} fps")
except Exception as e:
    print("  ultralytics unavailable/skip:", type(e).__name__, str(e)[:80])

# ---------- 3. verdict ----------
print("\n=== 3. VERDICT TABLE ===")
print(f"  {'measurement':28} {'fps':8} notes")
print(f"  {'PyAV decode-only':28} {str(pyav_fps):8} real backend, thread_type=AUTO")
print(f"  {'cv2 decode-only':28} {str(cv2_fps):8} hypothesis test (not pipeline path)")
print(f"  {'YOLO infer-only @current':28} {str(onnx_native_fps):8} imgsz={cur_imgsz}")
print(f"  {'YOLO infer-only @640':28} {str(onnx_640_fps):8}")
if pt_640 or pt_big:
    print(f"  {'ultralytics .pt @640/@1088':28} {str(pt_640)+'/'+str(pt_big):8} resize sensitivity")
print(f"  {'pipeline observed':28} {'~0.9':8} coupled loop")
print("""
  READ THE CASE:
  - decode-only ~1fps                      -> software HEVC decode is the wall.
      Fix = multithreaded/hardware decode or decode the SUB stream, NOT resize.
  - decode-only high, infer@cur ~1, @640 fast -> full-res inference is the wall.
      Fix = resize to 640 before infer. (If current imgsz is ALREADY 640, this
      case is impossible — resize is done; look at decode or loop structure.)
  - both individually fast, pipeline still ~0.9 -> loop STRUCTURE (blocking read /
      no frame drop / buffer growth). Fix = latest-frame capture thread that drops
      stale frames, decoupled from inference.
""")
