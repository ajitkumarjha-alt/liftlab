import os, sys, json, datetime as dt
sys.path.insert(0,"/opt/liftlab-reports"); sys.path.insert(0,"/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB","/var/lib/liftlab/gateway.db")
import dash_api as D
IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
def h(e): return dt.datetime.fromtimestamp(e,IST).strftime("%m-%d %H:%M:%S") if e else "None"
GW,CAM="site-A","ch27"
db=D._db()
ca=db.execute("SELECT computed_at FROM door_aggregate WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()[0]
t0=ca-D.WINDOW_DAYS*86400.0
print("=== FALSIFIER 2: window-bound closure audit, every input _tier2 sees ===")
print(f"  aggregate computed_at = {h(ca)}   window t0 = {h(t0)}\n")
print(f"  {'input':38} {'bounded?':10} effective [t0,t1]")
print(f"  {'-'*38} {'-'*10} {'-'*40}")
print(f"  {'gw_door_event walk (_ts_clause)':38} {'YES':10} [{h(t0)}, {h(ca)}]")
print(f"  {'transits (in-_tier2 _in_range)':38} {'YES':10} [{h(t0)}, {h(ca)}]  (falsifier 1: identical)")
# the two STORED tables _tier2 reads — neither is range-bounded, each has its own clock
cen=db.execute("SELECT computed_at FROM era_census WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone() \
    if db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='era_census'").fetchone()[0] else None
alp=db.execute("SELECT derived_at, evidence_rows, alphabet FROM floor_alphabet WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()
print(f"  {'_era_census (STORED table)':38} {'NO':10} all-era; own clock computed_at={h(cen['computed_at']) if cen else 'n/a'}")
print(f"  {'_alphabet_read (STORED table)':38} {'NO':10} all-era; own clock derived_at={h(alp['derived_at']) if alp else 'n/a'}")
print()
if alp:
    drift = alp["derived_at"] - ca
    print(f"  alphabet derived_at MINUS aggregate computed_at = {drift:+.0f}s ({drift/60:+.1f} min)")
    print(f"  alphabet evidence_rows now = {alp['evidence_rows']}   admitted floors = {len(json.loads(alp['alphabet']))}")
    if drift > 0:
        print("  >>> THE ALPHABET WAS RE-DERIVED AFTER THE AGGREGATE WAS COMPUTED.")
        print("      _tier2 admits a read only if its floor is in the alphabet, so a re-derivation")
        print("      between the two runs changes confident_reads, off_alphabet_rejected and every")
        print("      per_floor bucket -- in EITHER direction, depending on which floors moved.")
        print("      This input is NOT window-bounded and cannot be closed by any [t0,t1].")
    else:
        print("  >>> alphabet predates the aggregate; not the mechanism")
db.close()
