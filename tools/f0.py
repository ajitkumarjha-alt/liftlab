import json, os, sys, datetime as dt
sys.path.insert(0,"/opt/liftlab-reports"); sys.path.insert(0,"/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB","/var/lib/liftlab/gateway.db")
import dash_api as D
IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
def h(e): return dt.datetime.fromtimestamp(e,IST).strftime("%Y-%m-%d %H:%M:%S") if e else "None"
st={}
try: st=json.load(open("/var/lib/liftlab/restore_state.json"))
except Exception as e: print("restore_state unreadable:",e)
db=D._db()
GW,CAM="site-A","ch27"
row=db.execute("SELECT computed_at,compute_ms,source_rows,window_days,counting_version,door_version "
               "FROM door_aggregate WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()
dmax=db.execute("SELECT MAX(ts) FROM gw_door_event WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()[0]
tmax=db.execute("SELECT MAX(ts) FROM transit_event WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()[0]
print("=== SAME-DB PROVENANCE (dev-box restored copy) ===")
print(f"  restore_state.restored_at   {st.get('restored_at_h')} IST   (epoch {st.get('restored_at')})")
print(f"  restore_state.data_max_ts   {st.get('data_max_ts_h')} IST   (epoch {st.get('data_max_ts')})")
print(f"  restore_state.rows          {st.get('rows')}")
print()
print(f"  door_aggregate.computed_at  {h(row['computed_at'])} IST   (epoch {row['computed_at']:.0f})")
print(f"  door_aggregate.compute_ms   {row['compute_ms']}   source_rows={row['source_rows']}")
print(f"  door_aggregate window_days  {row['window_days']}  cv={row['counting_version']!r} dv={row['door_version']}")
print()
print(f"  newest gw_door_event ts IN THIS COPY   {h(dmax)} IST")
print(f"  newest transit_event ts IN THIS COPY   {h(tmax)} IST")
print()
delta = row["computed_at"] - (dmax or 0)
print(f"  computed_at MINUS newest door row in this copy = {delta:+.0f}s ({delta/3600:+.2f}h)")
if delta > 60:
    print("  >>> THE AGGREGATE WAS COMPUTED AGAINST A NEWER DB STATE THAN THIS COPY CONTAINS.")
    print("      It was written on liftlab-cloud and restored here. No recompute on this copy can")
    print("      reproduce it: rows it counted are not present. Any A/B on this pairing is VOID.")
else:
    print("  >>> the copy contains data at least as new as the aggregate; a same-state recompute is possible")
db.close()
