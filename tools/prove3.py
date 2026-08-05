import os, sys, time
sys.path.insert(0, "/opt/liftlab-reports"); sys.path.insert(0, "/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB", "/var/lib/liftlab/gateway.db")
import dash_api as D
GW, CAM = "site-A", "ch27"
db = D._db()
_dg, cached, meta = D._aggregate_read(db, GW, CAM, D.WINDOW_DAYS)
ca = meta["computed_at"]; t0 = ca - D.WINDOW_DAYS*86400.0
tj = D._transits_for_join(db, GW)
a=time.time(); repro = D._tier2(db, GW, CAM, tj.get(CAM, []), t0, ca); t=time.time()-a
def flat(d, pre=""):
    out={}
    if isinstance(d,dict):
        for k,v in d.items(): out.update(flat(v, f"{pre}.{k}" if pre else str(k)))
    elif isinstance(d,list):
        out[pre]=f"<n={len(d)}>"
        for i,v in enumerate(d[:400]): out.update(flat(v,f"{pre}[{i}]"))
    else: out[pre]=d
    return out
fa,fb=flat(cached),flat(repro); keys=sorted(set(fa)|set(fb))
# fields that are timestamps OF THE COMPUTATION, not of the data — these cannot match by definition
META={"eras_age_s","eras_computed_at","floor_alphabet_meta","floor_alphabet_source","era_span"}
def is_meta(k): return any(k==m or k.startswith(m+".") or k.startswith(m+"[") for m in META)
diff=[k for k in keys if fa.get(k)!=fb.get(k)]
data_diff=[k for k in diff if not is_meta(k)]
meta_diff=[k for k in diff if is_meta(k)]
print(f"CLOSED window [t0, computed_at]  re-derive {t:.1f}s")
print(f"  fields {len(keys)}  equal {len(keys)-len(diff)}  differ {len(diff)}")
print(f"    of which computation-metadata (age/computed_at/alphabet provenance): {len(meta_diff)}")
print(f"    of which DATA fields:                                               {len(data_diff)}")
for k in data_diff[:15]:
    print(f"      {k}: cached={str(fa.get(k))[:56]}  repro={str(fb.get(k))[:56]}")
print()
print("VERDICT:", "cache == fresh derivation over the same CLOSED window; all remaining diffs are"
      " computation-metadata. Cache path is sound." if not data_diff
      else f"{len(data_diff)} DATA fields still differ — investigate before shipping")
db.close()
