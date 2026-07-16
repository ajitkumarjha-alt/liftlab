#!/usr/bin/env python3
"""Decoupled relay uploader. ffmpeg writes SUB segments to local Pi tmpfs (never blocks on the
network); this drains them to the VM over ONE persistent connection, round-robin across cams,
sequentially. That sidesteps the 7-concurrent-PUT starvation entirely — the single well-behaved
connection is the path plain curl already proved at ~9.6 Mbps, and 7 subs need only ~6.5.

Runs under the B3 agent venv (httpx present). Config via env:
  CLOUD_URL, GATEWAY_TOKEN, GATEWAY_ID/GW, RELAY_OUT (local seg dir), RELAY_CAMS ("ch16 ch27 ...")
  RELAY_MAX_LOCAL (per-cam backlog cap; drop OLDEST past it — a live relay wants fresh, not complete)
  RELAY_UP_CONNS (persistent connections; 1-2 is the safe, contention-free range)
"""
import os
import sys
import glob
import time

import httpx

CLOUD = os.environ["CLOUD_URL"].rstrip("/")
GW = os.environ.get("GW") or os.environ.get("GATEWAY_ID", "site-A")
TOKEN = os.environ["GATEWAY_TOKEN"]
OUT = os.environ.get("RELAY_OUT", "/dev/shm/liftlab-relay-out")
CAMS = os.environ.get("RELAY_CAMS", "").split()
MAX_LOCAL = int(os.environ.get("RELAY_MAX_LOCAL", "30"))
CONNS = max(1, int(os.environ.get("RELAY_UP_CONNS", "2")))
PLAYLIST_N = 6
HDRS = {"Authorization": f"Bearer {TOKEN}"}


def log(m):
    print(f"[uploader] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {m}", file=sys.stderr, flush=True)


def complete_segments(cam):
    """Segments done being written = all but the newest (ffmpeg is still filling the newest)."""
    files = sorted(glob.glob(os.path.join(OUT, cam, "seg*.ts")))
    return files[:-1] if len(files) > 1 else []


def build_playlist(names):
    out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:3", "#EXT-X-MEDIA-SEQUENCE:0"]
    for n in names:
        out += ["#EXTINF:2.0,", n]
    return ("\n".join(out) + "\n").encode()


def main():
    if not CAMS:
        log("no RELAY_CAMS — nothing to do"); sys.exit(2)
    for cam in CAMS:
        os.makedirs(os.path.join(OUT, cam), exist_ok=True)
    sent = {cam: [] for cam in CAMS}
    client = httpx.Client(timeout=20.0, headers=HDRS,
                          limits=httpx.Limits(max_connections=CONNS, max_keepalive_connections=CONNS))
    log(f"draining {OUT} -> {CLOUD} (gw={GW}, {len(CAMS)} cams, {CONNS} conn, max_local={MAX_LOCAL})")
    idle = 0
    while True:
        did = False
        for cam in CAMS:                         # one segment per cam per pass = fair round-robin
            segs = complete_segments(cam)
            if len(segs) > MAX_LOCAL:            # backpressure: shed OLDEST, keep the feed live
                for old in segs[:len(segs) - MAX_LOCAL]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                log(f"{cam} backlog>{MAX_LOCAL}: dropped {len(segs) - MAX_LOCAL} oldest")
                segs = complete_segments(cam)
            if not segs:
                continue
            seg = segs[0]
            name = os.path.basename(seg)
            try:
                data = open(seg, "rb").read()
            except OSError:
                continue
            try:
                r = client.put(f"{CLOUD}/api/gw/{GW}/live/{cam}/{name}", content=data)
            except Exception as e:
                log(f"{cam} {name} PUT error: {type(e).__name__}: {e}")
                time.sleep(0.3)
                continue
            if r.status_code == 200:
                try:
                    os.remove(seg)
                except OSError:
                    pass
                sent[cam] = (sent[cam] + [name])[-PLAYLIST_N:]
                try:
                    client.put(f"{CLOUD}/api/gw/{GW}/live/{cam}/index.m3u8", content=build_playlist(sent[cam]))
                except Exception:
                    pass
                did = True
            elif r.status_code == 503:
                log(f"{cam} VM shed (503) — store guard active, backing off")
                time.sleep(0.5)
            else:
                log(f"{cam} {name} -> HTTP {r.status_code}")
        if did:
            idle = 0
        else:
            idle += 1
            time.sleep(0.2)                       # nothing ready; brief idle
            if idle % 300 == 0:
                log("idle — no complete segments to upload (producers running?)")


if __name__ == "__main__":
    main()
