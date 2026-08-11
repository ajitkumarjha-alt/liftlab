"""
Counting — zone-transit, not line-crossing.

A single line is unreliable: people loiter in doorways, drift across, and a
foot-point that jitters over the line produces phantom transits. The brief's
rule: a transit counts only when a track enters zone A and then reaches
zone B, in order. That is the difference between 92% and 99% precision.

Two zones, drawn once per camera (every cabin's angle differs):
    zone_landing  — outside the door, on the landing
    zone_cabin    — inside the cabin

    landing -> cabin  = BOARDED
    cabin -> landing  = ALIGHTED

A track must be seen in the origin zone for `min_frames` consecutive samples
before its entry into the destination zone counts. This kills jitter.

The detector is pluggable. YoloDetector is the production path (YOLO11n +
ByteTrack). MockDetector exists so the transit logic can be tested without
weights, and so counting logic is verified independently of detection quality.

PRIVACY: detections are (bbox, track_id) only. No face detection, no
embeddings, no crops. Frames are read and discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

# Identifies the counting LOGIC. Bump this whenever a change alters what gets counted (zones,
# dwell/displacement guards, tracker). A validation verdict is only valid for the version it was
# made against — precision must reset when this changes (same principle as the CLOSE_TRAVEL_MAX
# comparability boundary). The cloud's COUNTING_VERSION env MUST match this string.
COUNTING_VERSION = "2026-07-28-registry-zones"  # per-camera zones via the registry; wrong-polygon fallback removed


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: int
    conf: float = 1.0          # detector confidence; carried for the detection audit, NOT used in counting

    @property
    def foot(self) -> tuple[float, float]:
        """Bottom-centre: where the person meets the floor. Crosses a
        threshold far more cleanly than a box centroid."""
        return ((self.x1 + self.x2) / 2.0, self.y2)


class Detector(Protocol):
    def track(self, frame: np.ndarray) -> list[Detection]: ...


class YoloDetector:
    """Production detector. Weights download on first use."""

    def __init__(self, weights: str = "yolo11n.pt", conf: float = 0.35,
                 tracker: str = "bytetrack.yaml", device=None):
        from ultralytics import YOLO
        # Exported engines (.engine/.onnx) carry no task metadata — ultralytics guesses 'detect'
        # with a warning. Pin it instead of trusting the guess. Same suffix gate as below: the .pt
        # production path is byte-identical.
        if str(weights).endswith(".pt"):
            self.model = YOLO(weights)
        else:
            self.model = YOLO(weights, task="detect")
        # .to() is a PyTorch-weights operation: exported engines raise TypeError on it and take
        # their device at predict time instead — which track() below already provides via
        # kw["device"].
        if device is not None and str(weights).endswith(".pt"):
            self.model.to(device)          # move weights onto the GPU; Pi/desk-rig pass None = CPU
        self.conf = conf
        self.tracker = tracker
        self.device = device
        self.last_raw_n = 0                # boxes detected on the last frame, BEFORE the id gate
        self.last_raw_hi = 0               # ...of which conf >= 0.5 (ByteTrack's first-association tier)

    def track(self, frame: np.ndarray) -> list[Detection]:
        kw = dict(classes=[0], persist=True, verbose=False, tracker=self.tracker, conf=self.conf)
        if self.device is not None:
            kw["device"] = self.device     # ensure INFERENCE runs on the GPU, not CPU (track() default is CPU)
        r = self.model.track(frame, **kw)[0]
        # Raw counts BEFORE the id gate. ByteTrack can wedge (NaN-poisoned Kalman state after a
        # corrupt frame) into emitting boxes with id=None forever — at the return value that is
        # indistinguishable from an empty scene, which is how counting stops cold while door
        # analysis flows (2026-07-29/30 wedges). last_raw_hi counts boxes confident enough that the
        # tracker SHOULD have IDed them; callers use a long dets-without-ids streak as the tell.
        self.last_raw_n = 0 if r.boxes is None else len(r.boxes)
        try:
            self.last_raw_hi = (0 if r.boxes is None or r.boxes.conf is None
                                else int((r.boxes.conf >= 0.5).sum()))
        except Exception:
            self.last_raw_hi = self.last_raw_n
        if r.boxes is None or r.boxes.id is None:
            return []
        n = len(r.boxes.id)
        confs = (r.boxes.conf.cpu().numpy() if r.boxes.conf is not None else [1.0] * n)
        out = []
        for box, tid, cf in zip(r.boxes.xyxy.cpu().numpy(),
                                r.boxes.id.cpu().numpy().astype(int), confs):
            out.append(Detection(*box.tolist(), track_id=int(tid), conf=float(cf)))
        return out


class MockDetector:
    """Replays a scripted list of per-frame detections. For tests only."""

    def __init__(self, script: list[list[Detection]]):
        self.script = script
        self.i = 0

    def track(self, frame) -> list[Detection]:
        d = self.script[self.i] if self.i < len(self.script) else []
        self.i += 1
        return d


Zone = list[tuple[float, float]]  # polygon, image coords


def rect_to_poly(rect: tuple[int, int, int, int]) -> Zone:
    x, y, w, h = rect
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def as_zone(z) -> Zone:
    """Accept either a 4-tuple rect or a list of (x,y) points."""
    if z and len(z) == 4 and all(isinstance(v, (int, float)) for v in z):
        return rect_to_poly(tuple(int(v) for v in z))
    return [(float(a), float(b)) for a, b in z]


def in_poly(pt: tuple[float, float], poly: Zone) -> bool:
    """Ray casting. These cabin cameras are heavily barrel-distorted — the
    door threshold bows across the frame — so zones must be polygons drawn on
    the distorted image, not axis-aligned rectangles."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x < xin:
                inside = not inside
    return inside


def _centroid(poly: Zone) -> tuple[float, float]:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@dataclass
class Transit:
    track_id: int
    direction: str  # 'in' (boarded) | 'out' (alighted)
    offset_s: float


@dataclass
class Rejection:
    """A track that entered the destination zone but was NOT counted — the guard's audit trail.
    achieved_frac is the FURTHEST the foot travelled during the attempt, as a fraction of the
    zone separation (the same scale as disp_frac). So `reason='displacement', achieved_frac=0.24`
    means "a crossing that moved 24% of the gap, killed because disp_frac=0.35". Collect these and
    the disp_frac / min_frames thresholds can be chosen from the real distribution, not guessed."""
    track_id: int
    origin: str          # 'landing' | 'cabin'
    dest: str
    reason: str          # 'displacement' (moved too little) | 'dwell' (too few frames in dest)
    achieved_frac: float
    dwell_frames: int    # max consecutive frames the track dwelled in the destination
    offset_s: float


@dataclass
class ZoneCounter:
    """Ordered A->B transit counter with dwell + displacement guards. A count requires: dwell
    min_frames in the ORIGIN (arm), then dwell min_frames in the DESTINATION, AND the foot moved at
    least disp_frac of the zone separation. This rejects the flicker we saw — a near-stationary
    person (or object) whose foot jitters across the boundary, counted as boarding then alighting
    every ~40s. A single boundary-crossing frame or a barely-moving track no longer counts."""
    zone_landing: Zone
    zone_cabin: Zone
    min_frames: int = 2
    disp_frac: float = 0.35            # min foot travel to count, as a fraction of the zone separation
    attempt_stale_s: float = 6.0       # a crossing-attempt whose track vanishes this long -> resolve as rejected

    def __post_init__(self):
        self.zone_landing = as_zone(self.zone_landing)
        self.zone_cabin = as_zone(self.zone_cabin)
        self._sep = _dist(_centroid(self.zone_landing), _centroid(self.zone_cabin))
        self._min_disp = self.disp_frac * self._sep

    _streak: dict[int, tuple[str, int]] = field(default_factory=dict)   # tid -> (zone, count)
    _armed: dict[int, tuple[str, tuple]] = field(default_factory=dict)  # tid -> (origin zone, foot)
    _attempt: dict[int, dict] = field(default_factory=dict)            # tid -> in-progress crossing attempt
    transits: list[Transit] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)          # would-be crossings the guards killed

    def _zone_of(self, det: Detection) -> str | None:
        f = det.foot
        if in_poly(f, self.zone_cabin):
            return "cabin"
        if in_poly(f, self.zone_landing):
            return "landing"
        return None

    def cabin_ids(self, dets: list[Detection]) -> set:
        """Distinct track_ids whose FOOT is inside zone_cabin on this frame.

        This is the per-frame input to PEAK CAR OCCUPANCY. It reuses _zone_of, so occupancy and
        counting agree by construction about what "in the cabin" means — two definitions of the
        same boundary would eventually disagree and nobody would know which was right.

        IT IS A FLOOR, NOT A COUNT. A person the detector missed — occluded behind another body,
        which is exactly what happens in a full car — is not here. Every consumer of this number
        must say "measured minimum".
        """
        return {d.track_id for d in dets if self._zone_of(d) == "cabin"}

    def _flush_attempt(self, tid: int) -> None:
        """A crossing attempt ended (track left the zones or vanished) without being counted.
        Record WHY and how far it got, so rejected crossings become data, not silence."""
        att = self._attempt.pop(tid, None)
        if att is None:
            return
        reason = "dwell" if att["dwell"] < self.min_frames else "displacement"
        self.rejections.append(Rejection(tid, att["origin"], att["dest"], reason,
                                          att["frac"], att["dwell"], att["offset"]))

    def update(self, dets: list[Detection], offset_s: float) -> None:
        # resolve attempts whose track the detector lost mid-crossing (never returned to origin/None)
        for tid in list(self._attempt):
            if offset_s - self._attempt[tid]["offset"] > self.attempt_stale_s:
                self._flush_attempt(tid)

        for d in dets:
            z = self._zone_of(d)
            if z is None:
                # outside both zones: the track left -> any crossing attempt is abandoned (logged)
                self._flush_attempt(d.track_id)
                self._streak.pop(d.track_id, None)
                continue

            prev_zone, count = self._streak.get(d.track_id, (z, 0))
            count = count + 1 if prev_zone == z else 1
            self._streak[d.track_id] = (z, count)

            armed = self._armed.get(d.track_id)
            if armed is None:
                if count >= self.min_frames:
                    self._armed[d.track_id] = (z, d.foot)      # arm + remember WHERE it dwelled
                continue
            origin, armed_foot = armed

            if z == origin:
                # back in / still in the origin zone: a destination attempt is abandoned (logged)
                self._flush_attempt(d.track_id)
                continue

            # z != origin: the track is IN the destination zone -> a crossing attempt is underway.
            # Track its best (max) progress so a rejected crossing carries the frac it achieved.
            disp = _dist(d.foot, armed_foot)
            frac = disp / self._sep if self._sep else 0.0
            att = self._attempt.get(d.track_id)
            if att is None:
                self._attempt[d.track_id] = {"origin": origin, "dest": z,
                                             "frac": frac, "dwell": count, "offset": offset_s}
            else:
                att["dest"] = z
                if frac > att["frac"]:
                    att["frac"] = frac
                if count > att["dwell"]:
                    att["dwell"] = count
                att["offset"] = offset_s

            # DWELL: min_frames in the destination (a single boundary-jitter frame no longer counts).
            # DISPLACEMENT: the foot must have actually travelled across, not oscillated in place.
            if count >= self.min_frames:
                if disp < self._min_disp:
                    continue                                   # barely moved -> flicker; logged on abandonment
                if origin == "landing" and z == "cabin":
                    self.transits.append(Transit(d.track_id, "in", offset_s))
                elif origin == "cabin" and z == "landing":
                    self.transits.append(Transit(d.track_id, "out", offset_s))
                else:
                    continue
                self._attempt.pop(d.track_id, None)            # counted -> not a rejection
                # re-arm at the new position so a genuine return trip is still countable
                self._armed[d.track_id] = (z, d.foot)
                self._streak[d.track_id] = (z, 1)

    def counts(self) -> tuple[int, int]:
        return (sum(t.direction == "in" for t in self.transits),
                sum(t.direction == "out" for t in self.transits))


def attribute_to_stops(transits: list[Transit], cycles, pad_s: float = 2.0):
    """Assign each transit to the door cycle whose open window contains it.
    Transits outside any window are returned separately — they are a signal,
    not noise (doors held open, detector error, someone riding through)."""
    assigned: dict[int, list[Transit]] = {i: [] for i in range(len(cycles))}
    orphans: list[Transit] = []
    for t in transits:
        hit = None
        for i, c in enumerate(cycles):
            if c.open_start_s - pad_s <= t.offset_s <= c.close_full_s + pad_s:
                hit = i
                break
        (assigned[hit] if hit is not None else orphans).append(t)
    return assigned, orphans
