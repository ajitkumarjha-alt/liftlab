#!/usr/bin/env python3
"""door/floor CALIBRATION (steps 1-2). RUNS ON liftlab-cloud (no token, no network hop): reads the
segments already in /dev/shm/liftlab-live/site-A/ch29/ and writes crops + renders to the DURABLE calib
dir (/var/lib/liftlab/calib, NOT /run tmpfs — the --build source crops must survive a service restart),
served read-only at lift.gargi.online/calib/site-A/ch29/_calib_*.jpg . Posts nothing; never touches gw_event.

  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py                 # confirm ROIs (1 frame)
  sudo -E /opt/liftlab-analysis/.venv/bin/python door_calib.py --collect 40    # montage over 40 frames
Override boxes:  PANEL_ROIS="x,y,w,h;x,y,w,h"   DOOR_ROI="450,0,568,900"  (door_roi is CALIB space)
View:  https://lift.gargi.online/calib/site-A/ch29/_calib_frame.jpg  (and _calib_p0 / _calib_p1 / _calib_glyphs)
"""
import argparse
import glob
import io
import json
import os
import time
from pathlib import Path

import gpu_door as gd


class CalibError(Exception):
    """A recoverable calibration failure (bad input, nothing collected yet, geometry not measurable).
    EVERY library function below raises THIS, never SystemExit — a web endpoint maps it to a 4xx, the
    CLI prints it and exits 2. SystemExit would kill a uvicorn worker, so it's confined to __main__."""


# --- WEB-CALLABLE contract -------------------------------------------------------------------------
# door_calib is a LIBRARY first, a CLI second: render_rois / collect_crops / propose_cells /
# build_from_crops each take explicit params (env only as a default), return a JSON-able dict, write
# their artifacts to the served calib dir, and raise CalibError (not SystemExit) on bad input. main()
# is a thin argparse shell over them so the same code path backs a future /calibrate web action.


def _calib_dir(gw, cam):
    d = CALIB_DIR / gw / cam                 # DURABLE (survives restart); served at /calib/{gw}/{cam}/
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url(gw, cam, fname):
    return f"{CLOUD}/calib/{gw}/{cam}/{fname}"


def _service_owner():
    """(uid, gid) that should own calib files so the web app — which runs as the SERVICE user (liftlab)
    — can overwrite labels.json and re-collect doesn't strand root-owned crops. Prefer CALIB_OWNER env
    ('user' or 'user:group'); else INHERIT the existing calib dir's owner (so a manual chown sticks);
    else 'liftlab'. Returns None if the user can't be resolved (leave ownership alone)."""
    import grp
    import pwd
    spec = os.environ.get("CALIB_OWNER", "").strip()
    if not spec and CALIB_DIR.exists():
        try:
            st = CALIB_DIR.stat()
            return st.st_uid, st.st_gid          # inherit whatever the tree already is
        except OSError:
            pass
    user, _, group = spec.partition(":")
    try:
        pw = pwd.getpwnam(user or "liftlab")
        gid = grp.getgrnam(group).gr_gid if group else pw.pw_gid
        return pw.pw_uid, gid
    except (KeyError, OSError):
        return None


def _chown_tree(path):
    """When running as ROOT (sudo --collect etc.), hand the calib tree to the service user + make it
    group-writable, so the web app can write labels.json and a later re-collect doesn't strand
    root-owned files. No-op unless we're root on POSIX; best-effort — never fails the calibration run."""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    owner = _service_owner()
    if not owner:
        return
    import stat as _stat
    uid, gid = owner
    for p in [path, *path.rglob("*")]:
        try:
            os.chown(p, uid, gid)
            m = p.stat().st_mode | _stat.S_IWGRP
            if p.is_dir():
                m |= _stat.S_IXGRP
            os.chmod(p, m)
        except OSError:
            pass


def _write_result(outdir, name, result):
    """Persist a step's structured result as JSON next to its images so a web poll can read the
    outcome (+ artifact URLs) without re-running the step. The single chokepoint every command routes
    through, so it's also where we hand the tree back to the service user (see _chown_tree)."""
    try:
        (outdir / name).write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    _chown_tree(outdir)
    return result

GW = os.environ.get("GW", "site-A")
CAM = os.environ.get("CAM", "ch29")
CLOUD = os.environ.get("CLOUD_URL", "https://lift.gargi.online").rstrip("/")
LIVE_DIR = Path(os.environ.get("LIVE_DIR", "/dev/shm/liftlab-live"))
# DURABLE calibration dir — NOT /run (tmpfs): the --build source crops must survive a service restart
# (an apply_* deploy wiped them from RAM once). Served (read-only) at /calib/{gw}/{cam}/ by ops_api.
CALIB_DIR = Path(os.environ.get("CALIB_DIR", "/var/lib/liftlab/calib"))
CALIB_WH = (int(os.environ.get("CALIB_W", "1920")), int(os.environ.get("CALIB_H", "1080")))
DOOR_ROI = [int(x) for x in os.environ.get("DOOR_ROI", "450,0,568,900").split(",")]      # CALIB space (scaled)
# The scaled door_roi landed on the LEFT WALL (panel surface), not the leaf — the Pi's brightness
# scalar tolerates a correlated-but-wrong ROI; edge geometry does NOT. Override in FRAME px and eyeball:
DOOR_ROI_FRAME = os.environ.get("DOOR_ROI_FRAME", "")    # "x,y,w,h" in 704x576 frame px, used DIRECTLY
# panel ROIs in FRAME (704x576) px — CONFIRMED tight, both agree frame-for-frame.
PANEL_ROIS_DEFAULT = "124,116,51,92;379,44,40,89"
# FIXED-PITCH cells WITHIN the panel0 crop (x relative to the panel's left edge). No gap segmentation:
# HEVC smears the 1-2px inter-digit gap, so we crop known cell positions instead. Measure off the
# _calib_p0 ruler (absolute frame px) minus the panel origin. Set DIGIT_CELLS + ARROW_CELL for --build.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "/var/lib/liftlab/templates")


def _png_wh(path):
    """(w, h) straight from a PNG IHDR — no decode. None on anything unreadable."""
    import struct as _struct
    try:
        with open(path, "rb") as f:
            head = f.read(26)
    except OSError:
        return None
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = _struct.unpack(">II", head[16:24])
    return int(w), int(h)


def _cells_space(gw=None, cam=None, cells_explicit=False):
    """The panel space the ACTIVE cells were drawn in, mirroring _as_cells precedence.

    Explicit/env cells carry no recorded space (a 'x,y,w,h' string has no panel in it), so for
    those the current panel0 ROI dims are the only available statement of the space — GUESSED,
    and said so. roi.json cells carry panel_wh recorded by the /calib-cells wizard at draw time
    — STATED. Returns (drawn_space, drawn_src, current_roi_dims, roi_src); either pair may be
    (None, <why>)."""
    prois, psrc = _panel_rois(gw, cam)
    cur, cur_src = ((int(prois[0][2]), int(prois[0][3])), psrc) if prois else (None, psrc)
    if cells_explicit:
        return None, "explicit cells param (no recorded draw space)", cur, cur_src
    if os.environ.get("DIGIT_CELLS", ""):
        return None, "DIGIT_CELLS env (no recorded draw space)", cur, cur_src
    wh = (_roi_json(gw, cam).get("cells") or {}).get("panel_wh")
    if wh:
        return (int(wh[0]), int(wh[1])), "roi.json cells.panel_wh (recorded at draw)", cur, cur_src
    return None, "no recorded draw space", cur, cur_src


def _sha16(path):
    """First 16 hex chars of the file's sha256 — the content identity a label binds to."""
    import hashlib as _hl
    try:
        return _hl.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _load_bind(outdir):
    """labels_bind.json: crop filename -> sha16 of the png the label was SAVED against.
    Filenames are reused (collect_crops restarts numbering on an emptied store — the ch16
    label-inheritance postmortem, 2026-07-30), so the binding, not the filename, is the
    key of record. Missing/corrupt file => {} (legacy dir, nothing bound yet)."""
    try:
        v = json.loads((outdir / "labels_bind.json").read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_bind(outdir, bind):
    tmp = (outdir / "labels_bind.json").with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bind, indent=2, sort_keys=True))
    os.replace(tmp, outdir / "labels_bind.json")


def _parse_cells(s):
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out


def _roi_json(gw=None, cam=None):
    """The /calib-roi wizard's drawn boxes: {door_roi_frame:[x,y,w,h], panel_rois:[[x,y,w,h],...]}
    in FRAME px. Read ONLY when the corresponding env is absent — env still wins, so ch29's
    established flow is byte-for-byte unchanged. Missing/corrupt file => {} (never raises: a
    calibration aid must not break a run that was configured by env)."""
    try:
        v = json.loads((_calib_dir(gw or GW, cam or CAM) / "roi.json").read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _panel_rois(gw=None, cam=None):
    """Panel ROIs with an explicit precedence: env > roi.json > built-in default.

    The built-in default is CH29's geometry. On any other camera it is not a default, it is another
    lift's panel — and it fails as quietly bad OCR rather than as an error. So the source is returned
    alongside the boxes and surfaced by render_rois; 'default' on a new camera means STOP.
    """
    env = os.environ.get("PANEL_ROIS", "")
    if env:
        src = "PANEL_ROIS env"
    else:
        drawn = _roi_json(gw, cam).get("panel_rois")
        if drawn:
            return [tuple(int(v) for v in p) for p in drawn], "roi.json (/calib-roi wizard)"
        env, src = PANEL_ROIS_DEFAULT, "BUILT-IN DEFAULT (ch29 geometry — wrong for any other camera)"
    out = []
    for part in env.split(";"):
        part = part.strip()
        if part:
            out.append(tuple(int(v) for v in part.split(",")))
    return out, src


# The /dev/shm live ring rotates in SECONDS. ANY glob-then-stat/read races: a .ts globbed a moment ago
# can be gone before getmtime/open. Every LIVE_DIR reader below tolerates a mid-scan vanish (skip it,
# don't crash). Crucially the mtime SORT is done via _stat_mtime, never a bare key=os.path.getmtime
# (which raises FileNotFoundError from inside sorted() on a rotated-out file — the reported crash).
def _stat_mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:                                  # FileNotFoundError incl. — rotated out mid-scan
        return None


def _live_segs(gw=None, cam=None, retry=True):
    """Newest-first list of live .ts, robust to the ring rotating during the scan: glob, stat each
    (dropping any that vanished), sort by surviving mtime. Retries the glob ONCE if the whole snapshot
    raced away, then returns [] (callers decide: skip / CalibError)."""
    gw = gw or GW; cam = cam or CAM
    d = LIVE_DIR / gw / cam
    for _ in (0, 1):
        pairs = [(mt, p) for p in glob.glob(str(d / "*.ts")) for mt in (_stat_mtime(p),) if mt is not None]
        if pairs:
            pairs.sort(reverse=True)                 # newest first
            return [p for _, p in pairs]
        if not retry:
            break
    return []


def _newest_seg():
    segs = _live_segs()
    return segs[0] if segs else None                 # newest first


def _decode_last_frame(seg_path):
    """Last decoded frame of a segment, or None if it raced away / is a truncated partial write. Never
    raises — the ring can delete or half-overwrite seg_path between selection and this read."""
    import av
    try:
        with open(seg_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None                                  # rotated out between selection and open
    try:
        c = av.open(io.BytesIO(data))
        fr = None
        for f in c.decode(video=0):
            fr = f.to_ndarray(format="bgr24")
        c.close()
        return fr
    except Exception:
        return None                                  # truncated / non-decodable partial segment


def _newest_frame_http():                        # fallback if run somewhere without the local segments
    import requests
    try:
        tok = os.environ.get("ANALYSIS_TOKEN", "").split(":")[-1]
        base = f"{CLOUD}/api/gw/{GW}/live/{CAM}"
        h = {"Authorization": "Bearer " + tok}
        pl = requests.get(f"{base}/index.m3u8", headers=h, timeout=15).text
        segs = [ln.strip() for ln in pl.splitlines() if ln.strip().endswith(".ts")]
        if not segs:
            return None
        data = requests.get(f"{base}/{segs[-1]}", headers=h, timeout=15).content
    except Exception:
        return None                                  # network / 404 (segment rotated out server-side)
    import av
    try:
        c = av.open(io.BytesIO(data)); fr = None
        for f in c.decode(video=0):
            fr = f.to_ndarray(format="bgr24")
        c.close()
        return fr
    except Exception:
        return None


def newest_frame():
    seg = _newest_seg()
    if seg:
        fr = _decode_last_frame(seg)                 # None if it raced away -> fall back to HTTP
        if fr is not None:
            return fr
    return _newest_frame_http()


def _ruler(crop_bgr, factor=12, step=5, x0=0, y0=0):
    """Upscale a crop + draw a labelled grid in SOURCE (frame) coords so the operator reads exact
    x/y boundaries. x0/y0 offset the labels to the crop's position in the full frame (so the numbers
    are absolute frame px, ready to type into DOOR_ROI_FRAME / PANEL_ROIS)."""
    import cv2
    h, w = crop_bgr.shape[:2]
    big = cv2.resize(crop_bgr, (w * factor, h * factor), interpolation=cv2.INTER_NEAREST)
    for x in range(0, w + 1, step):
        cv2.line(big, (x * factor, 0), (x * factor, h * factor), (0, 200, 0), 1)
        cv2.putText(big, str(x0 + x), (x * factor + 1, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 200, 0), 1)
    for y in range(0, h + 1, step):
        cv2.line(big, (0, y * factor), (w * factor, y * factor), (0, 120, 0), 1)
        cv2.putText(big, str(y0 + y), (1, y * factor + 9), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 120, 0), 1)
    return big


def _frame_grid(img, step=40):
    """Faint labelled coordinate grid on the full frame, so the leaf seam (~x340-380) and the door
    box can be read off directly."""
    import cv2
    out = img.copy()
    H, W = out.shape[:2]
    for x in range(0, W, step):
        cv2.line(out, (x, 0), (x, H), (60, 60, 60), 1)
        cv2.putText(out, str(x), (x + 1, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 60), 1)
    for y in range(0, H, step):
        cv2.line(out, (0, y), (W, y), (60, 60, 60), 1)
        cv2.putText(out, str(y), (1, y + 9), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 60), 1)
    return out


def _montage(crops, cols, factor):
    import cv2
    import numpy as np
    if not crops:
        return None
    ups = [cv2.resize(c, (0, 0), fx=factor, fy=factor, interpolation=cv2.INTER_NEAREST) for c in crops]
    hh = max(u.shape[0] for u in ups); ww = max(u.shape[1] for u in ups)
    rows = []
    for k in range(0, len(ups), cols):
        row = ups[k:k + cols]
        cells = [np.pad(u, ((0, hh - u.shape[0]), (0, ww - u.shape[1]), (0, 0))) for u in row]
        while len(cells) < cols:
            cells.append(np.zeros((hh, ww, 3), np.uint8))
        rows.append(np.hstack(cells))
    return np.vstack(rows)


def render_index(gw=None, cam=None, outdir=None, cols=6):
    """Montage of every collected crop with its INDEX drawn on it, so the operator can pick a good
    ANCHOR crop (a two-digit + arrow + queue tile) by number for --anchor. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    paths = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not paths:
        raise CalibError("no _calib_crop_*.png — run collect first")
    F = 8
    tiles = []
    for p in paths:
        idx = int(Path(p).stem.split("_")[-1])
        im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        big = cv2.resize(cv2.cvtColor(im, cv2.COLOR_GRAY2BGR),
                         (im.shape[1] * F, im.shape[0] * F), interpolation=cv2.INTER_NEAREST)
        cv2.rectangle(big, (0, 0), (big.shape[1] - 1, big.shape[0] - 1), (0, 120, 0), 1)
        cv2.putText(big, str(idx), (2, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        tiles.append(big)
    cv2.imwrite(str(outdir / "_calib_index.jpg"), _montage(tiles, cols=cols, factor=1))
    result = {"gw": gwid, "cam": cam, "n": len(paths), "index_url": _url(gwid, cam, "_calib_index.jpg")}
    return _write_result(outdir, "_calib_index.json", result)


def render_anchor(crop, gw=None, cam=None, outdir=None):
    """Enlarge one crop with a FINE within-panel-px ruler so the operator can READ the anchor coords off
    it: tens-digit left, units-digit left, digit top, digit bottom, arrow left. Pick a two-digit + arrow
    (+ queue) crop by number from --index. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    p = outdir / f"_calib_crop_{int(crop):03d}.png"
    if not p.exists():
        raise CalibError(f"no {p.name} — pick an index shown by --index")
    im = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    ruled = _ruler(cv2.cvtColor(im, cv2.COLOR_GRAY2BGR), factor=14, step=2)   # labels in WITHIN-PANEL px
    cv2.imwrite(str(outdir / "_calib_anchor.jpg"), ruled)
    result = {"gw": gwid, "cam": cam, "crop": int(crop),
              "anchor_url": _url(gwid, cam, "_calib_anchor.jpg"),
              "read": "tens_left, units_left, digit_top, digit_bottom, arrow_left  (within-panel px off the ruler)"}
    return _write_result(outdir, "_calib_anchor.json", result)


def panelcheck(gw=None, cam=None, n=8, ref=55, outdir=None):
    """Dump N recent LIVE panel0 extractions next to a reference calib crop, cells drawn on each — so a
    GEOMETRY OFFSET is visible (digits inside the boxes on the calib crop, shifted out of them on live =
    the panel moved). Reads the newest segments directly (no collect loop). WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    prois, _psrc = _panel_rois(gwid, cam)
    if not prois:
        raise CalibError("no PANEL_ROIS set, no roi.json — draw the boxes at /calib-roi/{}/{}".format(gwid, cam))
    segs = _live_segs(gwid, cam)                     # newest-first, race-hardened (ring rotates in seconds)
    if not segs:
        raise CalibError(f"no live segments in {LIVE_DIR / gwid / cam} (ring empty / relay down)")
    tiles = []
    for sp in segs:                                  # each glob path is distinct content; decode until n
        if len(tiles) >= n:
            break
        fr = _decode_last_frame(sp)                  # None if it raced away / truncated -> skip
        if fr is not None:
            tiles.append(cv2.cvtColor(gd.crop(fr, prois[0]), cv2.COLOR_BGR2GRAY))
    dcells = _as_cells(None, "DIGIT_CELLS", gwid, cam)
    acell = _as_cells(None, "ARROW_CELL", gwid, cam)
    F = 10

    def _tile(gray, label, color):
        big = cv2.resize(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
                         (gray.shape[1] * F, gray.shape[0] * F), interpolation=cv2.INTER_NEAREST)
        for (x, y, w, h) in dcells:                  # where the reader crops — digits should sit INSIDE
            cv2.rectangle(big, (x * F, y * F), ((x + w) * F, (y + h) * F), (0, 220, 0), 1)
        if acell:
            x, y, w, h = acell[0]
            cv2.rectangle(big, (x * F, y * F), ((x + w) * F, (y + h) * F), (200, 0, 200), 1)
        cv2.putText(big, label, (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, color, 1)
        return big

    rendered = []
    refp = outdir / f"_calib_crop_{int(ref):03d}.png"
    if refp.exists():
        rendered.append(_tile(cv2.imread(str(refp), cv2.IMREAD_GRAYSCALE), f"calib#{int(ref)}", (0, 180, 255)))
    else:
        print(f"[panelcheck] ref crop {refp.name} not found — showing live only")
    for i, t in enumerate(tiles):
        rendered.append(_tile(t, f"live{i}", (255, 255, 255)))
    if not rendered:
        raise CalibError("no tiles (no ref crop, no decodable segments)")
    cv2.imwrite(str(outdir / "_calib_panelcheck.jpg"), _montage(rendered, cols=4, factor=1))
    result = {"gw": gwid, "cam": cam, "n_live": len(tiles), "ref": int(ref),
              "url": _url(gwid, cam, "_calib_panelcheck.jpg"),
              "note": "green=digit cells, magenta=arrow. Digits inside the boxes on calib but shifted out on "
                      "live = the panel moved (the ±2px shift search absorbs it at read time)."}
    _write_result(outdir, "_calib_panelcheck.json", result)
    print(f"[panelcheck] {len(tiles)} live panel0 crops vs calib#{int(ref)} -> {result['url']}")
    print(f"[panelcheck] {result['note']}")
    return result


def propose_cells(gw=None, cam=None, outdir=None, anchors=None, anchor_crop=None, arrow_w=None):
    """DETERMINISTIC cells from human-read anchors — NO auto-detection. The ch29 panel ROI also sees the
    moving door/lobby, so every auto approach (brightness v1, temporal-variance v2) FAILED: the door
    sweep, people and changing light give the WHOLE ROI variance, so nothing isolates the LEDs. Instead
    the operator reads 5 coords ONCE off the --anchor ruler (from a two-digit + arrow crop); fixed-pitch
    geometry does the rest, exactly and repeatably. Auto-detection can return later as a wizard, VALIDATED
    against these human anchors as fixtures.

    anchors = tens_left, units_left, digit_top, digit_bottom, arrow_left  (within-panel px; str or seq)
      pitch  = units_left - tens_left                         (an LED matrix is fixed-pitch)
      cell_w = pitch, but never past the arrow: min(pitch, arrow_left - units_left)
      3 digit cells RIGHT-ALIGNED on the units cell (d0 = one pitch left of tens: the 3rd char / P-prefix)
      arrow cell one cell wide at arrow_left; height = digit_bottom - digit_top (bottom set above the queue
      by the operator's eye). Writes _calib_cells.jpg (cells on the anchor crop) + _calib_cells.json. WEB-CALLABLE."""
    import cv2
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    if anchors is None:
        anchors = os.environ.get("CELLS_ANCHORS", "")
    if isinstance(anchors, str):
        vals = [int(v) for v in anchors.replace(";", ",").split(",") if v.strip() != ""]
    else:
        vals = [int(v) for v in anchors]
    if len(vals) != 5:
        raise CalibError("need 5 anchors: tens_left,units_left,digit_top,digit_bottom,arrow_left (within-panel "
                         "px). Run `--index` to pick a two-digit+arrow crop, then `--anchor N` to read them off the ruler.")
    tens_left, units_left, top, bottom, arrow_left = vals
    pitch = units_left - tens_left
    if pitch <= 0:
        raise CalibError(f"units_left ({units_left}) must be > tens_left ({tens_left}); got pitch={pitch}")
    if bottom <= top:
        raise CalibError(f"digit_bottom ({bottom}) must be > digit_top ({top})")
    if arrow_left < units_left:
        raise CalibError(f"arrow_left ({arrow_left}) should be to the RIGHT of units_left ({units_left})")
    height = bottom - top

    paths = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not paths:
        raise CalibError("no _calib_crop_*.png — run collect first")
    if anchor_crop is not None:
        cp = outdir / f"_calib_crop_{int(anchor_crop):03d}.png"
        if not cp.exists():
            raise CalibError(f"no {cp.name} — that anchor-crop index wasn't collected")
    else:
        cp = Path(paths[len(paths) // 2])                    # any crop for the confirm overlay if none named
    base = cv2.imread(str(cp), cv2.IMREAD_GRAYSCALE)
    Hp, Wp = base.shape[:2]

    w = pitch if (arrow_left - units_left) >= pitch else max(1, arrow_left - units_left)   # never overlap arrow
    warn = []

    def _fit(x, cw, tag):
        if x < 0:
            warn.append(f"{tag} left {x}<0 -> clamped to 0"); cw += x; x = 0
        if x + cw > Wp:
            warn.append(f"{tag} right {x + cw}>{Wp} -> clamped"); cw = Wp - x
        return int(x), int(max(1, cw))

    lefts = [tens_left - pitch, tens_left, units_left]       # d0 (3rd char / P-prefix), d1 (tens), d2 (units)
    cells = []
    for i, l in enumerate(lefts):
        cx, cw = _fit(int(l), int(w), f"d{i}")
        cells.append((cx, int(top), cw, int(height)))
    aw = int(arrow_w if arrow_w is not None else os.environ.get("CELLS_ARROW_W", int(w)))
    ax, aw = _fit(int(arrow_left), aw, "arrow")
    arrow_cell = (ax, int(top), aw, int(height))

    # confirm overlay — draw the derived cells on the anchor crop so they visibly land on the real glyphs
    F = 12
    big = cv2.resize(cv2.cvtColor(base, cv2.COLOR_GRAY2BGR), (Wp * F, Hp * F), interpolation=cv2.INTER_NEAREST)
    palette = [(0, 180, 255), (0, 220, 120), (255, 160, 0)]  # BGR: d0, d1, d2
    for i, (cx, cy, cw, ch) in enumerate(cells):
        cv2.rectangle(big, (cx * F, cy * F), ((cx + cw) * F, (cy + ch) * F), palette[i], 2)
        cv2.putText(big, f"d{i}", (cx * F + 2, cy * F + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, palette[i], 1)
    cv2.rectangle(big, (ax * F, top * F), ((ax + aw) * F, (top + height) * F), (200, 0, 200), 2)
    cv2.putText(big, "arrow", (ax * F + 2, top * F + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 0, 200), 1)
    cv2.imwrite(str(outdir / "_calib_cells.jpg"), big)

    dc = ";".join(f"{x},{y},{cw},{ch}" for (x, y, cw, ch) in cells)
    ac = ",".join(str(v) for v in arrow_cell)
    result = {"gw": gwid, "cam": cam, "digit_cells": dc, "arrow_cell": ac, "pitch": int(pitch),
              "cell_w": int(w), "cell_y": int(top), "cell_h": int(height),
              "anchors": {"tens_left": tens_left, "units_left": units_left, "digit_top": top,
                          "digit_bottom": bottom, "arrow_left": arrow_left},
              "anchor_crop": (int(anchor_crop) if anchor_crop is not None else None),
              "panel_wh": [int(Wp), int(Hp)], "warnings": warn,
              "overlay_url": _url(gwid, cam, "_calib_cells.jpg"),
              "note": "deterministic from human anchors; panel1 in-panel digit x differs a few px — spot-check p1"}
    _write_result(outdir, "_calib_cells.json", result)
    print(f"[cells] anchors tens_left={tens_left} units_left={units_left} top={top} bottom={bottom} arrow_left={arrow_left}")
    print(f"[cells] pitch={pitch}px cell_w={w}px height={height}px  (fixed-pitch, right-aligned)")
    for wln in warn:
        print(f"[cells] WARN {wln}")
    print(f"[cells] DIGIT_CELLS='{dc}'")
    print(f"[cells] ARROW_CELL='{ac}'")
    print(f"[cells] CONFIRM: {result['overlay_url']}  (cells drawn on {cp.name})")
    print(f"[cells] NOTE: {result['note']}")
    return result


def _resolve_geometry(fr, door_roi_frame=None, panel_rois=None, gw=None, cam=None):
    """(door_roi, dsrc, panel_rois, psrc) for a decoded frame.

    Door precedence: explicit arg > DOOR_ROI_FRAME env > roi.json (/calib-roi wizard) > the scaled
    calib-space DOOR_ROI, which is known to land on the WALL rather than the leaf and is therefore
    reported as wrong rather than used quietly."""
    H, W = fr.shape[:2]
    drf = door_roi_frame if door_roi_frame is not None else (DOOR_ROI_FRAME or None)
    dsrc = None
    if drf:
        dsrc = f"DOOR_ROI_FRAME={{}}"
    else:
        drawn = _roi_json(gw, cam).get("door_roi_frame")
        if drawn:
            drf, dsrc = drawn, "roi.json (/calib-roi wizard)={}"
    if drf:
        seq = drf.split(",") if isinstance(drf, str) else drf
        droi = tuple(int(v) for v in seq)
        dsrc = dsrc.format(droi) if "{}" in dsrc else dsrc
    else:
        droi = gd.scale_roi(DOOR_ROI, CALIB_WH, (W, H))
        dsrc = f"scaled from calib {DOOR_ROI} -> {droi}  (WRONG surface: draw the leaf at /calib-roi)"
    if panel_rois is None:
        prois, psrc = _panel_rois(gw, cam)
    elif isinstance(panel_rois, str):
        prois = [tuple(int(v) for v in part.split(",")) for part in panel_rois.split(";") if part.strip()]
        psrc = "explicit argument"
    else:
        prois = [tuple(p) for p in panel_rois]
        psrc = "explicit argument"
    return droi, dsrc, prois, psrc


def _as_cells(v, envkey, gw=None, cam=None):
    """Normalise cells from a web param OR env OR roi.json into [(x,y,w,h), ...].

    Precedence mirrors the ROIs exactly: explicit argument > env > roi.json (/calib-cells wizard).
    Env still wins, so ch29's DIGIT_CELLS/ARROW_CELL flow is unchanged. Accepts a 'x,y,w,h;...'
    string, a list of cells, or a single (x,y,w,h).
    """
    if v is None:
        v = os.environ.get(envkey, "")
    if isinstance(v, str) and not v:
        drawn = (_roi_json(gw, cam).get("cells") or {})
        key = "arrow_cell" if "ARROW" in envkey else "digit_cells"
        v = drawn.get(key) or ""
    if isinstance(v, str):
        return _parse_cells(v)
    if v and isinstance(v[0], (list, tuple)):
        return [tuple(c) for c in v]
    return [tuple(v)] if v else []


def _cells_src(envkey, gw=None, cam=None):
    """Where the cells came from — reported so 'built from the wrong geometry' is visible BEFORE a
    build, not inferred afterwards from bad OCR."""
    if os.environ.get(envkey, ""):
        return f"{envkey} env"
    key = "arrow_cell" if "ARROW" in envkey else "digit_cells"
    if (_roi_json(gw, cam).get("cells") or {}).get(key):
        return "roi.json (/calib-cells wizard)"
    return "UNSET"


def render_rois(gw=None, cam=None, door_roi_frame=None, panel_rois=None):
    """Decode the newest frame + write the ROI-overlay and ruler renders (frame/door/panels). Returns
    geometry + artifact URLs. WEB-CALLABLE (returns JSON-able dict; raises CalibError if no frame)."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    fr = newest_frame()
    if fr is None:
        raise CalibError(f"no frame: no segments in {LIVE_DIR/gw/cam} and HTTP fallback empty")
    H, W = fr.shape[:2]
    droi, dsrc, prois, psrc = _resolve_geometry(fr, door_roi_frame, panel_rois, gw, cam)
    ann = _frame_grid(gd.overlay_rois(fr, [tuple(droi)] + list(prois),
                                      ["door"] + [f"p{i}" for i in range(len(prois))]))
    cv2.imwrite(str(outdir / "_calib_frame.jpg"), ann)
    cv2.imwrite(str(outdir / "_calib_door.jpg"),
                _ruler(gd.crop(fr, droi), factor=3, step=20, x0=droi[0], y0=droi[1]))
    for i, pr in enumerate(prois):
        cv2.imwrite(str(outdir / f"_calib_p{i}.jpg"), _ruler(gd.crop(fr, pr), x0=pr[0], y0=pr[1]))
    artifacts = [_url(gw, cam, "_calib_frame.jpg"), _url(gw, cam, "_calib_door.jpg")] + \
                [_url(gw, cam, f"_calib_p{i}.jpg") for i in range(len(prois))]
    result = {"gw": gw, "cam": cam, "frame_wh": [W, H], "door_roi": list(droi), "door_src": dsrc,
              "panels": [list(p) for p in prois], "panel_src": psrc, "artifacts": artifacts}
    return _write_result(outdir, "_calib_rois.json", result)


def collect_crops(gw=None, cam=None, nframes=40, fresh=False, door_roi_frame=None, panel_rois=None):
    """Collect one panel0 crop per NEW segment (dedup by MTIME — the relay reuses seg filenames, so the
    PATH repeats while content changes; a path-keyed dedup was the old 1-crop bug). APPEND across runs
    (durable-dir promise); fresh=True starts over. Rebuilds the cumulative panel0 montage (matches what
    build reads, 1:1) + this-run door montage. Returns counts + URLs. WEB-CALLABLE — but BLOCKS ~2s ×
    nframes, so a web caller must run it as a background job, not inline in the request."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    fr0 = newest_frame()
    if fr0 is None:
        raise CalibError(f"no frame: no segments in {LIVE_DIR/gw/cam} and HTTP fallback empty")
    droi, _dsrc, prois, _psrc = _resolve_geometry(fr0, door_roi_frame, panel_rois, gw, cam)
    existing = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if fresh:
        for gp in existing:
            os.remove(gp)
        existing = []
    n = 1 + max([int(Path(g).stem.split("_")[-1]) for g in existing], default=-1)   # continue numbering
    doors = []; last_mtime = None; added = 0
    for _ in range(nframes):
        seg = _newest_seg()
        if seg:
            mt = _stat_mtime(seg)             # None if it rotated out between select and stat -> skip
            if mt is not None and mt != last_mtime:   # new CONTENT (path may repeat), decode + save
                last_mtime = mt
                f2 = _decode_last_frame(seg)
                if f2 is not None:
                    doors.append(gd.crop(f2, droi))
                    if len(doors) > 36:              # montage shows 36; a full-res door crop per frame
                        doors.pop(0)                 # held for the whole run is the other half of the OOM
                    cv2.imwrite(str(outdir / f"_calib_crop_{n:03d}.png"),
                                cv2.cvtColor(gd.crop(f2, prois[0]), cv2.COLOR_BGR2GRAY))
                    n += 1; added += 1
        time.sleep(2)                         # ~one per 2s segment
    allc = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    # MONTAGES ARE COSMETIC; THE CROPS ON DISK ARE THE DELIVERABLE. This used to upscale EVERY crop
    # 8x and hold all of them (plus padded copies) in memory at once — ~1GB by 120 crops. The runner
    # spawns collect inside the WEB APP's cgroup, so that transient is an OOM SIGKILL: rc=-9 with
    # every crop already safely written (ch16, 2026-07-30). Newest MONTAGE_MAX tiles at half the old
    # upscale keeps the montage useful and the peak tens of MB; and a montage failure of any kind
    # must never fail the collect that produced the crops.
    montage_max = int(os.environ.get("CALIB_MONTAGE_MAX", "60"))
    md = None
    try:
        tiles = [cv2.cvtColor(cv2.imread(c, cv2.IMREAD_GRAYSCALE), cv2.COLOR_GRAY2BGR)
                 for c in allc[-montage_max:]]
        pmont = _montage(tiles, cols=5, factor=4)
        del tiles
        if pmont is not None:
            cv2.imwrite(str(outdir / "_calib_glyphs0.jpg"), pmont)
            del pmont
        md = _montage(doors, cols=6, factor=2)
        if md is not None:
            cv2.imwrite(str(outdir / "_calib_doormap.jpg"), md)
    except Exception as e:
        print(f"[collect] montage skipped ({type(e).__name__}: {e}) — crops on disk are unaffected")
    # A stale montage under the SAME url is how a finished collect looked like "nothing was
    # collected" (ch16 2026-07-30: crops fresh, montage 07-22). Compare, flag, never trust silence.
    mont = outdir / "_calib_glyphs0.jpg"
    newest_crop = max((os.path.getmtime(c) for c in allc), default=None)
    montage_stale = bool(newest_crop) and ((not mont.exists())
                                           or os.path.getmtime(mont) < newest_crop - 1)
    if montage_stale:
        print(f"[collect] WARNING: _calib_glyphs0.jpg is OLDER than the newest crop — the montage "
              f"shows a previous population; the {len(allc)} crops on disk are the truth")
    result = {"gw": gw, "cam": cam, "added": added, "total": len(allc),
              "montage_stale": montage_stale,
              "glyphs_url": _url(gw, cam, "_calib_glyphs0.jpg"),
              "doormap_url": (_url(gw, cam, "_calib_doormap.jpg") if md is not None else None)}
    return _write_result(outdir, "_calib_collect.json", result)


def build_from_crops(gw=None, cam=None, labels=None, digit_cells=None, arrow_cell=None, align=None, out_path=None):
    """Build templates.npz from collected crops + labels + fixed cells (from cells step / env / param).
    LABEL SOURCE: an explicit `labels` (comma-string/list, legacy 1:1 row-major) OR — when none is given —
    labels.json written by the /calib-label wizard, keyed by crop FILENAME (indices shift as crops append,
    filenames don't). Crops with no label or marked '-' are EXCLUDED, with the count reported. Returns
    stats + the GPU fetch URL. WEB-CALLABLE (raises CalibError on missing crops/labels/cells)."""
    import cv2
    gw = gw or GW; cam = cam or CAM
    outdir = _calib_dir(gw, cam)
    if labels is None:
        labels = os.environ.get("LABELS", "")
    if isinstance(labels, str):
        labels = [x.strip() for x in labels.split(",") if x.strip()]
    crops = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    if not crops:
        raise CalibError("no _calib_crop_*.png — run collect first")

    if labels:                                                # explicit labels -> legacy 1:1 row-major
        if len(crops) != len(labels):
            raise CalibError(f"{len(crops)} crops but {len(labels)} labels — must be 1:1 row-major (or label at /calib-label)")
        pairs = list(zip(crops, labels))
    else:                                                     # default: labels.json (filename-keyed) from the wizard
        lj = outdir / "labels.json"
        if not lj.exists():
            raise CalibError(f"no labels: pass --labels, or label the crops at /calib-label/{gw}/{cam} "
                             f"(writes {lj.name})")
        try:
            lab_map = json.loads(lj.read_text())
        except (OSError, ValueError) as e:
            raise CalibError(f"labels.json unreadable: {e}")
        pairs = [(c, lab_map.get(Path(c).name)) for c in crops]

    kept = [(c, lab.strip()) for c, lab in pairs if lab and str(lab).strip() and str(lab).strip() != "-"]
    excluded = len(crops) - len(kept)                        # missing a label or marked '-'
    if not kept:
        raise CalibError(f"no usable labels (all {len(crops)} crops missing or '-') — label at /calib-label/{gw}/{cam}")

    # LABEL->CONTENT BINDING (2026-07-30, ch16 label-inheritance postmortem): labels.json is keyed
    # by FILENAME and collect_crops restarts numbering on an emptied store, so a re-Collect can
    # silently re-attach every old label to brand-new pixels (ch16: 59 fresh crops wearing the old
    # 121-crop trip labels). A label whose bind entry MISMATCHES its crop's current content is
    # stale-inherited: excluded, always, loudly. A label with NO bind entry is legacy: accepted and
    # counted, so established cams keep building until migrated (alphabet_audit --migrate-labels).
    # Governs the labels.json path only — explicit --labels is positional by declaration.
    n_stale = n_unbound = 0
    if not labels:
        bind = _load_bind(outdir)
        checked = []
        for c, lab in kept:
            b = bind.get(Path(c).name)
            if b is None:
                n_unbound += 1
                checked.append((c, lab))
            elif _sha16(c) != b:
                n_stale += 1
            else:
                checked.append((c, lab))
        if n_stale or n_unbound:
            print(f"[build] label binding: {n_stale} STALE-INHERITED labels EXCLUDED (content "
                  f"changed under the filename), {n_unbound} unbound legacy labels accepted; "
                  f"{len(checked) - n_unbound}/{len(kept)} verified against content")
        kept = checked
        if not kept:
            raise CalibError("every label failed the content-binding check — the crops changed "
                             "since the labels were saved (re-Collect inheritance). Relabel at "
                             f"/calib-label/{gw}/{cam} (saving re-binds automatically).")

    dcells = _as_cells(digit_cells, "DIGIT_CELLS", gw, cam)
    acell = _as_cells(arrow_cell, "ARROW_CELL", gw, cam)
    if not dcells or not acell:
        raise CalibError(f"fixed cells required (no segmentation): digit_cells 'x,y,w,h;x,y,w,h;x,y,w,h' "
                         f"+ arrow_cell 'x,y,w,h' (within-panel px). Draw them at "
                         f"/calib-cells/{gw or GW}/{cam or CAM}, or set DIGIT_CELLS/ARROW_CELL. "
                         f"digit_cells={_cells_src('DIGIT_CELLS', gw, cam)}, "
                         f"arrow_cell={_cells_src('ARROW_CELL', gw, cam)}")
    # ---- STANDING DIMS FILTER (2026-07-30, ch16 wrong-space postmortem) ----
    # Cells are WITHIN-PANEL px: cutting them out of a crop taken in a DIFFERENT panel space is
    # garbage by construction (the ch16 07-26 nudge left old-space cells live for days, silently).
    # Two invariants, both loud:
    #   1. the cells' own recorded draw space must match the current panel ROI — else the CELLS
    #      are wrong-space and no crop filtering can save the build: refuse.
    #   2. every crop must match the cells' space (>1px either axis = a different geometry era):
    #      skip it, count it, print the histogram. Skipping is NOT quarantine — the crop stays
    #      labeled and trusted in labels.json; it is just uncuttable by the CURRENT cells, and
    #      becomes buildable again if that geometry is ever restored.
    drawn, drawn_src, cur_roi, cur_src = _cells_space(gw, cam, cells_explicit=bool(digit_cells))
    if drawn and cur_roi and (abs(drawn[0] - cur_roi[0]) > 1 or abs(drawn[1] - cur_roi[1]) > 1):
        raise CalibError(f"WRONG-SPACE CELLS: cells were drawn in {drawn[0]}x{drawn[1]} "
                         f"({drawn_src}) but the current panel0 ROI is {cur_roi[0]}x{cur_roi[1]} "
                         f"({cur_src}) — the ch16 07-26 failure shape. Redraw at "
                         f"/calib-cells/{gw}/{cam} before building.")
    want, want_src = (drawn, drawn_src) if drawn else (cur_roi, cur_src)
    skipped_dims = {}
    if want is None:
        print(f"[build] dims filter OFF — no panel space to compare against ({drawn_src}; "
              f"panel ROI: {cur_src}). Every crop will be cut blind.")
        dims_kept = kept
    else:
        dims_kept = []
        for c, lab in kept:
            wh = _png_wh(c)
            if wh and abs(wh[0] - want[0]) <= 1 and abs(wh[1] - want[1]) <= 1:
                dims_kept.append((c, lab))
            else:
                k = f"{wh[0]}x{wh[1]}" if wh else "unreadable"
                skipped_dims[k] = skipped_dims.get(k, 0) + 1
        print(f"[build] dims filter vs {want[0]}x{want[1]} ({want_src}): "
              f"kept {len(dims_kept)}/{len(kept)} labeled crops"
              + (f"; skipped by dims: {skipped_dims}" if skipped_dims else "; nothing skipped"))
    # REFUSE a thin build. 12 = the floor below which even a minimal alphabet (two digits + both
    # arrows) cannot reach min_examples=3 per glyph — a build from fewer LOOKS like working
    # software while reading garbage; a refusal is diagnosable.
    min_build = int(os.environ.get("MIN_BUILD_CROPS", "12"))
    if len(dims_kept) < min_build:
        raise CalibError(f"REFUSING BUILD: only {len(dims_kept)} crops survive the dims filter "
                         f"(floor MIN_BUILD_CROPS={min_build}; {len(kept)} labeled, skipped by "
                         f"dims: {skipped_dims or 'none'}). Collect fresh crops in the current "
                         f"geometry, then rebuild.")
    # REFUSE silent glyph loss: a glyph with labeled evidence that the filter starves to n=0
    # would vanish from the alphabet without a trace. ALLOW_GLYPH_LOSS='M,E' acknowledges
    # specific losses explicitly (or '*' for all) — targeted, auditable, never implicit.
    def _glyphset(prs):
        out = {}
        for _, lab in prs:
            for g in gd._label_to_glyphs(str(lab)):
                out[g] = out.get(g, 0) + 1
        return out
    g_before, g_after = _glyphset(kept), _glyphset(dims_kept)
    lost = sorted(g for g in g_before if g_after.get(g, 0) == 0)
    allow = {x.strip() for x in os.environ.get("ALLOW_GLYPH_LOSS", "").split(",") if x.strip()}
    if lost and "*" not in allow and set(lost) - allow:
        raise CalibError(f"REFUSING BUILD: glyphs {sorted(set(lost) - allow)} drop to n=0 under "
                         f"the dims filter (had {[g_before[g] for g in lost]} crops in another "
                         f"space). Set ALLOW_GLYPH_LOSS={','.join(lost)} to acknowledge, or "
                         f"collect + label crops covering them first.")

    labeled = [(cv2.imread(c, cv2.IMREAD_GRAYSCALE), lab) for c, lab in dims_kept]
    tpl, stats = gd.build_templates(labeled, dcells, acell[0], align=(align or os.environ.get("ALIGN", "right")),
                                    min_examples=int(os.environ.get("MIN_GLYPH_EXAMPLES", "3")),
                                    exemplars=int(os.environ.get("GLYPH_EXEMPLARS", "3")))
    outp = out_path or os.environ.get("TEMPLATES_OUT", os.path.join(TEMPLATES_DIR, gw, f"{cam}.npz"))
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    gd.save_templates(tpl, outp)
    # RECORD THE ERA THIS BUILD PRODUCED. Without it, "which templates should this camera be running"
    # is only knowable by re-hashing the npz, which needs numpy — and the web app deliberately has
    # none. Writing it here, where the hash is already computed, lets the dash compare the era a
    # worker is REPORTING against the era it SHOULD be on, and flag a worker still running stale
    # templates instead of silently charting its output as current.
    thash = gd.templates_hash(tpl)
    result = {"gw": gw, "cam": cam, "stats": stats, "n_templates": len(tpl), "out_path": outp,
              "label_binding": {"n_stale_excluded": n_stale, "n_unbound_legacy": n_unbound},
              "dims_filter": {"expected": (list(want) if want else None), "source": want_src,
                              "n_kept": len(dims_kept), "n_labeled": len(kept),
                              "skipped_by_dims": skipped_dims,
                              "cells_draw_space": (list(drawn) if drawn else None),
                              "panel_roi_dims": (list(cur_roi) if cur_roi else None)},
              "n_labeled": len(dims_kept), "n_excluded": int(excluded), "n_crops": len(crops),
              "templates_hash": thash, "era": thash[:8], "built_at": time.time(),
              "fetch_url": f"{CLOUD}/api/gw/{gw}/templates/{cam}"}
    return _write_result(outdir, "_calib_build.json", result)


def foldback(gw=None, cam=None, outdir=None, db_path=None):
    """Self-improving loop: fold operator-REVIEWED /floorcheck samples back into the calib set. For each
    floor_sample with a confirmed reviewed_label not yet folded, decode its panel crop -> a new
    _calib_crop_*.png + a labels.json entry (so --build grows the thin glyphs the reviews flagged: 8/0/
    6/G). READ-ONLY on gateway.db (never writes the ingest DB); folded sample ids tracked locally in
    _folded.json. Reviews marked '-' are skipped (excluded). WEB-CALLABLE. Run --build after."""
    import sqlite3

    import cv2
    import numpy as np
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    dbp = db_path or os.environ.get("GATEWAY_DB", "/opt/liftlab-b3/cloud/gateway.db")
    if not os.path.exists(dbp):
        raise CalibError(f"gateway.db not found at {dbp} — set GATEWAY_DB to the cloud's ingest DB")
    folded_path = outdir / "_folded.json"
    folded = set()
    if folded_path.exists():
        try:
            folded = set(json.loads(folded_path.read_text()))
        except (OSError, ValueError):
            folded = set()
    db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)     # READ-ONLY — foldback never writes ingest
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT id, reviewed_label, crop_jpeg FROM floor_sample WHERE gateway_id=? AND cam=? "
                          "AND reviewed_label IS NOT NULL AND reviewed_label != ''", (gwid, cam)).fetchall()
    except sqlite3.OperationalError as e:
        db.close()
        raise CalibError(f"floor_sample not available ({e}) — deploy door_event_api + review samples at /floorcheck")
    db.close()
    labels = {}
    lj = outdir / "labels.json"
    if lj.exists():
        try:
            labels = json.loads(lj.read_text())
        except (OSError, ValueError):
            labels = {}
    bind = _load_bind(outdir)
    existing = sorted(glob.glob(str(outdir / "_calib_crop_*.png")))
    n = 1 + max([int(Path(g).stem.split("_")[-1]) for g in existing], default=-1)   # continue numbering
    added = 0
    for r in rows:
        if r["id"] in folded:
            continue
        lab = str(r["reviewed_label"]).strip()
        blob = r["crop_jpeg"]
        if lab == "-" or not blob:                           # reviewed-as-exclude, or no image -> skip
            folded.add(r["id"]); continue
        arr = cv2.imdecode(np.frombuffer(bytes(blob), np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None:
            folded.add(r["id"]); continue
        fname = f"_calib_crop_{n:03d}.png"
        cv2.imwrite(str(outdir / fname), arr)                # the reviewed panel crop -> a labeled calib crop
        labels[fname] = lab
        bind[fname] = _sha16(outdir / fname)                 # bind the label to THIS content, not the filename
        folded.add(r["id"]); n += 1; added += 1
    lj.write_text(json.dumps(labels, indent=2, sort_keys=True))
    _save_bind(outdir, bind)
    folded_path.write_text(json.dumps(sorted(folded)))
    result = {"gw": gwid, "cam": cam, "added": added, "reviewed_total": len(rows),
              "already_folded": len(folded) - added, "labels": len(labels),
              "note": "run --build to rebuild templates with the appended reviewed examples"}
    _write_result(outdir, "_calib_foldback.json", result)
    print(f"[foldback] +{added} reviewed samples appended ({len(rows)} reviewed total, "
          f"{len(folded) - added} already folded) -> {len(labels)} labeled crops")
    print(f"[foldback] now: door_calib.py --build   (grows the glyphs the reviews flagged)")
    return result


def labelcheck(gw=None, cam=None, outdir=None):
    """LABEL-SANITY pass: for each glyph, NCC every contributing crop-cell against that glyph's mean
    template; a crop far below its glyph's own distribution is a probable MISLABEL (e.g. a parked-6 tile
    labeled G, or vice-versa — the bidirectional G/6 contamination). Flags them for re-review at
    /calib-label. Uses labels.json + DIGIT_CELLS/ARROW_CELL. WEB-CALLABLE."""
    from collections import defaultdict

    import cv2
    import numpy as np
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    dcells = _as_cells(None, "DIGIT_CELLS", gwid, cam)
    acell = _as_cells(None, "ARROW_CELL", gwid, cam)
    if not dcells or not acell:
        raise CalibError("set DIGIT_CELLS + ARROW_CELL")
    lj = outdir / "labels.json"
    if not lj.exists():
        raise CalibError("no labels.json — label at /calib-label first")
    labels = json.loads(lj.read_text())
    tsz = (16, 10); n = len(dcells); blank = "blank"
    acc = defaultdict(list)                                   # glyph -> [(crop_fname, resized_cell)]
    for fname, lab in labels.items():
        lab = str(lab).strip()
        if not lab or lab == "-":
            continue
        p = outdir / fname
        panel = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) if p.exists() else None
        if panel is None:
            continue
        glyphs = gd._label_to_glyphs(lab)
        arrow = glyphs[-1] if glyphs and glyphs[-1] in ("up", "down") else None
        floor_chars = glyphs[:-1] if arrow else glyphs
        if not floor_chars or len(floor_chars) > n:
            continue
        cell_labs = [blank] * (n - len(floor_chars)) + floor_chars   # right-aligned (build's default)
        for cell, g in zip(dcells, cell_labs):
            if g == blank:
                continue
            acc[g].append((fname, cv2.resize(gd.crop(panel, cell), (tsz[1], tsz[0])).astype(np.float32)))
        if arrow:
            acc[arrow].append((fname, cv2.resize(gd.crop(panel, acell[0]), (tsz[1], tsz[0])).astype(np.float32)))
    flags = []
    print(f"[labelcheck] {len(acc)} glyphs from {len(labels)} labels")
    for g, items in sorted(acc.items()):
        mean = np.mean([c for _, c in items], axis=0)
        nccs = [(gd.ncc(c, mean), f) for f, c in items]
        arr = np.array([s for s, _ in nccs])
        med = float(np.median(arr))
        note = ""
        if len(items) >= 3:                                  # need a distribution to call an outlier
            mad = float(np.median(np.abs(arr - med))) + 1e-6
            thr = min(0.6, med - 4 * mad)
            out = sorted((s, f) for s, f in nccs if s < thr)
            for s, f in out:
                flags.append({"crop": f, "labeled": g, "ncc": round(s, 3), "glyph_median": round(med, 2)})
            if out:
                note = "  MISLABEL? " + ", ".join(f"{f}={s:.2f}" for s, f in out)
        print(f"    {g!r:6} n={len(items):3} median_ncc={med:.2f}{note}")
    result = {"gw": gwid, "cam": cam, "flags": flags,
              "note": "low-NCC crops are probable MISLABELS — re-review them at /calib-label, then --build"}
    _write_result(outdir, "_calib_labelcheck.json", result)
    print(f"[labelcheck] {len(flags)} probable mislabel(s) flagged" +
          (f" -> re-review at {CLOUD}/calib-label/{gwid}/{cam}" if flags else " (labels look consistent)"))
    return result


def fitcells(gw=None, cam=None, outdir=None, radius=3, iters=3):
    """AUTO-FIT per-cell geometry from the labeled crops — ends anchor guessing. The display is SLANTED
    (per-cell x/y offsets a uniform grid can't fit), so grid-search EACH cell's (x,y) INDEPENDENTLY
    (±radius) to maximise NCC of its labeled content against that glyph's consensus template. Human
    labels = ground truth; geometry = fitted parameters. A digit appears in BOTH tens and units, so the
    well-aligned units cell anchors the shared template and pulls the misaligned tens cell into line;
    iterate to converge. Prints fitted DIGIT_CELLS/ARROW_CELL + writes _calib_fitcells.jpg (overlay on a
    two-digit crop) for a one-time sanity check. Residual UNIFORM jitter is left to the runtime shift
    search — this fixes the fixed per-cell SLANT. WEB-CALLABLE."""
    from collections import defaultdict

    import cv2
    import numpy as np
    gwid = gw or GW; cam = cam or CAM
    outdir = outdir if outdir is not None else _calib_dir(gwid, cam)
    dcells = _as_cells(None, "DIGIT_CELLS", gwid, cam)
    acell0 = _as_cells(None, "ARROW_CELL", gwid, cam)
    if not dcells or not acell0:
        raise CalibError("set DIGIT_CELLS + ARROW_CELL (the STARTING geometry to refine)")
    lj = outdir / "labels.json"
    if not lj.exists():
        raise CalibError("no labels.json — label at /calib-label first")
    labels = json.loads(lj.read_text())
    tsz = (16, 10); n = len(dcells); BLANK = "blank"; R = int(radius)
    samples = []                                              # (panel, [glyph-or-blank per cell], arrow)
    for fname, lab in labels.items():
        lab = str(lab).strip()
        if not lab or lab == "-":
            continue
        p = outdir / fname
        panel = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) if p.exists() else None
        if panel is None:
            continue
        glyphs = gd._label_to_glyphs(lab)
        arrow = glyphs[-1] if glyphs and glyphs[-1] in ("up", "down") else None
        fc = glyphs[:-1] if arrow else glyphs
        if not fc or len(fc) > n:
            continue
        samples.append((panel, [BLANK] * (n - len(fc)) + fc, arrow))
    if len(samples) < 20:
        raise CalibError(f"only {len(samples)} usable labeled crops — need more coverage to fit geometry")

    def rs(panel, cell):
        return cv2.resize(gd.crop(panel, cell), (tsz[1], tsz[0])).astype(np.float32)

    def key(g, i):
        return f"{BLANK}_{i}" if g == BLANK else g

    def means(cells, acell):
        # UNITS-ANCHORED reference: the rightmost digit cell is the right-alignment anchor and reads
        # correctly (its units_left was calibrated), so build each DIGIT template from the UNITS cell —
        # a clean, aligned reference the misaligned tens cell is fitted TO. A pure self-consistent mean
        # would instead pull the CORRECT units cell toward a wrong tens (they'd meet in the middle).
        # blank_<i> is per-cell; a glyph never seen in units (e.g. P) falls back to its global mean.
        ui = n - 1
        glob, unit = defaultdict(list), defaultdict(list)
        for panel, cl, arrow in samples:
            for i, (cell, g) in enumerate(zip(cells, cl)):
                k = key(g, i)
                glob[k].append(rs(panel, cell))
                if g != BLANK and i == ui:
                    unit[g].append(rs(panel, cell))
            if arrow:
                glob[arrow].append(rs(panel, acell))
        return {k: (np.mean(unit[k], axis=0) if k in unit else np.mean(v, axis=0)) for k, v in glob.items()}

    def fit_one(base, targets):
        x, y, w, h = base
        best, bs = (0, 0), -1e18
        for dy in range(-R, R + 1):
            for dx in range(-R, R + 1):
                s = sum(ncc for p, tt in targets for ncc in (gd.ncc(rs(p, (x + dx, y + dy, w, h)), tt),))
                if s > bs:
                    bs, best = s, (dx, dy)
        return (x + best[0], y + best[1], w, h), best, (bs / len(targets) if targets else 0.0)

    orig = list(dcells) + [acell0[0]]
    cells, acell, scores = list(dcells), acell0[0], {}
    for _ in range(int(iters)):
        M = means(cells, acell)
        newc = []
        for i, base in enumerate(cells):
            tg = [(p, M[key(cl[i], i)]) for p, cl, arrow in samples if key(cl[i], i) in M]
            fc, _mv, sc = fit_one(base, tg)
            newc.append(fc); scores[f"d{i}"] = round(sc, 3)
        atg = [(p, M[arrow]) for p, cl, arrow in samples if arrow and arrow in M]
        acell, _amv, asc = fit_one(acell, atg)
        scores["arrow"] = round(asc, 3)
        cells = newc
    fin = list(cells) + [acell]
    moves = {(f"d{i}" if i < len(cells) else "arrow"): [fin[i][0] - orig[i][0], fin[i][1] - orig[i][1]]
             for i in range(len(fin))}                        # CUMULATIVE offset from the starting geometry

    dc = ";".join(f"{x},{y},{w},{h}" for x, y, w, h in cells)
    ac = ",".join(str(v) for v in acell)
    # overlay the FITTED cells on a two-digit crop for the one-time visual check
    two = next((s for s in samples if sum(1 for g in s[1] if g != BLANK) == 2 and s[2]), samples[0])
    F = 12
    big = cv2.resize(cv2.cvtColor(two[0], cv2.COLOR_GRAY2BGR), (two[0].shape[1] * F, two[0].shape[0] * F),
                     interpolation=cv2.INTER_NEAREST)
    for i, (x, y, w, h) in enumerate(cells):
        cv2.rectangle(big, (x * F, y * F), ((x + w) * F, (y + h) * F), (0, 220, 0), 2)
        cv2.putText(big, f"d{i}", (x * F + 2, y * F + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 0), 1)
    cv2.rectangle(big, (acell[0] * F, acell[1] * F), ((acell[0] + acell[2]) * F, (acell[1] + acell[3]) * F), (200, 0, 200), 2)
    cv2.imwrite(str(outdir / "_calib_fitcells.jpg"), big)

    result = {"gw": gwid, "cam": cam, "digit_cells": dc, "arrow_cell": ac, "moves": moves,
              "cell_ncc": scores, "n_samples": len(samples), "radius": R, "iters": int(iters),
              "overlay_url": _url(gwid, cam, "_calib_fitcells.jpg"),
              "note": "fitted per-cell geometry (slant absorbed). Sanity-check the overlay, then set these "
                      "DIGIT_CELLS/ARROW_CELL (and rebuild if the shift changes what --build crops)."}
    _write_result(outdir, "_calib_fitcells.json", result)
    print(f"[fitcells] {len(samples)} labeled crops; per-cell offsets (px): {moves}")
    print(f"[fitcells] per-cell mean NCC after fit: {scores}")
    print(f"[fitcells] DIGIT_CELLS='{dc}'")
    print(f"[fitcells] ARROW_CELL='{ac}'")
    print(f"[fitcells] SANITY-CHECK overlay: {result['overlay_url']}")
    return result


def readtest(gw=None, cam=None, db_path=None):
    """REPRODUCE: run the CURRENT reader on the REVIEWED /floorcheck crops (fixtures with known labels)
    and dump per-sample diagnostics — expected vs read, the chosen shift, per digit-cell top-3 glyph
    scores + blank score, arrow scores. Isolates the failure mechanism (shift misalignment vs template
    confusion vs blank logic) on the REAL crops. Read-only on gateway.db. Honours DOOR_SHIFT / DOOR_MARGIN
    / DOOR_MIN_SCORE so you can A/B (e.g. DOOR_SHIFT=0 to test the shift search). WEB-CALLABLE."""
    import sqlite3

    import cv2
    import numpy as np
    gwid = gw or GW; cam = cam or CAM
    tp = os.path.join(TEMPLATES_DIR, gwid, f"{cam}.npz")
    if not os.path.exists(tp):
        raise CalibError(f"no templates at {tp} — run --build first")
    tpl = gd.load_templates(tp)
    dcells = _as_cells(None, "DIGIT_CELLS", gwid, cam)
    acell = _as_cells(None, "ARROW_CELL", gwid, cam)
    if not dcells or not acell:
        raise CalibError("set DIGIT_CELLS + ARROW_CELL (the same geometry the GPU runs)")
    shift = int(os.environ.get("DOOR_SHIFT", "2"))
    margin = float(os.environ.get("DOOR_MARGIN", "0.05"))
    minsc = float(os.environ.get("DOOR_MIN_SCORE", "0.55"))
    rdr = gd.FloorReader(tpl, dcells, acell[0], min_score=minsc, shift_search=shift, margin_min=margin,
                         blank_min=float(os.environ.get("DOOR_BLANK_MIN", "0.45")),
                         shift_floor=float(os.environ.get("DOOR_SHIFT_FLOOR", "0.40")),
                         lit_range=float(os.environ.get("DOOR_LIT_RANGE", "120")),
                         blank_strong=float(os.environ.get("DOOR_BLANK_STRONG", "0.90")),
                         blank_lit_margin=float(os.environ.get("DOOR_BLANK_LIT_MARGIN", "0.15")),
                         confuse_band=float(os.environ.get("DOOR_CONFUSE_BAND", "0")),
                         disc_min=float(os.environ.get("DOOR_DISC_MIN", "0.10")))
    dbp = db_path or os.environ.get("GATEWAY_DB", "/opt/liftlab-b3/cloud/gateway.db")
    if not os.path.exists(dbp):
        raise CalibError(f"gateway.db not found at {dbp} — set GATEWAY_DB")
    db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True); db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT id,reviewed_label,floor,crop_jpeg FROM floor_sample WHERE gateway_id=? AND cam=? "
                          "AND reviewed_label IS NOT NULL AND reviewed_label!='' ORDER BY id DESC LIMIT ?",
                          (gwid, cam, int(os.environ.get("READTEST_N", "40")))).fetchall()
    except sqlite3.OperationalError as e:
        db.close(); raise CalibError(f"floor_sample not available ({e}) — review samples at /floorcheck first")
    db.close()
    print(f"[readtest] reader: shift_search={shift} margin_min={margin} min_score={minsc}; {len(rows)} reviewed fixtures")
    correct = total = 0
    for r in rows:
        if not r["crop_jpeg"]:
            continue
        arr = cv2.imdecode(np.frombuffer(bytes(r["crop_jpeg"]), np.uint8), cv2.IMREAD_GRAYSCALE)
        if arr is None:
            continue
        total += 1
        lab = str(r["reviewed_label"]).strip()
        exp = lab.rstrip("^vV") if lab and lab[-1] in "^vV" else lab   # expected floor string (drop arrow)
        res = rdr.read_panel(arr)
        dbg = rdr.debug_cells(arr)
        got = res["floor"]
        ok = (got == exp)
        correct += ok
        print(f"[readtest] #{r['id']:>5} expect {lab!r:7} -> read {str(got)!r:7}/{res.get('direction')} "
              f"status={res['status']} shift={dbg['shift']} {'OK' if ok else 'XX'}")
        for c in dbg["cells"]:
            if "top" in c:
                print(f"           cell{c['i']} top3={c['top']} blank={c['blank']} contrast={c['contrast']}")
            else:
                print(f"           cell{c['i']} {c.get('verdict')}")
        if dbg.get("arrow"):
            print(f"           arrow {dbg['arrow']}")
    print(f"[readtest] {correct}/{total} correct on reviewed fixtures "
          f"(try DOOR_SHIFT=0 to isolate the shift search; DOOR_MARGIN to tune the ambiguous gate)")
    return {"correct": correct, "total": total, "shift_search": shift, "margin_min": margin}


def main():
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.umask(0o002)                          # root (sudo): new files/dirs group-writable (664/775)
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, default=0)
    ap.add_argument("--frames", type=int, default=0, help="collect N frames -> door + panel montages")
    ap.add_argument("--index", action="store_true", help="montage of crops WITH indices, to pick an anchor crop")
    ap.add_argument("--anchor", type=int, default=None, metavar="N", help="enlarge crop N with a fine ruler to read anchor px")
    ap.add_argument("--panelcheck", action="store_true", help="live panel extractions vs a calib crop (cells drawn) to spot a geometry offset")
    ap.add_argument("--ref", type=int, default=55, help="reference calib crop index for --panelcheck (default 55)")
    ap.add_argument("--cells", action="store_true", help="DETERMINISTIC cells from --anchors (no auto-detect)")
    ap.add_argument("--anchors", default="", help="tens_left,units_left,digit_top,digit_bottom,arrow_left (within-panel px)")
    ap.add_argument("--anchor-crop", type=int, default=None, dest="anchor_crop", help="crop index to draw the derived cells on")
    ap.add_argument("--foldback", action="store_true", help="fold REVIEWED /floorcheck samples into the calib set (then --build)")
    ap.add_argument("--readtest", action="store_true", help="run the current reader on reviewed /floorcheck fixtures + dump per-cell scores")
    ap.add_argument("--labelcheck", action="store_true", help="flag probable MISLABELS (crop vs its glyph template) for re-review")
    ap.add_argument("--fitcells", action="store_true", help="AUTO-FIT per-cell geometry from labeled crops (ends anchor guessing)")
    ap.add_argument("--build", action="store_true", help="build templates.npz from collected crops + --labels")
    ap.add_argument("--labels", default="", help="row-major floor labels, e.g. '7^,8^,12^,...,P3v,6v'")
    ap.add_argument("--fresh", action="store_true", help="clear accumulated crops before collecting (default = append)")
    a = ap.parse_args()
    try:
        if a.index:
            r = render_index()
            print(f"[index] {r['n']} crops -> {r['index_url']}")
            print(f"[index] pick a two-digit + arrow (+ queue) tile; note its number for `--anchor N`")
            return
        if a.panelcheck:
            panelcheck(ref=a.ref)
            return
        if a.foldback:
            foldback()
            return
        if a.readtest:
            readtest()
            return
        if a.labelcheck:
            labelcheck()
            return
        if a.fitcells:
            fitcells()
            return
        if a.anchor is not None:
            r = render_anchor(a.anchor)
            print(f"[anchor] crop {r['crop']} -> {r['anchor_url']}")
            print(f"[anchor] read off the ruler: {r['read']}")
            print(f"[anchor] then: door_calib.py --cells --anchors 'tens_left,units_left,digit_top,digit_bottom,arrow_left' --anchor-crop {r['crop']}")
            return
        if a.cells:
            propose_cells(anchors=a.anchors, anchor_crop=a.anchor_crop)   # prints + writes _calib_cells.json
            return
        if a.build:
            r = build_from_crops(labels=a.labels)
            print(f"[build] {r['n_labeled']}/{r.get('n_crops','?')} crops labeled ({r['n_excluded']} excluded: no label or '-')")
            dr = r['stats'].get('dropped') or {}
            if dr:
                print(f"[build] DROPPED degenerate glyphs (< {r['stats'].get('min_examples')} examples): {dr} — read no_read until foldback grows them")
            print(f"[build] {r['stats']}")
            print(f"[build] wrote {r['n_templates']} templates -> {r['out_path']}")
            print(f"[build] the GPU fetches it (no scp needed): GET {r['fetch_url']}  (Bearer analysis token)")
            return
        r = render_rois()
        print(f"[calib] frame {r['frame_wh'][0]}x{r['frame_wh'][1]}; door_roi={tuple(r['door_roi'])}  "
              f"[{r['door_src']}]; panels(frame px)={[tuple(p) for p in r['panels']]}")
        for u in r["artifacts"]:
            print(f"[calib]   {u}")
        nframes = max(a.collect, a.frames)
        if nframes:
            c = collect_crops(nframes=nframes, fresh=a.fresh)
            print(f"[calib] +{c['added']} new crops this run -> {c['total']} total. Label ALL row-major:")
            print(f"[calib]   {c['glyphs_url']}")
            if c["doormap_url"]:
                print(f"[calib] door montage -> {c['doormap_url']}  "
                      f"(edge should SWEEP shut<->open; if it never moves, the box is on a fixed jamb/wall)")
    except CalibError as e:
        raise SystemExit(f"[calib] {e}")                   # CLI-only: catchable error -> stderr + nonzero exit


if __name__ == "__main__":
    main()
