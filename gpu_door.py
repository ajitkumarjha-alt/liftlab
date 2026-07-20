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


BLANK = "blank"


class FloorReader:
    """FIXED-PITCH GEOMETRIC CELLS — NO segmentation. HEVC at 0.4 Mbps smears the 1-2px gap between
    adjacent digits, so gap-finding fails on two-digit floors (which is most of a 44-floor tower). But
    an LED dot-matrix is fixed-pitch by construction: cell positions are KNOWN, not hunted. digit_cells
    are (x,y,w,h) sub-ROIs WITHIN the panel, left-to-right; arrow_cell likewise. Each cell is cropped
    BLINDLY and matched; an empty cell matches the learned 'blank' template (single-digit floor ->
    left cell blank). floor is the STRING of non-blank cell glyphs (OPEN alphabet: digits AND letters
    P/G/etc). Reads BOTH panels, AGREE-OR-DISCARD. arrow (always present, rightmost) -> direction."""

    def __init__(self, templates, digit_cells, arrow_cell, min_score=0.55, blank_range=40,
                 arrow_labels=ARROWS, blank_label=BLANK):
        self.templates = {k: np.asarray(v, dtype=np.float32) for k, v in templates.items()}
        self.digit_cells = list(digit_cells)
        self.arrow_cell = arrow_cell
        self.min_score = min_score
        self.blank_range = blank_range        # a cell with max-min brightness below this is BLANK (unlit
        self.blank_label = blank_label        #   padding cell of a single-digit floor). NCC can't match a
        self.arrow_labels = tuple(a for a in arrow_labels if a in self.templates)   # flat/dark cell (0 variance).
        bpref = self.blank_label + "_"
        # PER-CELL blank templates ("blank_<i>"): carry the cell's STATIC content (e.g. a door edge in the
        # hundreds cell) so blank is detected by NCC where the contrast test can't fire. See build_templates.
        self.blank_cells = {int(k[len(bpref):]): np.asarray(v, np.float32)
                            for k, v in self.templates.items()
                            if k.startswith(bpref) and k[len(bpref):].isdigit()}
        self.glyph_labels = tuple(k for k in self.templates            # lit glyphs only (not arrow/blank/per-cell-blank)
                                  if k not in self.arrow_labels and k != self.blank_label
                                  and not (k.startswith(bpref) and k[len(bpref):].isdigit()))
        self._tsz = next(iter(self.templates.values())).shape if self.templates else (16, 10)

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

    def read_panel(self, panel_gray):
        import cv2
        chars, scores = [], []
        for i, cell in enumerate(self.digit_cells):  # blind fixed-cell crop, no gap-finding
            ci = crop(panel_gray, cell)
            if ci.size == 0:
                return None
            if (int(ci.max()) - int(ci.min())) < self.blank_range:
                continue                             # flat/dark cell -> BLANK (NCC undefined on 0 variance)
            c = cv2.resize(ci, (self._tsz[1], self._tsz[0]))
            lab, s = None, -2.0
            for g in self.glyph_labels:              # best lit-glyph match for this cell
                sc = ncc(c, self.templates[g])
                if sc > s:
                    lab, s = g, sc
            bt = self.blank_cells.get(i)             # PER-CELL blank: fires where contrast can't — a static
            if bt is not None and ncc(c, bt) >= max(s, self.min_score):   # edge in the cell (e.g. hundreds)
                continue                             # blank explains this cell at least as well -> BLANK
            if lab is None or s < self.min_score:
                return None                          # a lit cell we can't confidently name -> discard panel
            scores.append(s)
            chars.append(lab)                        # non-blank cells, left-to-right = the floor string
        if not chars:
            return None
        direction = None
        if self.arrow_labels:
            arrow, arr_s = self._match(crop(panel_gray, self.arrow_cell), self.arrow_labels)
            if arrow is not None and arr_s >= self.min_score:
                direction, _ = arrow, scores.append(arr_s)
        return {"floor": "".join(chars), "direction": direction,   # STRING: "25","P3","G"
                "score": round(min(scores), 3), "n_cells": len(self.digit_cells)}

    @staticmethod
    def column_profile(reg_gray):
        """Per-column mean brightness — the diagnostic that settles whether a gap survives HEVC. A clean
        two-digit crop shows two bright humps with a dip between; a smeared one shows one broad hump."""
        return reg_gray.astype(np.float32).mean(axis=0)

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


def build_templates(labeled_panels, digit_cells, arrow_cell, tsz=(16, 10),
                    align="right", blank_label=BLANK):
    """FIXED-CELL builder (no segmentation). labeled_panels: (gray panel, label_str). For each: parse
    the label -> floor chars + arrow, ALIGN the chars into the fixed digit_cells (right-aligned pads the
    LEFT cells with 'blank'), crop each cell BLINDLY and accumulate under its char (or 'blank'), crop
    arrow_cell under the arrow. Learns every glyph AND 'blank' from known positions -> two-digit floors
    no longer fail. A label with more floor chars than cells is skipped (honest stats)."""
    import cv2
    acc, used, skipped = {}, 0, 0
    n = len(digit_cells)

    def _cell_labels(chars):
        pad = n - len(chars)
        if align == "left":
            return chars + [blank_label] * pad
        if align == "center":
            l = pad // 2
            return [blank_label] * l + chars + [blank_label] * (pad - l)
        return [blank_label] * pad + chars              # right-aligned (default)

    for panel, label in labeled_panels:
        glyphs = _label_to_glyphs(label)
        arrow = glyphs[-1] if glyphs and glyphs[-1] in ARROWS else None
        floor_chars = glyphs[:-1] if arrow else glyphs
        if not floor_chars or len(floor_chars) > n:
            skipped += 1
            continue
        used += 1
        for i, (cell, g) in enumerate(zip(digit_cells, _cell_labels(floor_chars))):
            # A blank padding cell now learns a PER-CELL blank template ("blank_<i>") instead of being
            # skipped. It captures whatever STATIC structure sits in that cell — e.g. the hundreds cell
            # overlaps a fixed door-edge diagonal — so the reader can NCC-match blank there, where the
            # contrast test never fires (a bright static edge always exceeds blank_range). The static
            # edge is common-mode in both this blank and the cell's glyph templates, so it cancels.
            key = f"{blank_label}_{i}" if g == blank_label else g
            c = cv2.resize(crop(panel, cell), (tsz[1], tsz[0])).astype(np.float32)
            acc.setdefault(key, []).append(c)
        if arrow:
            c = cv2.resize(crop(panel, arrow_cell), (tsz[1], tsz[0])).astype(np.float32)
            acc.setdefault(arrow, []).append(c)
    templates = {g: np.mean(v, axis=0) for g, v in acc.items()}
    return templates, {"used": used, "skipped": skipped, "glyphs": {g: len(v) for g, v in acc.items()}}


def save_templates(templates, path):
    np.savez(path, **{k: v.astype(np.float32) for k, v in templates.items()})


def load_templates(path):
    d = np.load(path)
    return {k: d[k] for k in d.files}


def fetch_templates(url, headers=None, timeout=15):
    """Fetch a templates.npz over HTTP (Bearer) and load it. The GPU box has NO scp scopes, so it PULLS
    the cloud-built templates the same way it pulls segments (analysis token) — one cloud source of
    truth, the same answer as the zones store."""
    import io as _io
    import requests
    r = requests.get(url, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    d = np.load(_io.BytesIO(r.content))
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


# ============================================================ unified per-frame engine + versioning
def templates_hash(templates):
    """CONTENT hash of a template set (sorted keys + raw float32 bytes) — the version/cache key. Stable
    across rebuilds with identical data (unlike the npz zip bytes, which carry timestamps). gpu_door
    stamps every event with this + refetches when it changes, so a rebuild propagates without a redeploy."""
    import hashlib
    h = hashlib.sha256()
    for k in sorted(templates):
        h.update(k.encode())
        h.update(np.ascontiguousarray(templates[k], dtype=np.float32).tobytes())
    return h.hexdigest()


def door_event_changed(prev_key, cur):
    """Emit-on-change gate: a gw_door_event row is worth writing when the (floor, direction, door_state)
    tuple changes from the last emitted one, OR a door CYCLE just completed (carries close_travel). This
    dedups the per-frame trace down to state transitions — compact but lossless for the stops/speed
    (FloorTracker) and door-timing analysis downstream. Returns (should_emit, new_key)."""
    key = (cur.get("floor"), cur.get("direction"), cur.get("door_state"))
    return (bool(cur.get("cycle")) or key != prev_key), key


class DoorFloorEngine:
    """ONE pass per frame: door edge -> DoorTracker (state + close_travel cycle), FloorReader on 1-2
    panels (AGREE-OR-DISCARD), FloorTracker (Tier-2 stops). process(frame_bgr, t) -> a read dict; emits
    nothing (the caller emits on-change). Geometry is FRAME px: door_roi=(x,y,w,h); panels=[(panel_roi,
    digit_cells, arrow_cell), ...] — 1 or 2. TWO panels give the free confidence check (agree-or-discard);
    ONE panel still reads, flagged panels_agreed=False. Templates are SHARED across panels (the LED glyphs
    are identical; only the cell GEOMETRY differs — panel1 needs its OWN cells, its own anchor read)."""

    def __init__(self, templates, door_roi, panels, min_score=0.55, blank_range=40,
                 door_tracker=None, floor_tracker=None):
        if not panels:
            raise ValueError("DoorFloorEngine needs at least one panel (panel_roi, digit_cells, arrow_cell)")
        self.door_roi = tuple(door_roi)
        self.readers = [(tuple(proi), FloorReader(templates, dcells, acell,
                                                  min_score=min_score, blank_range=blank_range))
                        for (proi, dcells, acell) in panels]
        self.door = door_tracker if door_tracker is not None else DoorTracker()
        self.floor = floor_tracker if floor_tracker is not None else FloorTracker()
        self.hash = templates_hash(templates)

    def process(self, frame_bgr, t):
        import cv2
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
        col, strength = door_edge_column(crop(gray, self.door_roi))
        cycle = self.door.update(t, col, strength)          # completed door cycle (close_travel) or None
        openness = self.door.openness(col) if col is not None else None
        reads = [rdr.read_panel(crop(gray, proi)) for proi, rdr in self.readers]
        got = [r for r in reads if r]
        floor = direction = conf = None
        agreed = False
        reason = "no_read"                                  # nothing matched — incl. MEP (M/E thin-known)
        if len(self.readers) >= 2:
            rec = self.readers[0][1].reconcile(reads)       # agree-or-discard
            if rec:
                floor, direction, conf, agreed, reason = rec["floor"], rec["direction"], rec["confidence"], True, "ok"
            elif len(got) == 2:
                reason = "disagree"                         # both read but conflict -> discard, don't guess
        else:                                               # single-panel mode (panel1 cells not calibrated yet)
            r0 = reads[0]
            if r0:
                floor, direction, conf, agreed, reason = r0["floor"], r0["direction"], r0["score"], False, "single_panel"
        stop = self.floor.update(t, floor, direction) if floor else None
        out = {"t": t, "floor": floor, "direction": direction, "door_state": self.door.state,
               "openness": (round(float(openness), 3) if openness is not None else None),
               "read_conf": (round(float(conf), 3) if conf is not None else None),
               "panels_agreed": agreed, "reason": reason, "n_panels": len(self.readers),
               "edge_strength": round(float(strength), 3), "cycle": cycle, "stop": stop}
        if cycle:
            out["close_travel_s"] = cycle.get("close_travel_s")
        return out
