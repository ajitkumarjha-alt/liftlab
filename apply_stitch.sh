#!/usr/bin/env bash
# Install the raised-travel-cap stitch.py into the Pi's liftlab package. This changes the DOOR
# analysis (CLOSE_TRAVEL_MAX 10->30, OPEN 8->15) so real long-close interference is no longer
# discarded. TOUCHES THE DOOR WATCH: it restarts liftlab-watch, which RE-SEEDS the baseline
# (needs a doors-shut moment / confirm). Run it during a lull, deliberately.
#   sudo bash /tmp/apply_stitch.sh          (needs /tmp/stitch.py)
set -uo pipefail
B4=/home/askjitk/liftlab-b4
B4PY="$B4/.venv/bin/python"
say(){ echo "[apply-stitch] $*"; }
[ "$(id -u)" = 0 ] || { echo "run as root: sudo bash $0"; exit 2; }
[ -f /tmp/stitch.py ] || { echo "missing /tmp/stitch.py"; exit 2; }
[ -x "$B4PY" ] || { echo "B4 venv python missing at $B4PY"; exit 2; }

"$B4PY" -m py_compile /tmp/stitch.py || { say "new stitch.py does not compile — aborting"; exit 1; }
# locate the INSTALLED module (PYTHONPATH=B4 so it resolves the same as the scheduler does)
TGT=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.__file__)" 2>/dev/null)
[ -n "$TGT" ] && [ -f "$TGT" ] || { say "could not locate installed liftlab.stitch — aborting"; exit 1; }
say "installed module: $TGT"
OLD=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.CLOSE_TRAVEL_MAX, s.OPEN_TRAVEL_MAX)")
cp "$TGT" "$TGT.bak.$(date +%Y%m%d-%H%M%S)"
install -m 644 /tmp/stitch.py "$TGT"
NEW=$(PYTHONPATH="$B4" "$B4PY" -c "import liftlab.stitch as s; print(s.CLOSE_TRAVEL_MAX, s.OPEN_TRAVEL_MAX)")
say "CLOSE/OPEN travel max: was [$OLD] -> now [$NEW]"
case "$NEW" in
  "30.0 15.0") say "verified new caps active" ;;
  *) say "UNEXPECTED caps ($NEW) — restore $TGT.bak.* ; aborting"; exit 1 ;;
esac

say "restarting liftlab-watch (RE-SEEDS baseline — confirm during a doors-shut moment)"
systemctl restart liftlab-watch; sleep 3
AC=$(systemctl is-active liftlab-watch)
say "liftlab-watch = $AC"
[ "$AC" = active ] && say "RESULT: PASS — new caps live. Future closes up to 30s are now retained." \
  || { say "RESULT: CHECK — journalctl -u liftlab-watch -n 30"; exit 1; }
say "NOTE: past >10s closes were already dropped at emission and are NOT in the DB — only"
say "  recoverable by re-running offline analysis on retained clips with the new caps."
