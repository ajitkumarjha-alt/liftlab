"""liftlab_report — repeatable, date-range-parameterised Excel report builder.

Reads the gateway SQLite DB (READ-ONLY) and exports a workbook of lift
analytics: per-lift analysis, fleet roll-up, raw audit trail, and a
"vs the sheet" panel against MEP-02 v28 assumed coefficients.

Era hygiene is the load-bearing wall of this module: the Pi door-watch
(gw_event) and the GPU DoorFloorEngine (gw_door_event) are DIFFERENT
INSTRUMENTS split at 2026-07-21, and no figure in the workbook ever pools
across that boundary (or any other era boundary). See eras.py.
"""

__version__ = "1.0.0"
