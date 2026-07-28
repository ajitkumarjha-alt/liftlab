#!/usr/bin/env python3
"""Model benchmark — price the two FREE levers before anyone touches ANALYZE_FPS.

    ANALYSIS_TOKEN=<the worker's token> python gpu_bench.py --segments 20 --variants base,fp16,trt,n

Frames come from the CLOUD, the same way gpu_analyze fetches them (the local /dev/shm ring died
with the dumb-streamer rework): the bench polls the live playlist and downloads N segments up
front, decodes ONCE, and only then runs the variants — so fetch cost never pollutes the variant
comparison. --clips <glob> still works for pre-downloaded .ts files. Counting zones are primed
from the registry for CAM before gpu_analyze imports, so the bench counts with the exact zones
production uses; a camera with no zones runs SPEED-ONLY and says so.

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
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
CONF = float(os.environ.get("CONF", "0.35"))
DEVICE = os.environ.get("DEVICE", "cuda")
CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
SEG_BUDGET_MS = float(os.environ.get("SEG_DUR_S", "2")) * 1000


def log(m):
    print(m, flush=True)


def _token():
    """The worker's analysis token. Forgiving about the 'site-A:tok' vs bare-token confusion: the
    Bearer the cloud compares against is the BARE token, so strip a matching gateway prefix."""
    t = os.environ.get("ANALYSIS_TOKEN", "")
    if ":" in t and t.split(":", 1)[0] == GW:
        t = t.split(":", 1)[1]
        log(f"(ANALYSIS_TOKEN had the '{GW}:' prefix — using the bare token)")
    return t


def prime_zone_env():
    """Zones for CAM from the registry -> env, BEFORE gpu_analyze imports (its module-level parse
    reads env). The bench then counts with the same zones production uses. Explicit env wins."""
    if os.environ.get("ZONE_LANDING") and os.environ.get("ZONE_CABIN"):
        return "env"
    try:
        req = urllib.request.Request(f"{CLOUD}/api/gw/{GW}/cameras",
                                     headers={"Authorization": "Bearer " + _token()})
        d = json.loads(urllib.request.urlopen(req, timeout=15).read())
        for c in d.get("cameras", []):
            if c.get("cam") == CAM:
                g = c.get("geometry") or {}
                if g.get("zone_landing") and g.get("zone_cabin"):
                    os.environ["ZONE_LANDING"] = g["zone_landing"]
                    os.environ["ZONE_CABIN"] = g["zone_cabin"]
                    if g.get("zone_frame"):
                        os.environ["ZONE_FRAME"] = g["zone_frame"]
                    return "registry"
    except Exception as e:
        log(f"registry zones unavailable ({type(e).__name__}: {str(e)[:80]})")
    return "none"


def load_frames(n_segments, clips=""):
    """Decode N segments ONCE. Every variant then sees byte-identical frames — a benchmark that
    re-decodes per variant is comparing decode jitter as much as inference.

    Cloud path: poll the live playlist and download new segments until N are collected (the
    playlist is a rotating ring of a few, so this takes ~n_segments x SEG_DUR_S of wall time).
    All fetching finishes BEFORE any variant runs — fetch cost cannot pollute the comparison."""
    import gpu_analyze as ga          # production fetch + decode, so this measures the real path
    blobs = []
    if clips:
        for p in sorted(glob.glob(clips))[:n_segments]:
            try:
                blobs.append(Path(p).read_bytes())
            except OSError:
                continue
        if not blobs:
            raise SystemExit(f"no readable files matched --clips {clips}")
    else:
        seen = set()
        deadline = time.time() + max(120, n_segments * 6)
        log(f"collecting {n_segments} segments from {ga.BASE} (live ring — takes about "
            f"{n_segments * int(float(os.environ.get('SEG_DUR_S', '2')))}s of wall time)…")
        while len(blobs) < n_segments and time.time() < deadline:
            got_new = False
            for nm in ga.playlist_segments():
                if nm in seen:
                    continue
                seen.add(nm)
                got_new = True
                try:
                    blobs.append(ga.http_get(f"{ga.BASE}/{nm}"))
                    log(f"  fetched {nm} ({len(blobs)}/{n_segments})")
                except Exception as e:
                    log(f"  fetch {nm} failed: {type(e).__name__}: {str(e)[:80]}")
                if len(blobs) >= n_segments:
                    break
            if not got_new and len(blobs) < n_segments:
                time.sleep(1.0)
        if not blobs:
            raise SystemExit(f"no segments collected from {ga.BASE} — is the relay delivering? "
                             f"(check /ops segment age)")
        if len(blobs) < n_segments:
            log(f"NOTE: collected only {len(blobs)}/{n_segments} before the deadline — proceeding")
    frames = []
    for data in blobs:
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
    """Track every frame, timing ONLY the model call, and count transits with the production logic.
    Zones come from ga.make_counter — the registry-primed production path; a camera with no zones
    returns counting fields as None (SPEED-ONLY, reported as such rather than counted wrong)."""
    H, W = frames[0].shape[:2]
    ctr = ga.make_counter(W, H)
    times, per_frame_dets = [], []
    for i, f in enumerate(frames):
        t0 = time.perf_counter()
        dets = det.track(f)
        times.append((time.perf_counter() - t0) * 1000)
        per_frame_dets.append(len(dets))
        if ctr is not None:
            ctr.update(dets, offset_s=i / 25.0)    # uniform clock: only RELATIVE order matters here
    if ctr is None:
        return {"ms": times, "dets": per_frame_dets, "boarded": None, "alighted": None,
                "transits": None, "rejections": None}
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

    # Token BEFORE any gpu_analyze import: ga reads ANALYSIS_TOKEN at module level for its Bearer
    # header, so the bare-token normalization has to land in the env first. --clips mode needs no
    # network, so a placeholder satisfies the import.
    bare = _token()
    if bare:
        os.environ["ANALYSIS_TOKEN"] = bare
    elif a.clips:
        os.environ.setdefault("ANALYSIS_TOKEN", "bench")   # import-only; never sent anywhere
    else:
        raise SystemExit("cloud fetch needs the worker's token: ANALYSIS_TOKEN=<token> "
                         "(the fleet unit env has it), or use --clips <glob> of local .ts files")
    zones_src = prime_zone_env()
    log(f"zones for {CAM}: {zones_src}" + ("" if zones_src != "none" and CAM != "ch29" else
        " (ch29 falls back to built-ins; any other cam runs SPEED-ONLY without zones)"
        if zones_src == "none" else ""))

    log(f"loading frames from {'clips' if a.clips else 'cloud live playlist'} …")
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
        # Print AS EACH COMPLETES: a later variant crashing (the .engine .to() TypeError) must not
        # eat the numbers already paid for.
        _mean = statistics.fmean(r["ms"])
        _p95 = sorted(r["ms"])[int(0.95 * (len(r["ms"]) - 1))]
        _seg = _mean * (len(frames) / max(1, a.segments))
        _tr = r["transits"] if r["transits"] is not None else "—"
        log(f"   -> {v}: {_mean:.1f} ms/frame (p95 {_p95:.1f}) | {_seg:.0f} ms/segment "
            f"({_seg / SEG_BUDGET_MS:.2f}x budget, {int(SEG_BUDGET_MS // _seg) if _seg > 0 else 0} cams fit) "
            f"| transits={_tr} in/out={r['boarded']}/{r['alighted']}  [banked]")
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
        tr = f"{r['transits']:9d}" if r["transits"] is not None else f"{'—':>9s}"
        io = (f"{str(r['boarded'])+'/'+str(r['alighted']):>9s}" if r["boarded"] is not None
              else f"{'—':>9s}")
        log(f"{r['v']:7s} {mean:9.1f} {p95:7.1f} {seg_ms:8.0f} {ratio:8.2f}x {cams:9d} "
            f"{tr} {io} {agree:>10s}")
    log("=" * 96)

    # ---- the verdict that actually decides this ----
    log("\nCOUNTING EQUIVALENCE (the thing that decides whether a lever is free):")
    if base is not None and base.get("transits") is None:
        log(f"  SPEED-ONLY run — no zones for {CAM}, so counting was not compared. Per-frame")
        log(f"  detection agreement (table above) is the only equivalence signal this run carries;")
        log(f"  save zones for {CAM} and re-run before treating any lever as counting-free.")
    for r in results:
        if r.get("skip") or r is base:
            continue
        if r.get("transits") is None or base.get("transits") is None:
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
