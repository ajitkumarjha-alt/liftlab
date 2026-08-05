import os, sys, datetime as dt
sys.path.insert(0,"/opt/liftlab-reports"); sys.path.insert(0,"/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB","/var/lib/liftlab/gateway.db")
import dash_api as D
GW,CAM="site-A","ch27"
db=D._db()
r=db.execute("SELECT computed_at,compute_ms FROM door_aggregate WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()
ca, dur = r["computed_at"], r["compute_ms"]/1000.0
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
tj=D._transits_for_join(db,GW).get(CAM,[])
CENSUS={"eras","eras_age_s","eras_computed_at","era_span","floor_alphabet_meta","floor_alphabet_source"}
def is_census(k): return any(k==c or k.startswith(c+".") or k.startswith(c+"[") for c in CENSUS)
print(f"aggregate computed_at={ca:.0f}  compute_ms={r['compute_ms']} ({dur:.0f}s)\n")
print(f"{'t0 assumption':44} {'diff':>6} {'census':>7} {'DATA':>6}")
for label,t0,t1 in (
    ("computed_at - 7d            (what I used)", ca-7*86400, ca),
    ("computed_at - dur - 7d      (walk START)", ca-dur-7*86400, ca),
    ("computed_at - dur - 7d, t1=start", ca-dur-7*86400, ca-dur),
):
    rp=D._tier2(db,GW,CAM,tj,t0,t1)
    fa,fb=flat(cached),flat(rp); ks=sorted(set(fa)|set(fb))
    d=[k for k in ks if fa.get(k)!=fb.get(k)]
    cen=[k for k in d if is_census(k)]; dat=[k for k in d if not is_census(k)]
    print(f"{label:44} {len(d):>6} {len(cen):>7} {len(dat):>6}")
    if len(dat)<=6 and dat:
        for k in dat: print(f"      {k}: cached={str(fa.get(k))[:44]} repro={str(fb.get(k))[:44]}")
db.close()
