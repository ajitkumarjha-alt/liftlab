#!/usr/bin/env bash
# Copy the geometry-verified ch29 zone_cabin (+ zone_landing) into the Pi's
# camera_zones.json so the occupancy sampler can run. door_roi is preserved. The
# polygons are 1920x1080 (calibration) = the live main-stream resolution, so no
# scaling. RUN ON THE PI. Backup-first, JSON-validated.
set -uo pipefail
ZF=/home/askjitk/liftlab-b4/camera_zones.json
B4PY=/home/askjitk/liftlab-b4/.venv/bin/python
say(){ echo "[zones] $*"; }
[ -f "$ZF" ] || { echo "camera_zones.json not at $ZF"; exit 2; }

cp "$ZF" "$ZF.bak.$(date +%Y%m%d-%H%M%S)"
say "backup written"
"$B4PY" - "$ZF" <<'PY'
import json, sys
p = sys.argv[1]
z = json.load(open(p))
ch = z.setdefault("ch29", {})
if not ch.get("door_roi"):
    print("  WARNING: ch29 has no door_roi — check this is the right file")
ch["zone_cabin"] = [[630, 870], [932, 747], [1042, 733], [1308, 1056], [587, 1056], [548, 914]]
ch["zone_landing"] = [[514, 394], [834, 322], [722, 529], [732, 684], [761, 776], [618, 827]]
ch["_provenance"] = "desk-rig 2026-07-11, geometry-verified 2026-07-15 (camera unmoved, 3 stills)"
json.dump(z, open(p, "w"), indent=2)
print("  ch29 keys now:", list(ch.keys()))
PY
"$B4PY" -c "import json; d=json.load(open('$ZF'))['ch29']; assert d.get('zone_cabin') and len(d['zone_cabin'])==6 and d.get('door_roi'); print('  verify: door_roi + zone_cabin(6pts) + zone_landing present')" \
  && say "RESULT: PASS — restart liftlab-watch to enable occupancy (it reads zone_cabin at start)." \
  || { say "RESULT: CHECK — restore newest $ZF.bak.*"; exit 1; }
