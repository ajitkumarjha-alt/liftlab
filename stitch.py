"""
Wall-clock stitching across a camera's clips.

Motion-triggered NVR exports cut door events ACROSS file boundaries: one clip
ends mid-opening, the next begins mid-dwell. Detected per-file, each half is a
truncated event and yields NO complete cycle — the event is silently lost. So we
must merge first, detect second.

Each clip carries its own TimestampModel (frame -> offset) and a wall-clock start
(filename anchor, or OSD-verified). We map every frame of every clip to absolute
wall time, concatenate onto one sorted timeline, and detect cycles there.

A cycle whose window straddles a DATA HOLE (a stretch >1 s with no frames — the
gap between two motion exports, or a within-clip recording gap) cannot be
trusted: the ramp would be extrapolated across missing footage. Those are
rejected, not reported. Surviving cycles pass a physical quality filter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .doors import detect_cycles, DoorCycle
from .timestamps import TimestampModel

# Physical plausibility bounds for a real lift door leaf. The MAX values reject garbage
# (mis-detected cycles), NOT slow real events. The old CLOSE_TRAVEL_MAX=10.0 truncated a
# close distribution that was still climbing at 10s (median 5.42, n=264) — real interference
# (doors held/blocked while closing) was silently discarded, understating the interference
# rate. A door blocked 30s is a real, notable event, not noise. Env-overridable so the caps
# can be tuned against the observed distribution without a code change.
OPEN_TRAVEL_MIN = 0.3
OPEN_TRAVEL_MAX = float(os.environ.get("OPEN_TRAVEL_MAX", "15.0"))   # was 8.0; validate vs open dist
CLOSE_TRAVEL_MIN = 1.0
CLOSE_TRAVEL_MAX = float(os.environ.get("CLOSE_TRAVEL_MAX", "30.0"))  # was 10.0 (truncating real closes)
DATA_HOLE_S = 1.0                       # a gap wider than this is missing footage


@dataclass
class ClipSignal:
    """One clip's openness signal plus the wall clock of its frame 0."""
    uri: str
    model: TimestampModel
    signal: np.ndarray
    start_wall: datetime                # frame 0 wall time (filename or OSD)


@dataclass
class StitchedTimeline:
    """A merged, wall-clock-sorted openness signal. Quacks like a TimestampModel
    for detect_cycles: offset_at(i) returns seconds from timeline start."""
    offsets: np.ndarray                 # seconds from start_wall, ascending
    signal: np.ndarray
    start_wall: datetime
    holes: list[tuple[float, float]] = field(default_factory=list)
    sources: list[tuple[float, float, str]] = field(default_factory=list)  # (t0,t1,uri)

    @property
    def n_frames(self) -> int:
        return len(self.offsets)

    def offset_at(self, i: int) -> float:
        return float(self.offsets[i])

    def ts_at(self, i: int) -> datetime:
        return self.start_wall + timedelta(seconds=float(self.offsets[i]))

    def wall_of(self, offset_s: float) -> datetime:
        return self.start_wall + timedelta(seconds=offset_s)


def build_timeline(clips: list[ClipSignal]) -> StitchedTimeline:
    """Concatenate every clip's per-frame samples onto one wall-clock timeline."""
    clips = [c for c in clips if len(c.signal) > 0]
    if not clips:
        raise ValueError("no non-empty clips to stitch")
    start_wall = min(c.start_wall for c in clips)

    off_parts, sig_parts, sources = [], [], []
    for c in clips:
        base = (c.start_wall - start_wall).total_seconds()
        offs = np.array([c.model.offset_at(i) for i in range(len(c.signal))],
                        dtype=np.float64) + base
        off_parts.append(offs)
        sig_parts.append(np.asarray(c.signal, dtype=np.float32))
        sources.append((float(offs[0]), float(offs[-1]), c.uri))

    off = np.concatenate(off_parts)
    sig = np.concatenate(sig_parts)
    order = np.argsort(off, kind="mergesort")        # stable: preserve within-clip order
    off, sig = off[order], sig[order]

    # data holes = gaps between consecutive samples wider than DATA_HOLE_S.
    # This captures both the seam between two clips AND within-clip recording gaps.
    dh = np.diff(off)
    holes = [(float(off[i]), float(off[i + 1]))
             for i in np.where(dh > DATA_HOLE_S)[0]]
    return StitchedTimeline(off, sig, start_wall, holes, sources)


def _passes_quality(c: DoorCycle) -> bool:
    return (OPEN_TRAVEL_MIN < c.open_travel_s < OPEN_TRAVEL_MAX
            and c.transfer_s >= 0
            and CLOSE_TRAVEL_MIN < c.close_travel_s < CLOSE_TRAVEL_MAX)


def _straddles_hole(c: DoorCycle, holes: list[tuple[float, float]]) -> bool:
    for h0, h1 in holes:
        # overlap between [open_start, close_full] and (h0, h1)
        if c.open_start_s < h1 and h0 < c.close_full_s:
            return True
    return False


@dataclass
class StitchResult:
    timeline: StitchedTimeline
    raw: list[DoorCycle]                # all detected, before filtering
    clean: list[DoorCycle]              # BOTH edges valid (holes + physical bounds)
    rejected_hole: list[DoorCycle] = field(default_factory=list)
    rejected_quality: list[DoorCycle] = field(default_factory=list)
    # Edge-level validity, one entry per raw cycle: {open_ok, close_ok}.
    # A data hole in the PLATEAU does not invalidate a recorded close ramp —
    # motion exports routinely miss opening ramps (pre-roll) while the close is
    # cleanly on film. Validated per edge on the 17-clip night.
    edges: list[dict] = field(default_factory=list)

    @property
    def valid_close_travels(self) -> list[float]:
        return [c.close_travel_s for c, e in zip(self.raw, self.edges) if e["close_ok"]]


def detect_stitched(clips: list[ClipSignal], *, open_level: float = 0.5,
                    min_open_s: float = 1.0, raw: bool = False) -> StitchResult:
    """Merge the clips and detect door cycles on the wall-clock timeline.

    raw=True: clip signals are UNNORMALIZED gray-level distances against a shared
    per-camera baseline (see doors.raw_signal_2pass); normalization happens HERE,
    once, on the merged timeline. This is the correct mode for motion-triggered
    exports — per-clip normalization self-cancels on event-dominated clips."""
    tl = build_timeline(clips)
    if raw:
        from .doors import _normalize_openness
        tl.signal = _normalize_openness(tl.signal)
    raw_cycles = detect_cycles(tl.signal, tl, open_level=open_level, min_open_s=min_open_s)
    def _edge_hole_free(t0: float, t1: float) -> bool:
        return not any(t0 - 0.3 < h1 and h0 < t1 + 0.3 for h0, h1 in tl.holes)

    clean, rej_hole, rej_q, edges = [], [], [], []
    for c in raw_cycles:
        open_ok = (_edge_hole_free(c.open_start_s, c.open_full_s)
                   and OPEN_TRAVEL_MIN < c.open_travel_s < OPEN_TRAVEL_MAX
                   and c.transfer_s >= 0)
        close_ok = (_edge_hole_free(c.close_start_s, c.close_full_s)
                    and CLOSE_TRAVEL_MIN < c.close_travel_s < CLOSE_TRAVEL_MAX)
        edges.append({"open_ok": open_ok, "close_ok": close_ok})
        if open_ok and close_ok:
            clean.append(c)
        elif _straddles_hole(c, tl.holes):
            rej_hole.append(c)
        else:
            rej_q.append(c)
    return StitchResult(tl, raw_cycles, clean, rej_hole, rej_q, edges)


def stitch_camera(uris: list[str], roi: tuple[int, int, int, int], *,
                  models: dict | None = None, open_level: float = 0.5,
                  min_open_s: float = 1.0) -> StitchResult:
    """The correct end-to-end path for one camera's clip set:
    shared closed baseline from the LONGEST clip (doors shut most of the night)
    -> raw two-pass signal per clip -> merge -> normalize once -> detect."""
    from .timestamps import build_model
    from .doors import closed_baseline, raw_signal_2pass

    models = dict(models or {})
    for u in uris:
        if u not in models:
            models[u] = build_model(u)
    # Deterministic chronological order: at overlapping export seams the stable
    # merge preserves input order for equal wall-times, so caller-dependent uri
    # order would change detection at seams (observed: 7 vs 6 raw cycles).
    uris = sorted(uris, key=lambda u: models[u].start_ts)
    longest = max(uris, key=lambda u: models[u].n_frames)
    baseline, closed_floor = closed_baseline(longest, roi)
    clips = [ClipSignal(u, models[u],
                        raw_signal_2pass(u, roi, models[u], baseline, closed_floor),
                        models[u].start_ts)
             for u in uris]
    return detect_stitched(clips, open_level=open_level, min_open_s=min_open_s, raw=True)


def cycle_wall_times(tl: StitchedTimeline, c: DoorCycle) -> dict:
    """The four door timestamps of a cycle as absolute wall clocks."""
    return {
        "door_open_start": tl.wall_of(c.open_start_s),
        "door_open_full": tl.wall_of(c.open_full_s),
        "door_close_start": tl.wall_of(c.close_start_s),
        "door_close_full": tl.wall_of(c.close_full_s),
    }
