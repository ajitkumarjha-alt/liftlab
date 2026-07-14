# NVR capabilities — site-A (Honeywell I-HNVR-1240, Dahua-OEM)

NVR `172.50.6.210` (eth0 NVR-leg). Determined 2026-07-14 by direct probing.
**RE-CHECK PER SITE** — a different NVR/firmware at site 2..5 may differ; run the
probes (`grammar_probe.py`, `onvif_probe.py`, `replay_probe.py`, `dahua_probe.py`)
before assuming.

## Channel identity (LIVE) — WORKS via ONVIF
- ONVIF device service on **:80** (`/onvif/device_service`), media `/onvif/media`.
- `GetProfiles` → **80 profiles** (40 channels × main+sub). `GetStreamUri` returns the
  authoritative per-channel LIVE URI, **path-based**:
  `rtsp://<host>:554/<channel>/<stream>?transmode=unicast&profile=vam`
  (stream 1 = main, 2 = sub). ch1→`/1/1`, ch29→`/29/1`; ch1-vs-ch29 diff 44.6 = real.
- **Constructed RTSP `?channel=N` is IGNORED** — `cam/playback` and `cam/realmonitor`,
  subtype 0/1/none, 0-/1-indexed, channel-in-path all return ONE default camera
  ("42B REFUGE"). Only the ONVIF-resolved `/ch/stream` path selects a channel.
- Password `lodha@000` must be **URL-encoded** (`lodha%40000`) in every RTSP URL.

## Historical (by channel + past time) — NOT AVAILABLE over the network
All three network mechanisms dead:
1. **RTSP `starttime`** ignored — pull of a past window returns the LIVE stream
   (validation frame: requested ch27 2026-07-12 08:00, OSD read 2026-07-14 13:03 live).
2. **ONVIF Replay service** — absent (no `replay` in `GetServices`).
3. **Dahua HTTP API** — STRIPPED by the OEM: `magicBox.cgi`, `global.cgi`,
   `mediaFileFind.cgi`, `loadfile.cgi`, `RPC2`/`RPC2_Login`, `RPC_Loadfile` all **404/405**.
   `magicBox getSystemInfo` = 404 → the Dahua CGI/RPC layer is not exposed.

**Consequence:** the only network path to footage is **LIVE capture** (ONVIF `/ch/stream`).
Historical retrieval needs an **NVR-side export / SDK / FM action** (physical/USB export,
vendor SDK, or Honeywell integration) — it is NOT a code fix. This changes the study's
intake path for site-A: analysis runs on **live captures of the right cabin**, not chosen
past windows, until FM provides a historical export route.

## What the code does with this
`pull_playback` / `analyze_local` are migrated off the broken `cam/playback?channel=`
builder onto the **ONVIF live channel map** (`onvif_resolve.py`) so a live analyze hits the
RIGHT cabin. `keep_validation_frame` remains the standing Proof #3 (OSD-verified) on every
commissioning pull.
