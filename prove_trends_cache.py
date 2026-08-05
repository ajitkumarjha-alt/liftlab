"""A/B proof for the trends cache path, run against the Litestream-restored gateway.db on dev-box.

Compares the OLD inline path (_tier2 derived on the request) against the NEW cache path
(_aggregate_read), and the unbounded vs bounded SQL loads. Reports wall seconds and a field-by-field
diff — equality is the claim, so it must be shown per field, not asserted.
"""
import json, os, sys, time
sys.path.insert(0, "/opt/liftlab-reports")          # liftlab_report + deps
sys.path.insert(0, os.environ.get("DASH_DIR", "/tmp/dashsrc"))
os.environ.setdefault("GATEWAY_DB", "/var/lib/liftlab/gateway.db")
import dash_api as D

GW, CAM = "site-A", os.environ.get("PROVE_CAM", "ch27")
db = D._db()
st = {}
try:
    with open("/var/lib/liftlab/restore_state.json") as fh: st = json.load(fh)
except Exception: pass
print(f"data as of: {st.get('data_max_ts_h')} IST (restored {st.get('restored_at_h')}), cam={CAM}")
print()

# ── A: OLD PATH — derive tier2 inline for the default window ──────────────────
t0, t1, _ = D._window(D.WINDOW_DAYS)
a = time.time()
tj = D._transits_for_join(db, GW)
old = D._tier2(db, GW, CAM, tj.get(CAM, []), t0, t1)
t_old = time.time() - a

# ── B: NEW PATH — read the stored aggregate ───────────────────────────────────
a = time.time()
_dg, new, meta = D._aggregate_read(db, GW, CAM, D.WINDOW_DAYS)
t_new = time.time() - a

print(f"A  inline _tier2 (old path)   {t_old:8.2f}s")
print(f"B  _aggregate_read (cache)    {t_new:8.4f}s   -> {t_old/max(t_new,1e-6):,.0f}x")
print(f"   aggregate computed_at={meta.get('computed_at')} state={meta.get('state')} "
      f"compute_ms={meta.get('compute_ms')}")
print()
if new is None:
    print("  NO STORED AGGREGATE -> cache path would fall through to live. Cannot diff."); raise SystemExit(1)

# ── field-by-field diff ───────────────────────────────────────────────────────
def flat(d, pre=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items(): out.update(flat(v, f"{pre}.{k}" if pre else str(k)))
    elif isinstance(d, list):
        out[pre] = f"<list n={len(d)}>"
        for i, v in enumerate(d[:400]): out.update(flat(v, f"{pre}[{i}]"))
    else: out[pre] = d
    return out
fo, fn = flat(old or {}), flat(new)
keys = sorted(set(fo) | set(fn))
same = [k for k in keys if fo.get(k) == fn.get(k)]
diff = [k for k in keys if fo.get(k) != fn.get(k)]
print(f"tier2 payload fields: {len(keys)}   equal: {len(same)}   DIFFERENT: {len(diff)}")
for k in diff[:25]:
    print(f"   {k}\n      old={str(fo.get(k))[:96]}\n      new={str(fn.get(k))[:96]}")
if len(diff) > 25: print(f"   ... and {len(diff)-25} more")
db.close()
