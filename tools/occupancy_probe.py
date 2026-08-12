#!/usr/bin/env python3
"""Report peak cabin occupancy over a frame range of a corpus mp4 — the measured minimum.

Drives the SAME ZoneCounter.cabin_ids the engine uses, so what this prints is what an episode
would carry. It reports the distribution, not just the peak: a single-frame maximum is the number
most likely to be a tracker artefact, and the median tells you whether the peak was sustained.
"""
import argparse, collections, contextlib, json, os, re, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


@contextlib.contextmanager
def _decoder_stderr():
    """Capture fd 2 so the FFmpeg/HEVC decoder's complaints can be COUNTED, not just scrolled past.

    They are written by the C layer, so a Python-level redirect cannot see them; this dup2s the
    real file descriptor. Everything captured is re-emitted afterwards — swallowing a real error to
    count it would be a poor trade.
    """
    tmp = tempfile.TemporaryFile(mode="w+")
    saved = os.dup(2)
    try:
        os.dup2(tmp.fileno(), 2)
        yield tmp
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        tmp.seek(0)


_DECODE_ERR = re.compile(r"Could not find ref|Error constructing|corrupt|error while decoding|"
                         r"decode_slice_header|no frame", re.I)


def _summarise_decoder_errors(buf, from_frame):
    lines = [ln.rstrip() for ln in buf.read().splitlines()]
    errs = [ln for ln in lines if _DECODE_ERR.search(ln)]
    for ln in lines:                                  # re-emit everything; nothing is swallowed
        sys.stderr.write(ln + "\n")
    return len(errs), errs[:3]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--roi-json", required=True)
    ap.add_argument("--from-frame", type=int, default=0)
    ap.add_argument("--to-frame", type=int, default=0)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--model", default=os.environ.get("MODEL", "yolo11n.pt"))
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--anchor", default="both", choices=("foot", "center", "both"),
                    help="membership point for cabin occupancy; 'both' compares them on the SAME "
                         "detections in one pass (exact comparison, one decode)")
    ap.add_argument("--device", default=None,
                    help="cuda for the production TensorRT engine; omit for CPU")
    a = ap.parse_args()

    import cv2, counting
    roi = json.load(open(a.roi_json))
    zl, zc = roi.get("zone_landing"), roi.get("zone_cabin")
    if not (zl and zc):
        raise SystemExit("roi.json has no zone_landing/zone_cabin — occupancy needs the cabin zone")
    ctr = counting.ZoneCounter(zone_landing=zl, zone_cabin=zc)
    det = counting.YoloDetector(weights=a.model, conf=a.conf, tracker="bytetrack.yaml", device=a.device)

    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    i = 0; per = []; peak_first = peak_last = None; peak = 0

    # SEQUENTIAL DECODE, DELIBERATELY — DO NOT "OPTIMISE" THIS INTO A SEEK.
    # Reaching from_frame by decoding and discarding is slow and correct. cap.set(POS_FRAMES) on
    # HEVC lands mid-GOP and returns frames reconstructed against reference frames that were never
    # decoded, so the first frames after a seek are silently wrong — not missing, WRONG, which is
    # worse. This project has met keyframe-seek damage before; the cost of decoding forward is the
    # price of every analysed frame being real.
    with _decoder_stderr() as errbuf:
        while True:
            ok, fr = cap.read()
            if not ok: break
            i += 1
            if a.to_frame and i > a.to_frame: break
            if i < a.from_frame or (i % a.stride): continue
            dets = det.track(fr)
            # BOTH ANCHORS OFF THE SAME DETECTIONS. Running the probe twice would compare two
            # tracker states as well as two anchors; this compares only what is being tested.
            nf = len(ctr.cabin_ids(dets, anchor="foot"))
            nc = len(ctr.cabin_ids(dets, anchor="center"))
            n = nf if a.anchor in ("foot", "both") else nc
            per.append((i, nf, nc, len(dets)))
            if n > peak:
                peak, peak_first, peak_last = n, i, i
            elif n == peak:
                peak_last = i
    cap.release()
    n_err, err_head = _summarise_decoder_errors(errbuf, a.from_frame)
    if not per:
        raise SystemExit("no frames analysed in that range")
    ns = sorted(x[1] for x in per)
    q = lambda p: ns[int(p * (len(ns) - 1))]
    tot = sorted(x[3] for x in per)
    print(f"=== cabin occupancy, {os.path.basename(a.video)} f{a.from_frame}-{a.to_frame} "
          f"(stride {a.stride}, {len(per)} analysed frames) ===")
    # DECODE HEALTH FIRST. A run over corrupt frames must not be able to print a confident number
    # and be quoted; if the decoder complained, that is said before any occupancy figure.
    if n_err:
        print(f"  !! DECODER REPORTED {n_err} ERROR LINE(S) during this run — frames may be "
              f"reconstructed against references that were never decoded.")
        for ln in err_head:
            print(f"       {ln}")
        print(f"     ch30_peak.mp4 and every HLS-derived capture start mid-GOP and complain for "
              f"~50 frames; errors THERE are expected and harmless. Errors inside "
              f"f{a.from_frame}-{a.to_frame} are not — treat the numbers below as suspect.")
    else:
        print(f"  decoder: no error lines (sequential decode from frame 1, no seek)")
    span = "" if peak_first == peak_last else f"..{peak_last}"
    print(f"  PEAK cabin occupancy (MEASURED MINIMUM): {peak}  first at frame {peak_first}{span}")
    print(f"  cabin per-frame   p50 {q(.5)}  p90 {q(.9)}  max {ns[-1]}   "
          f"(anchor={a.anchor if a.anchor != 'both' else 'foot, headline'})")
    print(f"  all detections    p50 {tot[len(tot)//2]}  max {tot[-1]}   (whole frame, both zones + outside)")
    hist = collections.Counter(x[1] for x in per)
    print("  distribution:", " ".join(f"{k}:{v}" for k, v in sorted(hist.items())))

    # ---- THE PAIR TEST -------------------------------------------------------------------
    if a.anchor == "both":
        fs = sorted(x[1] for x in per); cs = sorted(x[2] for x in per)
        qq = lambda v, p: v[int(p * (len(v) - 1))]
        print()
        print(f"  {'anchor':>8} {'peak':>5} {'p50':>4} {'p90':>4} {'mean':>6}   distribution")
        for lab, v in (("foot", fs), ("center", cs)):
            h = collections.Counter(v)
            print(f"  {lab:>8} {max(v):>5} {qq(v,.5):>4} {qq(v,.9):>4} {sum(v)/len(v):>6.2f}   "
                  + " ".join(f"{k}:{n}" for k, n in sorted(h.items())))
        gain = max(cs) - max(fs)
        print(f"  center - foot at peak: {gain:+d}   (per-frame mean "
              f"{sum(cs)/len(cs) - sum(fs)/len(fs):+.2f})")
        # A centroid can only ADD members relative to a foot for the same box when the box bottom
        # falls outside and the middle inside. If center ever reports FEWER, the polygon geometry is
        # not what either anchor assumes and neither number should be trusted.
        worse = sum(1 for _f, nf, nc, _t in per if nc < nf)
        if worse:
            print(f"  !! center reported FEWER than foot on {worse}/{len(per)} frames — the cabin "
                  f"polygon does not sit where a body's middle is above its feet. Check the zone "
                  f"against a frame before reading either column.")
    sustained = sum(1 for _f, n, _c, _t in per if n >= max(1, peak - 1))
    print(f"  frames at >= peak-1 ({max(1, peak-1)}): {sustained}/{len(per)} "
          f"({100.0*sustained/len(per):.0f}%) — a peak seen on one frame is an artefact candidate")
    if peak == ns[0]:
        print(f"  NOTE: occupancy was CONSTANT at {peak} across every analysed frame, so "
              f"'first at frame {peak_first}' is just the first frame analysed — it carries no "
              f"information about where the peak occurred and must not be read as if it did.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
