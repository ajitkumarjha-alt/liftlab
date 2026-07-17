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
def _edge_from_gradient(gx_abs, min_edge_rows, row_peak_ratio=3.0):
    """Per-row edge -> robust median. gx_abs = |Sobel-x| (rows x cols). In EACH row take the column of
    the strongest horizontal gradient, KEEP it only if that peak clearly beats the row mean (a real
    edge in that row, row_peak_ratio). The MEDIAN of the kept columns is the leaf's horizontal position
    — this is what makes a DIAGONAL / barrel-distorted edge tractable: the diagonal is a fixed offset,
    so the median moves with the leaf's TRANSLATION, and the median rejects per-row outliers. strength
    = fraction of rows with a clear edge; below min_edge_rows -> no trustworthy leaf (honest None)."""
    n_rows = gx_abs.shape[0]
    rowmax = gx_abs.max(axis=1)
    rowmean = gx_abs.mean(axis=1) + 1e-6
    strong = rowmax > (row_peak_ratio * rowmean)
    cols = gx_abs.argmax(axis=1)[strong]
    if cols.size < min_edge_rows:
        return None, float(cols.size) / max(1, n_rows)
    return float(np.median(cols)), float(strong.mean())


def door_edge_column(gray_roi):
    """Leaf-edge column in the ROI, DIAGONAL-robust (wide-angle skew ~30px/280px smears a column-sum
    profile). Per-row argmax(|Sobel-x|) kept if it beats the row mean, then MEDIAN across rows. Returns
    (median_col, strength=fraction-of-edge-rows). cv2 only here; the aggregation (_edge_from_gradient)
    is pure/tested."""
    import cv2
    if gray_roi.size == 0 or gray_roi.shape[1] < 3 or gray_roi.shape[0] < 6:
        return None, 0.0
    gx = np.abs(cv2.Sobel(gray_roi, cv2.CV_32F, 1, 0, ksize=3))
    return _edge_from_gradient(gx, min_edge_rows=max(3, gray_roi.shape[0] // 6))


# ============================================================ 2. DoorTracker (openness -> cycles)
class DoorTracker:
    """Consumes (t_wall, edge_col, strength) and emits door CYCLES. openness is the edge column
    normalised against ROLLING closed/open references (drift-free — a stale reference can't strand a
    cycle the way the Pi's frozen baseline does). A cycle is emitted on return-to-closed with the four
    door timestamps + close_travel_s. Thresholds are hysteretic to reject jitter."""

    def __init__(self, min_strength=0.30, open_th=0.50, near_open=0.90, close_th=0.10,
                 ref_window=600, min_span_col=6.0):
        self.min_strength = min_strength      # min FRACTION of rows with a clear edge (door_edge_column strength)
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
    """Template-match the LED indicator on the WHOLE panel crop. The alphabet is OPEN — any glyph the
    operator labelled (0-9 AND letters: P1/P2/P3, G, B, LG — a digits-only classifier fails exactly on
    the lobby/parking floors, which is where the RTT lobby anchor lives). floor is a STRING, not an int.
    The panel is SEGMENTED on the dark gaps; the RIGHTMOST glyph is the arrow (always present -> travel
    direction, free for C17/C18), the rest are the floor label. Reads BOTH panels and requires
    AGREEMENT — disagreement discarded (a confidence no single-panel OCR gets)."""

    def __init__(self, templates, min_score=0.55, min_glyph_w=3, gap_frac=0.35, max_glyphs=4,
                 arrow_labels=ARROWS):
        self.templates = {k: np.asarray(v, dtype=np.float32) for k, v in templates.items()}
        self.arrow_labels = tuple(a for a in arrow_labels if a in self.templates)
        self.glyph_labels = tuple(k for k in self.templates if k not in self.arrow_labels)  # digits + letters
        self.min_score = min_score
        self.min_glyph_w = min_glyph_w        # a bright run narrower than this is noise, not a glyph
        self.gap_frac = gap_frac              # column brighter than lo+gap_frac*(hi-lo) counts as "lit"
        self.max_glyphs = max_glyphs          # floor(1-2) + arrow -> up to ~4; more = smear, discard
        self._tsz = next(iter(self.templates.values())).shape if self.templates else (16, 12)

    def _match(self, cell_img, labels):
        import cv2
        if cell_img.size == 0 or cell_img.shape[0] < 2 or cell_img.shape[1] < 2:
            return None, -2.0
        c = cv2.resize(cell_img, (self._tsz[1], self._tsz[0]))
        best, bs = None, -2.0
        for lab in labels:
            t = self.templates.get(lab)
            if t is not None:
                s = ncc(c, t)
                if s > bs:
                    best, bs = lab, s
        return best, bs

    def segment_glyphs(self, reg_gray):
        """Column-brightness projection -> runs of lit columns = glyphs (variable count, any position)."""
        col = reg_gray.astype(np.float32).mean(axis=0)
        rng = float(col.max() - col.min())
        if rng < 1e-3:
            return []
        thr = float(col.min()) + self.gap_frac * rng
        on = col > thr
        runs, i, n = [], 0, len(on)
        while i < n:
            if on[i]:
                j = i
                while j < n and on[j]:
                    j += 1
                if j - i >= self.min_glyph_w:
                    runs.append((i, j))
                i = j
            else:
                i += 1
        return runs

    def read_panel(self, panel_gray):
        runs = self.segment_glyphs(panel_gray)
        if len(runs) < 2 or len(runs) > self.max_glyphs:
            return None                              # need floor-glyph(s) + the (always-present) arrow
        ax0, ax1 = runs[-1]                          # rightmost glyph = the arrow
        arrow, arr_s = self._match(panel_gray[:, ax0:ax1], self.arrow_labels)
        direction = arrow if (arrow is not None and arr_s >= self.min_score) else None
        chars, scores = [], [arr_s if direction else self.min_score]
        for (x0, x1) in runs[:-1]:
            lab, s = self._match(panel_gray[:, x0:x1], self.glyph_labels)
            if lab is None or s < self.min_score:
                return None                          # a floor glyph we can't confidently name -> discard
            chars.append(lab)
            scores.append(s)
        if not chars:
            return None
        return {"floor": "".join(chars), "direction": direction,   # floor is a STRING ("25","P3","G")
                "score": round(min(scores), 3), "n_glyphs": len(runs)}

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


# ============================================================ floor TRACE -> Tier 2 (stops + speed)
class FloorTracker:
    """Floor read EVERY frame -> a floor-vs-time TRACE. Tier 2 falls out of the same instrument, no
    door event needed: a STOP is the lift DWELLING at a floor >= dwell_s (25^ x4 in the montage);
    stops_per_floor is the per-floor demand; the SPEED between consecutive stops (floors/second) is the
    C21/C22 speed factor. floor labels are strings; _idx maps them to a physical index (numeric via int,
    others via floor_order, e.g. ['LG','G','1','2',...])."""

    def __init__(self, dwell_s=2.0, floor_order=None):
        self.dwell_s = dwell_s
        self.floor_order = list(floor_order) if floor_order else None
        self._cur = None
        self._arr = None
        self._last = None
        self._dir = None                       # arrow seen while AT the current floor (up-stop vs down-stop)
        self._last_stop = None
        self.occupancies = []
        self.stops = []
        self.segments = []
        self.stops_per_floor = {}

    def _idx(self, floor):
        if self.floor_order and floor in self.floor_order:
            return self.floor_order.index(floor)
        try:
            return int(floor)
        except (TypeError, ValueError):
            return None

    def _record_stop(self, occ):
        """A resolved dwell -> a stop, plus stops_per_floor and the SPEED segment from the previous stop
        (C21/C22). Shared by update() (leaving a floor) and flush() (the floor sat on now)."""
        self.stops.append(occ)
        self.stops_per_floor[occ["floor"]] = self.stops_per_floor.get(occ["floor"], 0) + 1
        if self._last_stop is not None:
            i0, i1 = self._idx(self._last_stop["floor"]), self._idx(occ["floor"])
            dt = occ["arrive_t"] - self._last_stop["depart_t"]
            if i0 is not None and i1 is not None and dt > 0:
                self.segments.append({"from": self._last_stop["floor"], "to": occ["floor"],
                                      "n_floors": abs(i1 - i0), "travel_s": round(dt, 2),
                                      "floors_per_s": round(abs(i1 - i0) / dt, 3),
                                      "direction": "up" if i1 > i0 else "down"})
        self._last_stop = occ

    def update(self, t, floor, direction=None):
        """One frame. Returns a STOP dict when a dwell resolves into a stop, else None."""
        if floor is None:
            return None
        if self._cur is None:
            self._cur, self._arr, self._last, self._dir = floor, t, t, direction
            return None
        if floor == self._cur:
            self._last = t
            if direction:
                self._dir = direction                       # remember the arrow shown while stopped here
            return None
        dwell = self._last - self._arr                      # occupancy of the floor we're leaving
        occ = {"floor": self._cur, "arrive_t": round(self._arr, 2), "depart_t": round(self._last, 2),
               "dwell_s": round(dwell, 2), "direction": self._dir}   # up-stop vs down-stop for C17/C18
        self.occupancies.append(occ)
        stop = None
        if dwell >= self.dwell_s:                           # DWELLED -> a stop (no door event needed)
            self._record_stop(occ)
            stop = occ
        self._cur, self._arr, self._last, self._dir = floor, t, t, direction
        return stop

    def flush(self, t=None):
        """Close the CURRENT occupancy as a stop if it's dwelled long enough — for status/shutdown, so
        the floor the lift is sitting on right now isn't invisible until it next moves. Idempotent."""
        if self._cur is None:
            return None
        tt = self._last if t is None else t
        dwell = tt - self._arr
        already = self.stops and self.stops[-1]["floor"] == self._cur and self.stops[-1]["arrive_t"] == round(self._arr, 2)
        if dwell >= self.dwell_s and not already:
            occ = {"floor": self._cur, "arrive_t": round(self._arr, 2), "depart_t": round(tt, 2),
                   "dwell_s": round(dwell, 2), "direction": self._dir}
            self._record_stop(occ)
            return occ
        return None


# ============================================================ template building (on-box, open alphabet)
def _label_to_glyphs(label):
    """'12^' -> ['1','2','up'] ; 'P3v' -> ['P','3','down'] ; '6v' -> ['6','down']. Trailing ^/v is the
    arrow; the rest are floor glyphs (digits AND letters). OPEN alphabet — whatever the operator typed."""
    arrow = None
    body = label.strip()
    if body.endswith("^"):
        arrow, body = "up", body[:-1]
    elif body[-1:] in ("v", "V"):
        arrow, body = "down", body[:-1]
    glyphs = list(body.strip())
    if arrow:
        glyphs.append(arrow)
    return glyphs


def build_templates(labeled_panels, min_glyph_w=3, gap_frac=0.35, tsz=(16, 12)):
    """labeled_panels: list of (gray panel crop, label_str). Segment each panel, map the glyph runs
    left-to-right to the label's glyphs (last = arrow), accumulate per glyph, return {label: mean
    template}. A crop whose run-count != label-glyph-count is a MIS-SEGMENTATION -> skipped (not a
    clean example), and how many were used/skipped is returned so calibration is honest."""
    import cv2
    seg = FloorReader({"_": np.zeros(tsz)}, min_glyph_w=min_glyph_w, gap_frac=gap_frac)
    acc, used, skipped = {}, 0, 0
    for panel, label in labeled_panels:
        glyphs = _label_to_glyphs(label)
        runs = seg.segment_glyphs(panel)
        if len(runs) != len(glyphs) or not glyphs:
            skipped += 1
            continue
        used += 1
        for (x0, x1), g in zip(runs, glyphs):
            cell = cv2.resize(panel[:, x0:x1], (tsz[1], tsz[0])).astype(np.float32)
            acc.setdefault(g, []).append(cell)
    templates = {g: np.mean(v, axis=0) for g, v in acc.items()}
    return templates, {"used": used, "skipped": skipped, "glyphs": {g: len(v) for g, v in acc.items()}}


def save_templates(templates, path):
    np.savez(path, **{k: v.astype(np.float32) for k, v in templates.items()})


def load_templates(path):
    d = np.load(path)
    return {k: d[k] for k in d.files}


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
