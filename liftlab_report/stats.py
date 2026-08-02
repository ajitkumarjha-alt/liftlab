"""Small-sample statistics, stdlib only.

Every function returns None (or a None-carrying dict) on insufficient n rather
than raising — a figure without n is a defect, but a crash on an empty era is
a worse one. CIs:
  median   — distribution-free order-statistic interval (binomial, z approx)
  mean     — normal approximation (t not available without scipy; at the n
             where the decision matters (>30) the difference is cosmetic)
  proportion — Wilson score interval
"""

from __future__ import annotations

import math
from collections import Counter

Z95 = 1.959963984540054


def pctl(sorted_vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile on an ASCENDING list (same convention as the
    dashboard's _pctl, so workbook numbers tie out against /dash)."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]


def mean_sd(vals: list[float]) -> tuple[float | None, float | None]:
    n = len(vals)
    if n == 0:
        return None, None
    m = sum(vals) / n
    if n < 2:
        return m, None
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return m, math.sqrt(var)


def mean_ci(vals: list[float]) -> dict:
    """{'mean','lo','hi','n'} — 95% normal-approx CI; lo/hi None when n<2."""
    n = len(vals)
    m, sd = mean_sd(vals)
    if m is None:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    if sd is None:
        return {"mean": m, "lo": None, "hi": None, "n": n}
    half = Z95 * sd / math.sqrt(n)
    return {"mean": m, "lo": m - half, "hi": m + half, "n": n}


def median_ci(sorted_vals: list[float]) -> dict:
    """Distribution-free 95% CI for the median via binomial order statistics
    (normal approximation to the binomial ranks). Needs n>=6 for a two-sided
    interval; below that lo/hi are None and the verdict must stay 'keep
    collecting'."""
    n = len(sorted_vals)
    med = pctl(sorted_vals, 0.5)
    if n < 6:
        return {"median": med, "lo": None, "hi": None, "n": n}
    lo_rank = int(math.floor(n / 2 - Z95 * math.sqrt(n) / 2))
    hi_rank = int(math.ceil(1 + n / 2 + Z95 * math.sqrt(n) / 2))
    lo_rank = max(1, lo_rank)
    hi_rank = min(n, hi_rank)
    return {"median": med, "lo": sorted_vals[lo_rank - 1],
            "hi": sorted_vals[hi_rank - 1], "n": n}


def wilson_ci(k: int, n: int) -> dict:
    """95% Wilson score interval for a proportion. {'pct','lo','hi','n'}."""
    if n == 0:
        return {"pct": None, "lo": None, "hi": None, "n": 0}
    p = k / n
    z2 = Z95 ** 2
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return {"pct": 100.0 * p, "lo": 100.0 * max(0.0, centre - half),
            "hi": 100.0 * min(1.0, centre + half), "n": n}


def verdict_vs_threshold(ci: dict, threshold: float) -> str:
    """Mechanical verdict, no editorialising. ci carries lo/hi (any scale)."""
    lo, hi = ci.get("lo"), ci.get("hi")
    if lo is None or hi is None:
        return "CI not computable at this n — keep collecting"
    if hi < threshold:
        return "CI clears threshold (below)"
    if lo > threshold:
        return "CI clears threshold (above)"
    return "CI straddles threshold — keep collecting"


BELOW, AT, ABOVE = "below", "at", "above"


def compare_to_threshold(value: float | None, threshold: float,
                         decimals: int = 2) -> str | None:
    """Three-way comparison AT THE PRECISION THE READER SEES.

    A median of 2.3149 displayed as "2.31" against a 2.31 threshold is not
    above it — reporting it as above is a claim the displayed digits do not
    support. Comparing rounded-to-displayed values makes the sentence agree
    with the number printed beside it.

    Returns 'below' | 'at' | 'above', or None when there is no value."""
    if value is None:
        return None
    v, t = round(float(value), decimals), round(float(threshold), decimals)
    if v < t:
        return BELOW
    if v > t:
        return ABOVE
    return AT


# An interval this wide relative to its own point estimate does not support
# stating that estimate as a result. At 0.5, a 2.75s median with an interval of
# [1.52, 4.25] (width 2.73, ratio 0.99) is suppressed: that range is consistent
# both with a lift comfortably under the assumed value and one far over it, so
# the midpoint is not a finding. The earlier 1.0 threshold admitted exactly that
# case.
MAX_CI_WIDTH_RATIO = 0.5


def interval_is_uninformative(ci: dict, min_n: int = 30,
                              max_width_ratio: float = MAX_CI_WIDTH_RATIO) -> bool:
    """True when a point estimate from this interval must NOT be stated as a
    result.

    Two ways an interval fails to support a value: too few observations, or a
    width large relative to the value itself. Printing a midpoint drawn from a
    wide interval in the same sentence shape used for a well-measured lift
    invites the reader to treat the two as comparable when they are not."""
    n = ci.get("n") or 0
    if n < min_n:
        return True
    lo, hi, point = ci.get("lo"), ci.get("hi"), ci.get("median")
    if point is None:
        point = ci.get("mean")
    if lo is None or hi is None or point is None:
        return True
    if point == 0:
        return True
    return (hi - lo) > max_width_ratio * abs(point)


def n_for_separation(ci: dict, threshold: float, value_key: str = "median"
                     ) -> int | None:
    """How many observations would be needed for this CI to clear `threshold`.

    The half-width of a 95% interval shrinks as 1/sqrt(n), so to shrink it from
    `half` to the distance `d` between the point estimate and the threshold
    takes n * (half/d)^2. Returns:
      * None when the CI already clears (nothing more is needed), when n is too
        small for a CI at all, or when the point estimate sits ON the threshold
        (no n separates a value from a line it lies on);
      * the projected n otherwise.

    This is a planning figure, not a promise: it assumes the spread of the data
    stays as it is and the point estimate does not move."""
    lo, hi = ci.get("lo"), ci.get("hi")
    point, n = ci.get(value_key), ci.get("n") or 0
    if lo is None or hi is None or point is None or n < 2:
        return None
    if hi < threshold or lo > threshold:
        return None                       # already separated
    d = abs(point - threshold)
    if d <= 0:
        return None
    half = max(point - lo, hi - point)
    if half <= d:
        return None
    return int(math.ceil(n * (half / d) ** 2))


# ── sampling-resolution (frame quantum) detection ────────────────────────────
# Durations derived from a frame-sampled video stream can only land on
# multiples of the frame interval 1/analyze_fps. That interval is NOT recorded
# per measurement (camera_registry.analyze_fps is 0.0 on the live gateway), so
# it is detected empirically from the observed values and re-detected on every
# run — if analyze_fps changes, the detected quantum follows it.

QUANTUM_MIN_N = 30          # below this, "no detectable quantum" is the honest answer
QUANTUM_MIN_COVER = 0.55    # share of values that must sit on the grid
QUANTUM_MIN_S = 0.005       # anything finer than 5 ms is jitter, not a frame interval


def grid_tol(q: float) -> float:
    """Absolute tolerance for 'on the grid at spacing q'. The observed jitter on
    the live gateway is ±1 ms on values written to 3 dp, so the floor is 3 ms."""
    return max(0.003, 0.02 * q)


def grid_coverage(values: list[float], q: float) -> float:
    """Share of `values` lying within tolerance of a POSITIVE multiple of q."""
    vals = [v for v in values if v and v > 0]
    if not vals or q <= 0:
        return 0.0
    tol = grid_tol(q)
    return sum(1 for v in vals
               if round(v / q) >= 1 and abs(v - round(v / q) * q) <= tol) / len(vals)


def detect_quantum(values: list[float], min_cover: float = QUANTUM_MIN_COVER,
                   min_n: int = QUANTUM_MIN_N) -> float | None:
    """Largest spacing q that explains >=min_cover of `values` as multiples.

    Candidates are the most frequent observed values and their adjacent
    differences (the "modal spacing" reading of the GCD idea) — a real frame
    quantum shows up both as the modal smallest duration and as the modal gap
    between adjacent modal values. Candidates within one tolerance of each
    other are merged, keeping the MOST FREQUENT member of the cluster: ±1 ms
    jitter puts 0.079 / 0.080 / 0.081 in one cluster and only the modal 0.080
    is the sampling interval. Each surviving candidate is then refined by least
    squares against the multiples it explains, because a 1 ms error in the
    estimate walks a whole tolerance off the grid by the 3rd multiple and would
    reject a quantum that is really there.

    The LARGEST qualifying q is returned because every divisor of a true
    quantum also fits the data; the divisors are not the sampling interval.

    Returns None when n is too small or nothing reaches min_cover — callers
    must then say the quantum is undetermined rather than assume one.
    """
    vals = [round(float(v), 3) for v in values if v and v > 0]
    if len(vals) < min_n:
        return None
    counts = Counter(vals)
    freq = [v for v, _n in counts.most_common(12)]
    cands = {v for v in freq if v >= QUANTUM_MIN_S}
    ordered = sorted(freq)
    for a, b in zip(ordered, ordered[1:]):
        d = round(b - a, 3)
        if d >= QUANTUM_MIN_S:
            cands.add(d)
    # cluster jitter-siblings, keep the modal member of each cluster
    merged: list[float] = []
    cluster: list[float] = []
    for q in sorted(cands):
        if cluster and q - cluster[0] <= grid_tol(q):
            cluster.append(q)
            continue
        if cluster:
            merged.append(max(cluster, key=lambda x: counts.get(x, 0)))
        cluster = [q]
    if cluster:
        merged.append(max(cluster, key=lambda x: counts.get(x, 0)))
    best = None
    for q in merged:
        q = _refine_quantum(vals, q)
        if grid_coverage(vals, q) >= min_cover and (best is None or q > best):
            best = q
    return best


def _refine_quantum(vals: list[float], q: float, rounds: int = 3) -> float:
    """Least-squares fit of q to the multiples it explains: q = Σk·v / Σk².

    Only values already near the grid vote, so outliers and off-grid drift do
    not drag the estimate. Returns q unchanged when nothing is on the grid."""
    for _ in range(rounds):
        tol = grid_tol(q)
        num = den = 0.0
        for v in vals:
            k = round(v / q)
            if k >= 1 and abs(v - k * q) <= tol:
                num += k * v
                den += k * k
        if den <= 0:
            return q
        nq = num / den
        if abs(nq - q) < 1e-6:
            return round(nq, 4)
        q = nq
    return round(q, 4)


def floor_share(values: list[float], quantum: float | None) -> dict:
    """How much of a pool is pinned at the one-quantum resolution floor.

    {'n_at_floor', 'n', 'frac', 'quantum'} — frac is None when no quantum was
    detected (the question is then unanswerable, not answered zero)."""
    vals = [float(v) for v in values if v and v > 0]
    if quantum is None or not vals:
        return {"n_at_floor": 0, "n": len(vals), "frac": None, "quantum": quantum}
    tol = grid_tol(quantum)
    k = sum(1 for v in vals if abs(v - quantum) <= tol)
    return {"n_at_floor": k, "n": len(vals), "frac": k / len(vals),
            "quantum": quantum}


def floor_suppression_note(kind: str, fs: dict, remedy: str) -> str:
    """The sentence that REPLACES a verdict when a pool is resolution-bound."""
    pct = 100.0 * (fs["frac"] or 0.0)
    return (f"not measurable — measurement at sampling-resolution floor "
            f"({pct:.0f}% of {kind} values at the {fs['quantum']:.2f}s quantum, "
            f"n={fs['n']}); {remedy}")


OPEN_TRAVEL_REMEDY = ("needs higher analyze_fps or a different open-detection "
                      "method")
CLOSE_TRAVEL_REMEDY = ("needs higher analyze_fps before a close-travel figure "
                       "from this pool can be quoted")


# Close-travel histogram bins. 2.00 (sheet assumption) and 2.31 (Bank C cliff)
# are bin EDGES so the assumption and the cliff are readable straight off the
# chart instead of buried inside a bin.
CLOSE_HIST_EDGES = [0.5, 1.0, 1.5, 2.0, 2.31, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
DWELL_HIST_EDGES = [2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0, 30.0, 60.0]


def hist(vals: list[float], edges: list[float]) -> list[int]:
    h = [0] * (len(edges) + 1)
    for v in vals:
        for i, e in enumerate(edges):
            if v < e:
                h[i] += 1
                break
        else:
            h[-1] += 1
    return h


def hist_labels(edges: list[float]) -> list[str]:
    labels = []
    prev = 0.0
    for e in edges:
        labels.append(f"{prev:g}–{e:g}")
        prev = e
    labels.append(f"{prev:g}+")
    return labels
