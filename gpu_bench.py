#!/usr/bin/env python3
"""Model benchmark — price the two FREE levers before anyone touches ANALYZE_FPS.

    python gpu_bench.py --segments 20 --variants base,fp16,trt,n

Measures, on THE SAME FRAMES, for each model variant:
  SPEED     ms/frame of model.track(), the only cost that has to fit the 2s segment budget
  COUNTING  the transits those detections actually produce, through the production ZoneCounter

The second one is the point. "TensorRT/FP16 is 2-3x for zero counting change" is a CLAIM, and it is
the claim that matters: FP16 and TensorRT both perturb detection confidences slightly, and a
detection sitting near CONF (0.35) can flip either side of it. A flipped detection can create or
destroy a track, and a track is a transit. So this does not report a speed number and call the
lever free — it reports whether the counts moved, and by how much, on real frames from this camera.

If counting is identical, the lever IS free and needs no version bump. If it is not, the size of the
difference is the price, and that is a decision to take deliberately rather than discover in a
month's data.

WHAT IT DOES NOT DO: change anything. No writes, no config, no service touched. It builds detectors
in-process, runs them over decoded frames, prints a table. Run it on liftlab-gpu while the fleet is
STOPPED (or accept that a running worker is competing for the same GPU and inflating every number —
it says so if it detects one).

Variants:
  base  MODEL as configured (yolo11m.pt) at fp32          — the current production path
  fp16  the same weights with half=True                   — free if counting holds
  trt   TensorRT engine exported from the same weights    — exports on first run (slow, ~minutes)
  n     yolo11n.pt at fp32                                — the documented revert; 80% @ n=66 is the bar
"""
import argparse
import glob
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("ANALYSIS_TOKEN", "bench")     # gpu_analyze requires it at import; unused here

LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
CONF = float(os.environ.get("CONF", "0.35"))
DEVICE = os.environ.get("DEVICE", "cuda")
SEG_BUDGET_MS = float(os.environ.get("SEG_DUR_S", "2")) * 1000


def log(m):
    print(m, flush=True)


def load_frames(n_segments, clips=""):
    """Decode N segments ONCE. Every variant then sees byte-identical frames — a benchmark that
    re-decodes per variant is comparing decode jitter as much as inference."""
    import gpu_analyze as ga          # production decode + zone scaling, so this measures the real path
    paths = []
    if clips:
        paths = sorted(glob.glob(clips))[:n_segments]
    else:
        d = LIVE_DIR / GW / CAM
        segs = glob.glob(str(d / "*.ts"))
        # The live ring rotates in seconds; a file globbed a moment ago can vanish before the read.
        stat = []
        for p in segs:
            try:
                stat.append((os.path.getmtime(p), p))
            except OSError:
                pass
        paths = [p for _, p in sorted(stat, reverse=True)[:n_segments]]
    if not paths:
        raise SystemExit(f"no segments: looked in {LIVE_DIR/GW/CAM} (or --clips). Is the relay delivering?")
    frames = []
    for p in paths:
        try:
            data = Path(p).read_bytes()
        except OSError:
            continue                   # rotated out mid-scan
        fr, _rel = ga.decode_segment(data)
        frames.extend(fr)
    if not frames:
        raise SystemExit("decoded 0 frames — segments unreadable")
    return frames, ga


def build(variant, base_model):
    """(detector, label, note). Uses the PRODUCTION detector class so the inference call is identical;
    only the weights/precision differ."""
    import counting
    if variant == "base":
        return counting.YoloDetector(weights=base_model, conf=CONF, device=DEVICE), base_model + " fp32", ""
    if variant == "n":
        w = os.environ.get("BENCH_N_MODEL", "yolo11n.pt")
        return counting.YoloDetector(weights=w, conf=CONF, device=DEVICE), w + " fp32", "the documented revert"
    if variant == "fp16":
        d = counting.YoloDetector(weights=base_model, conf=CONF, device=DEVICE)
        d.model.half()                 # weights -> fp16; track() then runs half precision
        return d, base_model + " fp16", "same weights, half precision"
    if variant == "trt":
        eng = os.path.splitext(base_model)[0] + ".engine"
        if not os.path.exists(eng):
            log(f"  [trt] exporting {base_model} -> {eng} (minutes, one time)…")
            from ultralytics import YOLO
            try:
                YOLO(base_model).export(format="engine", half=True, device=0)
            except Exception as e:
                return None, "TensorRT", f"export FAILED: {type(e).__name__}: {str(e)[:90]}"
        if not os.path.exists(eng):
            return None, "TensorRT", "export produced no .engine"
        return counting.YoloDetector(weights=eng, conf=CONF, device=DEVICE), eng + " fp16", "TensorRT"
    return None, variant, "unknown variant"


def run(det, frames, ga):
    """Track every frame, timing ONLY the model call, and count transits with the production logic."""
    import counting
    H, W = frames[0].shape[:2]
    sx, sy = W / ga.CALIB_W, H / ga.CALIB_H
    ctr = counting.ZoneCounter(ga.scale_zone(ga.ZONE_LANDING, sx, sy),
                               ga.scale_zone(ga.ZONE_CABIN, sx, sy))
    times, per_frame_dets = [], []
    for i, f in enumerate(frames):
        t0 = time.perf_counter()
        dets = det.track(f)
        times.append((time.perf_counter() - t0) * 1000)
        per_frame_dets.append(len(dets))
        ctr.update(dets, offset_s=i / 25.0)        # uniform clock: only RELATIVE order matters here
    b, a = ctr.counts()
    return {"ms": times, "dets": per_frame_dets, "boarded": b, "alighted": a,
            "transits": len(ctr.transits), "rejections": len(ctr.rejections)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=int, default=20, help="live segments to decode (~50 frames each)")
    ap.add_argument("--clips", default="", help="glob of .ts/.mp4 files instead of the live ring")
    ap.add_argument("--variants", default="base,fp16,n", help="base,fp16,trt,n")
    a = ap.parse_args()

    base_model = os.environ.get("MODEL", "yolo11m.pt")
    # A running worker shares the GPU and inflates every number here. Say so rather than quietly
    # benchmarking contention.
    try:
        import subprocess
        n_workers = int(subprocess.run(["pgrep", "-fc", "gpu_analyze.py"], capture_output=True,
                                       text=True).stdout.strip() or 0)
    except Exception:
        n_workers = 0
    if n_workers:
        log(f"WARNING: {n_workers} gpu_analyze worker(s) are running and competing for this GPU.")
        log("         Stop the fleet for a clean read: sudo systemctl stop liftlab-gpu-fleet\n")

    log(f"loading frames from {'clips' if a.clips else LIVE_DIR/GW/CAM} …")
    frames, ga = load_frames(a.segments, a.clips)
    H, W = frames[0].shape[:2]
    log(f"{len(frames)} frames at {W}x{H}; conf={CONF} device={DEVICE} budget={SEG_BUDGET_MS:.0f}ms/segment\n")

    results, base = [], None
    for v in [x.strip() for x in a.variants.split(",") if x.strip()]:
        det, label, note = build(v, base_model)
        if det is None:
            log(f"{v:6s} SKIPPED — {note}")
            results.append({"v": v, "label": label, "skip": note})
            continue
        log(f"{v:6s} running {label} …")
        r = run(det, frames, ga)
        r.update(v=v, label=label, note=note)
        if base is None:
            base = r
        results.append(r)
        del det

    # ---- report ----
    fps_per_seg = len(frames) / max(1, a.segments)
    log("\n" + "=" * 96)
    log(f"{'variant':7s} {'ms/frame':>9s} {'p95':>7s} {'seg ms':>8s} {'x budget':>9s} {'cams fit':>9s} "
        f"{'transits':>9s} {'in/out':>9s} {'det agree':>10s}")
    log("-" * 96)
    for r in results:
        if r.get("skip"):
            log(f"{r['v']:7s} {'—':>9s}  {r['skip'][:70]}")
            continue
        mean = statistics.fmean(r["ms"])
        p95 = sorted(r["ms"])[int(0.95 * (len(r["ms"]) - 1))]
        seg_ms = mean * fps_per_seg
        ratio = seg_ms / SEG_BUDGET_MS
        cams = int(SEG_BUDGET_MS // seg_ms) if seg_ms > 0 else 0
        if r is base:
            agree = "baseline"
        else:
            same = sum(1 for x, y in zip(r["dets"], base["dets"]) if x == y)
            agree = f"{100.0 * same / max(1, len(base['dets'])):.1f}%"
        log(f"{r['v']:7s} {mean:9.1f} {p95:7.1f} {seg_ms:8.0f} {ratio:8.2f}x {cams:9d} "
            f"{r['transits']:9d} {str(r['boarded'])+'/'+str(r['alighted']):>9s} {agree:>10s}")
    log("=" * 96)

    # ---- the verdict that actually decides this ----
    log("\nCOUNTING EQUIVALENCE (the thing that decides whether a lever is free):")
    for r in results:
        if r.get("skip") or r is base:
            continue
        d_t = r["transits"] - base["transits"]
        d_b = r["boarded"] - base["boarded"]
        d_a = r["alighted"] - base["alighted"]
        if d_t == 0 and d_b == 0 and d_a == 0:
            log(f"  {r['v']:6s} IDENTICAL counts on {len(frames)} frames — free on this sample.")
            log(f"         (identical here is evidence, not proof; a longer/busier sample is stronger)")
        else:
            log(f"  {r['v']:6s} COUNTS MOVED: transits {d_t:+d}, boarded {d_b:+d}, alighted {d_a:+d} "
                f"vs {base['transits']} baseline transits")
            log(f"         -> NOT free. This is a COUNTING_VERSION bump and a validation redo.")
    log("\nNote: 'cams fit' is inference only. Decode, the door pass and the fetch also cost, so treat")
    log("it as a ceiling and confirm against drop_frac on /ops with real cameras enabled.")


if __name__ == "__main__":
    sys.exit(main() or 0)
