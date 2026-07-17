"""
gpu_door.py — classical CV on FIXED ROIs, one pass per frame: door-edge position + floor OCR.

NOT a learned model, NOT tesseract. Same shape as the transit pipeline's honesty: it either locates
the edge / matches a glyph or it says it couldn't (a confidence, never a guess). Opt-in; emits to a
SEPARATE gpu_door_event stream; never touches gw_event or the counting path. Runs ALONGSIDE the Pi
(the reference cross-check) until proven, per gpu-owns-event-direction.

Three parts, each independently testable:
  1. door edge/openness   — Sobel-x column profile in door_roi -> leaf-edge column -> openness scalar.
  2. DoorTracker          — openness trace + REAL timestamps -> door_open/close start/full, close_travel.
                            "closed" is the rolling MODAL edge column (drift-free) — this is the whole
                            point vs the Pi's frozen brightness baseline.
  3. FloorReader          — template NCC on the two indicator panels, per digit/arrow cell, TWO panels
                            AGREE-OR-DISCARD (free confidence check), arrow -> travel direction (C17/C18).

cv2 is imported INSIDE the extraction fns so the pure logic (DoorTracker, NCC, reconcile) imports and
tests without OpenCV.
"""
from __future__ import annotations

from collections import deque

import numpy as np

DIGITS = tuple("0123456789")
ARROWS = ("up", "down")


# ============================================================ geometry
def scale_roi(roi_xywh, calib_wh, frame_wh):
    """Per-axis scale a calibration-space (x,y,w,h) ROI to the actual frame — same transform the zones
    use (door_roi is marked at 1920x1080 desk-rig; the GPU sees 704x576 sub)."""
    x, y, w, h = roi_xywh
    sx = frame_wh[0] / calib_wh[0]
    sy = frame_wh[1] / calib_wh[1]
    return (int(round(x * sx)), int(round(y * sy)), int(round(w * sx)), int(round(h * sy)))


def crop(img, roi_xywh):
    x, y, w, h = roi_xywh
    return img[max(0, y):y + h, max(0, x):x + w]


# ============================================================ 1. door edge / openness
def door_edge_column(gray_roi):
    """Dominant vertical edge column in the ROI = the moving door-leaf boundary. Returns
    (col, strength): strength = peak-to-mean of the |Sobel-x| column profile, so the caller can REJECT
    a frame with no clear edge (honest failure) instead of inventing a position. cv2 only here."""
    import cv2
    if gray_roi.size == 0 or gray_roi.shape[1] < 3:
        return None, 0.0
    gx = cv2.Sobel(gray_roi, cv2.CV_32F, 1, 0, ksize=3)
    prof = np.abs(gx).sum(axis=0)
    if prof.size >= 5:
        prof = np.convolve(prof, np.ones(5) / 5.0, mode="same")
    col = int(np.argmax(prof))
    strength = float(prof[col] / (prof.mean() + 1e-6))
    return col, strength


# ============================================================ 2. DoorTracker (openness -> cycles)
class DoorTracker:
    """Consumes (t_wall, edge_col, strength) and emits door CYCLES. openness is the edge column
    normalised against ROLLING closed/open references (drift-free — a stale reference can't strand a
    cycle the way the Pi's frozen baseline does). A cycle is emitted on return-to-closed with the four
    door timestamps + close_travel_s. Thresholds are hysteretic to reject jitter."""

    def __init__(self, min_strength=2.0, open_th=0.50, near_open=0.90, close_th=0.10,
                 ref_window=600, min_span_col=6.0):
        self.min_strength = min_strength      # reject frames with no clear leaf edge
        self.open_th = open_th                # openness rising past this = opening under way
        self.near_open = near_open            # reached this = fully open (open_full)
        self.close_th = close_th              # fell below this = fully closed (close_full)
        self.min_span_col = min_span_col      # need this many px between closed and open refs to trust openness
        self._cols = deque(maxlen=ref_window)  # recent edge columns -> rolling refs
        self.state = "closed"                 # closed | opening | open | closing
        self._ev = {}                         # timestamps of the cycle in progress
        self.cycles = []

    def _refs(self):
        if len(self._cols) < 20:
            return None, None
        arr = np.fromiter(self._cols, dtype=np.float32)
        return float(np.percentile(arr, 10)), float(np.percentile(arr, 90))  # closed ~ p10, open ~ p90

    def openness(self, col):
        lo, hi = self._refs()
        if lo is None or (hi - lo) < self.min_span_col:
            return None
        return float(np.clip((col - lo) / (hi - lo), 0.0, 1.0))

    def update(self, t, col, strength):
        """Feed one frame. Returns a completed cycle dict, or None."""
        if col is None or strength < self.min_strength:
            return None                        # no trustworthy edge this frame -> skip (honest gap)
        self._cols.append(col)
        o = self.openness(col)
        if o is None:
            return None
        st = self.state
        if st == "closed":
            if o >= self.open_th:
                self.state = "opening"; self._ev = {"open_start": t}
        elif st == "opening":
            if o >= self.near_open:
                self.state = "open"; self._ev["open_full"] = t
            elif o < self.close_th:
                self.state = "closed"; self._ev = {}          # aborted (blip) — not a cycle
        elif st == "open":
            if o < self.near_open:
                self.state = "closing"; self._ev["close_start"] = t
        elif st == "closing":
            if o < self.close_th:
                self._ev["close_full"] = t
                self.state = "closed"
                return self._emit()
            elif o >= self.near_open:
                self.state = "open"; self._ev.pop("close_start", None)   # re-opened mid-close
        return None

    def _emit(self):
        e = self._ev
        self._ev = {}
        need = ("open_start", "open_full", "close_start", "close_full")
        if not all(k in e for k in need):
            return None
        cyc = {k: e[k] for k in need}
        cyc["close_travel_s"] = round(e["close_full"] - e["close_start"], 3)
        cyc["open_travel_s"] = round(e["open_full"] - e["open_start"], 3)
        cyc["dwell_s"] = round(e["close_start"] - e["open_full"], 3)
        self.cycles.append(cyc)
        return cyc


# ============================================================ 3. FloorReader (template OCR)
def ncc(a, b):
    """Normalised cross-correlation of two same-size arrays. 1.0 = identical pattern, ~0 = unrelated.
    Robust to brightness/contrast (mean-subtracted, magnitude-normalised) — right for a dim LED panel."""
    a = a.astype(np.float32).ravel(); b = b.astype(np.float32).ravel()
    a = a - a.mean(); b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


class FloorReader:
    """Template-match the LED indicator. digit_cells/arrow_cell are calibrated (x,y,w,h) sub-ROIs
    WITHIN a panel crop; templates is {label: 2D-uint8}. Reads BOTH panels and requires AGREEMENT —
    disagreement is discarded (a confidence no single-panel OCR gets). arrow -> travel direction."""

    def __init__(self, templates, digit_cells, arrow_cell=None, min_score=0.55):
        self.templates = {k: np.asarray(v, dtype=np.float32) for k, v in templates.items()}
        self.digit_cells = digit_cells
        self.arrow_cell = arrow_cell
        self.min_score = min_score
        self._tsz = next(iter(self.templates.values())).shape if self.templates else (16, 12)

    def _match(self, cell_img, labels):
        import cv2
        if cell_img.size == 0:
            return None, 0.0
        c = cv2.resize(cell_img, (self._tsz[1], self._tsz[0]))
        best, bs = None, -2.0
        for lab in labels:
            t = self.templates.get(lab)
            if t is None:
                continue
            s = ncc(c, t)
            if s > bs:
                best, bs = lab, s
        return best, bs

    def read_panel(self, panel_gray):
        scores = []
        digits = ""
        for cell in self.digit_cells:
            lab, s = self._match(crop(panel_gray, cell), DIGITS)
            if lab is None or s < self.min_score:
                return None                         # a cell we can't read -> discard the whole panel
            digits += lab; scores.append(s)
        direction, ds = (None, 1.0)
        if self.arrow_cell is not None:
            direction, ds = self._match(crop(panel_gray, self.arrow_cell), ARROWS)
            if direction is None or ds < self.min_score:
                direction = None                    # floor still usable without a confident arrow
            scores.append(ds if direction else self.min_score)
        try:
            floor = int(digits)
        except ValueError:
            return None
        return {"floor": floor, "direction": direction, "score": round(min(scores), 3)}

    def reconcile(self, panel_reads):
        """AGREE-OR-DISCARD across the two panels. Both read + agree on floor -> confident (mean score,
        +agreement). Disagree or any panel unread -> None (discard; never publish a floor we can't
        corroborate)."""
        reads = [r for r in panel_reads if r]
        if len(reads) < 2:
            return None
        if any(r["floor"] != reads[0]["floor"] for r in reads):
            return None
        # direction: agree if both present + equal; else the one that's present; else None
        dirs = [r["direction"] for r in reads if r["direction"]]
        direction = dirs[0] if dirs and all(d == dirs[0] for d in dirs) else None
        return {"floor": reads[0]["floor"], "direction": direction,
                "confidence": round(min(r["score"] for r in reads), 3), "panels": len(reads), "agree": True}


# ============================================================ calibration helpers (step 1/2)
def save_crop(img, path):
    import cv2
    cv2.imwrite(path, img)


def overlay_rois(frame_bgr, rois, labels=None):
    """Draw candidate ROIs on a frame so the operator can confirm they sit on the panels (zone_verify
    approach). Returns the annotated BGR frame."""
    import cv2
    out = frame_bgr.copy()
    for i, (x, y, w, h) in enumerate(rois):
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 1)
        cv2.putText(out, (labels[i] if labels else str(i)), (x, max(10, y - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    return out
