"""C27 SAMPLING-BIAS TEST.

Hypothesis: door cycles only complete when the closing motion was sampled, and a SLOW close is more
likely to be caught by a sparse sampler than a fast one (it occupies more sampling opportunities).
If true, the completed-cycle pool is biased toward slow closes and the C27 median is biased HIGH.

Test: for every cycle in the EXACT pool C27 is computed from (clean_close, close_travel_s >= the
one-frame quantization floor, gap-excluded, per camera+era), measure how densely the door stream was
sampled around that cycle, then compare close_travel_s across density strata.

If the mechanism bites: sparsely-sampled cycles show systematically LONGER closes.
If it does not: the strata agree, and C27 survives.
"""
import sys
from datetime import datetime

sys.path.insert(0, "/home/ajit_kumarjha_lodhagroup_com/projects/liftlab")
from liftlab_report import eras, reader, stats  # noqa: E402

DB = sys.argv[1]
GW = "site-A"
DAYS = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0

db = reader.open_ro(DB)
t1 = db.execute("SELECT MAX(ts) FROM gw_door_event").fetchone()[0]
t0 = t1 - DAYS * 86400
rows = reader.fetch_gpu_rows(db, GW, t0, t1)
cycles, _f = reader.fetch_gpu_cycles(db, GW, t0, t1)
db.close()

FLOOR = eras.MIN_PLAUSIBLE_CLOSE_S

# per-camera ascending timestamps of EVERY door observation, for density lookups
obs = {}
for r in rows:
    if r["ts"] is None:
        continue
    obs.setdefault(r["cam"], []).append(float(r["ts"]))
for v in obs.values():
    v.sort()


def density(cam, a, b):
    """Sampling around one cycle: n observations in [a,b], and the LARGEST hole inside it.

    The largest hole is the operative number: close_travel is bounded by the two samples that
    bracket the descent, so one big hole is what makes a fast close unmeasurable (and therefore
    absent from the pool) or a measured value coarse."""
    ts = obs.get(cam) or []
    lo, hi = 0, len(ts)
    while lo < hi:
        m = (lo + hi) // 2
        if ts[m] < a:
            lo = m + 1
        else:
            hi = m
    i = lo
    inside = []
    while i < len(ts) and ts[i] <= b:
        inside.append(ts[i]); i += 1
    if len(inside) < 2:
        return len(inside), (b - a) if b > a else None
    gaps = [y - x for x, y in zip(inside, inside[1:])]
    return len(inside), max(gaps)


pool = []
for c in cycles:
    if not (t0 <= c["ts"] < t1) or eras.in_gap(c["cam"], c["ts"]):
        continue
    if not c.get("clean_close") or c.get("close_travel_s") is None:
        continue
    v = float(c["close_travel_s"])
    if v < FLOOR:
        continue
    a = c["ts"]
    b = c.get("close_ts") or c["ts"]
    n, maxgap = density(c["cam"], a - 2.0, b + 2.0)   # +/-2s so the bracketing samples count
    # CONFOUND CONTROL. max_gap measured INSIDE the cycle window is contaminated by construction: a
    # longer close means a longer window, which gives a large hole more opportunity to occur. So
    # also measure AMBIENT sampling density in a FIXED 60s window ENDING when the cycle opens —
    # same length for every cycle, and causally prior to the close it is being correlated with.
    n_amb, gap_amb = density(c["cam"], a - 60.0, a)
    pool.append({"cam": c["cam"], "era": c.get("era_id"), "ct": v,
                 "n_obs": n, "max_gap": maxgap,
                 "amb_n": n_amb, "amb_gap": gap_amb,
                 "dur": max(0.001, b - a)})

print(f"range: {DAYS:g}d  pool (the exact C27 pool): n={len(pool)}  floor={FLOOR}s")
if not pool:
    sys.exit("empty pool")


def med_ci(vals):
    sv = sorted(vals)
    d = stats.median_ci(sv)
    return d.get("median"), d.get("lo"), d.get("hi"), len(sv)


def strata(items, keyfn, label, nbins=4):
    have = [i for i in items if keyfn(i) is not None]
    have.sort(key=keyfn)
    if len(have) < 4 * 8:
        print(f"  {label}: too few ({len(have)}) to stratify")
        return
    print(f"  {label}  (bins from densest to sparsest)")
    per = len(have) // nbins
    prev_med = None
    for k in range(nbins):
        chunk = have[k * per:(k + 1) * per] if k < nbins - 1 else have[k * per:]
        m, lo, hi, n = med_ci([c["ct"] for c in chunk])
        kv = [keyfn(c) for c in chunk]
        arrow = ""
        if prev_med is not None and m is not None:
            arrow = f"   delta vs densest {m - prev_med:+.3f}s" if k else ""
        print(f"    bin{k + 1}  {label}={kv[0]:.2f}..{kv[-1]:.2f}   n={n:<5d} "
              f"median={m:.3f}s  CI[{lo if lo is None else round(lo,3)}, "
              f"{hi if hi is None else round(hi,3)}]{arrow}")
        if k == 0:
            prev_med = m


def spearman(xs, ys):
    def rank(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


print()
print("=== FLEET ===")
strata(pool, lambda c: c["max_gap"], "max sampling hole (s)")
have = [c for c in pool if c["max_gap"] is not None]
print(f"    Spearman rho(max_gap, close_travel_s) = {spearman([c['max_gap'] for c in have], [c['ct'] for c in have]):+.3f}  n={len(have)}")

print()
print("=== FLEET, CONFOUND-CONTROLLED (ambient density in a fixed 60s window BEFORE the cycle) ===")
amb = [c for c in pool if c["amb_gap"] is not None and c["amb_n"] >= 2]
print(f"    cycles with a usable prior window: {len(amb)}")
strata(amb, lambda c: c["amb_gap"], "ambient max hole before cycle (s)")
if amb:
    print(f"    Spearman rho(ambient_gap, close_travel_s) = "
          f"{spearman([c['amb_gap'] for c in amb], [c['ct'] for c in amb]):+.3f}")
    print(f"    Spearman rho(ambient_n,   close_travel_s) = "
          f"{spearman([float(c['amb_n']) for c in amb], [c['ct'] for c in amb]):+.3f}  (expect NEGATIVE if bias is real)")
print()
print("    control: does the IN-CYCLE gap simply track duration? (if rho ~ 1, that metric is circular)")
hv = [c for c in pool if c["max_gap"] is not None]
print(f"    Spearman rho(max_gap, cycle_duration) = {spearman([c['max_gap'] for c in hv], [c['dur'] for c in hv]):+.3f}")

print()
print("=== PER CAMERA ===")
for cam in sorted({c["cam"] for c in pool}):
    sub = [c for c in pool if c["cam"] == cam and c["max_gap"] is not None]
    if len(sub) < 32:
        m, lo, hi, n = med_ci([c["ct"] for c in sub])
        print(f"  {cam}: n={n} too few to stratify (median={m})")
        continue
    rho = spearman([c["max_gap"] for c in sub], [c["ct"] for c in sub])
    m_all, lo_all, hi_all, n_all = med_ci([c["ct"] for c in sub])
    print(f"  {cam}: n={n_all}  median={m_all:.3f}s CI[{round(lo_all,3)}, {round(hi_all,3)}]  rho={rho:+.3f}")
    strata(sub, lambda c: c["max_gap"], "    max hole (s)")
