#!/usr/bin/env python3
"""Alphabet contamination audit v2: exemplar crops admitted while a camera's digit_cells
were in the WRONG coordinate space (the ch16 07-26 panel nudge left the saved cells in
the old 110x170 panel space until the post-nudge redraw).

Since 8779e7d the derived floor alphabet pools ADMISSION evidence all-era, and
labels.json feeds _labels_evidence directly — so a garbage exemplar admitted during the
wrong-space window survives the redraw unless it is quarantined. Same posture as the
phantom-floor quarantine: FLAG, never delete. Evidence stays on disk (quarantine.json
keeps the original label + why + provenance); trust is withdrawn by setting the
labels.json entry to '-', which build_from_crops, foldback and _labels_evidence all
ALREADY treat as excluded. Quarantine is about TRUST; the standing dims filter in
build_from_crops is about SPACE — the two are deliberately not conflated: this audit
never quarantines a crop merely for being old-space.

v2 (2026-07-30) — the audit now audits itself:
  * BOUNDARY FROM EVIDENCE: default boundary is derived from the step drop in ch16's
    hourly with_floor rate (gw_door_event), not a file mtime or a typed date. An
    explicit --boundary is honoured and labelled stated-vs-guessed (naive input is
    interpreted in the server zone and SAYS SO; pass an offset to make it stated).
  * JOIN RATE: reports what fraction of store PNGs pixel-joined to floor_sample rows,
    exact first (the fold wrote each PNG from the decoded crop_jpeg array — a lossless
    copy of the lossy source, deterministic on one box), then a tolerant pass
    (mean|diff| <= 1.0) for decoder drift. Unjoined crops fall back to file mtime and
    every such record SAYS its time source.
  * MTIME CLUSTERS: >=4 crops within 2s share an mtime era (a cp without -p — e.g. the
    crops-pre-nudge-0722 sweep). Clustered mtimes are UNRELIABLE: such crops are still
    reported but NEVER auto-quarantined on time alone.
  * BUILDABLE POPULATION: per-glyph count of crops that survive BOTH quarantine and the
    dims filter against the NEW panel space — the number that decides whether the
    redraw restores floor attribution or merely enables a fresh Collect.
  * ARCHIVE RECONCILE: counts crops-pre-nudge-0722/ and states whether old-space crops
    are still live in the active store.

Run (cloud VM, next to door_calib.py / gpu_door.py):
  cd /opt/liftlab-analysis
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16                     # report
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16 --provenance        # label origin
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16 --quarantine        # preview
  sudo .venv/bin/python alphabet_audit.py --gw site-A --cam ch16 --quarantine --yes  # apply
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from door_calib import (CALIB_DIR, CAM, GW, CalibError, _calib_dir, _cells_space,
                        _chown_tree, _load_bind, _png_wh, _save_bind, _sha16,
                        _write_result)

GATEWAY_DB = os.environ.get("GATEWAY_DB", "/opt/liftlab-b3/cloud/gateway.db")
_LOCAL = datetime.now().astimezone()
TZ_NAME = _LOCAL.tzname() or time.strftime("%Z")
TZ_OFFSET = _LOCAL.strftime("%z")


def _iso(ts):
    """Epochs are absolute; every rendered string carries the zone NAME so a UTC/IST
    confusion cannot hide in a bare '2026-07-26 00:00:00'."""
    if not ts:
        return None
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} {TZ_NAME}"


def _labels(outdir):
    try:
        return json.loads((outdir / "labels.json").read_text())
    except (OSError, ValueError):
        return {}


def derive_boundary(gw, cam, db_path, min_side_hours=24, min_side_rows=200):
    """The nudge boundary from EVIDENCE: two-segment split of the camera's hourly
    with_floor rate (fraction of gw_door_event rows carrying a floor) maximising the
    before->after DROP — the step the wrong-space cells actually caused. File mtimes
    are not evidence. Returns None when the data cannot support a split."""
    import sqlite3
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = db.execute("SELECT ts, floor FROM gw_door_event WHERE gateway_id=? AND cam=? "
                          "ORDER BY ts", (gw, cam)).fetchall()
    except sqlite3.OperationalError:
        db.close()
        return None
    db.close()
    if len(rows) < 2 * min_side_rows:
        return None
    hours = {}
    for ts, floor in rows:
        h = int(float(ts) // 3600)
        n, f = hours.get(h, (0, 0))
        hours[h] = (n + 1, f + (1 if (floor is not None and str(floor) != "") else 0))
    hs = sorted(hours)
    if len(hs) < 2 * min_side_hours:
        return None
    N, F = [0], [0]
    for h in hs:
        n, f = hours[h]
        N.append(N[-1] + n)
        F.append(F[-1] + f)
    best = None
    for i in range(min_side_hours, len(hs) - min_side_hours + 1):
        nb, na = N[i], N[-1] - N[i]
        if nb < min_side_rows or na < min_side_rows:
            continue
        drop = F[i] / nb - (F[-1] - F[i]) / na
        if best is None or drop > best[0]:
            best = (drop, i, F[i] / nb, (F[-1] - F[i]) / na, nb, na)
    if best is None:
        return None
    drop, i, rb, ra, nb, na = best
    epoch = hs[i] * 3600.0
    return {"epoch": epoch, "iso": _iso(epoch),
            "with_floor_before": round(rb, 3), "with_floor_after": round(ra, 3),
            "drop": round(drop, 3), "rows_before": nb, "rows_after": na,
            "quality": "STRONG" if drop >= 0.25 else "WEAK",
            "source": "derived: gw_door_event hourly with_floor two-segment split "
                      "(evidence; epoch-based, timezone-independent)"}


def _resolve_boundary(gw, cam, boundary, db_path):
    """(epoch, info-dict). Explicit --boundary wins but is labelled stated-vs-guessed;
    otherwise derive from evidence and REFUSE to run on a weak derivation — a refused
    audit is diagnosable, a misclassified boundary day is not."""
    derived = derive_boundary(gw, cam, db_path)
    if boundary:
        dt = datetime.fromisoformat(boundary)
        stated = dt.tzinfo is not None
        bts = dt.timestamp()
        return bts, {"epoch": bts, "iso": _iso(bts),
                     "source": ("explicit --boundary (offset STATED)" if stated else
                                f"explicit --boundary (NAIVE — interpreted in {TZ_NAME} "
                                f"{TZ_OFFSET}, zone GUESSED from server)"),
                     "derived_for_comparison": derived}
    if derived is None:
        raise CalibError("cannot derive the boundary from gw_door_event (no DB / too little "
                         "data) — pass --boundary explicitly (with a UTC/IST offset)")
    if derived["quality"] == "WEAK":
        raise CalibError(f"derived boundary is WEAK (with_floor drop {derived['drop']} at "
                         f"{derived['iso']}) — not enough of a step to trust. Pass --boundary "
                         f"explicitly (with an offset). Candidate: {derived}")
    return derived["epoch"], derived


def _provenance(outdir, gw, cam, db_path):
    """crop filename -> floor_sample provenance by pixel match, exact then tolerant.
    Also returns {id: cam} ownership for the cross-fold leak check, and join stats —
    the join rate governs how much of the audit rests on mtimes."""
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
            by_shape.setdefault(arr.shape, []).append([r, arr, False])   # [row, pixels, used]
    prov, n_exact, n_tol = {}, 0, 0
    store, unmatched = [], []
    for p in sorted(glob.glob(str(outdir / "_calib_crop_*.png"))):
        im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if im is not None:
            store.append((Path(p).name, im))
    for fname, im in store:
        hit = None
        for ent in by_shape.get(im.shape, []):
            if not ent[2] and np.array_equal(im, ent[1]):
                hit, ent[2] = ent[0], True
                break
        if hit is not None:
            prov[fname] = {"floor_sample_id": int(hit["id"]), "ts": hit["ts"],
                           "received_at": hit["received_at"], "match": "exact"}
            n_exact += 1
        else:
            unmatched.append((fname, im))
    # tolerant second pass: crop_jpeg is lossy but the fold wrote the PNG from the
    # DECODED array, so exact should win on this box; tolerance only exists to survive
    # a jpeg-decoder version drift between fold-time and audit-time.
    for fname, im in unmatched:
        for ent in by_shape.get(im.shape, []):
            if ent[2]:
                continue
            if float(np.mean(np.abs(im.astype(np.int16) - ent[1].astype(np.int16)))) <= 1.0:
                prov[fname] = {"floor_sample_id": int(ent[0]["id"]), "ts": ent[0]["ts"],
                               "received_at": ent[0]["received_at"], "match": "tolerant"}
                ent[2] = True
                n_tol += 1
                break
    try:
        folded_ids = json.loads((outdir / "_folded.json").read_text())
    except (OSError, ValueError):
        folded_ids = []
    stats = {"n_store_pngs": len(store), "n_joined_exact": n_exact,
             "n_joined_tolerant": n_tol,
             "join_rate": round((n_exact + n_tol) / len(store), 3) if store else None,
             "n_folded_ids_recorded": len(folded_ids),
             "method": "pixel-exact first (PNG was written from the decoded crop_jpeg "
                       "array — lossless copy of the lossy source), then tolerant "
                       "mean|diff|<=1.0 for decoder drift; wizard-collected crops have "
                       "no floor_sample row and can never join (mtime is expected there)"}
    return prov, id_cam, stats


def _mtime_cluster_map(paths, window=2.0, min_n=4):
    """fname -> True for crops whose mtimes sit in a tight cluster (>= min_n within
    `window` seconds) — the signature of a cp/restore sweep, NOT of organic admission.
    A clustered mtime says when the sweep ran, nothing about when the crop was cut."""
    stamped = sorted((os.path.getmtime(p), Path(p).name) for p in paths)
    clustered, run = {}, []
    for mt, fname in stamped:
        if run and mt - run[-1][0] > window:
            if len(run) >= min_n:
                for _, f in run:
                    clustered[f] = True
            run = []
        run.append((mt, fname))
    if len(run) >= min_n:
        for _, f in run:
            clustered[f] = True
    return clustered


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


def audit(gw=None, cam=None, boundary=None, old_space="110x170", new_dims=None,
          archive="crops-pre-nudge-0722", db_path=None):
    """Full contamination report as a JSON-able dict; writes _alphabet_audit.json next
    to the crops. Read-only on all evidence."""
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
    dbp = db_path or GATEWAY_DB
    bts, boundary_info = _resolve_boundary(gwid, cam, boundary, dbp)
    w0, h0 = (int(x) for x in old_space.lower().split("x"))
    old_dims = {(w0, h0), (h0, w0)}
    drawn, drawn_src, cur_roi, cur_src = _cells_space(gwid, cam)
    if new_dims:
        nd = tuple(int(x) for x in new_dims.lower().split("x"))
        nd_src = "--new-dims (explicit)"
    else:
        nd, nd_src = cur_roi, f"current panel0 ROI ({cur_src})"

    prov_warn, prov, id_cam, join = None, {}, {}, None
    try:
        prov, id_cam, join = _provenance(outdir, gwid, cam, dbp)
    except CalibError as e:
        prov_warn = str(e)          # degrade to mtimes, but SAY so — never silently
    clustered = _mtime_cluster_map(crops)

    inventory = []
    for p in crops:
        fname = Path(p).name
        mtime = os.path.getmtime(p)
        pr = prov.get(fname)
        if pr and (pr["ts"] or pr["received_at"]):
            admitted = pr["ts"] or pr["received_at"]
            t_src = (f"floor_sample.{'ts (capture)' if pr['ts'] else 'received_at'} "
                     f"[{pr['match']} join]")
            reliable = True
        else:
            admitted = mtime
            t_src = "file mtime" + (" — CLUSTERED (cp-sweep era, unreliable)"
                                    if clustered.get(fname) else "")
            reliable = not clustered.get(fname)
        dims = _png_wh(p)
        rec = {"crop": fname, "label": str(labels.get(fname) or "").strip(),
               "dims": list(dims) if dims else None,
               "first_admitted": admitted, "first_admitted_iso": _iso(admitted),
               "time_source": t_src, "time_reliable": reliable,
               "floor_sample_id": pr and pr["floor_sample_id"],
               "file_mtime_iso": _iso(mtime),
               "flagged": admitted >= bts,
               "old_space": dims in old_dims if dims else None,
               "matches_new_dims": (dims is not None and nd is not None
                                    and abs(dims[0] - nd[0]) <= 1 and abs(dims[1] - nd[1]) <= 1),
               "already_quarantined": fname in quarantined_already}
        rec["contaminated"] = bool(rec["flagged"] and rec["old_space"])
        # trust boundary: contamination is only ACTIONABLE on a reliable clock — a
        # clustered mtime post-dating the boundary is exactly how a legitimately-old
        # crop gets mis-convicted, so those go to manual confirmation, never auto.
        rec["auto_quarantinable"] = bool(rec["contaminated"] and rec["time_reliable"])
        inventory.append(rec)
    inventory.sort(key=lambda r: r["first_admitted"])
    flagged = [r for r in inventory if r["flagged"]]
    contaminated = [r for r in inventory if r["contaminated"]]
    auto_q = [r for r in contaminated if r["auto_quarantinable"]]
    manual_q = [r for r in contaminated if not r["auto_quarantinable"]]

    # thin glyphs: before vs after withdrawing what quarantine will ACTUALLY remove.
    before, arrows = _glyph_counts({r["crop"]: r["label"] for r in inventory})
    after, _ = _glyph_counts({r["crop"]: r["label"] for r in inventory},
                             exclude={r["crop"] for r in auto_q})
    glyphs = {g: {"before": n, "after": after.get(g, 0), "arrow": g in arrows,
                  "quarantine_removes": n - after.get(g, 0)} for g, n in sorted(before.items())}
    total_loss = [g for g, d in glyphs.items() if d["after"] == 0 and d["quarantine_removes"]]
    thin_after = [g for g, d in glyphs.items() if d["after"] == 1 and not d["arrow"]]

    # BUILDABLE POPULATION — the question the redraw decision hangs on: after quarantine
    # AND the dims filter, what remains cuttable by the NEW cells, per glyph?
    buildable_crops = {r["crop"]: r["label"] for r in inventory
                       if r["label"] and r["label"] != "-"
                       and not r["contaminated"] and not r["already_quarantined"]
                       and r["matches_new_dims"]}
    buildable, _ = _glyph_counts(buildable_crops)
    min_ex = int(os.environ.get("MIN_GLYPH_EXAMPLES", "3"))
    buildable_admissible = sorted(g for g, n in buildable.items() if n >= min_ex)
    fresh_collect_needed = len(buildable_crops) == 0 or not buildable_admissible

    # archive reconcile: the crops-pre-nudge-0722 sweep should have taken the WHOLE
    # pre-boundary old-space population — anything old-space still live in the active
    # store escaped it.
    arch_dir = outdir / archive
    n_archived = len(glob.glob(str(arch_dir / "*.png"))) if arch_dir.exists() else None
    still_live_old = [r["crop"] for r in inventory if r["old_space"]]
    archive_reconcile = {
        "dir": str(arch_dir), "n_archived_pngs": n_archived,
        "active_old_space_total": len(still_live_old),
        "active_old_space_crops": still_live_old,
        "active_old_space_pre_boundary": [r["crop"] for r in inventory
                                          if r["old_space"] and not r["flagged"]],
        "interpretation": ("archive dir missing — cannot reconcile" if n_archived is None else
                           "clean: no old-space crops remain in the active store"
                           if not still_live_old else
                           f"{len(still_live_old)} old-space crops are STILL LIVE in the "
                           f"active store — the pre-nudge sweep did not take them")}

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

    result = {
        "gw": gwid, "cam": cam,
        "timezone": {"name": TZ_NAME, "utc_offset": TZ_OFFSET,
                     "note": "epochs are absolute; every ISO string in this report is "
                             "rendered in this zone and carries its name"},
        "boundary": boundary_info,
        "old_space": sorted(map(list, old_dims)),
        "new_dims": {"value": list(nd) if nd else None, "source": nd_src},
        "cells_draw_space": {"value": list(drawn) if drawn else None, "source": drawn_src},
        "provenance_warning": prov_warn, "join": join,
        "mtime_clustered_crops": sorted(f for f, v in clustered.items() if v),
        "n_crops": len(inventory), "n_flagged": len(flagged),
        "n_contaminated": len(contaminated),
        "n_auto_quarantinable": len(auto_q),
        "n_manual_confirmation_needed": len(manual_q),
        "manual_confirmation_needed": [r["crop"] for r in manual_q],
        "inventory": inventory,
        "glyphs": glyphs, "total_loss_glyphs": total_loss, "thin_after_quarantine": thin_after,
        "buildable": {"n_crops": len(buildable_crops), "per_glyph": buildable,
                      "admissible_at_min_examples": buildable_admissible,
                      "min_examples": min_ex,
                      "fresh_collect_needed": fresh_collect_needed},
        "archive_reconcile": archive_reconcile,
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
        "audited_at": time.time(), "audited_at_iso": _iso(time.time()),
    }
    return _write_result(outdir, "_alphabet_audit.json", result)


QUARANTINE_CRITERION = ("--quarantine acts on CONTAMINATED crops only (old-space dims "
                        "AND first-admitted >= boundary) with a RELIABLE clock — NEVER "
                        "on 'flagged' alone. A row marked only 'flagged' is untouched.")


def quarantine(gw=None, cam=None, boundary=None, old_space="110x170", new_dims=None,
               archive="crops-pre-nudge-0722", db_path=None, reason=None, yes=False):
    """Apply the audit's AUTO-quarantinable verdicts only — see QUARANTINE_CRITERION
    (printed, and enforced here: the selection is auto_quarantinable, never 'flagged').
    Without yes=True this is a preview: prints the target set, WRITES NOTHING.
    quarantine.json gains the evidence, labels.json entry -> '-'. Backup first, atomic
    writes, idempotent, never touches a png. Crops convicted only by a clustered mtime
    are SKIPPED and listed for manual confirmation."""
    result = audit(gw, cam, boundary, old_space, new_dims, archive, db_path)
    gwid, cam = gw or GW, cam or CAM
    outdir = _calib_dir(gwid, cam)
    targets = [r for r in result["inventory"] if r["auto_quarantinable"]]
    if not yes:
        result["quarantine_preview"] = {
            "criterion": QUARANTINE_CRITERION, "n_targets": len(targets),
            "targets": [r["crop"] for r in targets],
            "note": "NOTHING WRITTEN — re-run with --quarantine --yes to apply"}
        return result
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
                "time_source": r["time_source"],
                "floor_sample_id": r["floor_sample_id"],
                "reason": reason or (f"wrong_space_{old_space}_post_nudge "
                                     f"(boundary {result['boundary'].get('iso')})"),
                "flagged_at": time.time()}
        labels[f] = "-"
        applied.append(f)
    for name, obj in (("quarantine.json", q), ("labels.json", labels)):
        tmp = (outdir / name).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
        os.replace(tmp, outdir / name)
    _chown_tree(outdir)
    result["quarantine_applied"] = {
        "applied": applied, "already_quarantined": already,
        "skipped_unreliable_clock": [r["crop"] for r in result["inventory"]
                                     if r["contaminated"] and not r["auto_quarantinable"]],
        "labels_backup": backup,
        "restore": "to reverse: copy each quarantine.json entry's 'label' back into "
                   "labels.json (or restore the .bak) — the crop pngs were never touched"}
    return _write_result(outdir, "_alphabet_audit.json", result)


def provenance(gw=None, cam=None, archive="crops-pre-nudge-0722"):
    """Label provenance for the active store: (a) hand-labeled, (b) auto-labeled by the
    reader, or (c) INHERITED BY INDEX from a swept population (labels.json is keyed by
    filename; collect_crops restarts numbering on an emptied store, so fresh crops wear
    the swept crops' filenames — and therefore their labels).

    (b) is impossible by code: collect_crops never writes labels.json (grep it — crops
    and montages only). The decision is between (a) and (c), on this evidence:
      * ORPHAN TAIL: labels.json entries for filenames beyond the store's range are the
        swept population's labels still sitting in the live file.
      * FILENAME OVERLAP with the archive: the inheritance mechanism itself.
      * labels.json mtime vs crop mtimes (weak — any single picker save renews it).
      * archive labels.json per-index equality, when the sweep copied one.
      * labels_bind.json: bound+matching labels are PROVEN hand-labels for that content;
        v1 dirs have no bindings, which is why this question is hard — and why saves
        now bind."""
    gwid, cam = gw or GW, cam or CAM
    outdir = _calib_dir(gwid, cam)
    crops = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not crops:
        raise CalibError(f"no _calib_crop_*.png under {outdir}")
    labels = _labels(outdir)
    bind = _load_bind(outdir)
    store = [Path(p).name for p in crops]
    store_set = set(store)
    mtimes = {Path(p).name: os.path.getmtime(p) for p in crops}
    lj = outdir / "labels.json"
    lj_mtime = os.path.getmtime(lj) if lj.exists() else None
    orphans = sorted(k for k in labels if k not in store_set)
    arch_dir = outdir / archive
    arch_names = sorted(Path(p).name for p in glob.glob(str(arch_dir / "_calib_crop_*.png")))
    overlap = sorted(store_set & set(arch_names))
    arch_labels = None
    if (arch_dir / "labels.json").exists():
        try:
            arch_labels = json.loads((arch_dir / "labels.json").read_text())
        except (OSError, ValueError):
            arch_labels = None

    rows = []
    for f in store:
        b = bind.get(f)
        bstat = ("none" if b is None else
                 "match" if _sha16(outdir / f) == b else "MISMATCH")
        rows.append({"crop": f, "dims": list(_png_wh(outdir / f) or ()) or None,
                     "label": str(labels.get(f) or "").strip(),
                     "mtime_iso": _iso(mtimes[f]),
                     "in_archive": f in set(arch_names),
                     "archive_label_same": (None if arch_labels is None else
                                            arch_labels.get(f) == labels.get(f)),
                     "binding": bstat})

    labeled_rows = [r for r in rows if r["label"] and r["label"] != "-"]
    proven_hand = [r["crop"] for r in rows if r["binding"] == "match"]
    inherited = bool(overlap) and (bool(orphans) or
                                   (lj_mtime is not None and mtimes and
                                    lj_mtime < min(mtimes.values())))
    if inherited:
        verdict = ("INHERITED_BY_INDEX: the store's filenames overlap the swept "
                   f"population ({len(overlap)} of {len(store)}) and labels.json still "
                   f"carries {len(orphans)} orphaned entries for swept crops — the live "
                   "labels are the OLD population's labels re-attached to new pixels. "
                   "Every unbound label is wrong-for-its-crop until re-confirmed. "
                   "VOID before any build (--void-labels).")
    elif labeled_rows and len(proven_hand) == len(labeled_rows):
        verdict = "HAND_LABELED (every label is content-bound and matches its crop)"
    else:
        verdict = ("INCONCLUSIVE: no orphan tail / archive overlap proving inheritance, "
                   "but v1 labels carry no per-entry timestamps or bindings — only "
                   f"{len(proven_hand)}/{len(labeled_rows)} labels are content-proven. "
                   "Re-confirm in the picker (each save binds) if in doubt.")
    result = {"gw": gwid, "cam": cam, "verdict": verdict,
              "auto_label_possible": False,
              "auto_label_note": "collect_crops writes crops + montages only — it has "
                                 "no labels.json write path, so (b) is impossible",
              "n_store": len(store), "n_labels_total": len(labels),
              "n_orphaned_labels": len(orphans), "orphaned_labels": orphans,
              "labels_json_mtime_iso": _iso(lj_mtime),
              "oldest_crop_iso": _iso(min(mtimes.values())) if mtimes else None,
              "newest_crop_iso": _iso(max(mtimes.values())) if mtimes else None,
              "archive": {"dir": str(arch_dir), "n_pngs": len(arch_names),
                          "has_own_labels_json": arch_labels is not None,
                          "filename_overlap_with_store": len(overlap)},
              "per_index": rows,
              "timezone": {"name": TZ_NAME, "utc_offset": TZ_OFFSET}}
    return _write_result(outdir, "_label_provenance.json", result)


def void_labels(gw=None, cam=None, yes=False):
    """Void the ACTIVE label set: labels.json and labels_bind.json move to timestamped
    .void.* siblings (evidence kept, trust withdrawn — same posture as quarantine) and
    empty files take their place. For the inherited-by-index verdict, where every label
    is wrong for the crop it sits on. Preview unless yes=True."""
    gwid, cam = gw or GW, cam or CAM
    outdir = _calib_dir(gwid, cam)
    labels = _labels(outdir)
    plan = {"gw": gwid, "cam": cam, "n_labels_voided": len(labels),
            "moves": [f"{n} -> {n}.void.<ts>" for n in ("labels.json", "labels_bind.json")
                      if (outdir / n).exists()]}
    if not yes:
        plan["note"] = "NOTHING WRITTEN — re-run with --void-labels --yes to apply"
        return plan
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in ("labels.json", "labels_bind.json"):
        p = outdir / n
        if p.exists():
            shutil.move(str(p), str(outdir / f"{n}.void.{stamp}"))
    (outdir / "labels.json").write_text("{}")
    _chown_tree(outdir)
    plan["voided_at"] = _iso(time.time())
    return _write_result(outdir, "_label_void.json", plan)


def migrate_labels(gw=None, cam=None, archive="crops-pre-nudge-0722", yes=False):
    """Content-key the EXISTING labels: bind every current label to its crop's current
    pixels in labels_bind.json. REFUSES on an inherited-by-index verdict — binding would
    launder wrong labels into 'proven' ones; void first, relabel in the picker (which
    binds each save). Orphaned labels (crop file gone) are left in labels.json as
    evidence but never bound and never used: build pairs by store glob and the picker
    drops them from its queue, so they are inert. Preview unless yes=True."""
    gwid, cam = gw or GW, cam or CAM
    prov = provenance(gwid, cam, archive)
    if prov["verdict"].startswith("INHERITED_BY_INDEX"):
        raise CalibError("REFUSING to bind: provenance says the labels are inherited by "
                         "index — binding them would certify wrong labels as "
                         "content-proven. --void-labels first, then relabel.")
    outdir = _calib_dir(gwid, cam)
    labels = _labels(outdir)
    bind = _load_bind(outdir)
    to_bind = {f: _sha16(outdir / f) for f, lab in labels.items()
               if str(lab or "").strip() and str(lab).strip() != "-"
               and (outdir / f).exists() and bind.get(f) is None}
    result = {"gw": gwid, "cam": cam, "n_newly_bound": len(to_bind),
              "n_already_bound": len(bind), "n_orphaned_left_unbound":
                  sum(1 for f in labels if not (outdir / f).exists()),
              "provenance_verdict": prov["verdict"]}
    if not yes:
        result["note"] = "NOTHING WRITTEN — re-run with --migrate-labels --yes to apply"
        return result
    bind.update(to_bind)
    _save_bind(outdir, bind)
    _chown_tree(outdir)
    return _write_result(outdir, "_label_migrate.json", result)


def main():
    ap = argparse.ArgumentParser(description="alphabet contamination audit v2 (flag, don't delete)")
    ap.add_argument("--gw", default=GW)
    ap.add_argument("--cam", default=CAM)
    ap.add_argument("--boundary", default=None,
                    help="override the evidence-derived boundary (ISO; include a +05:30/+00:00 "
                         "offset to make the zone STATED rather than guessed)")
    ap.add_argument("--old-space", default="110x170", help="pre-nudge panel dims WxH")
    ap.add_argument("--new-dims", default=None,
                    help="post-nudge panel dims WxH (default: current panel0 ROI)")
    ap.add_argument("--archive", default="crops-pre-nudge-0722",
                    help="subdir holding the already-swept pre-nudge crops")
    ap.add_argument("--db", default=None, help="gateway.db path (default $GATEWAY_DB)")
    ap.add_argument("--quarantine", action="store_true",
                    help=QUARANTINE_CRITERION + " Preview without --yes.")
    ap.add_argument("--provenance", action="store_true",
                    help="label provenance report: hand-labeled vs inherited-by-index")
    ap.add_argument("--void-labels", action="store_true",
                    help="void the active label set (inherited-by-index remedy); needs --yes")
    ap.add_argument("--migrate-labels", action="store_true",
                    help="content-bind existing labels (refuses on inherited verdict); needs --yes")
    ap.add_argument("--yes", action="store_true",
                    help="actually write for --quarantine/--void-labels/--migrate-labels")
    a = ap.parse_args()

    if a.provenance:
        r = provenance(a.gw, a.cam, a.archive)
        print(f'timezone: {r["timezone"]["name"]} (UTC{r["timezone"]["utc_offset"]})')
        for row in r["per_index"]:
            print(f'{row["crop"]}  dims={row["dims"]}  label={row["label"] or "(none)"!r:8s} '
                  f'mtime={row["mtime_iso"]}  in_archive={row["in_archive"]} '
                  f'archive_label_same={row["archive_label_same"]}  binding={row["binding"]}')
        print(f'\nstore={r["n_store"]} crops; labels.json holds {r["n_labels_total"]} entries '
              f'({r["n_orphaned_labels"]} ORPHANED — swept crops); labels.json mtime '
              f'{r["labels_json_mtime_iso"]}; crops {r["oldest_crop_iso"]} .. {r["newest_crop_iso"]}')
        ar = r["archive"]
        print(f'archive: {ar["n_pngs"]} pngs, own labels.json={ar["has_own_labels_json"]}, '
              f'filename overlap with store: {ar["filename_overlap_with_store"]}')
        print(f'auto-label (b): impossible — {r["auto_label_note"]}')
        print(f'\nVERDICT: {r["verdict"]}')
        return
    if a.void_labels:
        r = void_labels(a.gw, a.cam, yes=a.yes)
        print(json.dumps(r, indent=2))
        return
    if a.migrate_labels:
        r = migrate_labels(a.gw, a.cam, a.archive, yes=a.yes)
        print(json.dumps(r, indent=2))
        return

    if a.quarantine:
        r = quarantine(a.gw, a.cam, a.boundary, a.old_space, a.new_dims, a.archive, a.db,
                       yes=a.yes)
    else:
        r = audit(a.gw, a.cam, a.boundary, a.old_space, a.new_dims, a.archive, a.db)

    b = r["boundary"]
    print(f'timezone: {r["timezone"]["name"]} (UTC{r["timezone"]["utc_offset"]})')
    print(f'boundary: {b.get("iso")}  [{b.get("source")}]')
    if b.get("with_floor_before") is not None:
        print(f'  with_floor {b["with_floor_before"]} -> {b["with_floor_after"]} '
              f'(drop {b["drop"]}, {b["rows_before"]}/{b["rows_after"]} rows, {b["quality"]})')
    if r["join"]:
        j = r["join"]
        print(f'join rate: {j["join_rate"]} of {j["n_store_pngs"]} store PNGs '
              f'({j["n_joined_exact"]} exact + {j["n_joined_tolerant"]} tolerant; '
              f'_folded.json records {j["n_folded_ids_recorded"]} ids) — unjoined crops '
              f'run on file mtimes')
    if r["provenance_warning"]:
        print(f'WARNING: {r["provenance_warning"]} — ALL timestamps are file mtimes')
    if r["mtime_clustered_crops"]:
        print(f'mtime clusters: {len(r["mtime_clustered_crops"])} crops share cp-sweep '
              f'mtimes — never auto-flagged on time alone')
    print()
    for rec in r["inventory"]:
        mark = ("CONTAMINATED" if rec["auto_quarantinable"] else
                "contaminated? (manual — unreliable clock)" if rec["contaminated"] else
                "flagged" if rec["flagged"] else "ok")
        print(f'{rec["first_admitted_iso"]}  {rec["crop"]}  label={rec["label"] or "(none)"!r:8s} '
              f'dims={rec["dims"]}  [{rec["time_source"]}]  {mark}')
    print(f'\n{r["n_crops"]} crops: {r["n_flagged"]} flagged, {r["n_contaminated"]} contaminated '
          f'({r["n_auto_quarantinable"]} auto, {r["n_manual_confirmation_needed"]} need manual '
          f'time confirmation)')
    print(f'total-loss glyphs: {r["total_loss_glyphs"] or "none"};  thin (n=1) after: '
          f'{r["thin_after_quarantine"] or "none"}')

    bd = r["buildable"]
    print(f'\nBUILDABLE vs new dims {r["new_dims"]["value"]} [{r["new_dims"]["source"]}]: '
          f'{bd["n_crops"]} crops; per glyph: {bd["per_glyph"] or "{}"}; '
          f'admissible at min_examples={bd["min_examples"]}: '
          f'{bd["admissible_at_min_examples"] or "NONE"}')
    if bd["fresh_collect_needed"]:
        print('PLAIN STATEMENT: the redraw does NOT restore floor attribution — it only '
              'enables a fresh Collect. Templates must be rebuilt from crops that do not '
              'exist yet.')
    ar = r["archive_reconcile"]
    print(f'\narchive reconcile: {ar["n_archived_pngs"]} pngs in {ar["dir"]}; '
          f'{ar["active_old_space_total"]} old-space still in active store '
          f'({len(ar["active_old_space_pre_boundary"])} pre-boundary). {ar["interpretation"]}')
    ls = r["leak_scan"]
    print(f'leak scan over {ls["other_cams_scanned"]}: '
          f'{len(ls["byte_identical_crops"])} byte-identical crops, '
          f'{len(ls["cross_camera_folds"])} cross-camera folds'
          + (" — FLEET-WIDE PROBLEM, see _alphabet_audit.json"
             if ls["byte_identical_crops"] or ls["cross_camera_folds"] else " — clean"))
    if r["contaminated_possibly_in_current_npz"]:
        print(f'current npz may contain {len(r["contaminated_possibly_in_current_npz"])} '
              f'contaminated crops — rebuild (with the dims filter deployed) after quarantine')
    print(f'\n{QUARANTINE_CRITERION}')
    if a.quarantine and "quarantine_applied" in r:
        qa = r["quarantine_applied"]
        print(f'QUARANTINED {len(qa["applied"])} ({len(qa["already_quarantined"])} already; '
              f'{len(qa["skipped_unreliable_clock"])} skipped on unreliable clock); '
              f'labels backup: {qa["labels_backup"]}')
    elif a.quarantine:
        qp = r["quarantine_preview"]
        print(f'PREVIEW: {qp["n_targets"]} target(s): {qp["targets"] or "none"} — '
              f'{qp["note"]}')
    else:
        print(f'report only — targets under the criterion right now: '
              f'{sum(1 for x in r["inventory"] if x["auto_quarantinable"])}')


if __name__ == "__main__":
    try:
        main()
    except CalibError as e:
        print(f"CALIB ERROR: {e}")
        raise SystemExit(2)
