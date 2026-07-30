#!/usr/bin/env python3
"""Executable smoke for door_calib.build_from_crops — the check a compile cannot do.

2026-07-30: 87c2e38e shipped a NameError in build_from_crops' result telemetry (a
counter was renamed in one commit, the dict that reports it written in another) and
broke Build fleet-wide. py_compile passed — the defect only exists at runtime. This
harness RUNS build_from_crops end-to-end against a synthetic fixture with gpu_door and
cv2 stubbed (stdlib only, no numpy/cv2), so every line of the function body — dims
filter, binding check, glyph-loss gate, result dicts — actually executes before a ship.

PRE-SHIP GATE for any door_calib change:
    python smoke_door_calib.py     # exit 0 + "SMOKE PASS" or DO NOT SHIP
"""
import json
import os
import struct
import sys
import tempfile
import types
import zlib
from pathlib import Path


def _stub_gpu_door():
    m = types.ModuleType("gpu_door")
    m.BLANK = "blank"
    m.ARROWS = ("up", "down")

    def _label_to_glyphs(label):
        arrow, body = None, label.strip()
        if body.endswith("^"):
            arrow, body = "up", body[:-1]
        elif body[-1:] in ("v", "V"):
            arrow, body = "down", body[:-1]
        g = list(body.strip())
        if arrow:
            g.append(arrow)
        return g

    m._label_to_glyphs = _label_to_glyphs
    m.crop = lambda img, cell: img
    m.build_templates = lambda labeled, dc, ac, **kw: (
        {"7": object()},
        {"used": len(labeled), "skipped": 0, "dropped": {},
         "min_examples": kw.get("min_examples", 3),
         "exemplars": {"7": 1}, "glyphs": {"7": len(labeled)}})
    m.save_templates = lambda tpl, path: Path(path).write_bytes(b"npz")
    m.templates_hash = lambda tpl: "f" * 64
    return m


def _stub_cv2():
    m = types.ModuleType("cv2")
    m.IMREAD_GRAYSCALE = 0
    m.imread = lambda p, flag=0: object()      # consumed only by the gpu_door stub
    return m


def _png(path, w, h):
    """A real minimal PNG so _png_wh and _sha16 exercise genuine bytes."""
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="smoke_calib_"))
    gw, cam, W, H = "site-T", "chT", 107, 152
    d = tmp / gw / cam
    d.mkdir(parents=True)
    labels = {}
    for i in range(13):                        # clears MIN_BUILD_CROPS=12
        f = f"_calib_crop_{i:03d}.png"
        _png(d / f, W, H)
        labels[f] = "7^"
    (d / "labels.json").write_text(json.dumps(labels))
    (d / "roi.json").write_text(json.dumps({
        "panel_rois": [[10, 20, W, H]],
        "cells": {"digit_cells": [[5, 5, 10, 16], [20, 5, 10, 16]],
                  "arrow_cell": [35, 5, 10, 16], "panel_wh": [W, H]}}))
    for k in ("LABELS", "DIGIT_CELLS", "ARROW_CELL", "PANEL_ROIS", "TEMPLATES_DIR"):
        os.environ.pop(k, None)
    os.environ.update(CALIB_DIR=str(tmp), GW=gw, CAM=cam,
                      TEMPLATES_OUT=str(tmp / "t.npz"),
                      LABEL_BIND_LEGACY_CAMS=cam)     # unbound labels accepted in the smoke
    sys.modules["gpu_door"] = _stub_gpu_door()
    sys.modules["cv2"] = _stub_cv2()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import door_calib
    door_calib._chown_tree = lambda p: None    # ownership is not the code under test
    r = door_calib.build_from_crops(gw=gw, cam=cam)
    for key in ("label_binding", "dims_filter", "stats", "templates_hash", "era",
                "n_labeled", "n_excluded", "n_crops"):
        assert key in r, f"missing result key: {key}"
    # second pass: STALE binding path (content changed under a bound filename)
    bind = {f: "0" * 16 for f in labels}       # wrong sha for every crop -> all stale
    (d / "labels_bind.json").write_text(json.dumps(bind))
    try:
        door_calib.build_from_crops(gw=gw, cam=cam)
        raise AssertionError("stale-binding build should have refused (CalibError)")
    except door_calib.CalibError:
        pass                                    # correct: every label stale -> refuse
    print("SMOKE PASS:", json.dumps(r["label_binding"]), json.dumps(r["dims_filter"]))


if __name__ == "__main__":
    main()
