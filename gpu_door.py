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

import json
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
# Tracker LOGIC revision — folded into door_version's ERA PREFIX by the worker, so a logic change
# moves the comparability boundary the way a template rebuild does. The 5f1488a time-guards changed
# emissions WITHOUT moving the era and pre-guard rows poisoned the pool for a week; never again.
# h2 = close-start hysteresis band + debounce (2026-07-29 flap fix).
TRACKER_LOGIC = "h2"


class DoorTracker:
    """Consumes (t_wall, edge_col, strength) and emits door CYCLES. openness is the edge column
    normalised against ROLLING closed/open references (drift-free — a stale reference can't strand a
    cycle the way the Pi's frozen baseline does). A cycle is emitted on return-to-closed with the four
    door timestamps + close_travel_s. Thresholds are hysteretic to reject jitter.

    h2 hysteresis (the 3-camera flap fix): 'open -> closing' no longer triggers on one frame dipping
    under near_open — a person occluding the leaf minted closing<->open flap chains (p50 gap 0.32s)
    and each flap could complete as a garbage cycle. Now the descent must reach close_start_th (a
    real hysteresis band under near_open) and have persisted close_debounce_s; close_start is
    stamped at the NEAR_OPEN crossing (_below_since), so close_travel keeps its near_open->close_th
    definition and the compliance number's meaning is unchanged. A descent that plummets straight
    past close_th while still 'open' completes as a fast cycle rather than stranding the state."""

    def __init__(self, min_strength=0.30, open_th=0.50, near_open=0.90, close_th=0.10,
                 ref_window=600, min_span_col=6.0, max_gap_s=15.0,
                 min_close_s=0.3, max_close_s=30.0, min_open_s=0.15, max_open_s=30.0,
                 close_start_th=0.75, close_debounce_s=0.4):
        self.min_strength = min_strength      # min FRACTION of rows with a clear edge (door_edge_column strength)
        self.open_th = open_th                # openness rising past this = opening under way
        self.near_open = near_open            # reached this = fully open (open_full)
        self.close_th = close_th              # fell below this = fully closed (close_full)
        self.min_span_col = min_span_col      # need this many px between closed and open refs to trust openness
        # TIME GUARDS. The tracker paired close_start->close_full across arbitrary gaps: a stream gap
        # while in 'closing' produced a 77-minute "close" (4641s), and a single noisy frame entering
        # AND completing 'closing' produced a sub-frame 0.08s "close". Neither is physical.
        self.max_gap_s = max_gap_s            # analyzed-frame jump > this while mid-cycle -> abandon (missed the real cycle)
        self.min_close_s = min_close_s        # close_travel below this = a one-frame artifact -> WITHHELD
        self.max_close_s = max_close_s        # close_travel above this = spans a gap -> WITHHELD
        self.min_open_s = min_open_s
        self.max_open_s = max_open_s
        self.close_start_th = close_start_th  # descent must reach this (not just dip under near_open)...
        self.close_debounce_s = close_debounce_s   # ...and have persisted this long, to enter 'closing'
        self._below_since = None              # when the current descent left near_open (close_start anchor)
        self._cols = deque(maxlen=ref_window)  # recent edge columns -> rolling refs
        self.state = "closed"                 # closed | opening | open | closing
        self._ev = {}                         # timestamps of the cycle in progress
        self._last_t = None                   # wall-clock of the last analyzed frame that advanced state
        self.cycles = []
        self.abandoned = 0                    # cycles dropped on a time gap (visibility, not silent)

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
        # TIME-GAP GUARD. If we haven't seen a usable frame in max_gap_s while a cycle is in progress,
        # the door almost certainly opened/closed unseen (dropped segments, a stall, low edge strength).
        # Pairing the next edge to the pre-gap one is what produced the 4641s close. Abandon the cycle
        # and re-derive state from the current openness — never fabricate a close across the blind span.
        if (self._last_t is not None and (t - self._last_t) > self.max_gap_s
                and self.state != "closed"):
            self._ev = {}
            self.state = "open" if o >= self.near_open else ("closed" if o < self.close_th else "opening")
            self._below_since = None
            self.abandoned += 1
            self._last_t = t
            return None
        self._last_t = t
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
            # h2 HYSTERESIS. A dip under near_open is a CANDIDATE descent, not a closing: people
            # occluding the leaf oscillate 0.85<->0.92 and the old one-frame trigger minted flap
            # chains. 'closing' needs the descent to reach close_start_th AND to have persisted
            # close_debounce_s. close_start is stamped at the near_open crossing (_below_since), so
            # close_travel keeps its meaning. A shallow dip that recovers resets cleanly.
            if o >= self.near_open:
                self._below_since = None
            else:
                if self._below_since is None:
                    self._below_since = t
                if o < self.close_th:
                    # plummeted straight through while still 'open' (fast/suppressed descent):
                    # complete the cycle rather than strand the state machine at 'open'
                    self._ev["close_start"] = self._below_since
                    self._ev["close_full"] = t
                    self.state = "closed"
                    self._below_since = None
                    return self._emit()
                if o < self.close_start_th and (t - self._below_since) >= self.close_debounce_s:
                    self.state = "closing"
                    self._ev["close_start"] = self._below_since
                    self._below_since = None
        elif st == "closing":
            if o < self.close_th:
                self._ev["close_full"] = t
                self.state = "closed"
                return self._emit()
            elif o >= self.near_open:
                self.state = "open"; self._ev.pop("close_start", None)   # re-opened mid-close
                self._below_since = None
        return None

    def _emit(self):
        e = self._ev
        self._ev = {}
        need = ("open_start", "open_full", "close_start", "close_full")
        if not all(k in e for k in need):
            return None
        cyc = {k: e[k] for k in need}
        ct = round(e["close_full"] - e["close_start"], 3)
        ot = round(e["open_full"] - e["open_start"], 3)
        cyc["dwell_s"] = round(e["close_start"] - e["open_full"], 3)
        cyc["open_travel_s"] = ot
        # EMIT THE FACT, WITHHOLD AN IMPLAUSIBLE MEASUREMENT (same discipline as the cloud ingest for
        # gw_event). A cycle happened — that is real and kept — but a close_travel outside the physical
        # band is a pairing/noise artifact, not a measurement, so it is set null and reasoned rather
        # than fed to the compliance median. 0.08s (sub-frame) and gap-spanning values both fail here.
        if ct < self.min_close_s or ct > self.max_close_s:
            cyc["close_travel_s"] = None
            cyc["close_quality"] = (f"withheld: close_travel {ct}s outside "
                                    f"[{self.min_close_s},{self.max_close_s}]s (pairing/noise artifact)")
        else:
            cyc["close_travel_s"] = ct
            cyc["close_quality"] = "ok"
        self.cycles.append(cyc)
        return cyc


# Tracker LOGIC revision for the STATE-ONLY engine. NOT the module default: TRACKER_LOGIC above
# still reads "h2". h3 is selected PER CAMERA at runtime (DOOR_TRACKER env, from the registry), and
# door_version stamps whichever one actually ran — see gpu_analyze.build_door_engine.
TRACKER_LOGIC_H3 = "h3-state"

# h3 emits states h2 never had. The cloud ingest whitelists {closed,opening,open,closing} and 400s
# anything else (door_event_api.DOOR_STATES), and dash's cycle funnel walks those same four. So the
# engine's own vocabulary is mapped to the wire vocabulary at the emission boundary rather than
# widening the wire — no VM deploy is needed to ship h3, and the funnel keeps working.
#
# The residual, which is real: h3 has NO 'opening' state (it goes closed -> open directly), so for
# an h3 camera the funnel's closed->opening and opening->open counts are structurally zero. That is
# a property of the engine, not a fault, and door_version is what tells the two apart.
H3_STATE_TO_WIRE = {"closed": "closed", "open": "open", "descending": "closing", "unknown": None}


def load_state_template(path):
    """Load a per-camera closed-door state template artefact. -> (template ndarray, meta dict).

    Raises ValueError with a reason on anything wrong. The caller's contract is that a bad template
    means THAT CAMERA RUNS h2 — never that it runs h3 against a template it could not verify.
    """
    import base64
    import hashlib
    import cv2
    with open(path) as fh:
        meta = json.load(fh)
    for k in ("cam", "band_y", "template_wh", "md5", "png_b64", "tracker"):
        if k not in meta:
            raise ValueError(f"missing key {k!r}")
    if meta.get("tracker") != TRACKER_LOGIC_H3:
        raise ValueError(f"template is for tracker {meta.get('tracker')!r}, not {TRACKER_LOGIC_H3!r}")
    png = base64.b64decode(meta["png_b64"])
    got = hashlib.md5(png).hexdigest()
    if got != meta["md5"]:
        raise ValueError(f"md5 mismatch: artefact says {meta['md5']}, pixels hash {got}")
    tpl = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise ValueError("PNG decode failed")
    wh = tuple(meta["template_wh"])
    if (tpl.shape[1], tpl.shape[0]) != wh:
        raise ValueError(f"template is {tpl.shape[1]}x{tpl.shape[0]}, artefact says {wh[0]}x{wh[1]}")
    return tpl, meta


def state_closedness(gray, band_y, roi_x_w, tpl):
    """NCC of this frame's state band against the closed template. High = closed.

    Identical construction to the offline harness — one function, so the graded engine and the
    deployed engine cannot drift apart in how they compute the signal they are graded on.
    """
    import cv2
    y0, y1 = band_y
    x, w = roi_x_w
    band = gray[y0:y1, x:x + w]
    if band.size == 0:
        return None
    crop_r = cv2.resize(band, (tpl.shape[1], tpl.shape[0]))
    return ncc(crop_r, tpl)


class DoorTrackerH3:
    """State-only door engine. Detects door CYCLES from a closedness signal and does NOT time them.

    WHY STATE-ONLY. h2 measures travel from a signal that was never validated to carry travel, and
    three passes of TEST B have now failed to validate one: `ncc_top` saturates above head height
    (ed42761), and the column-wise position proxy's ramp foot wanders on ch27 (668cf46). h2's own
    clean-corpus numbers are 41 phantom emissions on ch27 against 13 real closes, and one travel
    value produced per camera against 4 and 11 timed closes. The state half, by contrast, is
    validated: TEST A passes at AUC 1.000 / LOO 99.1% on ch27 and 0.958 / 89.6% on ch30.

    So this engine ships the half that is validated and refuses the half that is not. Every cycle
    carries `close_travel_s = None` with a reason — not because the measurement was implausible, as
    h2's withholding clause means, but because THIS ENGINE DOES NOT MEASURE IT. A consumer that sees
    null gets an explicit reason string rather than a gap it has to interpret.

    INPUT is a closedness scalar per frame — high when the leaf is shut. `ncc_top` against a
    per-camera closed template is what it was validated on. Levels are taken from ROLLING
    percentiles of the signal's own recent history, as h2 does for its edge column, so a lighting
    change or a floor change moves both plateaus together instead of stranding the state machine.

    WHAT KILLS THE PHANTOMS. Three things, in order of how much each is doing:
      * a REFRACTORY window after every emission — h2's ch27 phantoms come in bursts inside stretches
        where the door never moves, so the first emission is the error and the next four are the
        error repeated;
      * requiring a confident OPEN plateau before a close can be armed at all — a door that never
        opened cannot close, and h2's state machine could re-enter 'closing' from a dead band;
      * hysteresis with a dead band between the plateaus, so noise in the middle advances nothing.

    THERE IS NO OCCLUSION FLAG, DELIBERATELY. One was built and removed. It measured reversals — the
    closedness falling back from its running maximum, as if someone stepped through the band — and on
    the only labelled evidence available it fired on nothing it should have: 0 of 17 cycles on ch30
    including the 4.60s complex close, and 5 of 54 on ch27 of which NOT ONE was a matched cycle,
    while both hand-labelled extended closes (f21317 4.76s, f46263 6.28s) came back unflagged. A
    field that misses every event it exists to catch is worse than no field, because a consumer will
    reasonably believe it. This engine emits facts or nothing. The removal is recorded in git; if a
    corpus with hand-labelled occlusions ever exists, a detector can be built and VALIDATED against
    it, and this is where it goes.

    `descent_s` IS still emitted, as a measured fact rather than a judgement: it is the observed
    open->closed wall time, and it is NOT a travel measurement — it is the state machine's own
    transit, bounded by the debounce and the sampling cadence. Do not feed it to compliance.
    """

    def __init__(self, open_th=0.15, closed_th=0.85, ref_window=600, min_span=0.08,
                 close_debounce_s=0.32, refractory_s=6.0, max_descent_s=12.0, min_open_s=0.4):
        self.open_th = open_th                  # normalised closedness at/below this = confidently open
        self.closed_th = closed_th              # at/above this = confidently closed
        self.min_span = min_span                # raw p10..p90 span below this = signal not trustworthy
        self.close_debounce_s = close_debounce_s   # closed plateau must hold this long to emit
        self.refractory_s = refractory_s        # no second emission within this of the last one
        self.max_descent_s = max_descent_s      # descents longer than this are abandoned, not flagged
        self.min_open_s = min_open_s            # the open plateau must hold this long to arm a close
        self._vals = deque(maxlen=ref_window)
        self.state = "unknown"                  # unknown | open | descending | closed
        self._open_since = None
        self._desc_from = None
        self._closed_since = None
        self._last_emit_t = None
        self.cycles = []
        self.suppressed = 0                     # emissions withheld by the refractory — counted, not hidden
        self.abandoned = 0                      # descents dropped for exceeding max_descent_s

    def _levels(self):
        if len(self._vals) < 40:
            return None, None
        arr = np.fromiter(self._vals, dtype=np.float32)
        return float(np.percentile(arr, 10)), float(np.percentile(arr, 90))

    def closedness(self, v):
        lo, hi = self._levels()
        if lo is None or (hi - lo) < self.min_span:
            return None
        return float(np.clip((v - lo) / (hi - lo), 0.0, 1.0))

    def update(self, t, v):
        """Feed one frame's raw closedness. Returns a completed cycle dict, or None."""
        if v is None:
            return None
        self._vals.append(v)
        c = self.closedness(v)
        if c is None:
            return None

        if c <= self.open_th:
            if self.state in ("unknown", "closed"):
                self.state = "open"
                self._open_since = t
            elif self.state == "descending":
                # recovered to fully open before reaching closed — not a close
                self.state = "open"
                self._open_since = t
                self._desc_from = None
            self._closed_since = None
            return None

        if self.state == "open":
            if (t - (self._open_since or t)) < self.min_open_s:
                return None                      # the open plateau has not held long enough to arm
            if c < self.closed_th:
                self.state = "descending"
                self._desc_from = t
            return None

        if self.state == "descending":
            if c < self.closed_th:
                if (t - self._desc_from) > self.max_descent_s:
                    # A descent this long did not observe one close: the signal drifted, or the door
                    # sat part-open. Abandon rather than emit a cycle whose start is unknown — the
                    # same discipline h2 applies on a time gap.
                    self.state = "unknown"
                    self._desc_from = None
                    self._open_since = None
                    self.abandoned += 1
                return None
            if self._closed_since is None:
                self._closed_since = t
            if (t - self._closed_since) < self.close_debounce_s:
                return None
            return self._emit(t)

        # state == "closed" or "unknown" with c above open_th: nothing to arm, stay put
        if self.state == "unknown":
            self.state = "closed"
        return None

    def _emit(self, t):
        desc_s = t - self._desc_from if self._desc_from else None
        self.state = "closed"
        self._closed_since = None
        self._desc_from = None
        self._open_since = None

        if self._last_emit_t is not None and (t - self._last_emit_t) < self.refractory_s:
            self.suppressed += 1
            return None
        self._last_emit_t = t

        cyc = {
            "close_full": t,
            "close_ts": t,
            "ts": t,
            # NOT a withheld measurement — an unmeasured one. h2's null means "computed, implausible,
            # dropped"; this null means "this engine does not compute it at all".
            "close_travel_s": None,
            "close_quality": ("not-measured: h3 is a state-only engine; travel is unvalidated "
                              "(TEST B failed three passes) and is sampled by hand weekly instead"),
            # Observed state-machine transit, NOT a travel measurement. See the class docstring.
            "descent_s": None if desc_s is None else round(desc_s, 3),
            "tracker": TRACKER_LOGIC_H3,
        }
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
                 arrow_labels=ARROWS, blank_label=BLANK, shift_search=2, margin_min=0.05,
                 blank_min=0.45, shift_floor=0.40, lit_range=120, blank_strong=0.90,
                 blank_lit_margin=0.15, confuse_band=0.0, disc_min=0.10, valid_floors=None):
        # A tower has a FIXED set of floors. A read that assembles to something outside it — a phantom
        # hundreds digit ("167" on a P3-26 tower), or a glyph scored into a digit cell ("7G") — is a
        # misread no matter how each cell scored on its own. When the alphabet is known, an off-alphabet
        # read is emitted as reason='off_alphabet' rather than 'ok', so it is recorded (emit-fact-flag-
        # quality) but never counted as a confident floor. Empty/None = no whitelist (the old behaviour).
        self.valid_floors = set(valid_floors) if valid_floors else None
        self.templates = {k: np.asarray(v, dtype=np.float32) for k, v in templates.items()}
        self.digit_cells = list(digit_cells)
        self.arrow_cell = arrow_cell
        self.min_score = min_score
        # RIGID SHIFT SEARCH radius (px). The LED display translates as one; per-frame jitter (~2-4px
        # measured) at tight cells breaks the most sensitive cell (tens) while units/arrow — anchored /
        # robust — still read. Before reading, search (dx,dy) in [-R..R]^2 for the shift that best aligns
        # ALL cells rigidly, then read there. Reader-side, no rebuild. 0 = old fixed-position behavior.
        self.shift_search = int(shift_search)
        # MARGIN CHECK: if a cell's top-2 glyph NCC scores are within margin_min, the glyph is AMBIGUOUS
        # (HEVC eats the 1-2px middle-bar that separates 8/0, 6/G) — emit no_read, not a confident wrong
        # digit that poisons floor attribution. 0 disables. The tied candidates go into the event.
        self.margin_min = float(margin_min)
        # BLANK confidence floor for a DIM/DARK cell — decoupled from the glyph min_score so a single-
        # digit floor's dim tens cell (blank ~0.58-0.80 beats junk glyphs 0.3-0.45) reads blank.
        self.blank_min = float(blank_min)
        # ...BUT blank may only WIN in a cell that isn't clearly LIT (contrast < lit_range) OR whose blank
        # is EXCEPTIONAL (>= blank_strong, i.e. the static door edge ~1.0). A LIT cell (a real digit is
        # present, contrast >= lit_range) with a weak glyph + moderate blank must NOT be blanked — that
        # DELETES the digit (a confident misread, worse than an honest no_read).
        self.lit_range = float(lit_range)
        self.blank_strong = float(blank_strong)
        # In a LIT cell an exceptional blank (the cell0 door edge) may still win, but ONLY if it beats
        # the glyph by this margin — else a blank_1 that barely edges a lit '6' (0.907 vs 0.83) would
        # delete the digit (63 -> 3). The door edge clears it easily (blank ~1.0 >> a weak glyph).
        self.blank_lit_margin = float(blank_lit_margin)
        # CONFUSABLE-PAIR diff-region tiebreak (opt-in; 0 = off). When top-2 glyph scores are within
        # confuse_band, decide between them on the pixels where the two win-exemplars DIFFER, not whole
        # cell — for a genuine pair (3/5, 8/6) that exemplars sharpened so the margin no longer flags.
        self.confuse_band = float(confuse_band)
        self.disc_min = float(disc_min)
        # SHIFT quality floor — on a low-contrast frame the shift search can lock onto a junk match at a
        # nonzero shift and misalign every cell. If the best shift's glyph-bearing cells don't average at
        # least this NCC, don't trust it: fall back to (0,0) (the read then honestly blanks / no_reads).
        self.shift_floor = float(shift_floor)
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
        t0 = next(iter(self.templates.values()), None)
        self._tsz = tuple(int(x) for x in t0.shape[-2:]) if t0 is not None else (16, 10)   # last 2 dims (multi-exemplar)

    @staticmethod
    def _ncc(c, tmpl):
        """NCC of a cell against a template that may be a (k,H,W) stack of EXPOSURE exemplars — take the
        MAX (a real crop matches its own exposure's sharp exemplar, not a blurred all-exposure mean)."""
        if getattr(tmpl, "ndim", 2) == 3:
            return max(ncc(c, tmpl[j]) for j in range(tmpl.shape[0]))
        return ncc(c, tmpl)

    @staticmethod
    def _best_exemplar(c, tmpl):
        if getattr(tmpl, "ndim", 2) == 3:
            return tmpl[max(range(tmpl.shape[0]), key=lambda k: ncc(c, tmpl[k]))]
        return tmpl

    def _discriminant(self, c, lab1, lab2):
        """Sign/strength of the cell along the DIFFERENCE of the two glyphs' best exemplars: correlation
        of the (mean-subtracted) cell with (e1 - e2). >0 = more like lab1 on the discriminating pixels;
        <0 = more like lab2. Concentrates the decision on where the pair differs (the 1-2px HEVC eats),
        instead of the shared bulk that whole-cell NCC is dominated by."""
        e1 = self._best_exemplar(c, self.templates[lab1]).astype(np.float32).ravel()
        e2 = self._best_exemplar(c, self.templates[lab2]).astype(np.float32).ravel()
        d = e1 - e2; d = d - d.mean()
        cc = c.astype(np.float32).ravel(); cc = cc - cc.mean()
        nd = float(np.sqrt((d * d).sum())); ncr = float(np.sqrt((cc * cc).sum()))
        return float((cc * d).sum() / (nd * ncr)) if nd > 1e-6 and ncr > 1e-6 else 0.0

    def _match(self, cell_img, labels):
        import cv2
        if cell_img.size == 0 or cell_img.shape[0] < 2 or cell_img.shape[1] < 2:
            return None, -2.0
        c = cv2.resize(cell_img, (self._tsz[1], self._tsz[0]))
        best, bs = None, -2.0
        for lab in labels:
            t = self.templates.get(lab)
            if t is not None:
                s = self._ncc(c, t)
                if s > bs:
                    best, bs = lab, s
        return best, bs

    def _blank_at_zero(self, panel_gray, base_cell, i):
        """Per-cell blank NCC judged at ZERO shift (base cell). The per-cell blank is STATIC FRAME content
        — the door edge in the hundreds cell does NOT jitter with the LED display — so it must be scored
        at the fixed frame position, never at the digits' searched shift (which would misalign it and
        wrongly flip blank<->glyph). Returns None if this cell has no blank template."""
        import cv2
        bt = self.blank_cells.get(i)
        if bt is None:
            return None
        b0 = crop(panel_gray, base_cell)
        if b0.size == 0 or b0.shape[0] < 2 or b0.shape[1] < 2:
            return -2.0
        return self._ncc(cv2.resize(b0, (self._tsz[1], self._tsz[0])), bt)

    def _cell_align(self, panel_gray, base_cell, dx, dy, i):
        """Glyph-match quality of a cell for the shift objective — GLYPH-BEARING cells only. Glyphs are
        scored at (base + shift) (they follow the display jitter); BLANK is judged at ZERO shift (static
        frame content, see _blank_at_zero). Returns None for a blank cell (low contrast OR blank beats
        every glyph) so the static edge can't dominate the rigid sum and pin a false [0,0] peak."""
        import cv2
        x, y, w, h = base_cell
        ci = crop(panel_gray, (x + dx, y + dy, w, h))
        if ci.size == 0 or ci.shape[0] < 2 or ci.shape[1] < 2:
            return None
        if (int(ci.max()) - int(ci.min())) < self.blank_range:
            return None                              # blank by contrast -> not glyph-bearing
        c = cv2.resize(ci, (self._tsz[1], self._tsz[0]))
        gbest = max((self._ncc(c, self.templates[g]) for g in self.glyph_labels), default=-2.0)
        b = self._blank_at_zero(panel_gray, base_cell, i)   # blank judged at ZERO shift
        if b is not None and b >= gbest:
            return None                              # per-cell blank wins -> not glyph-bearing
        return gbest

    def _best_shift(self, panel_gray):
        """Rigid (dx,dy) in [-R..R]^2 that best aligns the GLYPH-BEARING cells (the LED display translates
        as one). Blank cells are excluded (see _cell_align) so the static edge can't pin it. Ties keep
        (0,0); a shift with no glyph-bearing cell is rejected."""
        R = self.shift_search
        if R <= 0:
            return 0, 0

        def score(dx, dy):
            vals = [v for i, c in enumerate(self.digit_cells)
                    if (v := self._cell_align(panel_gray, c, dx, dy, i)) is not None]
            return (sum(vals), len(vals)) if vals else (-1e9, 0)
        best_s, (best, bn) = (0, 0), score(0, 0)
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                if dx == 0 and dy == 0:
                    continue
                s, n = score(dx, dy)
                if s > best:
                    best, bn, best_s = s, n, (dx, dy)
        # QUALITY GATE (Fix C): on a low-contrast frame the winning shift can be a junk match -> if its
        # glyph-bearing cells don't average shift_floor, don't trust the shift; fall back to (0,0).
        if best_s != (0, 0) and (bn == 0 or best / bn < self.shift_floor):
            return 0, 0
        return best_s

    def read_panel(self, panel_gray):
        import cv2
        dx, dy = self._best_shift(panel_gray)        # absorb rigid panel jitter before reading

        chars, scores, dbg = [], [], []              # dbg: per-cell top-2 + blank, EVERY read — the
                                                     # candidates column was ambiguous-only, which made
                                                     # the ch16/ch29 phantom-tens forensics impossible

        def _out(floor, direction, score, status, candidates=None):
            return {"floor": floor, "direction": direction, "score": score, "status": status,
                    "candidates": candidates, "cells": dbg,
                    "n_cells": len(self.digit_cells), "shift": [dx, dy]}

        for i, cell in enumerate(self.digit_cells):  # glyphs at the searched shift; blank at ZERO shift
            ci = crop(panel_gray, (cell[0] + dx, cell[1] + dy, cell[2], cell[3]))
            if ci.size == 0:
                return _out(None, None, None, "no_read")
            if (int(ci.max()) - int(ci.min())) < self.blank_range:
                dbg.append([["flat", round((int(ci.max()) - int(ci.min())) / 255.0, 3)]])
                continue                             # flat/dark cell -> BLANK (NCC undefined on 0 variance)
            c = cv2.resize(ci, (self._tsz[1], self._tsz[0]))
            top1 = top2 = -2.0
            lab1 = lab2 = None                       # top-2 glyph matches -> the margin check
            for g in self.glyph_labels:
                sc = self._ncc(c, self.templates[g])
                if sc > top1:
                    top2, lab2, top1, lab1 = top1, lab1, sc, g
                elif sc > top2:
                    top2, lab2 = sc, g
            # PER-CELL blank at ZERO shift (STATIC frame content — the door edge doesn't jitter with the
            # display; judging it at the digits' shift killed every nonzero-shift read). Blank may WIN only
            # if it beats every glyph AND either the cell isn't clearly LIT (dim/dark padding: contrast <
            # lit_range, blank >= blank_min) OR its blank is EXCEPTIONAL (>= blank_strong, the door edge).
            # A LIT cell (contrast >= lit_range) with a weak glyph + moderate blank -> no_read, NOT blank
            # (blanking a lit cell deletes the digit -> a confident misread, worse than an honest gap).
            lit = (int(ci.max()) - int(ci.min())) >= self.lit_range
            b = self._blank_at_zero(panel_gray, cell, i)
            # BLANK wins iff it beats every glyph AND: (dim cell) blank >= blank_min; OR (LIT cell) blank
            # is exceptional (the door edge) AND beats the glyph by blank_lit_margin — never delete a lit
            # digit on a blank that merely edges it (0.907 vs 0.83 = a deleted '6', worse than no_read).
            if b is not None and b >= top1 and (
                    (not lit and b >= self.blank_min)
                    or (lit and b >= self.blank_strong and b >= top1 + self.blank_lit_margin)):
                dbg.append([["blank", round(b, 3)], [lab1, round(top1, 3)]])
                continue                             # blank explains this cell -> BLANK
            dbg.append([[lab1, round(top1, 3) if top1 > -2 else None],
                        ([lab2, round(top2, 3)] if lab2 is not None else None),
                        ["blank", round(b, 3) if b is not None else None]])
            if lab1 is None or top1 < self.min_score:
                return _out(None, None, None, "no_read")   # a lit cell we can't confidently name
            # CONFUSABLE-PAIR diff-region tiebreak (opt-in, confuse_band>0): whole-cell NCC can pick the
            # wrong one of a similar pair (3/5, 8/6) that exemplars sharpened; re-decide on the pixels
            # where the two win-exemplars DIFFER. proj<0 -> the OTHER glyph; inconclusive + a true near-
            # tie -> ambiguous.
            if self.confuse_band > 0 and lab2 is not None and (top1 - top2) < self.confuse_band:
                proj = self._discriminant(c, lab1, lab2)
                if proj < -self.disc_min:
                    lab1, top1, lab2, top2 = lab2, top2, lab1, top1   # difference region overrules
                elif abs(proj) < self.disc_min and (top1 - top2) < self.margin_min:
                    return _out(None, None, round(top1, 3), "ambiguous",
                                candidates=[[lab1, round(top1, 3)], [lab2, round(top2, 3)]])
            # MARGIN CHECK: top-2 too close -> AMBIGUOUS (HEVC ate the discriminating segment). A gap is
            # honest; a confident 40-that-was-48 poisons attribution. Emit no_read + both candidates.
            elif self.margin_min > 0 and lab2 is not None and (top1 - top2) < self.margin_min:
                return _out(None, None, round(top1, 3), "ambiguous",
                            candidates=[[lab1, round(top1, 3)], [lab2, round(top2, 3)]])
            scores.append(top1)
            chars.append(lab1)                       # non-blank cells, left-to-right = the floor string
        if not chars:
            return _out(None, None, None, "no_read")
        direction = None
        if self.arrow_labels:                        # arrow shifts with the display too (rigid)
            acell = (self.arrow_cell[0] + dx, self.arrow_cell[1] + dy, self.arrow_cell[2], self.arrow_cell[3])
            arrow, arr_s = self._match(crop(panel_gray, acell), self.arrow_labels)
            if arrow is not None and arr_s >= self.min_score:
                # A SINGLE-CLASS CLASSIFIER OUTPUT IS NOT A MEASUREMENT. arrow_labels is filtered to
                # the arrows that have TEMPLATES, and a template only exists for an arrow the
                # operator labelled during calibration. If the labelled sample happened to catch the
                # lift travelling one way, only that arrow gets a template — and then this match can
                # only ever return that one direction, at any confidence, forever.
                # That is not hypothetical: ch27 was calibrated from crops labelled with 'V' (down)
                # only and reported 100% down; ch30 from '^' (up) only and reported 83% up / 0% down.
                # Both physically impossible, and both invisible downstream because nothing recorded
                # how many arrows the reader could even name. The score still contributes to
                # read_conf exactly as before, so FLOOR reading is unchanged — only the direction
                # claim is withheld.
                if len(self.arrow_labels) >= 2:
                    direction = arrow
                scores.append(arr_s)
        floor_str = "".join(chars)
        if self.valid_floors is not None and floor_str not in self.valid_floors:
            # Assembled to a floor this tower does not have -> not a confident read. Keep the string
            # and score for the spot-check page, but flag it so attribution and C21/C22 skip it.
            return _out(floor_str, direction, round(min(scores), 3), "off_alphabet")
        return _out(floor_str, direction, round(min(scores), 3), "ok")   # STRING: "25","P3","G"

    def debug_cells(self, panel_gray):
        """Diagnostic dump for one panel at the chosen shift: per digit-cell top-3 glyph scores + the
        per-cell blank score + contrast, and the arrow top-2. This is what pinpoints a bad read —
        shift != [0,0] with a wrong winner = misalignment; a wrong glyph winning by > margin at [0,0] =
        template confusion; a high blank score that lost = blank logic. Pure diagnostics, changes nothing."""
        import cv2
        dx, dy = self._best_shift(panel_gray)
        out = {"shift": [dx, dy], "cells": [], "arrow": None}
        for i, cell in enumerate(self.digit_cells):
            ci = crop(panel_gray, (cell[0] + dx, cell[1] + dy, cell[2], cell[3]))
            rec = {"i": i}
            if ci.size == 0:
                rec["verdict"] = "offpanel"; out["cells"].append(rec); continue
            rec["contrast"] = int(ci.max()) - int(ci.min())
            if rec["contrast"] < self.blank_range:
                rec["verdict"] = "blank(contrast)"; out["cells"].append(rec); continue
            c = cv2.resize(ci, (self._tsz[1], self._tsz[0]))
            scored = sorted(((round(self._ncc(c, self.templates[g]), 3), g) for g in self.glyph_labels), reverse=True)
            rec["top"] = [[g, s] for s, g in scored[:3]]
            b = self._blank_at_zero(panel_gray, cell, i)   # blank judged at ZERO shift (static edge)
            rec["blank"] = round(b, 3) if b is not None else None
            out["cells"].append(rec)
        acell = (self.arrow_cell[0] + dx, self.arrow_cell[1] + dy, self.arrow_cell[2], self.arrow_cell[3])
        ac = crop(panel_gray, acell)
        if ac.size and self.arrow_labels:
            cc = cv2.resize(ac, (self._tsz[1], self._tsz[0]))
            out["arrow"] = [[a, round(self._ncc(cc, self.templates[a]), 3)] for a in self.arrow_labels]
        return out

    @staticmethod
    def column_profile(reg_gray):
        """Per-column mean brightness — the diagnostic that settles whether a gap survives HEVC. A clean
        two-digit crop shows two bright humps with a dip between; a smeared one shows one broad hump."""
        return reg_gray.astype(np.float32).mean(axis=0)

    def reconcile(self, panel_reads):
        """AGREE-OR-DISCARD across the two panels: both status=='ok' + same floor -> confident. Anything
        else (a panel unread/ambiguous, or a floor disagreement) -> None. reads are read_panel dicts
        (which now always return a dict with 'status'; a None is tolerated for back-compat)."""
        ok = [r for r in panel_reads if r and r.get("status") == "ok" and r.get("floor")]
        if len(ok) < 2 or any(r["floor"] != ok[0]["floor"] for r in ok):
            return None
        dirs = [r["direction"] for r in ok if r["direction"]]
        direction = dirs[0] if dirs and all(d == dirs[0] for d in dirs) else None
        return {"floor": ok[0]["floor"], "direction": direction,
                "confidence": round(min(r["score"] for r in ok), 3), "panels": len(ok), "agree": True}


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


def _cluster(crops, K):
    """Up to K EXPOSURE exemplars for a glyph — k-means on the flattened crops (deterministic init). A
    single blurry MEAN averages across exposure conditions until a real crop matches it poorly (a true G
    scored 0.25 on the G-mean while matching the 6-mean at 0.72); per-exposure exemplars stay sharp and
    the reader takes MAX over them. Returns a (k, H, W) stack (k <= K; k = n when few crops)."""
    arrs = [np.asarray(c, np.float32) for c in crops]
    shp = arrs[0].shape
    X = np.stack([a.ravel() for a in arrs])
    n = len(X)
    if n <= K:
        return X.reshape(n, *shp)
    d0 = np.linalg.norm(X - X.mean(0), axis=1)                # spread the K seeds along the exposure axis
    cen = X[np.argsort(d0)[np.linspace(0, n - 1, K).astype(int)]].copy()
    for _ in range(8):
        a = ((X[:, None, :] - cen[None, :, :]) ** 2).sum(-1).argmin(1)
        newc = np.stack([X[a == k].mean(0) if np.any(a == k) else cen[k] for k in range(K)])
        if np.allclose(newc, cen):
            break
        cen = newc
    return cen.reshape(K, *shp)


def build_templates(labeled_panels, digit_cells, arrow_cell, tsz=(16, 10),
                    align="right", blank_label=BLANK, min_examples=3, exemplars=3):
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
    # DROP DEGENERATE glyphs: a lit glyph learned from < min_examples crops is unreliable — e.g. M from
    # ONE edge-dominated MEP crop becomes a blank-clone that scores ~0.96 on every hundreds cell and
    # misreads every floor as M0. Blank_<i> and arrows are exempt (structural / plentiful). A dropped
    # glyph just reads no_read until foldback grows it — honest, vs a confident wrong.
    templates, dropped = {}, {}
    for g, v in acc.items():
        exempt = g.startswith(blank_label + "_") or g in ARROWS
        if not exempt and len(v) < min_examples:
            dropped[g] = len(v); continue
        templates[g] = _cluster(v, exemplars)                # (k,H,W) exposure exemplars; reader maxes over them
    return templates, {"used": used, "skipped": skipped, "dropped": dropped, "min_examples": min_examples,
                       "exemplars": {g: int(t.shape[0]) for g, t in templates.items()},
                       "glyphs": {g: len(v) for g, v in acc.items()}}


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
                 door_tracker=None, floor_tracker=None, shift_search=2, margin_min=0.05,
                 blank_min=0.45, shift_floor=0.40, lit_range=120, blank_strong=0.90,
                 blank_lit_margin=0.15, confuse_band=0.0, disc_min=0.10, valid_floors=None,
                 state_tpl=None, state_meta=None):
        if not panels:
            raise ValueError("DoorFloorEngine needs at least one panel (panel_roi, digit_cells, arrow_cell)")
        self.door_roi = tuple(door_roi)
        # h3 state template. Present -> door_pass runs the NCC path and `door_tracker` must be a
        # DoorTrackerH3. Absent -> the edge-column path, unchanged. The band and the ROI x-slice come
        # from the ARTEFACT, not from a module constant, so the geometry travels with the pixels it
        # describes and a template can never be applied to a band it was not cut from.
        self.state_tpl = state_tpl
        self.state_meta = state_meta or {}
        if state_tpl is not None:
            if not isinstance(door_tracker, DoorTrackerH3):
                raise ValueError("a state template requires an explicit DoorTrackerH3 door_tracker; "
                                 f"got {type(door_tracker).__name__}")
            for k in ("band_y", "roi_x_w"):
                if k not in self.state_meta:
                    raise ValueError(f"state template meta missing {k!r}")
            self.state_band_y = tuple(self.state_meta["band_y"])
            self.state_roi_x_w = tuple(self.state_meta["roi_x_w"])
        else:
            self.state_band_y = self.state_roi_x_w = None
        self.valid_floors = set(valid_floors) if valid_floors else None
        self.readers = [(tuple(proi), FloorReader(templates, dcells, acell, min_score=min_score,
                                                  blank_range=blank_range, shift_search=shift_search,
                                                  margin_min=margin_min, blank_min=blank_min,
                                                  shift_floor=shift_floor, lit_range=lit_range,
                                                  blank_strong=blank_strong, blank_lit_margin=blank_lit_margin,
                                                  confuse_band=confuse_band, disc_min=disc_min,
                                                  valid_floors=valid_floors))
                        for (proi, dcells, acell) in panels]
        self.door = door_tracker if door_tracker is not None else DoorTracker()
        self.floor = floor_tracker if floor_tracker is not None else FloorTracker()
        self.hash = templates_hash(templates)

    def door_pass(self, gray, t):
        """THE DOOR HALF, isolated. -> (cycle, door_state_wire, openness, edge_strength).

        Both engines run through here and nothing else calls the trackers, so the offline acceptance
        harness can drive the SAME code the worker runs instead of importing a tracker and
        re-implementing the plumbing around it. That re-implementation is precisely how a graded
        engine and a deployed engine come to differ.

        h2 reads an edge column; h3 reads NCC against a closed template. `openness` is reported by
        both but is not the same instrument: h2's is the normalised edge column (which 5e58211 showed
        agrees with the physical door only ~65% of the time), h3's is 1 - normalised closedness off
        the NCC state signal that TEST A validated at AUC 1.000/0.958. It is an exact algebraic
        restatement of what h3 measures, not an inference — and door_version says which instrument
        produced any given row.
        """
        if self.state_tpl is not None:
            v = state_closedness(gray, self.state_band_y, self.state_roi_x_w, self.state_tpl)
            cycle = self.door.update(t, v)
            c = self.door.closedness(v) if v is not None else None
            openness = None if c is None else (1.0 - c)
            strength = 0.0 if v is None else float(v)
            return cycle, H3_STATE_TO_WIRE.get(self.door.state), openness, strength
        col, strength = door_edge_column(crop(gray, self.door_roi))
        cycle = self.door.update(t, col, strength)          # completed door cycle (close_travel) or None
        openness = self.door.openness(col) if col is not None else None
        return cycle, self.door.state, openness, strength

    def process(self, frame_bgr, t):
        import cv2
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY) if frame_bgr.ndim == 3 else frame_bgr
        cycle, door_state_wire, openness, strength = self.door_pass(gray, t)
        reads = [rdr.read_panel(crop(gray, proi)) for proi, rdr in self.readers]
        oks = [r for r in reads if r["status"] == "ok"]
        ambigs = [r for r in reads if r["status"] == "ambiguous"]
        floor = direction = conf = None
        agreed = False
        reason = "no_read"                                  # nothing matched — incl. MEP (M/E thin-known)
        candidates = None
        if len(self.readers) >= 2:
            rec = self.readers[0][1].reconcile(reads)       # agree-or-discard (status=='ok' both)
            if rec:
                floor, direction, conf, agreed, reason = rec["floor"], rec["direction"], rec["confidence"], True, "ok"
            elif len(oks) == 2:
                reason = "disagree"                         # both read but conflict -> discard, don't guess
            elif ambigs:
                reason, candidates = "ambiguous", ambigs[0]["candidates"]   # a cell's top-2 too close
        else:                                               # single-panel mode (panel1 cells not calibrated yet)
            r0 = reads[0]
            if r0["status"] == "ok":
                floor, direction, conf, agreed, reason = r0["floor"], r0["direction"], r0["score"], False, "single_panel"
            elif r0["status"] == "ambiguous":
                reason, candidates = "ambiguous", r0["candidates"]
        # DIAGNOSABILITY (2026-07-30): the candidates column was populated only on
        # reason='ambiguous', so confident phantoms ('1G', '7G') left no per-cell trace and
        # the DB could not answer "what did each cell score". When the ambiguous top-2 isn't
        # using the slot, carry panel0's per-cell top-2 + blank instead (a dict, so the two
        # shapes stay distinguishable). Pure metadata: floor/reason/conf are untouched, so
        # this does NOT move the door era.
        if candidates is None and reads and reads[0].get("cells"):
            candidates = {"cells": reads[0]["cells"], "shift": reads[0].get("shift")}
        stop = self.floor.update(t, floor, direction) if floor else None
        # n_arrow_labels: how many DISTINCT arrows this camera's reader can name at all. Below 2 the
        # direction field is suppressed above, and this column is what lets a consumer tell
        # "this lift went down" from "this camera can only say down".
        n_arrow_labels = len(self.readers[0][1].arrow_labels) if self.readers else 0
        out = {"t": t, "floor": floor, "direction": direction, "door_state": door_state_wire,
               "n_arrow_labels": n_arrow_labels,
               "openness": (round(float(openness), 3) if openness is not None else None),
               "read_conf": (round(float(conf), 3) if conf is not None else None),
               "panels_agreed": agreed, "reason": reason, "n_panels": len(self.readers),
               "edge_strength": round(float(strength), 3), "cycle": cycle, "stop": stop,
               "candidates": candidates, "shift": reads[0].get("shift")}
        if cycle:
            out["close_travel_s"] = cycle.get("close_travel_s")
        return out
