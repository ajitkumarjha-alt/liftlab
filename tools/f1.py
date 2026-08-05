import os, sys, datetime as dt
sys.path.insert(0,"/opt/liftlab-reports"); sys.path.insert(0,"/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB","/var/lib/liftlab/gateway.db")
import dash_api as D
IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
def h(e): return dt.datetime.fromtimestamp(e,IST).strftime("%m-%d %H:%M:%S")
GW,CAM="site-A","ch27"
db=D._db()
ca=db.execute("SELECT computed_at FROM door_aggregate WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()[0]
t0=ca-D.WINDOW_DAYS*86400.0
# A: how aggregate_refresh builds it
A=D._transits_for_join(db,GW).get(CAM,[])
# B: how dash_trends builds it (bounded query + its own comprehension)
tr_w,tr_args=D._ts_clause(t0,ca)
tr=D._q(db,"SELECT ts, direction FROM transit_event WHERE gateway_id=? AND cam=?"+tr_w,(GW,CAM,*tr_args))
B=sorted((r["ts"],r["direction"]) for r in tr if r["ts"] is not None)
# _tier2 filters A by [t0,ca) internally, so compare like-for-like
A_f=[x for x in A if D._in_range(x[0],t0,ca)]
print("=== FALSIFIER 1: join-input identity (site-A/ch27) ===")
print(f"  A  _transits_for_join (aggregate_refresh path), unbounded : len={len(A)}")
print(f"  A' same, after _tier2's internal [t0,ca) filter           : len={len(A_f)}")
print(f"  B  dash_trends bounded query + comprehension              : len={len(B)}")
sa,sb=set(A_f),set(B)
only_a,only_b=sa-sb,sb-sa
print(f"  symmetric difference: {len(only_a)+len(only_b)}  (only in A': {len(only_a)}, only in B: {len(only_b)})")
for lbl,s in (("only in A'",only_a),("only in B",only_b)):
    for x in sorted(s)[:5]: print(f"    {lbl}: ts={x[0]:.3f} ({h(x[0])}) dir={x[1]}")
print()
if not only_a and not only_b:
    print("  IDENTICAL -> falsifier 1 DEAD. The two call sites build the same join input.")
else:
    print("  DIFFER -> re-running the field diff with the join input FORCED IDENTICAL ...")
    def flat(d,pre=""):
        o={}
        if isinstance(d,dict):
            for k,v in d.items(): o.update(flat(v,f"{pre}.{k}" if pre else str(k)))
        elif isinstance(d,list):
            o[pre]=f"<n={len(d)}>"
            for i,v in enumerate(d[:400]): o.update(flat(v,f"{pre}[{i}]"))
        else: o[pre]=d
        return o
    _dg,cached,_m=D._aggregate_read(db,GW,CAM,D.WINDOW_DAYS)
    rA=D._tier2(db,GW,CAM,A,t0,ca)
    rB=D._tier2(db,GW,CAM,B,t0,ca)
    fa,fb,fc=flat(cached),flat(rA),flat(rB)
    ks=sorted(set(fa)|set(fb)|set(fc))
    print(f"    cached vs recompute(A input): {sum(1 for k in ks if fa.get(k)!=fb.get(k))} differ")
    print(f"    cached vs recompute(B input): {sum(1 for k in ks if fa.get(k)!=fc.get(k))} differ")
    print(f"    recompute(A) vs recompute(B): {sum(1 for k in ks if fb.get(k)!=fc.get(k))} differ")
db.close()
