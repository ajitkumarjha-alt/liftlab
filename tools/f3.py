import os, sys, datetime as dt
sys.path.insert(0,"/opt/liftlab-reports"); sys.path.insert(0,"/tmp/dashsrc")
os.environ.setdefault("GATEWAY_DB","/var/lib/liftlab/gateway.db")
import dash_api as D
IST=dt.timezone(dt.timedelta(hours=5,minutes=30))
def h(e): return dt.datetime.fromtimestamp(e,IST).strftime("%m-%d %H:%M:%S") if e else "None"
GW,CAM="site-A","ch27"
db=D._db()
ca=db.execute("SELECT computed_at FROM door_aggregate WHERE gateway_id=? AND cam=?",(GW,CAM)).fetchone()[0]
t0=ca-D.WINDOW_DAYS*86400.0
era=db.execute("SELECT door_version FROM gw_door_event WHERE gateway_id=? AND cam=? ORDER BY ts DESC LIMIT 1",(GW,CAM)).fetchone()[0][:8]
print("=== CANDIDATE 3: rows inside the window that ARRIVED after the aggregate ran ===")
n_win=db.execute("SELECT COUNT(*) FROM gw_door_event WHERE gateway_id=? AND cam=? AND door_version LIKE ? "
                 "AND ts>=? AND ts<?",(GW,CAM,era+"%",t0,ca)).fetchone()[0]
late=db.execute("SELECT COUNT(*) FROM gw_door_event WHERE gateway_id=? AND cam=? AND door_version LIKE ? "
                "AND ts>=? AND ts<? AND received_at > ?",(GW,CAM,era+"%",t0,ca,ca)).fetchone()[0]
print(f"  rows with ts in [t0, computed_at):            {n_win}")
print(f"  ...of which received_at > computed_at (LATE): {late}   ({late/max(n_win,1):.3%})")
r=db.execute("SELECT MIN(received_at-ts), MAX(received_at-ts), AVG(received_at-ts) FROM gw_door_event "
             "WHERE gateway_id=? AND cam=? AND received_at IS NOT NULL AND ts>=?",(GW,CAM,t0)).fetchone()
print(f"  received_at - ts on this cam: min={r[0]:.1f}s max={r[1]:.1f}s mean={r[2]:.1f}s")
print()
if late:
    print("  >>> CLOSES IT: the aggregate could not have counted these rows -- they were not in the")
    print("      database when precompute ran on the cloud box. A recompute on any later copy will")
    print("      always include them. This is NOT closable by a window bound: the window is on ts,")
    print("      the skew is on received_at.")
else:
    print("  >>> no late arrivals inside the window; candidate 3 dead too")
db.close()
