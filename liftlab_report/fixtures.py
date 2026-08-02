"""Synthetic gateway-DB fixture for tests and offline proof runs.

Mirrors the LIVE DDLs exactly (events_api.gw_source/gw_event,
door_event_api.gw_door_event, analysis_api.transit_event,
validation_api.camera_validation, camera_registry_api.camera_registry,
survey_api.channel_map) so reader.py exercises the same shapes it will meet
on liftlab-cloud. Timestamps are deterministic — no wall-clock dependence.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta

from . import eras

GW = "site-A"
DOOR_VERSION = "e79e50d3+495e8f48"       # templates+geometry content hash


def _ddl(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS gw_source (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gateway_id TEXT NOT NULL, camera TEXT NOT NULL,
        declared_tz TEXT, tz_source TEXT, start_ts TEXT, created_at REAL);
    CREATE TABLE IF NOT EXISTS gw_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        door_open_start_ts TEXT, door_open_full_ts TEXT,
        door_close_start_ts TEXT, door_close_full_ts TEXT,
        close_travel_s REAL, open_valid INTEGER DEFAULT 1,
        plateau REAL, ramp_residual REAL,
        floor TEXT, boarded INTEGER, alighted INTEGER, created_at REAL,
        quality TEXT);
    CREATE TABLE IF NOT EXISTS gw_door_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL,
        floor TEXT, direction TEXT, door_state TEXT, openness REAL, read_conf REAL,
        panels_agreed INTEGER, reason TEXT, close_travel_s REAL,
        door_version TEXT, templates_hash TEXT, received_at REAL,
        candidates TEXT);
    CREATE TABLE IF NOT EXISTS transit_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, gateway_id TEXT, cam TEXT, ts REAL,
        ts_bucket INTEGER, direction TEXT, track_id INTEGER, received_at REAL,
        cycle_id INTEGER);
    CREATE TABLE IF NOT EXISTS camera_validation (
        gateway_id TEXT, cam TEXT, state TEXT DEFAULT 'validating',
        n_reviewed INTEGER DEFAULT 0, n_exact INTEGER DEFAULT 0,
        confirmed_at REAL, provenance TEXT, updated_at REAL,
        counting_version TEXT, PRIMARY KEY (gateway_id, cam));
    CREATE TABLE IF NOT EXISTS camera_registry (
        gateway_id TEXT, cam TEXT, enabled INTEGER DEFAULT 0, stride INTEGER DEFAULT 2,
        analyze_fps REAL DEFAULT 0, note TEXT, updated_at REAL,
        door_levels TEXT DEFAULT '', floor_range TEXT DEFAULT '',
        PRIMARY KEY (gateway_id, cam));
    CREATE TABLE IF NOT EXISTS channel_map (
        gateway_id TEXT, channel INTEGER, is_lift INTEGER);
    CREATE TABLE IF NOT EXISTS analyzer_status (
        gateway_id TEXT, cam TEXT, ts REAL, counting_version TEXT,
        PRIMARY KEY (gateway_id, cam));
    """)


PRECISIONS = {"ch16": (19, 20), "ch27": (25, 30), "ch29": (43, 50),
              "ch30": (23, 25), "ch32": (20, 20), "ch34": (17, 20),
              "ch37": (23, 25)}


def _ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=eras.IST)


def add_pi_cycle(db, cam: str, open_dt: datetime, close_travel=1.9,
                 boarded=2, alighted=1, quality=None, dwell=8.0, open_travel=2.8):
    sid = db.execute(
        "INSERT INTO gw_source (gateway_id,camera,declared_tz,tz_source,start_ts,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (GW, cam, "Asia/Kolkata", "stated", open_dt.isoformat(), open_dt.timestamp())
    ).lastrowid
    o_full = open_dt + timedelta(seconds=open_travel)
    c_start = o_full + timedelta(seconds=dwell)
    c_full = c_start + timedelta(seconds=close_travel)
    db.execute(
        "INSERT INTO gw_event (source_id,door_open_start_ts,door_open_full_ts,"
        "door_close_start_ts,door_close_full_ts,close_travel_s,open_valid,floor,"
        "boarded,alighted,quality,created_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?)",
        (sid, open_dt.isoformat(), o_full.isoformat(), c_start.isoformat(),
         c_full.isoformat(), close_travel if quality in (None, "ok") else None,
         None, boarded, alighted, quality, open_dt.timestamp()))


def add_gpu_cycle(db, cam: str, open_dt: datetime, close_travel=2.1,
                  dwell=6.0, open_travel=2.5, floor="12",
                  door_version=DOOR_VERSION, reopen=False):
    """closed→opening→open→closing→closed state stream for one cycle."""
    t = open_dt.timestamp()
    rows = [(t - 1.0, "closed", None, None),
            (t, "opening", None, None),
            (t + open_travel, "open", None, floor)]
    if reopen:
        rows += [(t + open_travel + dwell, "closing", None, None),
                 (t + open_travel + dwell + 3.0, "open", None, floor),
                 (t + open_travel + 2 * dwell + 3.0, "closing", None, None),
                 (t + open_travel + 2 * dwell + 3.0 + close_travel, "closed",
                  close_travel, None)]
    else:
        rows += [(t + open_travel + dwell, "closing", None, None),
                 (t + open_travel + dwell + close_travel, "closed",
                  close_travel, None)]
    for ts, st, ct, fl in rows:
        db.execute(
            "INSERT INTO gw_door_event (gateway_id,cam,ts,floor,direction,door_state,"
            "openness,read_conf,panels_agreed,reason,close_travel_s,door_version,"
            "templates_hash,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (GW, cam, ts, fl, "up" if fl else None, st, None,
             0.9 if fl else None, 1 if fl else 0, "" if fl else "no_read",
             ct, door_version, door_version.split("+")[0], ts))


def add_transit(db, cam: str, dt: datetime, direction="in", track_id=1):
    ts = dt.timestamp()
    db.execute(
        "INSERT INTO transit_event (gateway_id,cam,ts,ts_bucket,direction,"
        "track_id,received_at) VALUES (?,?,?,?,?,?,?)",
        (GW, cam, ts, int(ts // 2), direction, track_id, ts))


# The live gateway samples at 12.5 fps, so every travel value it can produce is
# a multiple of this. The fixture reproduces that so the quantum detector and
# the floor/suppression rules are exercised against realistic shapes rather
# than against clean synthetic numbers they would never meet.
FRAME_QUANTUM_S = 0.08


def _quantized(seconds: float, jitter_ms: int = 0) -> float:
    """Snap to the frame grid, optionally with the ±1 ms jitter the live rows
    carry (values are written to 3 dp by the analyzer)."""
    k = max(1, round(seconds / FRAME_QUANTUM_S))
    return round(k * FRAME_QUANTUM_S + jitter_ms / 1000.0, 3)


def add_quantized_gpu_cycles(db, cam: str, start: datetime, n: int,
                             close_s, open_s, door_version=DOOR_VERSION,
                             step_min: int = 5):
    """n GPU cycles whose travels sit on the frame grid. close_s/open_s may be
    a float or a callable(k) so a caller can mix one-frame artifacts into an
    otherwise plausible pool."""
    for k in range(n):
        ct = close_s(k) if callable(close_s) else close_s
        ot = open_s(k) if callable(open_s) else open_s
        add_gpu_cycle(db, cam, start + timedelta(minutes=step_min * k),
                      close_travel=_quantized(ct, (k % 3) - 1),
                      open_travel=_quantized(ot, (k % 3) - 1),
                      dwell=6.0 + (k % 4), floor=str(10 + k % 5),
                      door_version=door_version)


def make_fixture(path: str, *, with_pi=True, with_gpu=True) -> str:
    """A small but era-complete gateway DB:
    * pi_watch cycles 2026-07-15 and 2026-07-18 (both pi sub-eras)
    * gpu_engine cycles + transits 2026-07-22..23 and 2026-07-30
      (both counting eras), incl. one reopen and one withheld-quality pi row
    * a cycle inside the Jul-31 outage gap (must be excluded from rates)
    * ch32: a frame-quantized pool on 2026-07-25 mixing one-frame close
      artifacts with real closes, and open travels mostly pinned at one frame —
      the shapes the floor filter and the suppression rule exist to catch
    * ch34: a channel that goes silent for a whole day between live days, for
      the suspected-gap detector
    * validation rows for all 7 cams under 2026-07-28-registry-zones
    """
    db = sqlite3.connect(path)
    _ddl(db)
    for i, ch in enumerate(eras.CHANNELS):
        db.execute("INSERT INTO channel_map VALUES (?,?,1)", (GW, ch))
        exact, n = PRECISIONS[f"ch{ch}"]
        db.execute(
            "INSERT INTO camera_validation (gateway_id,cam,state,n_reviewed,"
            "n_exact,confirmed_at,updated_at,counting_version) VALUES (?,?,?,?,?,?,?,?)",
            (GW, f"ch{ch}", "live", n, exact,
             _ist(2026, 7, 29, 12, 0).timestamp(),
             _ist(2026, 7, 29, 12, 0).timestamp(), "2026-07-28-registry-zones"))
        db.execute(
            "INSERT INTO camera_registry (gateway_id,cam,enabled,note,updated_at)"
            " VALUES (?,?,1,?,?)",
            (GW, f"ch{ch}", f"fixture lift {ch}", time.time()))
        db.execute(
            "INSERT INTO analyzer_status (gateway_id,cam,ts,counting_version)"
            " VALUES (?,?,?,?)",
            (GW, f"ch{ch}", _ist(2026, 7, 30, 12, 0).timestamp(),
             "2026-07-28-registry-zones"))
    if with_pi:
        for k in range(12):
            add_pi_cycle(db, "ch29", _ist(2026, 7, 15, 9, 0) + timedelta(minutes=7 * k),
                         close_travel=1.8 + 0.05 * (k % 5), boarded=2 + k % 3,
                         alighted=1 + k % 2)
        for k in range(12):
            add_pi_cycle(db, "ch29", _ist(2026, 7, 18, 9, 0) + timedelta(minutes=7 * k),
                         close_travel=2.0 + 0.06 * (k % 6), boarded=1 + k % 3,
                         alighted=k % 2)
        add_pi_cycle(db, "ch29", _ist(2026, 7, 18, 11, 0), close_travel=2.2,
                     quality="close_suspect")          # withheld, never pooled
    if with_gpu:
        for day, base in ((22, 9), (23, 10)):
            for k in range(15):
                add_gpu_cycle(db, "ch29",
                              _ist(2026, 7, day, base, 0) + timedelta(minutes=6 * k),
                              close_travel=2.05 + 0.04 * (k % 7), floor=str(10 + k % 5))
                add_transit(db, "ch29",
                            _ist(2026, 7, day, base, 1) + timedelta(minutes=6 * k),
                            "in", track_id=100 + k)
                if k % 2 == 0:
                    add_transit(db, "ch29",
                                _ist(2026, 7, day, base, 2) + timedelta(minutes=6 * k),
                                "out", track_id=200 + k)
        add_gpu_cycle(db, "ch29", _ist(2026, 7, 22, 14, 0), reopen=True)
        for k in range(10):        # second counting era, after registry-zones
            add_gpu_cycle(db, "ch16", _ist(2026, 7, 30, 9, 0) + timedelta(minutes=9 * k),
                          close_travel=1.95 + 0.05 * (k % 4), floor=str(3 + k % 7))
            add_transit(db, "ch16", _ist(2026, 7, 30, 9, 1) + timedelta(minutes=9 * k),
                        "in", track_id=300 + k)
        # inside the Jul-31 full-outage gap window — must be rate-excluded
        add_gpu_cycle(db, "ch16", _ist(2026, 7, 31, 13, 0), close_travel=2.4)
        add_transit(db, "ch16", _ist(2026, 7, 31, 13, 1), "in", track_id=999)

        # ch32 — the quantization story, on the frame grid. Three populations,
        # because two DIFFERENT guards are involved and a test that cannot tell
        # them apart proves nothing:
        #   k%5==0  one frame (0.08s) — already implausible under PLAUS_LO=0.3,
        #           so it must never reach a close pool by either route
        #   k%5==1  4-6 frames (0.32-0.48s) — PLAUSIBLE by the old bound but
        #           still far too short to be a real close: this is the band
        #           MIN_PLAUSIBLE_CLOSE_S exists for, and the band the live
        #           gateway actually loses cycles in
        #   else    real closes straddling the 2.31s cliff
        # Open travel is pinned at one frame for 3 of every 4 cycles — which is
        # what makes open travel unmeasurable rather than fast.
        add_quantized_gpu_cycles(
            db, "ch32", _ist(2026, 7, 25, 9, 0), 60,
            close_s=lambda k: (FRAME_QUANTUM_S if k % 5 == 0
                               else 4 * FRAME_QUANTUM_S + FRAME_QUANTUM_S * (k % 3)
                               if k % 5 == 1
                               else 1.8 + 0.24 * (k % 6)),
            open_s=lambda k: (FRAME_QUANTUM_S if k % 4 else 2.4),
            door_version="aa11bb22+cc33dd44")
        for k in range(20):
            add_transit(db, "ch32", _ist(2026, 7, 25, 9, 2) + timedelta(minutes=15 * k),
                        "in", track_id=400 + k)

        # ch34 — live on Jul 25 and Jul 28, silent through Jul 26 and Jul 27.
        # Jul 26 is NOT declared, so the detector must flag it; Jul 27 IS
        # declared, so the detector must stay quiet about it.
        for day in (25, 28):
            add_quantized_gpu_cycles(
                db, "ch34", _ist(2026, 7, day, 10, 0), 8,
                close_s=lambda k: 2.0 + 0.08 * (k % 4), open_s=2.4,
                door_version="ee55ff66+7788aa99")
    db.commit()
    db.close()
    return path
