#!/usr/bin/env python3
"""Column-wise closedness — a door-POSITION proxy that does not saturate.

WHY THIS EXISTS. ed42761 established the split that blocks h3: `ncc_top` is an excellent door STATE
signal (TEST A, AUC 1.000 / LOO 99.1% on ch27) and is structurally unable to time a close. The leaf
covers a band above head height before it finishes covering the doorway below, so `ncc_top` reaches
its closed value early — ch30's truth close 20459->20511 has it crossing at frame 20420, 39 frames
BEFORE the close starts — and its remaining motion is asymptotic. Appearance similarity saturates.
No affine correction recovers a signal that ranks its own measurements backwards (Pearson -0.57 to
-0.83 against the hand travels).

WHAT IS DIFFERENT HERE. This measures WHERE THE LEAF EDGE IS, not how closed the band LOOKS.

  Per frame, reduce the same top band to a column-mean gray profile: one value per x column. Hold
  two reference profiles, OPEN_ref and CLOSED_ref, both running medians over the frames the state
  signal calls confidently open / confidently closed. Then

      position(t) = fraction of columns whose value is nearer CLOSED_ref than OPEN_ref

  A column the leaf has not reached yet still matches OPEN_ref, however closed the rest of the band
  looks, so the count keeps climbing for as long as the leaf keeps moving. That is the property
  `ncc_top` lacks. It is also not a guess: this is the method that produced the ground-truth travels
  on this corpus, so it is a candidate already known to ramp under hand inspection.

WHAT THIS MODULE DOES NOT DO. It does not decide door state, detect closes, or measure travel. It
emits a per-frame signal and diagnostics. `travel_criterion.py` scores it, and only a pass admits it
to a tracker — the same validate-first order as door_signal_v2.py.

The pass is single-decode: top-band crops and column profiles are collected in one read, the closed
template is built from the TRAIN split of the door-closed windows afterwards, and `ncc_top` is
recomputed from the stored crops. That reproduces door_signal_v2's `ncc_top` exactly (verified
against its dump) while costing one pass instead of two.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_io
from door_signal_v2 import TEMPLATE_WH, TOP_BAND_Y, closed_window_frames

TOP_WH = (TEMPLATE_WH[0], max(8, TEMPLATE_WH[1] // 4))     # (96, 32) — as door_signal_v2 builds it

BAND_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "band_coords.json")


def travel_band(cam):
    """Rows the leaf actually sweeps on THIS camera, from tools/band_coords.json (band_placement.py).

    STATE AND TRAVEL USE DIFFERENT ROWS, DELIBERATELY. `ncc_top` stays on the incumbent TOP_BAND_Y
    for both cameras — that is the configuration TEST A passes on (AUC 1.000 / LOO 99.1% on ch27),
    and a passing configuration is not moved to satisfy a travel fix. The travel band is chosen by
    transition energy instead, because ncc_top's defect is that it SATURATES: the leaf covers a strip
    above head height before it finishes covering the doorway below, so the top band stops moving
    part-way through the event being timed. On ch30 these are different regions (state y15-85, travel
    y242-312); on ch27 the derivation lands on the incumbent and the two coincide.

    Falls back to TOP_BAND_Y when the JSON is absent, so this module still runs standalone.
    """
    if not os.path.exists(BAND_JSON):
        return tuple(TOP_BAND_Y), "fallback (band_coords.json absent)"
    b = json.load(open(BAND_JSON))
    if cam not in b:
        return tuple(TOP_BAND_Y), f"fallback ({cam} not in band_coords.json)"
    return tuple(b[cam]["band_y"]), "band_coords.json"

# Plateau band of the STATE signal used to select reference frames. A frame is "confidently open" /
# "confidently closed" only in the outer 15% of the signal's own p10..p90 span, so the moving part of
# every close is excluded from both references by construction.
CONF_LO, CONF_HI = 0.15, 0.85

# Running-median reference, two modes.
#
#   nearest (default) — the median of the REF_K confident frames NEAREST IN TIME. This is the mode
#   that works, and the reason is a property of the corpus rather than a tuning preference: the car
#   changes floor every 20-60s and ch27's lobby changes appearance with it (bright carpet on 43,
#   dark marble elsewhere, per 63064cb). A reference must therefore track the STOP, not the run.
#
#   window — the median over a fixed +/-REF_WIN frame neighbourhood. Kept because the first run of
#   this tool used it at REF_WIN=3000 (+/-2 min) and that is why ch27 read a close as reaching only
#   0.64: OPEN_ref was a median blended across several floors, so columns the leaf had already
#   covered still scored nearer the blend than the leaf. The failure was the window length, not the
#   method — see README_DOORWATCH. Retained so that claim stays reproducible.
#
# Both interpolate linearly between anchors every REF_STEP frames, so refs never step mid-close.
REF_WIN, REF_STEP, REF_MIN = 3000, 250, 20
REF_K = 60                # 'nearest' mode: confident frames per reference median

SMOOTH_K = 5              # the ~5-frame kernel the position signal is smoothed with before edge-finding


def smooth_np(v, k):
    if k <= 1:
        return np.asarray(v, dtype=np.float64)
    ker = np.ones(k) / k
    pad = k // 2
    return np.convolve(np.pad(np.asarray(v, dtype=np.float64), pad, mode="edge"), ker, "valid")[:len(v)]


def scan(cam, stride=1, limit_frames=None):
    """One decode pass -> (frames, STATE-band crops, TRAVEL-band column profiles).

    Two bands come out of the same read: the state band feeds `ncc_top` unchanged, the travel band
    feeds the column profiles the position proxy works on. See travel_band() for why they differ.
    """
    import cv2
    spec = truth_io.CORPUS[cam]
    path = truth_io.video_path(cam)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {path}")
    x, _, w, _ = spec["roi"]
    sy0, sy1 = TOP_BAND_Y                       # state band — unchanged, TEST A passes here
    (ty0, ty1), src = travel_band(cam)          # travel band — per-camera, transition-energy derived
    frames, crops, profs = [], [], []
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if stride > 1 and (i % stride):
            continue
        if limit_frames and i > limit_frames:
            break
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        frames.append(i)
        crops.append(cv2.resize(g[sy0:sy1, x:x + w], TOP_WH))
        profs.append(g[ty0:ty1, x:x + w].mean(axis=0).astype(np.float32))
    cap.release()
    if not frames:
        raise SystemExit(f"{cam}: no frames read from {path}")
    print(f"  bands: state y{sy0}-{sy1} (fixed), travel y{ty0}-{ty1} (from {src})")
    return np.array(frames), np.stack(crops), np.stack(profs)


def ncc_against(crops, tpl, chunk=4000):
    """Vectorised zero-mean NCC of every crop against one template. Chunked: the full float32 stack
    of ch27's 70260 crops would be 863MB."""
    T = tpl.astype(np.float32).ravel()
    T = T - T.mean()
    tn = float(np.sqrt((T * T).sum()))
    out = np.zeros(len(crops), dtype=np.float64)
    for a in range(0, len(crops), chunk):
        C = crops[a:a + chunk].astype(np.float32).reshape(min(chunk, len(crops) - a), -1)
        C -= C.mean(axis=1, keepdims=True)
        n = np.sqrt((C * C).sum(axis=1))
        n[n < 1e-6] = np.inf
        out[a:a + len(C)] = (C @ T) / (n * tn)
    return out


def build_top_template(cam, frames, crops):
    """Median top-band crop over the TRAIN split of the door-closed windows — the same construction,
    the same split, and therefore the same template as door_signal_v2.build_templates."""
    pos = {f: k for k, f in enumerate(frames)}
    idx = [pos[f] for f in closed_window_frames(cam, "train") if f in pos]
    if len(idx) < 3:
        raise SystemExit(f"{cam}: only {len(idx)} train-split closed frames resolved")
    return np.median(crops[idx], axis=0).astype(np.uint8), len(idx)


def pctl(v, q):
    return float(np.percentile(np.asarray(v, dtype=np.float64), q))


def confident_masks(state):
    """Boolean masks of confidently-open / confidently-closed frames, from the state signal alone.

    Levels come from the signal's own p10/p90 plateaus rather than from any label, so the references
    below stay independent of the ground truth the travel test scores against.
    """
    CL, OP = pctl(state, 90), pctl(state, 10)
    rng = CL - OP
    closed = state >= OP + CONF_HI * rng
    open_ = state <= OP + CONF_LO * rng
    return open_, closed, OP, CL


def running_refs(profs, mask, mode="nearest", win=REF_WIN, step=REF_STEP, min_n=REF_MIN, k=REF_K):
    """Running median of the column profile over frames in `mask`, per column.

    Anchors every `step` samples; anchors with fewer than `min_n` contributing frames fall back to
    the global median over the mask, so a stretch of the run with no confident frames of one class
    borrows the run-wide reference instead of inventing one. See the REF_* notes above for why
    `nearest` is the default.
    """
    n, ncol = profs.shape
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        raise SystemExit("no frames in reference mask — the state signal has no confident plateau")
    glob = np.median(profs[idx], axis=0)
    anchors = list(range(0, n, step)) + [n - 1]
    vals, fell_back = [], 0
    for a in anchors:
        if mode == "nearest":
            j = int(np.searchsorted(idx, a))
            lo = max(0, min(j - k // 2, len(idx) - k))
            sel = idx[lo:lo + k]
        else:
            sel = idx[(idx >= a - win) & (idx <= a + win)]
        if len(sel) < min_n:
            vals.append(glob)
            fell_back += 1
        else:
            vals.append(np.median(profs[sel], axis=0))
    vals = np.stack(vals)
    out = np.empty((n, ncol), dtype=np.float32)
    for c in range(ncol):
        out[:, c] = np.interp(np.arange(n), anchors, vals[:, c])
    return out, fell_back, len(anchors)


# ---- PER-EVENT REFERENCES -----------------------------------------------------------------
# The reference pair is rebuilt from scratch for every candidate event, from frames immediately
# either side of that event, and is used for that event only.
#
# WHY A RUNNING REFERENCE IS WRONG HERE. ch27's car changes floor every 20-60s and the lobby behind
# the doorway changes appearance with it — bright carpet on 43, dark marble elsewhere (63064cb).
# band_placement.py measured the consequence directly: on ch27, rows move in the OPPOSITE direction
# between closes on a third of them (sign-consistency 0.636, against ch30's 1.000). A reference
# median that spans several stops is therefore not merely stale, it is a blend of two opposite
# polarities, and columns the leaf has already covered can score nearer the blend than the leaf.
# That is the mechanism behind ch27's wandering ramp foot (sd 2.26s, 668cf46).
#
# CIRCULARITY, STATED. This is the construction that produced the ground-truth trajectories, so a
# TEST B pass on it is partly self-confirmation and the README caveat applies unchanged. Two things
# bound it: the estimator is handed ONE instant (the anchor) exactly as travel_criterion.py already
# specifies, and the reference offsets below are fixed constants, not fitted per close. It is not
# eliminated, and no result from this module should be quoted as if it were.
REF_BACK_GAP, REF_BACK_N = 200, 50     # open ref:   [anchor-GAP-N, anchor-GAP)  — before the close
REF_FWD_GAP, REF_FWD_N = 25, 50        # closed ref: (anchor+GAP, anchor+GAP+N]  — after it settles


def event_refs(frames, profs, anchor, back_gap=REF_BACK_GAP, back_n=REF_BACK_N,
               fwd_gap=REF_FWD_GAP, fwd_n=REF_FWD_N, min_n=10):
    """(open_ref, closed_ref, diag) for ONE event, as local medians either side of its anchor.

    Returns (None, None, why) when either side is too thin — the edges of the file, or two closes
    packed closer than the reference offsets. A thin reference is not silently padded from elsewhere
    in the run; that would reintroduce the blending this exists to remove.
    """
    pos = np.searchsorted(frames, anchor)
    o_lo, o_hi = anchor - back_gap - back_n, anchor - back_gap
    c_lo, c_hi = anchor + fwd_gap, anchor + fwd_gap + fwd_n
    om = (frames >= o_lo) & (frames < o_hi)
    cm = (frames > c_lo) & (frames <= c_hi)
    if int(om.sum()) < min_n or int(cm.sum()) < min_n:
        return None, None, f"thin refs (open {int(om.sum())}, closed {int(cm.sum())})"
    open_ref = np.median(profs[om], axis=0)
    closed_ref = np.median(profs[cm], axis=0)
    sep = float(np.abs(open_ref - closed_ref).mean())
    return open_ref, closed_ref, f"sep={sep:.1f}gray n_open={int(om.sum())} n_closed={int(cm.sum())} pos={pos}"


def event_anchors(cam):
    """Anchors to measure: every truth close's end_f, whatever its status.

    Excluded rows are anchored too — criterion (c) is scored on exactly those, and a close the
    travel test may not score is still a close the estimator must not silently mis-handle.
    """
    return [(t["end_f"], t) for t in truth_io.load_truth(cam)]


def position_from_refs(profs, open_ref, closed_ref):
    """fraction of columns nearer CLOSED_ref than OPEN_ref. 0 = fully open, 1 = fully closed."""
    d_closed = np.abs(profs - closed_ref)
    d_open = np.abs(profs - open_ref)
    return (d_closed < d_open).mean(axis=1)


def emit_event_mode(cam, frames, profs, state, st_s, fps, a):
    """Per-event position: rebuild the reference pair around each anchor and measure only its window.

    The output schema is the one travel_criterion.py already reads (frame, vt, position, ncc_top),
    with an added `event` column. Windows do not overlap on this corpus — the closest pair of truth
    closes is 672 frames apart against a 12s (~300 frame) window — so a frame appears at most once
    and the flat file stays unambiguous. That is asserted below, not assumed.
    """
    back = int(round(a.win_back_s * fps))
    fwd = int(round(a.win_fwd_s * fps))
    anchors = event_anchors(cam)
    print(f"  ref mode: EVENT — refs rebuilt per anchor from "
          f"[a-{REF_BACK_GAP}-{REF_BACK_N}, a-{REF_BACK_GAP}) open / "
          f"(a+{REF_FWD_GAP}, a+{REF_FWD_GAP}+{REF_FWD_N}] closed")
    print(f"  {len(anchors)} anchors from truth closes (all statuses)")

    rows, seen, skipped = [], {}, []
    for anchor, t in anchors:
        open_ref, closed_ref, why = event_refs(frames, profs, anchor)
        if open_ref is None:
            skipped.append((anchor, t["status"], why))
            print(f"    SKIP anchor f{anchor} ({t['status']}): {why}")
            continue
        m = (frames >= anchor - back) & (frames <= anchor + fwd)
        idx = np.flatnonzero(m)
        if len(idx) < 6:
            skipped.append((anchor, t["status"], "window too short"))
            continue
        raw = position_from_refs(profs[idx], open_ref, closed_ref)
        pos = smooth_np(raw, a.smooth)
        for k, j in enumerate(idx):
            f = int(frames[j])
            if f in seen:
                raise SystemExit(
                    f"{cam}: frame {f} falls in two event windows (anchors {seen[f]} and {anchor}). "
                    f"The flat per-event file cannot represent that — narrow --win-back-s/--win-fwd-s.")
            seen[f] = anchor
            rows.append({"event": anchor, "frame": f, "vt": round((f - 1) / fps, 4),
                         "epoch": round((f - 1) / fps, 4),
                         "osd": truth_io.frame_to_osd(cam, f - 1),
                         "ncc_top": round(float(state[j]), 5),
                         "position": round(float(pos[k]), 5),
                         "position_raw": round(float(raw[k]), 5)})
        span = float(pos.max() - pos.min())
        print(f"    f{anchor:>6} {t['status']:<26} {why}  span={span:.3f}")

    with open(a.out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=["event", "frame", "vt", "epoch", "osd", "ncc_top",
                                            "position", "position_raw"])
        wr.writeheader(); wr.writerows(rows)
    print(f"  wrote {len(rows)} rows across {len(set(r['event'] for r in rows))} events -> {a.out}")
    if skipped:
        print(f"  SKIPPED {len(skipped)} anchors: " +
              ", ".join(f"f{f}({s})" for f, s, _ in skipped))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", required=True, choices=sorted(truth_io.CORPUS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--frame-stride", type=int, default=1)
    ap.add_argument("--limit-frames", type=int, default=None)
    ap.add_argument("--smooth", type=int, default=SMOOTH_K)
    ap.add_argument("--ref-mode", choices=("nearest", "window", "event"), default="nearest")
    ap.add_argument("--win-back-s", type=float, default=8.0,
                    help="event mode: emitted window before the anchor (matches travel_criterion)")
    ap.add_argument("--win-fwd-s", type=float, default=4.0)
    ap.add_argument("--ref-k", type=int, default=REF_K)
    ap.add_argument("--ref-win", type=int, default=REF_WIN)
    ap.add_argument("--cache", default=None,
                    help=".npz of (frames, profiles, state). Written if absent, reused if present — "
                         "so a reference change costs seconds instead of a 10-minute re-decode.")
    a = ap.parse_args()

    cam = a.cam
    fps = truth_io.CORPUS[cam]["fps"]
    print(f"=== position proxy (column-wise closedness): {cam} ===")
    if a.cache and os.path.exists(a.cache):
        z = np.load(a.cache)
        frames, profs, state = z["frames"], z["profs"], z["state"]
        print(f"  reusing cached scan: {len(frames)} frames, {profs.shape[1]} columns "
              f"({a.cache})")
    else:
        frames, crops, profs = scan(cam, a.frame_stride, a.limit_frames)
        print(f"  decoded {len(frames)} frames (stride {a.frame_stride}), "
              f"top band y{TOP_BAND_Y[0]}-{TOP_BAND_Y[1]}, {profs.shape[1]} columns")
        tpl, n_tpl = build_top_template(cam, frames, crops)
        state = ncc_against(crops, tpl)
        print(f"  ncc_top template from {n_tpl} train-split closed frames")
        del crops
        if a.cache:
            np.savez(a.cache, frames=frames, profs=profs, state=state)

    st_s = smooth_np(state, 3)                      # same 3-kernel door_signal_validate uses
    open_m, closed_m, OP, CL = confident_masks(st_s)
    print(f"  state plateaus: open={OP:.4f} closed={CL:.4f}; "
          f"confident open {int(open_m.sum())} frames, confident closed {int(closed_m.sum())}")

    if a.ref_mode == "event":
        return emit_event_mode(cam, frames, profs, state, st_s, fps, a)

    rk = dict(mode=a.ref_mode, k=a.ref_k, win=a.ref_win)
    open_ref, fb_o, na = running_refs(profs, open_m, **rk)
    closed_ref, fb_c, _ = running_refs(profs, closed_m, **rk)
    sep = np.abs(open_ref - closed_ref).mean(axis=0)
    print(f"  refs: mode={a.ref_mode} "
          f"({'k=%d nearest confident frames' % a.ref_k if a.ref_mode == 'nearest' else '+/-%d frames' % a.ref_win})"
          f", {na} anchors, global fallback on {fb_o} open / {fb_c} closed")
    print(f"  per-column |OPEN_ref - CLOSED_ref|: median {np.median(sep):.2f} gray, "
          f"{int((sep >= 2).sum())}/{len(sep)} columns separated by >=2")

    raw = position_from_refs(profs, open_ref, closed_ref)
    pos = smooth_np(raw, a.smooth)
    po, pc = float(np.median(pos[open_m])), float(np.median(pos[closed_m]))
    print(f"  position: median {po:.3f} on confident-open frames, {pc:.3f} on confident-closed "
          f"(separation {pc - po:+.3f})")

    with open(a.out, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=["frame", "vt", "epoch", "osd", "ncc_top",
                                            "position", "position_raw"])
        wr.writeheader()
        base = 0.0
        for k, f in enumerate(frames):
            vt = (f - 1) / fps
            wr.writerow({"frame": int(f), "vt": round(vt, 4), "epoch": round(base + vt, 4),
                         "osd": truth_io.frame_to_osd(cam, f - 1),
                         "ncc_top": round(float(state[k]), 5),
                         "position": round(float(pos[k]), 5),
                         "position_raw": round(float(raw[k]), 5)})
    print(f"  wrote {len(frames)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
