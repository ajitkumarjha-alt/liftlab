#!/usr/bin/env python3
"""A/B the DECODE, not the scene: does the worker's decode path produce different pixels than a
plain software decode of the same bytes?

WHY. On 2026-09-02 ~17:50 IST — the fleet restart after the NVIDIA driver reinstall (580.159.03 ->
580.173.02, CUDA 13.0, kernel 6.8.0-1066) — ch29's NCC scores fell ~0.06 across EVERY cell and
EVERY glyph with `shift` still [0,0]. Uniform loss with zero translation is not a moved camera and
not a changed template (`templates_hash` 260d4a0f… is byte-identical either side): it is softer
pixels arriving at the reader. ch29's lobby 'G' sat closest to a gate and went first — the tens
cell's blank NCC fell from ~0.95 to ~0.86, below `blank_strong`, so a junk '6' wins the empty cell
and "G" is emitted as "6G" -> invalid_label -> discarded.

This script decides WHERE the softening happens by decoding ONE segment four ways and scoring the
same panel crop from each with the SAME live templates:

    live       gpu_analyze.decode_segment()  — exactly what the worker does right now
    nvdec      ffmpeg -hwaccel cuda -c:v hevc_cuvid  (forced, even if USE_NVDEC=0)
    ffmpeg_sw  ffmpeg software HEVC, no hwaccel      — the reference decode
    pyav       PyAV / libavcodec CPU                 — the USE_NVDEC=0 fallback path

UPDATE 2026-09-08 — THIS PROBE NOW CONFIRMS, IT NO LONGER DISCOVERS. Two stream-copied ch29 clips
(2026-08-11 and 2026-09-07) were decoded on liftlab-cloud with OpenCV/FFmpeg 62.28.101 — no driver,
no cuvid — and each reproduced its own era's DB scores: G 0.908 / blank 0.927 for August, G 0.810 /
blank 0.856 for September. The degradation is already in the delivered HEVC, so the decode is NOT
the cause. Pixel stats on the same two clips show the September image is SHARPER (Laplacian
variance +21%, tens-cell contrast 199 -> 235) with the inter-stroke space 28% darker at unchanged
resolution, frame rate, bitrate and overall brightness — a contrast/gamma/edge-enhancement change
on the CAMERA or NVR.

So the expected outcome here is that ALL FOUR paths AGREE. That closes the GPU box out completely.
If they do NOT agree, the decode is contributing a second, independent regression on top of the
camera-side one, and both need fixing.

Either way the fix is NOT to loosen the blank gate: DOOR_BLANK_STRONG / DOOR_BLANK_LIT_MARGIN /
DOOR_MIN_SCORE are process-wide and would move ch16/ch27/ch30 with ch29.

Run on liftlab-gpu, next to gpu_analyze.py:
    cd /opt/liftlab-analysis
    sudo -E .venv/bin/python probe_decode_ab.py                    # pull the newest live segment
    sudo -E .venv/bin/python probe_decode_ab.py --seg /tmp/x.ts    # a segment you already have
    sudo -E .venv/bin/python probe_decode_ab.py --save /tmp/ab     # + dump the panel crops as PNG

`sudo -E` matters: the live USE_NVDEC lives in /etc/liftlab-gpu.env and an env-stripping sudo would
have this script probe a DIFFERENT configuration than the one that is failing.
"""
import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import time

import numpy as np

# ── the reader and its geometry, exactly as the worker loads them ──────────────────────────────
import gpu_door as gd

APPDIR = os.path.dirname(os.path.abspath(__file__))
LIVE_THASH = "260d4a0f67ff058743c9f8aeb6c25e9500c07236284cd33c1ffa0b507f21e28c"


def _env_report():
    """What is actually installed. Printed FIRST and unconditionally: the whole hypothesis is that
    one of these versions moved on 2026-09-02, and a probe that reports scores without reporting the
    configuration they were measured under is not evidence."""
    print("── environment " + "─" * 64)
    print(f"  USE_NVDEC         = {os.environ.get('USE_NVDEC', '(unset -> 0, CPU PyAV path)')}")
    for label, argv in (("ffmpeg", ["ffmpeg", "-version"]),
                        ("nvidia-smi", ["nvidia-smi",
                                        "--query-gpu=driver_version,name", "--format=csv,noheader"])):
        try:
            out = subprocess.run(argv, capture_output=True, timeout=20).stdout.decode("utf-8", "ignore")
            print(f"  {label:17} = {out.strip().splitlines()[0] if out.strip() else '(no output)'}")
        except Exception as e:
            print(f"  {label:17} = UNAVAILABLE ({type(e).__name__})")
    try:
        dec = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"],
                             capture_output=True, timeout=20).stdout.decode("utf-8", "ignore")
        hev = [l.strip() for l in dec.splitlines() if "hevc" in l.lower()]
        print(f"  hevc decoders     = {', '.join(l.split()[1] for l in hev) or '(none)'}")
    except Exception as e:
        print(f"  hevc decoders     = UNAVAILABLE ({type(e).__name__})")
    try:
        import av
        print(f"  PyAV              = {av.__version__}")
        for k, v in sorted(av.library_versions.items()):
            print(f"    {k:15} = {v}")
    except Exception as e:
        print(f"  PyAV              = UNAVAILABLE ({type(e).__name__}: {e})")
    try:
        import cv2
        print(f"  cv2               = {cv2.__version__}")
    except Exception as e:
        print(f"  cv2               = UNAVAILABLE ({type(e).__name__})")
    print(f"  kernel            = {os.uname().release}")


# ── the four decode paths ──────────────────────────────────────────────────────────────────────
def _wh_from_header(data):
    import av
    c = av.open(io.BytesIO(data))
    vs = c.streams.video[0]
    wh = (vs.width, vs.height)
    c.close()
    return wh


def _raw_to_frames(buf, W, H):
    fsz = W * H * 3
    return [np.frombuffer(buf[i:i + fsz], np.uint8).reshape(H, W, 3)
            for i in range(0, len(buf) - fsz + 1, fsz)]


def dec_nvdec(data, wh):
    """VERBATIM the argv in gpu_analyze._decode_nvdec — including `-hwaccel cuda` alongside
    `-c:v hevc_cuvid`, which is the combination whose NV12->BGR24 conversion runs in the driver's
    video post-processor and is therefore the one a driver reinstall can change."""
    p = subprocess.run(["ffmpeg", "-hwaccel", "cuda", "-c:v", "hevc_cuvid", "-i", "pipe:0",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                       input=data, capture_output=True, timeout=120)
    if p.returncode != 0 or not p.stdout:
        err = p.stderr.decode("utf-8", "ignore").strip().splitlines()
        raise RuntimeError(f"rc={p.returncode}: {err[-1] if err else 'no stdout'}")
    return _raw_to_frames(p.stdout, *wh)


def dec_ffmpeg_sw(data, wh):
    """The REFERENCE: same container, same pixel format, software HEVC. No CUDA anywhere near it."""
    p = subprocess.run(["ffmpeg", "-c:v", "hevc", "-i", "pipe:0",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                       input=data, capture_output=True, timeout=180)
    if p.returncode != 0 or not p.stdout:
        err = p.stderr.decode("utf-8", "ignore").strip().splitlines()
        raise RuntimeError(f"rc={p.returncode}: {err[-1] if err else 'no stdout'}")
    return _raw_to_frames(p.stdout, *wh)


def dec_pyav(data, wh):
    """The USE_NVDEC=0 fallback, i.e. what the worker runs if NVDEC is off or failing. Note this is
    still libavcodec + libswscale: a package bump moves this path too, so it is NOT a control."""
    import av
    frames = []
    c = av.open(io.BytesIO(data))
    for f in c.decode(video=0):
        frames.append(f.to_ndarray(format="bgr24"))
    c.close()
    return frames


def dec_live(data, wh):
    """gpu_analyze's OWN decode_segment — whatever USE_NVDEC currently resolves to. This is the only
    path whose result is the thing actually feeding the reader in production."""
    import gpu_analyze as ga
    frames, _rel = ga.decode_segment(data)
    return frames


# ── scoring ────────────────────────────────────────────────────────────────────────────────────
def load_geometry(roi_path, panel_arg, cells_arg):
    """Geometry from ch29_roi.json unless overridden. PANEL_ROIS/DIGIT_CELLS in the live env win if
    present — the probe must score the cells the worker scores, not the ones the file remembers."""
    panel = cells = arrow = None
    if os.path.exists(roi_path):
        j = json.load(open(roi_path))
        panel = tuple(j["panel_rois"][0])
        cells = [tuple(c) for c in j["cells"]["digit_cells"]]
        arrow = tuple(j["cells"]["arrow_cell"])
    if os.environ.get("PANEL_ROIS"):
        panel = tuple(int(v) for v in os.environ["PANEL_ROIS"].split(";")[0].split(","))
    if os.environ.get("DIGIT_CELLS"):
        cells = [tuple(int(v) for v in p.split(",")) for p in os.environ["DIGIT_CELLS"].split(";") if p]
    if os.environ.get("ARROW_CELL"):
        arrow = tuple(int(v) for v in os.environ["ARROW_CELL"].split(";")[0].split(","))
    if panel_arg:
        panel = tuple(int(v) for v in panel_arg.split(","))
    if cells_arg:
        cells = [tuple(int(v) for v in p.split(",")) for p in cells_arg.split(";") if p]
    if not (panel and cells and arrow):
        raise SystemExit("no panel/cells geometry — pass --panel/--cells or run beside ch29_roi.json")
    return panel, cells, arrow


def score_frames(frames, reader, panel, stride):
    """-> per-decode aggregate. Reports the two numbers the incident turns on: the winning glyph's
    NCC in the units cell, and the per-cell BLANK NCC of the tens cell (cell 1) — the one that
    crossed blank_strong. Plus the tens-cell CONTRAST, because the gate it crossed is only armed
    when the cell reads as LIT (contrast >= lit_range)."""
    import cv2
    out = {"n": 0, "glyph": [], "blank1": [], "contrast1": [], "sharp": [],
           "g_units": [], "lobby": 0, "kept": 0, "blocked": 0, "floors": {}}
    for i, fr in enumerate(frames):
        if i % stride:
            continue
        pg = cv2.cvtColor(gd.crop(fr, panel), cv2.COLOR_BGR2GRAY)
        if pg.size == 0:
            continue
        out["n"] += 1
        out["sharp"].append(float(cv2.Laplacian(pg, cv2.CV_64F).var()))
        r = reader.read_panel(pg)
        key = f"{r['floor']}/{r['status']}"
        out["floors"][key] = out["floors"].get(key, 0) + 1
        dbg = r.get("cells") or []
        if len(dbg) < 3:
            continue
        units, tens = dbg[2], dbg[1]
        c1 = gd.crop(pg, reader.digit_cells[1])
        if c1.size:
            out["contrast1"].append(int(c1.max()) - int(c1.min()))
        b = reader._blank_at_zero(pg, reader.digit_cells[1], 1)
        if b is not None:
            out["blank1"].append(float(b))
        if units and units[0] and units[0][0] not in ("blank", "flat") and units[0][1] is not None:
            out["glyph"].append(float(units[0][1]))
        if not (units and units[0] and units[0][0] == "G"):
            continue
        out["lobby"] += 1
        out["g_units"].append(float(units[0][1]))
        if tens and tens[0] and tens[0][0] in ("blank", "flat"):
            out["kept"] += 1
        else:
            out["blocked"] += 1
    return out


def _m(x):
    return round(statistics.mean(x), 3) if x else None


def _p(x, q):
    return round(sorted(x)[int(q * (len(x) - 1))], 3) if x else None


def main():
    ap = argparse.ArgumentParser(description="A/B one HEVC segment across decode paths")
    ap.add_argument("--seg", default=None, help="path to a .ts segment (default: pull the newest live one)")
    ap.add_argument("--roi", default=os.path.join(APPDIR, "ch29_roi.json"))
    ap.add_argument("--panel", default=None, help="x,y,w,h override (e.g. 124,116,51,92)")
    ap.add_argument("--cells", default=None, help="x,y,w,h;... digit cells override")
    ap.add_argument("--templates", default=os.path.join(APPDIR, "ch29_templates.npz"))
    ap.add_argument("--stride", type=int, default=2, help="score every Nth decoded frame")
    ap.add_argument("--save", default=None, help="directory to dump the panel crop of frame 0 per path")
    args = ap.parse_args()

    _env_report()

    # ── the segment ────────────────────────────────────────────────────────────────────────────
    if args.seg:
        data = open(args.seg, "rb").read()
        src = args.seg
    else:
        import gpu_analyze as ga
        names, seq, ok = ga.playlist_segments()
        if not ok or not names:
            raise SystemExit("could not fetch the live playlist — pass --seg instead")
        src = f"{ga.BASE}/{names[-1]}"
        data = ga.http_get(src)
    print(f"\n── segment {'─' * 66}\n  {src}\n  {len(data)} bytes")

    # ── templates: the LIVE ones, or the run is meaningless ────────────────────────────────────
    z = np.load(args.templates)
    tpl = {k: z[k] for k in z.files}
    th = gd.templates_hash(tpl)
    print(f"  templates_hash    = {th[:32]}…  ({'LIVE SET' if th == LIVE_THASH else 'NOT the set the rows were read with'})")
    panel, cells, arrow = load_geometry(args.roi, args.panel, args.cells)
    reader = gd.FloorReader(tpl, cells, arrow)
    print(f"  panel={panel}  cells={cells}  arrow={arrow}")
    print(f"  gates: lit_range={reader.lit_range} blank_strong={reader.blank_strong} "
          f"blank_lit_margin={reader.blank_lit_margin} min_score={reader.min_score} "
          f"margin_min={reader.margin_min}")

    try:
        wh = _wh_from_header(data)
    except Exception as e:
        raise SystemExit(f"cannot read the segment header ({type(e).__name__}: {e})")
    print(f"  header WxH        = {wh[0]}x{wh[1]}")

    # ── decode every way, score every way ──────────────────────────────────────────────────────
    paths = [("live", dec_live), ("nvdec", dec_nvdec), ("ffmpeg_sw", dec_ffmpeg_sw), ("pyav", dec_pyav)]
    got, scored = {}, {}
    print("\n── decode " + "─" * 69)
    for name, fn in paths:
        t0 = time.time()
        try:
            fr = fn(data, wh)
            if not fr:
                raise RuntimeError("zero frames")
            got[name] = fr
            print(f"  {name:10} {len(fr):4d} frames  {fr[0].shape[1]}x{fr[0].shape[0]}  {1000*(time.time()-t0):.0f}ms")
        except Exception as e:
            print(f"  {name:10} FAILED — {type(e).__name__}: {str(e)[:120]}")
    if not got:
        raise SystemExit("every decode path failed — nothing to compare")

    print("\n── scores (same templates, same cells, same crop) " + "─" * 30)
    hdr = (f"  {'path':10} {'n':>5} {'units top1':>10} {'blank_1':>8} {'contrast_1':>10} "
           f"{'sharp':>8} {'lobby':>6} {'G kept':>7} {'blocked':>8}")
    print(hdr)
    for name in [n for n, _ in paths if n in got]:
        s = score_frames(got[name], reader, panel, args.stride)
        scored[name] = s
        print(f"  {name:10} {s['n']:5d} {str(_m(s['glyph'])):>10} {str(_m(s['blank1'])):>8} "
              f"{str(_m(s['contrast1'])):>10} {str(_m(s['sharp'])):>8} {s['lobby']:6d} "
              f"{s['kept']:7d} {s['blocked']:8d}")

    # ── pixels, not just scores: is the DIFFERENCE in the image or in the reading of it? ───────
    if len(got) > 1:
        print("\n── panel-crop pixel diff vs ffmpeg_sw (the reference decode) " + "─" * 18)
        ref_name = "ffmpeg_sw" if "ffmpeg_sw" in got else sorted(got)[0]
        ref = got[ref_name]
        for name, fr in got.items():
            if name == ref_name:
                continue
            n = min(len(fr), len(ref))
            if n == 0:
                continue
            # Frame i of one decode is frame i of the other ONLY if both emitted every frame; a
            # count mismatch means they are not aligned and the diff below is meaningless. Say so.
            aligned = len(fr) == len(ref)
            d = [np.abs(gd.crop(fr[i], panel).astype(np.int16)
                        - gd.crop(ref[i], panel).astype(np.int16)) for i in range(0, n, args.stride)]
            if not d:
                continue
            mean_ad = float(np.mean([x.mean() for x in d]))
            max_ad = int(max(x.max() for x in d))
            print(f"  {name:10} vs {ref_name}: mean|diff|={mean_ad:.3f}  max|diff|={max_ad:3d}  "
                  f"frames {len(fr)} vs {len(ref)}"
                  + ("" if aligned else "  ** FRAME COUNTS DIFFER — diff is not frame-aligned **"))

    if args.save:
        import cv2
        os.makedirs(args.save, exist_ok=True)
        for name, fr in got.items():
            cv2.imwrite(os.path.join(args.save, f"panel_{name}.png"), gd.crop(fr[0], panel))
        print(f"\n  panel crops written to {args.save}/panel_<path>.png")

    # ── the verdict, stated in the terms the incident is about ─────────────────────────────────
    print("\n── verdict " + "─" * 68)
    print("  August baseline (ch29, from gw_door_event.candidates): units top1 ~0.87, "
          "tens blank_1 at the lobby ~0.95")
    print("  September (Sep 3-7):                                   units top1 ~0.80, "
          "tens blank_1 at the lobby ~0.86")
    sw, live = scored.get("ffmpeg_sw"), scored.get("live")
    if sw and live:
        gs, gl = _m(sw["glyph"]), _m(live["glyph"])
        bs, bl = _m(sw["blank1"]), _m(live["blank1"])
        if gs is not None and gl is not None:
            delta = gs - gl
            bdelta = (f"{(bs - bl):+.3f}" if bs is not None and bl is not None else "n/a")
            print(f"  ffmpeg_sw - live: units top1 {gs} - {gl} = {delta:+.3f}   "
                  f"blank_1 {bs} - {bl} = {bdelta}")
            if delta >= 0.03:
                print("  => THE DECODE IS THE FAULT. Software decode of the SAME bytes scores higher "
                      "than the live path.\n     Restore the August decode output; do NOT loosen "
                      "DOOR_BLANK_STRONG/DOOR_BLANK_LIT_MARGIN (process-wide).")
            elif abs(delta) < 0.01:
                print("  => THE DECODE IS EXONERATED on this segment. Both paths agree, so the "
                      "softening is UPSTREAM\n     of this box — the relay's ffmpeg re-encode, or "
                      "the camera. Compare a pre-Sep-2 segment next.")
            else:
                print(f"  => INCONCLUSIVE on one segment (delta {delta:+.3f}). Re-run over several, "
                      "and over a\n     pre-2026-09-02 segment if the relay still has one.")
    else:
        print("  live and/or ffmpeg_sw did not decode — cannot compare. Fix that first; the "
              "comparison IS the test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
