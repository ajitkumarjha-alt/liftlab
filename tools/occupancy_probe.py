#!/usr/bin/env python3
"""Report peak cabin occupancy over a frame range of a corpus mp4 — the measured minimum.

Drives the SAME ZoneCounter.cabin_ids the engine uses, so what this prints is what an episode
would carry. It reports the distribution, not just the peak: a single-frame maximum is the number
most likely to be a tracker artefact, and the median tells you whether the peak was sustained.
"""
import argparse, json, os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--roi-json", required=True)
    ap.add_argument("--from-frame", type=int, default=0)
    ap.add_argument("--to-frame", type=int, default=0)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--model", default=os.environ.get("MODEL", "yolo11n.pt"))
    ap.add_argument("--conf", type=float, default=0.35)
    a = ap.parse_args()

    import cv2, counting
    roi = json.load(open(a.roi_json))
    zl, zc = roi.get("zone_landing"), roi.get("zone_cabin")
    if not (zl and zc):
        raise SystemExit("roi.json has no zone_landing/zone_cabin — occupancy needs the cabin zone")
    ctr = counting.ZoneCounter(zone_landing=zl, zone_cabin=zc)
    det = counting.YoloDetector(weights=a.model, conf=a.conf, tracker="bytetrack.yaml", device=None)

    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    i = 0; per = []; peak_at = None; peak = 0
    while True:
        ok, fr = cap.read()
        if not ok: break
        i += 1
        if a.to_frame and i > a.to_frame: break
        if i < a.from_frame or (i % a.stride): continue
        dets = det.track(fr)
        n = len(ctr.cabin_ids(dets))
        per.append((i, n, len(dets)))
        if n > peak: peak, peak_at = n, i
    cap.release()
    if not per:
        raise SystemExit("no frames analysed in that range")
    ns = sorted(x[1] for x in per)
    q = lambda p: ns[int(p * (len(ns) - 1))]
    tot = sorted(x[2] for x in per)
    print(f"=== cabin occupancy, {os.path.basename(a.video)} f{a.from_frame}-{a.to_frame} "
          f"(stride {a.stride}, {len(per)} analysed frames) ===")
    print(f"  PEAK cabin occupancy (MEASURED MINIMUM): {peak}  at frame {peak_at}")
    print(f"  cabin per-frame   p50 {q(.5)}  p90 {q(.9)}  max {ns[-1]}")
    print(f"  all detections    p50 {tot[len(tot)//2]}  max {tot[-1]}   (whole frame, both zones + outside)")
    hist = collections.Counter(x[1] for x in per)
    print("  distribution:", " ".join(f"{k}:{v}" for k, v in sorted(hist.items())))
    sustained = sum(1 for _f, n, _t in per if n >= max(1, peak - 1))
    print(f"  frames at >= peak-1 ({max(1, peak-1)}): {sustained}/{len(per)} "
          f"({100.0*sustained/len(per):.0f}%) — a peak seen on one frame is an artefact candidate")
    return 0

if __name__ == "__main__":
    sys.exit(main())
