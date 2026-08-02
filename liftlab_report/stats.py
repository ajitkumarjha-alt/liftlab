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
