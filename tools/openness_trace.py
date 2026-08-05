#!/usr/bin/env python3
"""Dump the per-frame door signal EXACTLY as the tracker sees it, around named OSD windows.

WHY. DECISION 2 (4e3b797) showed the tracker stamps close_start and close_full 0-2 frames apart on
closes that physically took 2.1-2.6s: it never observes the descent. Independent frame analysis of
the same mp4s shows a simple ROI bright-pixel fraction ramps smoothly over 50-65 frames on both
cameras, so the intermediate leaf positions ARE in the raw pixels. Something between the pixels and
the state machine is throwing them away. This finds where.

The signal path is FOUR stages, and "openness" is not one thing:

  1. gray ROI            -> door_edge_column()  -> (col, strength)      [gpu_door.py:63]
  2. (col, strength)     -> min_strength gate   -> frame kept or SKIPPED [DoorTracker.update]
  3. col                 -> rolling p10/p90     -> refs (lo, hi), span   [DoorTracker._refs]
  4. col, lo, hi         -> clip((col-lo)/(hi-lo)) -> openness in [0,1]   [DoorTracker.openness]

A cliff can be minted at ANY of them: a saturating edge metric (1), the leaf going unreadable
mid-descent so the tracker sees no frames at all (2), a collapsed reference span (3), or clipping
(4). The h2 baseline printed only `openness span=1.0` — which is stage-4 output and tells you
nothing about which stage flattened it.

So this logs all four per frame, plus a threshold-free continuous control (ROI bright-pixel
fraction, the metric already proven to ramp), and it replays the WHOLE video so the rolling refs
carry their true history before the window of interest.

USAGE
  python3 tools/openness_trace.py --video ~/dwrec/rec/ch30_full.mp4 --cam ch30 \\
      --roi 2,2,335,446 --osd-base 12:04:48 --windows 12:08:55,12:12:16 \\
      --frame-stride 2 --out /tmp/ch30_trace.csv
"""
import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = "2026-08-05"


def osd_to_epoch(hhmmss, day=DAY):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return dt.datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), h, m, s, tzinfo=IST).timestamp()


def epoch_to_osd(ep):
    return dt.datetime.fromtimestamp(ep, IST).strftime("%H:%M:%S")


def trace(video, roi, osd_base_ep, skip_before, stride, windows, half):
    """Replay the full video through the REPO's tracker, logging every frame in the windows.

    Returns (rows, emitted, diag). One row per frame that reached the tracker OR was rejected by it;
    `kept` says which. Nothing here changes tracker behaviour — the tracker is fed identically to
    doorwatch_replay.py, we only observe more.
    """
    import cv2
    import numpy as np
    import gpu_door

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    x, y, w, h = [int(round(v)) for v in roi]

    tr = gpu_door.DoorTracker()
    rows, emitted = [], []
    all_cols, all_strength = [], []
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        i += 1
        if stride > 1 and (i % stride):
            continue
        vt = (i - 1) / fps
        if vt < skip_before:
            continue
        t = osd_base_ep + vt

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sub = gray[y:y + h, x:x + w]
        col, strength = gpu_door.door_edge_column(sub)

        in_window = (not windows) or any(abs(t - c) <= half for c in windows)

        # --- stage 3/4 as the tracker will compute them, BEFORE this frame is appended.
        # (doorwatch_replay.py logs openness at this same point, so this matches the baseline.)
        lo_pre, hi_pre = tr._refs()
        o_pre = tr.openness(col) if col is not None else None

        # --- continuous control: does the RAW ROI ramp here? threshold-free (mean, std) plus a
        # bright-pixel fraction at fixed cuts. This is the metric independent frame analysis says
        # ramps over 50-65 frames; if it ramps in THIS roi on THIS frame set, the information is
        # present and any flatness downstream is self-inflicted.
        ctrl = {}
        if in_window and sub.size:
            f = sub.astype(np.float32)
            ctrl = {
                "roi_mean": float(f.mean()),
                "roi_std": float(f.std()),
                "bright_f_128": float((sub >= 128).mean()),
                "bright_f_160": float((sub >= 160).mean()),
                "bright_f_p": float((sub >= np.percentile(sub, 50)).mean()),
                # column-mean profile centroid: a continuous, non-argmax summary of WHERE the
                # bright mass sits horizontally. Moves smoothly as a leaf translates.
                "col_centroid": float(
                    (np.arange(sub.shape[1]) * f.mean(axis=0)).sum() / max(1e-6, f.mean(axis=0).sum())
                ),
            }

        state_before = tr.state
        kept = col is not None and strength >= tr.min_strength
        if col is not None:
            all_cols.append(col)
            all_strength.append(strength)

        cyc = tr.update(t, col, strength)

        lo_post, hi_post = tr._refs()
        if cyc:
            for c in (cyc if isinstance(cyc, list) else [cyc]):
                if isinstance(c, dict):
                    emitted.append((t, c))

        if in_window:
            rows.append({
                "frame": i, "vt": round(vt, 3), "osd": epoch_to_osd(t), "epoch": round(t, 3),
                "col": None if col is None else round(col, 2),
                "strength": round(strength, 4),
                "kept": int(kept),
                "ref_lo": None if lo_pre is None else round(lo_pre, 2),
                "ref_hi": None if hi_pre is None else round(hi_pre, 2),
                "ref_span": None if lo_pre is None else round(hi_pre - lo_pre, 2),
                "openness": None if o_pre is None else round(o_pre, 4),
                "state_before": state_before, "state_after": tr.state,
                "emitted": int(bool(cyc)),
                **{k: round(v, 4) for k, v in ctrl.items()},
            })

    cap.release()
    diag = {"fps": round(fps, 3), "size": f"{W}x{H}", "roi": (x, y, w, h),
            "frames_with_col": len(all_cols),
            "col_min": round(min(all_cols), 2) if all_cols else None,
            "col_max": round(max(all_cols), 2) if all_cols else None,
            "strength_min": round(min(all_strength), 3) if all_strength else None,
            "strength_max": round(max(all_strength), 3) if all_strength else None,
            "abandoned": tr.abandoned}
    return rows, emitted, diag


def sparkline(vals, lo=None, hi=None, width=None):
    """Tiny inline plot so the shape is visible in the terminal without a plotting dep."""
    ticks = " .:-=+*#%@"
    v = [x for x in vals if x is not None]
    if not v:
        return ""
    lo = min(v) if lo is None else lo
    hi = max(v) if hi is None else hi
    rng = (hi - lo) or 1.0
    out = []
    for x in vals:
        if x is None:
            out.append("_")
        else:
            k = int(round((x - lo) / rng * (len(ticks) - 1)))
            out.append(ticks[max(0, min(len(ticks) - 1, k))])
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--cam", required=True)
    ap.add_argument("--roi", required=True)
    ap.add_argument("--osd-base", required=True)
    ap.add_argument("--skip-before", type=float, default=0.0)
    ap.add_argument("--frame-stride", type=int, default=2)
    ap.add_argument("--windows", default="", help="comma-separated OSD HH:MM:SS centres; empty = whole run")
    ap.add_argument("--half-width", type=float, default=15.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    roi = [float(v) for v in a.roi.split(",")]
    wins = [osd_to_epoch(s) for s in a.windows.split(",") if s.strip()]
    rows, emitted, diag = trace(a.video, roi, osd_to_epoch(a.osd_base), a.skip_before,
                                a.frame_stride, wins, a.half_width)

    import gpu_door
    print(f"=== {a.cam}  tracker={gpu_door.TRACKER_LOGIC}  {os.path.basename(a.video)} "
          f"stride={a.frame_stride} ===")
    print(f"  {diag['size']} @ {diag['fps']}fps  roi={diag['roi']}  "
          f"raw edge col over whole run: min={diag['col_min']} max={diag['col_max']}  "
          f"strength {diag['strength_min']}..{diag['strength_max']}  abandoned={diag['abandoned']}")
    print(f"  frames with an edge: {diag['frames_with_col']}   emitted cycles: {len(emitted)}")

    for c in wins:
        sel = [r for r in rows if abs(r["epoch"] - c) <= a.half_width]
        if not sel:
            continue
        print(f"\n--- window centred {epoch_to_osd(c)}  ({len(sel)} frames, "
              f"stride {a.frame_stride} = {diag['fps']/a.frame_stride:.1f}fps) ---")
        print(f"  {'osd':>8} {'vt':>8} {'col':>7} {'str':>6} {'k':>1} "
              f"{'lo':>6} {'hi':>6} {'span':>6} {'openness':>8} {'brightf':>7} {'centroid':>8}  state")
        for r in sel:
            print(f"  {r['osd']:>8} {r['vt']:>8.2f} "
                  f"{'-' if r['col'] is None else format(r['col'], '7.2f')} "
                  f"{r['strength']:>6.3f} {r['kept']:>1} "
                  f"{'-' if r['ref_lo'] is None else format(r['ref_lo'], '6.1f')} "
                  f"{'-' if r['ref_hi'] is None else format(r['ref_hi'], '6.1f')} "
                  f"{'-' if r['ref_span'] is None else format(r['ref_span'], '6.1f')} "
                  f"{'-' if r['openness'] is None else format(r['openness'], '8.4f')} "
                  f"{r.get('bright_f_128', float('nan')):>7.4f} "
                  f"{r.get('col_centroid', float('nan')):>8.2f}  "
                  f"{r['state_before']}->{r['state_after']}"
                  + ("  <<< EMIT" if r["emitted"] else ""))
        print("\n  shape (each char = 1 analyzed frame):")
        print("    col      : " + sparkline([r["col"] for r in sel]))
        print("    openness : " + sparkline([r["openness"] for r in sel], 0.0, 1.0))
        print("    bright_f : " + sparkline([r.get("bright_f_128") for r in sel]))
        print("    centroid : " + sparkline([r.get("col_centroid") for r in sel]))

    if emitted:
        print("\n  cycles emitted during the whole run (osd, close_travel_s):")
        print("   " + ", ".join(
            f"{epoch_to_osd(t)}({'-' if c.get('close_travel_s') is None else format(c['close_travel_s'], '.2f')})"
            for t, c in emitted))

    if a.out and rows:
        keys = sorted({k for r in rows for k in r})
        with open(a.out, "w", newline="") as fh:
            wcsv = csv.DictWriter(fh, fieldnames=keys)
            wcsv.writeheader()
            wcsv.writerows(rows)
        print(f"\n  wrote {len(rows)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
