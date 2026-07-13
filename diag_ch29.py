#!/usr/bin/env python3
"""
ch29 analyze_local diagnostic (runs on the Pi, B4 venv).

Pull (or reuse) a 3-min ch29 window, KEEP the footage, and print three report
blocks: timestamp integrity, door signal + cycle stats, and an anchor-only
rebuild. Reads NVR creds from /etc/liftlab-agent.env itself (no shell export
needed). Prints no secrets.

  reuse : if a clip already exists in /tmp/ch29diag, analyse it (no re-pull).
  fresh : else pull ch29 [WINDOW] to /tmp/ch29diag and keep it.
"""
import glob
from datetime import datetime
from pathlib import Path

import numpy as np

from liftlab import nvr
from liftlab.timestamps import build_model, integrity_report
from liftlab.stitch import stitch_camera

ENV = "/etc/liftlab-agent.env"
WINDOW = ("2026-07-13T08:30:00", "2026-07-13T08:33:00")   # 3-min window
CHANNEL = 29
ROI = (450, 0, 568, 900)
DIAG_DIR = Path("/tmp/ch29diag")


def load_env(path: str) -> dict:
    cfg: dict = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as e:
        print(f"WARN: cannot read {path}: {e}")
    return cfg


def rule(title: str) -> None:
    print("\n" + "=" * 64 + f"\n{title}\n" + "=" * 64)


def main() -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(glob.glob(str(DIAG_DIR / "*.mp4")))
    if existing:
        clip = existing[0]
        print(f"=== reusing existing clip {clip}")
        print("    (delete /tmp/ch29diag/*.mp4 to force a fresh pull) ===")
    else:
        cfg = load_env(ENV)
        ns = nvr.NvrSettings(
            host=cfg.get("NVR_HOST", ""), port=int(cfg.get("NVR_PORT", "554")),
            user=cfg.get("NVR_USER", ""), password=cfg.get("NVR_PASS", ""))
        if not ns.host:
            print(f"FATAL: NVR_HOST not found in {ENV}")
            return
        start = datetime.fromisoformat(WINDOW[0])
        end = datetime.fromisoformat(WINDOW[1])
        print(f"=== pulling ch{CHANNEL} {WINDOW[0]} -> {WINDOW[1]} "
              f"to {DIAG_DIR} (footage KEPT) ===")
        res = nvr.pull(ns, CHANNEL, start, end, uploads_dir=DIAG_DIR)
        print("pull ok:", res["ok"], "| err:", res.get("error"), "| path:", res.get("path"))
        if not res["ok"]:
            return
        clip = res["path"]

    rule("[1] TIMESTAMP INTEGRITY")
    m = build_model(clip)
    print(integrity_report(m))

    rule(f"[2] DOOR SIGNAL + CYCLES   ROI ch{CHANNEL}={ROI}")
    sr = stitch_camera([clip], ROI, models={clip: m})
    sig = np.asarray(sr.timeline.signal, dtype=float)
    print("signal: n=%d  min=%.3f  max=%.3f  mean=%.3f  std=%.3f"
          % (len(sig), sig.min(), sig.max(), sig.mean(), sig.std()))
    print("cycles: raw=%d  clean=%d  rej_hole=%d  rej_quality=%d"
          % (len(sr.raw), len(sr.clean), len(sr.rejected_hole), len(sr.rejected_quality)))
    for i, (c, e) in enumerate(zip(sr.raw, sr.edges)):
        print("  cycle %d: open_travel=%.2f  close_travel=%.2f  open_ok=%s  close_ok=%s"
              % (i, c.open_travel_s, c.close_travel_s, e["open_ok"], e["close_ok"]))

    rule("[3] ANCHOR-ONLY REBUILD (start_ts = requested window start)")
    start = datetime.fromisoformat(WINDOW[0])
    m2 = build_model(clip, start_ts=start, tz="Asia/Kolkata")
    print("start_ts=%s (src=%s)  duration_actual=%.3fs  pts_source=%s  monotonic=%s  effective_fps=%.3f"
          % (m2.start_ts.isoformat(), m2.start_ts_source, m2.duration_actual_s,
             m2.pts_source, m2.monotonic, m2.effective_fps))
    print("\nTELL: in [1], compare 'actual span' to the 180s window. Collapsed/tiny span or"
          "\n'monotonic NO' => PTS is the culprit -> apply the window_s uniform-clock fix."
          "\nIf span is ~180s but cycles raw=0 / signal std~0 => baseline/ROI, not PTS.")


if __name__ == "__main__":
    main()
