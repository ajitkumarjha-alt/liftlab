#!/usr/bin/env python3
"""READ-ONLY ch29 fps localization. Measures the STREAM, not the pipeline. Changes
NOTHING (no pipeline/controller/config writes). Run with the B4 venv.

1) ONVIF profile inventory: GetProfiles + GetVideoEncoderConfigurations -> per
   profile token/name, main/sub, codec, resolution, CONFIGURED fps, bitrate;
   identify the profile the pipeline's /29/1?profile=vam URL maps to.
2) ffprobe ADVERTISED fps on that exact URL.
3) ffmpeg BARE DELIVERED fps, 120s, no analysis, per-10s bucket (decay).
4) comparison table + which case we're in.
"""
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")
import onvif_resolve  # noqa: E402  (reuse its ONVIF SOAP helpers; read-only)

ENV = "/etc/liftlab-agent.env"
CH = 29


def load_env(p):
    cfg = {}
    for line in Path(p).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


c = load_env(ENV)
USER, PW, HOST = c.get("NVR_USER", ""), c.get("NVR_PASS", ""), c.get("NVR_HOST", "")


def lname(t):
    return t.rsplit("}", 1)[-1]


def ftext(el, name):
    for x in el.iter():
        if lname(x.tag) == name and x.text:
            return x.text
    return None


def creds(uri):
    return uri if urlparse(uri).username else uri.replace(
        "rtsp://", f"rtsp://{quote(USER, safe='')}:{quote(PW, safe='')}@", 1)


def mask(u):
    return re.sub(r"://[^/]*@", "://***:***@", u or "")


print(f"READ-ONLY ch29 fps localization — NVR {HOST}\n")

# ---------- 1. ONVIF inventory ----------
print("=== 1. ONVIF PROFILE INVENTORY ===")
media = onvif_resolve._media_url(HOST, USER, PW, 8)
print(f"  media service: {media}")
profiles = []
try:
    root = ET.fromstring(onvif_resolve._soap(media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>', USER, PW, 15))
    for prof in root.iter():
        if lname(prof.tag) in ("Profiles", "Profile") and prof.get("token"):
            name = enc = res = fps = br = None
            vec = None
            for ch in prof:
                if lname(ch.tag) == "Name":
                    name = ch.text
                if lname(ch.tag) == "VideoEncoderConfiguration":
                    vec = ch
            if vec is not None:
                enc = ftext(vec, "Encoding")
                for x in vec.iter():
                    if lname(x.tag) == "Resolution":
                        res = f"{ftext(x,'Width')}x{ftext(x,'Height')}"
                    if lname(x.tag) == "RateControl":
                        fps = ftext(x, "FrameRateLimit")
                        br = ftext(x, "BitrateLimit")
            profiles.append({"token": prof.get("token"), "name": name, "enc": enc, "res": res, "fps": fps, "br": br})
except Exception as e:
    print("  GetProfiles FAILED:", str(e)[:100])

print(f"  total profiles: {len(profiles)}  (resolving ch{CH} channel/stream via GetStreamUri ...)")
for p in profiles:
    try:
        uri = onvif_resolve._stream_uri(media, p["token"], USER, PW, 8)
        parts = urlparse(uri).path.strip("/").split("/") if uri else []
        p["channel"] = int(parts[0]) if parts and parts[0].isdigit() else None
        p["stream"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        p["uri"] = uri
    except Exception:
        p["channel"] = p["stream"] = p["uri"] = None

ch29 = sorted([p for p in profiles if p.get("channel") == CH], key=lambda x: x.get("stream") or 9)
print(f"  ch{CH} profiles: {len(ch29)}")
for p in ch29:
    kind = {1: "main", 2: "sub"}.get(p.get("stream"), "?")
    print(f"    [{kind}] stream={p.get('stream')} token={p['token']} enc={p['enc']} res={p['res']} "
          f"CONFIGURED_fps={p['fps']} bitrate={p['br']} uri={p['uri']}")

pipe = next((p for p in ch29 if p.get("stream") == 1), None)
onvif_fps = pipe["fps"] if pipe else None
print(f"  -> pipeline maps to: {('stream1(main) token='+pipe['token']+' configured '+str(pipe['fps'])+'fps') if pipe else 'unknown'}")
hi = [p for p in ch29 if p["fps"] and pipe and p["fps"] != pipe["fps"]]
print(f"  -> higher/other-fps ch{CH} profiles: {[(p['stream'], p['fps']) for p in hi] or 'none (all same fps)'}")

# GetVideoEncoderConfigurations (standalone list, as requested)
try:
    vroot = ET.fromstring(onvif_resolve._soap(media, '<GetVideoEncoderConfigurations xmlns="http://www.onvif.org/ver10/media/wsdl"/>', USER, PW, 12))
    vcfgs = [x for x in vroot.iter() if lname(x.tag) == "Configurations"]
    fpsset = sorted({ftext(v, "FrameRateLimit") for v in vcfgs if ftext(v, "FrameRateLimit")})
    print(f"  GetVideoEncoderConfigurations: {len(vcfgs)} configs; distinct FrameRateLimit values = {fpsset}")
except Exception as e:
    print("  GetVideoEncoderConfigurations:", str(e)[:80])

URL = creds(pipe["uri"]) if pipe and pipe["uri"] else None
if not URL:
    cm = onvif_resolve.resolve_map(HOST, USER, PW, cache_path="/tmp/onvif_map_probe.json")
    raw = onvif_resolve.uri_for(cm, CH, prefer=1)
    URL = creds(raw) if raw else None
print(f"\n  pipeline ch{CH} URL: {mask(URL)}")

# ---------- 2. ffprobe advertised ----------
print("\n=== 2. FFPROBE ADVERTISED fps ===")
if URL:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
             "-of", "default=noprint_wrappers=1", URL], text=True, timeout=30)
        for line in out.strip().splitlines():
            print("  " + line)
    except Exception as e:
        print("  ffprobe FAILED:", str(e)[:120])

# ---------- 3. ffmpeg bare delivered ----------
print("\n=== 3. FFMPEG BARE DELIVERED fps (120s, no analysis, per-10s bucket) ===")
buckets = []
if URL:
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-rtsp_transport", "tcp", "-i", URL, "-t", "120",
         "-an", "-f", "null", "-progress", "pipe:1", "-stats_period", "10", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    fr, tms, lastfr, lastt = 0, 0.0, 0, 0.0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("frame="):
            try:
                fr = int(line.split("=")[1])
            except Exception:
                pass
        elif line.startswith("out_time_ms="):
            try:
                tms = int(line.split("=")[1]) / 1e6
            except Exception:
                pass
        elif line.startswith("progress="):
            dt = tms - lastt
            if dt > 0:
                buckets.append((round(tms, 1), fr - lastfr, round((fr - lastfr) / dt, 2)))
                lastfr, lastt = fr, tms
    proc.wait()
    print("   end_s  frames  fps")
    for t, frn, f in buckets:
        print(f"   {t:>5}  {frn:>6}  {f}")
    if buckets:
        tot_t, tot_fr = buckets[-1][0], sum(b[1] for b in buckets)
        delivered = round(tot_fr / tot_t, 2) if tot_t else 0
        first, last = buckets[0][2], buckets[-1][2]
        decay = first > 0 and last < 0.6 * first
        print(f"   OVERALL delivered fps: {delivered}  ({tot_fr} frames / {tot_t}s)")
        print(f"   decay: {'YES' if decay else 'no'} (first bucket {first} -> last {last} fps)")

# ---------- 4. comparison ----------
print("\n=== 4. COMPARISON TABLE ===")
adv = "(see block 2)"
dlv = f"{round(sum(b[1] for b in buckets)/buckets[-1][0],2)}" if buckets else "n/a"
print(f"   {'source':20} {'fps':10} notes")
print(f"   {'ONVIF configured':20} {str(onvif_fps):10} ch{CH} main stream (profile=vam)")
print(f"   {'ffprobe advertised':20} {adv:10} avg_frame_rate / r_frame_rate above")
print(f"   {'ffmpeg delivered':20} {dlv:10} per-bucket + decay above")
print(f"   {'pipeline observed':20} {'~0.9':10} known, last diagnostic")
print("\n   CASE:")
print("   - delivered ~= configured ~= 0.9  -> genuinely low-fps stream/profile (fix = higher-fps profile, not pipeline)")
print("   - configured high but delivered ~0.9 -> NVR/network starving delivery")
print("   - delivered high but pipeline ~0.9 -> pipeline loop/pacing bug (blocking read / per-frame sleep / buffer growth)")
print("     (ffmpeg delivered NOT decaying while pipeline does => buffer growth in our loop)")
