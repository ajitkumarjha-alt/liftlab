#!/usr/bin/env bash
# Install the raised-travel-cap stitch.py into the Pi's liftlab package (CLOSE_TRAVEL_MAX 10->30,
# OPEN 8->15) so real long-close interference is no longer discarded from the door data.
# Restarts liftlab-watch, which REUSES the persisted baseline (baseline_ch29.npz, proven last night:
# up in 12.8s, baseline_source='persisted', confirmed) — NO lull, NO re-seed needed.
#   sudo bash /tmp/apply_stitch.sh          (needs /tmp/stitch.py)
set -uo pipefail
B4=/home/askjitk/liftlab-b4
B4PY="$B4/.venv/bin/python"
RUNDIR=/home/askjitk/liftlab-watch
NPZ="$RUNDIR/baseline_ch29.npz"
MARK="$RUNDIR/stitch_cap_change.txt"
PIAG=/home/askjitk/liftlab-b3/pi-agent
say(){ echo "[apply-stitch] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/stitch.py ] || { echo "missing /tmp/stitch.py"; exit 2; }
[ -x "$B4PY" ] || { echo "B4 venv python missing at $B4PY"; exit 2; }

"$B4PY" -m py_compile /tmp/stitch.py || { say "new stitch.py does not compile — aborting"; exit 1; }
TGT=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.__file__)" 2>/dev/null)
[ -n "$TGT" ] && [ -f "$TGT" ] || { say "could not locate installed liftlab.stitch — aborting"; exit 1; }
OLD=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.CLOSE_TRAVEL_MAX, s.OPEN_TRAVEL_MAX)")
say "installed module: $TGT   (caps now: $OLD)"

# persistence sanity: the restart must reuse the baseline, not re-seed
if [ -f "$NPZ" ]; then say "persisted baseline present ($NPZ) — restart will reuse it (no lull)"; \
  else say "WARNING: no $NPZ — restart WOULD need a seed. Confirm persistence before proceeding."; fi

cp "$TGT" "$TGT.bak.$(date +%Y%m%d-%H%M%S)"
install -m 644 /tmp/stitch.py "$TGT"
NEW=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.CLOSE_TRAVEL_MAX, s.OPEN_TRAVEL_MAX)")
[ "$NEW" = "30.0 15.0" ] || { say "UNEXPECTED caps ($NEW) — restore $TGT.bak.* ; aborting"; exit 1; }
say "caps: was [$OLD] -> now [$NEW]"

# record the change boundary for comparability (analysis must split on this)
CHANGE_TS=$(date -u +%FT%TZ)
echo "$CHANGE_TS CLOSE_TRAVEL_MAX 10->30 OPEN_TRAVEL_MAX 8->15" | tee -a "$MARK"
chown askjitk:askjitk "$MARK" 2>/dev/null || true

say "restarting liftlab-watch (reuses persisted baseline)"
systemctl restart liftlab-watch; sleep 14   # persistence proved ~12.8s
AC=$(systemctl is-active liftlab-watch)
ST=$("$PIAG/.venv/bin/python" "$PIAG/watch_local.py" status 29 2>/dev/null)
SRC=$(echo "$ST" | grep -oE "'baseline_source': '[a-z-]+'" | head -1)
CONF=$(echo "$ST" | grep -oE "'baseline_confirmed': (True|False)" | head -1)
FPS=$(echo "$ST" | grep -oE "'signal_fps': [0-9.]+" | head -1)
say "AFTER: liftlab-watch=$AC  $SRC  $CONF  $FPS"
case "$SRC" in
  *persisted*) say "RESULT: PASS — new caps live, baseline REUSED (no re-seed). Closes up to 30s now retained." ;;
  *) say "RESULT: CHECK — baseline_source is not 'persisted' ($SRC). If 'live-seed', it is hunting a lull;"
     say "  the caps are still active, but confirm/seed as usual. journalctl -u liftlab-watch -n 30" ;;
esac
say ""
say "COMPARABILITY: cycles collected AFTER $CHANGE_TS include closes >10s that were previously"
say "  REJECTED. The interference rate and slow-mode median will JUMP at this boundary — because we"
say "  STOPPED DISCARDING events, not because door behaviour changed. Any analysis spanning it MUST"
say "  split into two datasets at $CHANGE_TS (recorded in $MARK)."
