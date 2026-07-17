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


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: int

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
                 tracker: str = "bytetrack.yaml"):
        from ultralytics import YOLO
        self.model = YOLO(weights)
        self.conf = conf
        self.tracker = tracker

    def track(self, frame: np.ndarray) -> list[Detection]:
        r = self.model.track(frame, classes=[0], persist=True, verbose=False,
                             tracker=self.tracker, conf=self.conf)[0]
        if r.boxes is None or r.boxes.id is None:
            return []
        out = []
        for box, tid in zip(r.boxes.xyxy.cpu().numpy(),
                            r.boxes.id.cpu().numpy().astype(int)):
            out.append(Detection(*box.tolist(), track_id=int(tid)))
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

    def __post_init__(self):
        self.zone_landing = as_zone(self.zone_landing)
        self.zone_cabin = as_zone(self.zone_cabin)
        self._sep = _dist(_centroid(self.zone_landing), _centroid(self.zone_cabin))
        self._min_disp = self.disp_frac * self._sep

    _streak: dict[int, tuple[str, int]] = field(default_factory=dict)   # tid -> (zone, count)
    _armed: dict[int, tuple[str, tuple]] = field(default_factory=dict)  # tid -> (origin zone, foot)
    transits: list[Transit] = field(default_factory=list)

    def _zone_of(self, det: Detection) -> str | None:
        f = det.foot
        if in_poly(f, self.zone_cabin):
            return "cabin"
        if in_poly(f, self.zone_landing):
            return "landing"
        return None

    def update(self, dets: list[Detection], offset_s: float) -> None:
        for d in dets:
            z = self._zone_of(d)
            if z is None:
                # outside both zones: keep arming state, reset streak
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

            # DWELL: min_frames in the destination (a single boundary-jitter frame no longer counts).
            # DISPLACEMENT: the foot must have actually travelled across, not oscillated in place.
            if z != origin and count >= self.min_frames:
                if _dist(d.foot, armed_foot) < self._min_disp:
                    continue                                   # barely moved -> flicker, not a crossing
                if origin == "landing" and z == "cabin":
                    self.transits.append(Transit(d.track_id, "in", offset_s))
                elif origin == "cabin" and z == "landing":
                    self.transits.append(Transit(d.track_id, "out", offset_s))
                else:
                    continue
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
