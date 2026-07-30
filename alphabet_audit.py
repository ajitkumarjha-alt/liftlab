#!/usr/bin/env python3
"""Alphabet contamination audit: exemplar crops admitted while a camera's digit_cells
were in the WRONG coordinate space (the ch16 07-26 panel nudge left the saved cells in
the old 110x170 panel space until the post-nudge redraw).

Since 8779e7d the derived floor alphabet pools ADMISSION evidence all-era, and
labels.json feeds _labels_evidence directly — so a garbage exemplar admitted during the
wrong-space window survives the redraw unless it is quarantined. Same posture as the
phantom-floor quarantine: FLAG, never delete. Evidence stays on disk (quarantine.json
keeps the original label + why + provenance); trust is withdrawn by setting the
labels.json entry to '-', which build_from_crops, foldback and _labels_evidence all
ALREADY treat as excluded — no new exclusion semantics, no code change downstream.

Audit (default, READ-ONLY on evidence; writes only the _alphabet_audit.json artifact):
  1. every _calib_crop_*.png, sorted by FIRST-ADMITTED time — floor_sample.ts via a
     pixel-exact provenance match when the crop came from /floorcheck foldback (the
     capture time, not the fold time; _folded.json stores only ids, so content match
     is the only honest join), file mtime for wizard-collected crops
  2. flags crops first admitted >= --boundary; flagged crops with old-space dims are
     contaminated BY CONSTRUCTION (cut with the pre-nudge ROI)
  3. thin glyphs (n<=1) before/after quarantine, and TOTAL-LOSS glyphs — an n=1 glyph
     whose only exemplar is contaminated loses everything, not a dilution
  4. whether the current templates.npz build predates or could contain flagged crops
  5. cross-camera leak scan — byte-identical flagged crops in other cams' stores, and
     foldback ids that belong to a different camera's floor_sample rows — on top of
     the by-construction per-camera scoping (calib dirs, labels.json, alpha_rows
     WHERE cam=?, templates {gw}/{cam}.npz)

--quarantine applies step 2's verdicts (labels.json backed up first, atomic writes).
WEB-CALLABLE convention: importable fns, JSON-able results, CalibError not SystemExit.

Run (cloud VM, next to door_calib.py):
  cd /opt/liftlab-analysis
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16                # report
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16 --quarantine  # apply
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import struct
import time
from datetime import datetime
from pathlib import Path

from door_calib import (CALIB_DIR, CAM, GW, CalibError, _calib_dir, _chown_tree,
                        _panel_rois, _write_result)

GATEWAY_DB = os.environ.get("GATEWAY_DB", "/opt/liftlab-b3/cloud/gateway.db")


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None


def _png_dims(path):
    """(w, h) straight from the PNG IHDR — no decode, no cv2."""
    try:
        with open(path, "rb") as f:
            head = f.read(26)
    except OSError:
        return None
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def _labels(outdir):
    try:
        return json.loads((outdir / "labels.json").read_text())
    except (OSError, ValueError):
        return {}


def _provenance(outdir, gw, cam, db_path):
    """crop filename -> {floor_sample id, ts, received_at} by PIXEL-EXACT match of each
    reviewed sample's decoded crop_jpeg against the folded png (foldback wrote the png
    lossless from that exact array). Also returns {id: cam} ownership for the WHOLE
    gateway — the cross-fold leak check needs to know which camera every id belongs to."""
    import sqlite3

    import cv2
    import numpy as np
    if not os.path.exists(db_path):
        raise CalibError(f"gateway.db not found at {db_path} — set GATEWAY_DB (audit "
                         "falls back to file mtimes without it)")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)   # READ-ONLY, like foldback
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT id, cam, ts, received_at, crop_jpeg FROM floor_sample "
                          "WHERE gateway_id=? AND reviewed_label IS NOT NULL "
                          "AND reviewed_label != ''", (gw,)).fetchall()
    except sqlite3.OperationalError as e:
        db.close()
        raise CalibError(f"floor_sample not readable ({e})")
    db.close()
    id_cam = {int(r["id"]): str(r["cam"]) for r in rows}
    by_shape = {}
    for r in rows:
        if str(r["cam"]) != cam or not r["crop_jpeg"]:
            continue
        arr = cv2.imdecode(np.frombuffer(bytes(r["crop_jpeg"]), np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is not None:
            by_shape.setdefault(arr.shape, []).append((r, arr))
    prov = {}
    for p in sorted(glob.glob(str(outdir / "_calib_crop_*.png"))):
        im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if im is None:
            continue
        for r, arr in by_shape.get(im.shape, []):
            if np.array_equal(im, arr):
                prov[Path(p).name] = {"floor_sample_id": int(r["id"]),
                                      "ts": r["ts"], "received_at": r["received_at"]}
                break
    return prov, id_cam


def _glyph_counts(label_by_crop, exclude=()):
    """Per-glyph contributing-crop counts over trusted labels ('' and '-' are already
    untrusted). Uses gpu_door's own parser so arrow/blank semantics can't drift."""
    from gpu_door import ARROWS, _label_to_glyphs
    counts = {}
    for fname, lab in label_by_crop.items():
        lab = str(lab or "").strip()
        if not lab or lab == "-" or fname in exclude:
            continue
        for g in _label_to_glyphs(lab):
            counts[g] = counts.get(g, 0) + 1
    return counts, set(ARROWS)


def audit(gw=None, cam=None, boundary="2026-07-26T00:00:00", old_space="110x170",
          db_path=None):
    """Full contamination report as a JSON-able dict; writes _alphabet_audit.json next
    to the crops. Read-only on all evidence. --boundary is SERVER-LOCAL time."""
    gwid, cam = gw or GW, cam or CAM
    outdir = _calib_dir(gwid, cam)
    crops = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not crops:
        raise CalibError(f"no _calib_crop_*.png under {outdir} — nothing to audit")
    labels = _labels(outdir)
    try:
        quarantined_already = set(json.loads((outdir / "quarantine.json").read_text()))
    except (OSError, ValueError):
        quarantined_already = set()
    bts = datetime.fromisoformat(boundary).timestamp()
    w0, h0 = (int(x) for x in old_space.lower().split("x"))
    old_dims = {(w0, h0), (h0, w0)}
    prois, proi_src = _panel_rois(gwid, cam)
    cur_dims = (prois[0][2], prois[0][3]) if prois else None

    prov_warn, prov, id_cam = None, {}, {}
    try:
        prov, id_cam = _provenance(outdir, gwid, cam, db_path or GATEWAY_DB)
    except CalibError as e:
        prov_warn = str(e)          # degrade to mtimes, but SAY so — never silently

    inventory = []
    for p in crops:
        fname = Path(p).name
        mtime = os.path.getmtime(p)
        pr = prov.get(fname)
        # capture time, not fold time: the wrong-space window is about when the crop
        # was CUT from the stream, and foldback can run days later
        admitted = (pr and (pr["ts"] or pr["received_at"])) or mtime
        dims = _png_dims(p)
        rec = {"crop": fname, "label": str(labels.get(fname) or "").strip(),
               "dims": list(dims) if dims else None,
               "first_admitted": admitted, "first_admitted_iso": _iso(admitted),
               "admitted_source": ("floor_sample (foldback)" if pr else "collect/mtime"),
               "floor_sample_id": pr and pr["floor_sample_id"],
               "file_mtime_iso": _iso(mtime),
               "flagged": admitted >= bts,
               "old_space": dims in old_dims if dims else None,
               "dims_match_current_roi": (dims == cur_dims) if (dims and cur_dims) else None,
               "already_quarantined": fname in quarantined_already}
        rec["contaminated"] = bool(rec["flagged"] and rec["old_space"])
        inventory.append(rec)
    inventory.sort(key=lambda r: r["first_admitted"])
    flagged = [r for r in inventory if r["flagged"]]
    contaminated = [r for r in inventory if r["contaminated"]]

    # thin glyphs: before vs after withdrawing the contaminated crops. TOTAL LOSS is an
    # n>=1 glyph left at n=0 — on M/E (n=1) a single junk exemplar is everything.
    before, arrows = _glyph_counts({r["crop"]: r["label"] for r in inventory})
    after, _ = _glyph_counts({r["crop"]: r["label"] for r in inventory},
                             exclude={r["crop"] for r in contaminated})
    glyphs = {g: {"before": n, "after": after.get(g, 0), "arrow": g in arrows,
                  "contaminated_contrib": n - after.get(g, 0)} for g, n in sorted(before.items())}
    total_loss = [g for g, d in glyphs.items() if d["after"] == 0 and d["contaminated_contrib"]]
    thin_after = [g for g, d in glyphs.items() if d["after"] == 1 and not d["arrow"]]
    floors_before = {str(r["label"]).rstrip("^vV") for r in inventory
                     if r["label"] and r["label"] != "-"}
    floors_after = {str(r["label"]).rstrip("^vV") for r in inventory
                    if r["label"] and r["label"] != "-" and not r["contaminated"]}
    lost_floor_evidence = sorted(floors_before - floors_after)

    # current templates.npz: contaminated crops that existed by built_at could be IN the
    # live templates — presence is conservative (label time isn't recorded anywhere)
    try:
        build = json.loads((outdir / "_calib_build.json").read_text())
    except (OSError, ValueError):
        build = None
    in_build = ([r["crop"] for r in contaminated if r["first_admitted"] <= build["built_at"]]
                if build and build.get("built_at") else None)

    # cross-camera leak: per-camera scoping is by construction (calib dirs, labels.json,
    # alpha_rows WHERE cam=?, {gw}/{cam}.npz) — verify it EMPIRICALLY anyway
    flagged_sha = {}
    for r in flagged:
        flagged_sha[hashlib.sha256((outdir / r["crop"]).read_bytes()).hexdigest()] = r["crop"]
    leaks, cross_folds = [], []
    gwdir = CALIB_DIR / gwid
    other_cams = sorted(d.name for d in gwdir.iterdir()
                        if d.is_dir() and d.name != cam) if gwdir.exists() else []
    for oc in other_cams:
        for p in glob.glob(str(gwdir / oc / "_calib_crop_*.png")):
            h = hashlib.sha256(Path(p).read_bytes()).hexdigest()
            if h in flagged_sha:
                leaks.append({"cam": oc, "crop": Path(p).name, "same_as": flagged_sha[h]})
        try:
            ids = set(json.loads((gwdir / oc / "_folded.json").read_text()))
        except (OSError, ValueError):
            ids = set()
        wrong = sorted(i for i in ids if id_cam.get(int(i)) == cam)
        if wrong:
            cross_folds.append({"cam": oc, "foreign_floor_sample_ids": wrong})

    # boundary evidence so the operator can tighten --boundary to the real nudge moment
    def _mt(name):
        p = outdir / name
        return _iso(os.path.getmtime(p)) if p.exists() else None
    result = {
        "gw": gwid, "cam": cam, "boundary": boundary, "boundary_epoch": bts,
        "old_space": sorted(map(list, old_dims)), "current_panel0_dims": cur_dims,
        "panel_roi_source": proi_src,
        "provenance_warning": prov_warn,
        "n_crops": len(inventory), "n_flagged": len(flagged),
        "n_contaminated": len(contaminated),
        "inventory": inventory,
        "glyphs": glyphs, "total_loss_glyphs": total_loss, "thin_after_quarantine": thin_after,
        "lost_floor_evidence": lost_floor_evidence,
        "current_build": build and {"era": build.get("era"), "built_at": build.get("built_at"),
                                    "built_at_iso": _iso(build.get("built_at")),
                                    "n_labeled": build.get("n_labeled")},
        "contaminated_possibly_in_current_npz": in_build,
        "leak_scan": {"other_cams_scanned": other_cams,
                      "byte_identical_crops": leaks, "cross_camera_folds": cross_folds,
                      "by_construction": "calib dirs, labels.json/_labels_evidence, "
                                         "alpha_rows and templates npz are all scoped "
                                         "WHERE gateway_id=? AND cam=? — all-era pools "
                                         "TIME, never cameras"},
        # old-space crops from BEFORE the boundary were consistent with the old cells,
        # but the NEXT build cuts every kept crop with the POST-REDRAW cells — a mixed-
        # dims store contaminates the rebuild from the other side. Surface it; the
        # redraw acceptance run must decide (dims filter in build, or a second sweep).
        "pre_boundary_old_space": [r["crop"] for r in inventory
                                   if not r["flagged"] and r["old_space"]],
        "boundary_evidence_mtimes": {n: _mt(n) for n in
                                     ("roi.json", "_calib_cells.json", "_calib_roi.json",
                                      "_calib_build.json", "_calib_foldback.json")},
        "audited_at": time.time(),
    }
    return _write_result(outdir, "_alphabet_audit.json", result)


def quarantine(gw=None, cam=None, boundary="2026-07-26T00:00:00", old_space="110x170",
               db_path=None, reason=None):
    """Apply the audit's contaminated verdicts: quarantine.json gains the evidence
    (original label, dims, provenance, why), labels.json entry -> '-'. Backup first,
    atomic writes, idempotent, never touches a png. Returns audit + apply summary."""
    result = audit(gw, cam, boundary, old_space, db_path)
    gwid, cam = gw or GW, cam or CAM
    outdir = _calib_dir(gwid, cam)
    targets = [r for r in result["inventory"] if r["contaminated"]]
    labels = _labels(outdir)
    try:
        q = json.loads((outdir / "quarantine.json").read_text())
    except (OSError, ValueError):
        q = {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = None
    if (outdir / "labels.json").exists():
        backup = str(outdir / f"labels.json.bak.{stamp}")
        shutil.copy(outdir / "labels.json", backup)
    applied, already = [], []
    for r in targets:
        f = r["crop"]
        if f in q and str(labels.get(f, "")).strip() == "-":
            already.append(f)
            continue
        q[f] = {"label": r["label"], "dims": r["dims"],
                "first_admitted": r["first_admitted"],
                "first_admitted_iso": r["first_admitted_iso"],
                "admitted_source": r["admitted_source"],
                "floor_sample_id": r["floor_sample_id"],
                "reason": reason or f"wrong_space_{old_space}_post_nudge (boundary {boundary})",
                "flagged_at": time.time()}
        labels[f] = "-"
        applied.append(f)
    for name, obj in (("quarantine.json", q), ("labels.json", labels)):
        tmp = (outdir / name).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
        os.replace(tmp, outdir / name)
    _chown_tree(outdir)
    result["quarantine_applied"] = {
        "applied": applied, "already_quarantined": already, "labels_backup": backup,
        "restore": "to reverse: copy each quarantine.json entry's 'label' back into "
                   "labels.json (or restore the .bak) — the crop pngs were never touched"}
    return _write_result(outdir, "_alphabet_audit.json", result)


def main():
    ap = argparse.ArgumentParser(description="alphabet contamination audit (flag, don't delete)")
    ap.add_argument("--gw", default=GW)
    ap.add_argument("--cam", default=CAM)
    ap.add_argument("--boundary", default="2026-07-26T00:00:00",
                    help="nudge moment, SERVER-LOCAL ISO time; crops admitted at/after are flagged")
    ap.add_argument("--old-space", default="110x170", help="pre-nudge panel dims WxH")
    ap.add_argument("--db", default=None, help="gateway.db path (default $GATEWAY_DB)")
    ap.add_argument("--quarantine", action="store_true",
                    help="apply: contaminated labels -> '-' + quarantine.json (default: report only)")
    a = ap.parse_args()
    fn = quarantine if a.quarantine else audit
    r = fn(a.gw, a.cam, a.boundary, a.old_space, a.db)
    for rec in r["inventory"]:
        mark = ("CONTAMINATED" if rec["contaminated"] else
                "flagged" if rec["flagged"] else "ok")
        print(f'{rec["first_admitted_iso"]}  {rec["crop"]}  label={rec["label"] or "(none)"!r:8s} '
              f'dims={rec["dims"]}  src={rec["admitted_source"]}  {mark}')
    print(f'\n{r["n_crops"]} crops, {r["n_flagged"]} flagged >= {r["boundary"]}, '
          f'{r["n_contaminated"]} contaminated (old-space {r["old_space"]}); '
          f'current panel0 dims {r["current_panel0_dims"]} [{r["panel_roi_source"]}]')
    if r["provenance_warning"]:
        print(f'WARNING: {r["provenance_warning"]} — timestamps are file mtimes only')
    print(f'total-loss glyphs: {r["total_loss_glyphs"] or "none"};  '
          f'thin (n=1) after quarantine: {r["thin_after_quarantine"] or "none"};  '
          f'floors losing all human evidence: {r["lost_floor_evidence"] or "none"}')
    ls = r["leak_scan"]
    print(f'leak scan over {ls["other_cams_scanned"]}: '
          f'{len(ls["byte_identical_crops"])} byte-identical crops, '
          f'{len(ls["cross_camera_folds"])} cross-camera folds'
          + (" — FLEET-WIDE PROBLEM, see _alphabet_audit.json"
             if ls["byte_identical_crops"] or ls["cross_camera_folds"] else " — clean"))
    if r["pre_boundary_old_space"]:
        print(f'NOTE: {len(r["pre_boundary_old_space"])} PRE-boundary old-space crops will '
              f'mismatch the post-redraw cells at the next --build (they are NOT quarantined '
              f'by this audit) — decide before the rebuild')
    if r["contaminated_possibly_in_current_npz"]:
        print(f'current npz may contain {len(r["contaminated_possibly_in_current_npz"])} '
              f'contaminated crops (present before built_at) — rebuild after quarantine')
    if a.quarantine:
        qa = r["quarantine_applied"]
        print(f'\nQUARANTINED {len(qa["applied"])} ({len(qa["already_quarantined"])} already); '
              f'labels backup: {qa["labels_backup"]}')
    else:
        print('\nreport only — re-run with --quarantine to apply')


if __name__ == "__main__":
    try:
        main()
    except CalibError as e:
        print(f"CALIB ERROR: {e}")
        raise SystemExit(2)
