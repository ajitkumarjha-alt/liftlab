#!/usr/bin/env python3
"""Record WHY an era boundary is there. Run at cut-over, once per rebuilt camera.

`door_version` already says an era CHANGED — gpu_analyze composes it from the templates hash, the
tracker logic, the door levels and the geometry hash, so any rebuild moves it by construction and
nobody has to remember to declare one. What the string cannot say is WHY. A year from now,
`260d4a0f…` -> `<new>` on three cameras within an hour of each other is either self-evidently a
coordinated recalibration or an unexplained discontinuity, and which one it is depends entirely on
whether somebody wrote it down where the data is read rather than in a commit message.

NOTHING DERIVES THESE. An inferred reason would be a guess carrying the authority of a record, and
the whole value of the field is that a person stood behind it. There is no API that sets one.

The note attaches to the FULL door_version, not the era prefix: a prefix is shared by every
geometry variant of one template set, so a rebuild that changed cells but not templates would land
on the prefix of the era it replaced. `--current` resolves the camera's newest stamped
door_version, which is what you want immediately after a cut-over — but it reads the DB, so run it
AFTER the rebuilt worker has posted at least one row, or it will stamp the era you just left.

usage:
  era_note.py --list                                  # every note on the gateway
  era_note.py --list --cam ch29
  era_note.py --cam ch29 --current --reason "..."     # stamp the camera's newest door_version
  era_note.py --cam ch29 --era 1a2b3c4dh3+deadbeef --reason "..."
  era_note.py --cam ch29 --era <dv> --delete

  # the 2026-09-02 recalibration, all three cameras at once:
  for C in ch27 ch29 ch30; do
    era_note.py --cam $C --current --reason "recalibrated against the post-2026-09-02 camera image
      profile (INCIDENT_decode_regression_0902.md). Templates and cells rebuilt; pre-09-02 data is
      valid under the previous era and MUST NOT be pooled across this boundary."
  done
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.environ.get("LIFTLAB_APP", "/opt/liftlab-b3/cloud"))

import dash_api as D  # noqa: E402


def _current_dv(db, gw, cam):
    """The camera's newest stamped door_version. Same rule alphabet_refresh uses, so 'the current
    era' means one thing across the system."""
    r = db.execute("SELECT door_version dv FROM gw_door_event WHERE gateway_id=? AND cam=? "
                   "AND door_version IS NOT NULL AND door_version<>'' AND ts IS NOT NULL "
                   "ORDER BY ts DESC LIMIT 1", (gw, cam)).fetchone()
    return r["dv"] if r else None


def _rows(db, gw, cam=None):
    q = ("SELECT cam, door_version dv, reason, noted_at, noted_by FROM era_note "
         "WHERE gateway_id=?" + (" AND cam=?" if cam else "") + " ORDER BY cam, noted_at")
    return db.execute(q, (gw, cam) if cam else (gw,)).fetchall()


def do_list(db, gw, cam):
    D._era_note_table(db)
    rows = _rows(db, gw, cam)
    if not rows:
        print(f"[era_note] no notes recorded for {gw}" + (f"/{cam}" if cam else ""))
        return 0
    for r in rows:
        cur = _current_dv(db, gw, r["cam"])
        print(f"\n{r['cam']}  {r['dv']}" + ("   <- CURRENT" if r["dv"] == cur else ""))
        print(f"  noted {D._iso_ist(r['noted_at'])} by {r['noted_by'] or '?'}")
        for ln in (r["reason"] or "").splitlines():
            print(f"    {ln.strip()}")
    return 0


def do_set(db, gw, cam, era, reason, by, replace):
    D._era_note_table(db)
    # A REASON THAT SAYS NOTHING IS WORSE THAN NO REASON — it makes the field look answered. The
    # bar is deliberately low but not zero.
    reason = " ".join((reason or "").split())
    if len(reason) < 12:
        print(f"[era_note] refusing a {len(reason)}-character reason. The field exists so a future "
              f"reader does not have to ask what happened; write the sentence you would say to "
              f"them.", file=sys.stderr)
        return 2
    existing = db.execute("SELECT reason, noted_at, noted_by FROM era_note WHERE gateway_id=? "
                          "AND cam=? AND door_version=?", (gw, cam, era)).fetchone()
    if existing and not replace:
        print(f"[era_note] {gw}/{cam} {era} ALREADY has a note, recorded "
              f"{D._iso_ist(existing['noted_at'])} by {existing['noted_by'] or '?'}:\n"
              f"    {existing['reason']}\n"
              f"  Pass --replace to overwrite it. Refusing by default: a recalibration log that "
              f"can be silently rewritten is not a log.", file=sys.stderr)
        return 3
    db.execute("INSERT INTO era_note (gateway_id, cam, door_version, reason, noted_at, noted_by) "
               "VALUES (?,?,?,?,?,?) ON CONFLICT(gateway_id, cam, door_version) DO UPDATE SET "
               "reason=excluded.reason, noted_at=excluded.noted_at, noted_by=excluded.noted_by",
               (gw, cam, era, reason, time.time(), by))
    db.commit()
    # The census caches per (gw, cam) for 15 minutes, so a note written now would not appear on the
    # dash until the TTL expired — long enough for someone to conclude it did not save.
    try:
        with D._census_lock:
            D._census_cache.pop((gw, cam), None)
    except Exception:
        pass
    print(f"[era_note] {gw}/{cam} {era}: recorded. It now appears in eras.csv (era_reason) and "
          f"under the era selector on the camera panel.")
    return 0


def do_delete(db, gw, cam, era):
    D._era_note_table(db)
    n = db.execute("DELETE FROM era_note WHERE gateway_id=? AND cam=? AND door_version=?",
                   (gw, cam, era)).rowcount
    db.commit()
    try:
        with D._census_lock:
            D._census_cache.pop((gw, cam), None)
    except Exception:
        pass
    print(f"[era_note] {gw}/{cam} {era}: {n} note(s) deleted")
    return 0 if n else 1


def main():
    ap = argparse.ArgumentParser(description="record why a door era boundary is there")
    ap.add_argument("--gw", default=os.environ.get("GW", "site-A"))
    ap.add_argument("--cam", default=None)
    ap.add_argument("--era", default=None, help="the FULL door_version to annotate")
    ap.add_argument("--current", action="store_true",
                    help="annotate the camera's newest stamped door_version (run AFTER the "
                         "rebuilt worker has posted a row, or you will stamp the old era)")
    ap.add_argument("--reason", default=None)
    ap.add_argument("--by", default=os.environ.get("USER") or "unknown")
    ap.add_argument("--replace", action="store_true", help="overwrite an existing note")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    db = D._db()
    try:
        if a.list or not (a.reason or a.delete):
            return do_list(db, a.gw, a.cam)
        if not a.cam:
            print("[era_note] --cam is required to set or delete a note", file=sys.stderr)
            return 2
        era = a.era
        if a.current:
            era = _current_dv(db, a.gw, a.cam)
            if not era:
                print(f"[era_note] {a.gw}/{a.cam} has no stamped door_version — the rebuilt worker "
                      f"has not posted a row yet. Wait for one, or pass --era explicitly.",
                      file=sys.stderr)
                return 4
            print(f"[era_note] {a.gw}/{a.cam} current door_version = {era}")
        if not era:
            print("[era_note] pass --era <door_version> or --current", file=sys.stderr)
            return 2
        if a.delete:
            return do_delete(db, a.gw, a.cam, era)
        return do_set(db, a.gw, a.cam, era, a.reason, a.by, a.replace)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
