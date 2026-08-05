import json, os, sys, time
sys.path.insert(0, "/opt/liftlab-reports"); sys.path.insert(0, "/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB", "/var/lib/liftlab/gateway.db")
import dash_api as D
GW, CAM = "site-A", "ch27"
db = D._db()
_dg, cached, meta = D._aggregate_read(db, GW, CAM, D.WINDOW_DAYS)
ca = meta.get("computed_at")
print(f"aggregate computed_at={ca}  compute_ms={meta.get('compute_ms')}")
# the window the aggregate ACTUALLY describes: _window(7) evaluated AT computed_at
t0_cache = ca - D.WINDOW_DAYS * 86400.0
t0_now, _t1, _ = D._window(D.WINDOW_DAYS)
print(f"cache window t0  = {t0_cache:.0f}  ({D._iso_ist(t0_cache)})")
print(f"fresh window t0  = {t0_now:.0f}  ({D._iso_ist(t0_now)})")
print(f"difference       = {(t0_now-t0_cache)/3600:.2f} hours of extra history in the cache\n")
tj = D._transits_for_join(db, GW)
a=time.time(); repro = D._tier2(db, GW, CAM, tj.get(CAM, []), t0_cache, None); t=time.time()-a
def flat(d, pre=""):
    out={}
    if isinstance(d,dict):
        for k,v in d.items(): out.update(flat(v, f"{pre}.{k}" if pre else str(k)))
    elif isinstance(d,list):
        out[pre]=f"<n={len(d)}>"
        for i,v in enumerate(d[:400]): out.update(flat(v,f"{pre}[{i}]"))
    else: out[pre]=d
    return out
fa,fb = flat(cached), flat(repro)
keys=sorted(set(fa)|set(fb))
diff=[k for k in keys if fa.get(k)!=fb.get(k)]
print(f"re-derived with the CACHE'S OWN t0 ({t:.1f}s): {len(keys)} fields, "
      f"equal {len(keys)-len(diff)}, DIFFERENT {len(diff)}")
for k in diff[:12]:
    print(f"   {k}: cached={str(fa.get(k))[:60]}  repro={str(fb.get(k))[:60]}")
print()
print("VERDICT:", "cache is byte-equal to a fresh derivation over the SAME window -> the only"
      " difference from a NOW-window run is the rolling offset, fully explained"
      if not diff else "UNEXPLAINED DIFFERENCES REMAIN — do not ship the cache path")
db.close()
