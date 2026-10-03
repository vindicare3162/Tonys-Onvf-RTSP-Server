# Session Handover — ONVIF/RTSP proxy on Tower

Living document for carrying investigation state between Claude Code sessions.
Newest session at the top of the log. Keep "Current status" accurate at all times.
Canonical copy lives here in the repo; a synced copy goes to the Obsidian vault at
`01 Projects/Active/ONVIF Grey Snapshots/Handover.md` at the end of each session.

## Current status

- **Problem:** ONVIF snapshots served to UniFi Protect came through as grey / smeared blobs.
- **Root cause of the recurrence:** the earlier fix was never deployed; Tower ran an image
  built 2026-09-13 from stock v9.2.5 and the changes lived only as an uncommitted local diff.
- **Mechanism (confirmed, see Evidence):** every main stream is H265. ffmpeg's HEVC decoder
  emits the first P-frame it receives when started mid-GOP, synthesising missing references
  as flat mid-grey. Stock code produced a grey frame on every capture.
- **Fix: DEPLOYED and VERIFIED 2026-10-03 10:28 AEST.** `-skip_frame nokey` before `-i`
  (first emitted frame is always a decoded keyframe) + background per-camera cache +
  grey-fingerprint safety net. Image `78ac74c07e64`, container recreated, all checks in
  "Verification after deploy" passed. ONVIF endpoint now answers in <0.1 s with clean frames
  on all three cameras. Both Tapo lenses also started creating ONVIF event subscriptions
  (previously HTTP 400 on every attempt) thanks to the dict-form GetCapabilities fix.
- **Committed locally** as four commits by concern (f7a9af7 snapshots, 6d1255c ONVIF events,
  96c6f58 watchdog, 65f77cc vNICs) plus this file. **Not pushed** to the fork yet.
- **Next:** watch Protect thumbnails over a day; see "Open questions" for follow-ups.
## Deployment facts (so nobody has to rediscover them)

| Item | Value |
|---|---|
| Host | Unraid "Tower" 192.168.1.51, SSH as root |
| Server repo / compose dir | `/mnt/user/appdata/onvif-rtsp` (clone of upstream at f15c604, only `docker-compose.yml` locally modified: nvidia runtime + unraid labels) |
| Container | `onvif-rtsp-server`, host networking, `runtime: nvidia`, data bind-mounted from `./data` |
| Image | `onvif-rtsp-onvif-rtsp-server:latest` **78ac74c07e64 — built 2026-10-03 10:27 AEST** with the fix. Previous: 23dc2441ce3d (stock, 2026-09-13), now dangling = rollback point. **Code is baked in at build time (COPY . .), so editing files on the host does nothing until `docker compose up -d --build`.** |
| Web UI | http://192.168.1.51:5552 (no login) |
| MediaMTX API | http://localhost:9997 on Tower (v3 API) |
| Virtual cameras (vNICs on `bond0.2`, DHCP) | Tapo C575D Zoom → 192.168.2.158:8002 · Reolink Elite Pro Floodlight → 192.168.2.233:8003 · Tapo C575D Wide → 192.168.2.230:8004 |
| Physical cameras | Tapo C575D 192.168.5.70 (stream6 = zoom 4K H265, stream1 = wide 4K H265) · Reolink 192.168.5.197 (Preview_01_main 7680x2160 H265) |
| NVR | UniFi Protect "Fraser UNVR" 192.168.2.202. Polls `/onvif/snapshot` every ~15 s per camera and holds persistent RTSP readers on all 6 MediaMTX paths. |
| ONVIF auth | HTTP Basic with `onvifUsername`/`onvifPassword` from `data/camera_config.json`, 30-min per-IP cache after first success |
| Local repo | `F:\Projects\Tonys-Onvf-RTSP-Server`, remote = vindicare3162 fork of BigTonyTones upstream |

## What was deployed (commits f7a9af7..65f77cc; was an uncommitted diff until 2026-10-03)

Files: `app/camera.py`, `app/ffmpeg_manager.py`, `app/linux_network.py`, `app/manager.py`, `app/onvif_service.py` (+359/−80).

1. **Background snapshot cache** (`camera.py` `start_snapshot_cache` / `_snapshot_cache_loop`): refreshes a cached JPEG every ~2 s + capture time; `/onvif/snapshot` serves the cache instantly. Includes a corruption check (size floor 0.10 bytes/pixel AND PIL greyscale stddev < 35 → reject and retry) and a 3.5 MB recompress cap for Protect's 4 MB gRPC limit.
2. **`-ss 1` as an output option** in `capture_snapshot` so ffmpeg decodes ~1 s before emitting a frame.
3. ONVIF event forwarder: `GetCapabilities({'Category':'All'})` dict-form call, `InitialTerminationTime PT1H` on subscriptions, exponential backoff with jitter.
4. Watchdog: reload a single MediaMTX path via its API instead of restarting MediaMTX for one stale camera.
5. `linux_network.py`: escape dots in VLAN parent names for sysctl keys; don't `dhclient -r` before requesting (prevents IP swaps between vNICs on restart).

## Session log

### 2026-10-03 (session 2, part 2) — deploy after shfs recovery
- Ben rebooted Tower (shfs recovered; parity check running afterwards, normal after a hard
  reset). Container came back on the old image; vNIC IPs unchanged (.158/.233/.230).
- Vault mirror written earlier via the cache path became visible through `/mnt/user` and
  reached CouchDB after the reboot (id `01 projects/active/onvif grey snapshots/handover.md`).
- Split the local diff into four commits by concern (camera.py hunks separated with a
  filtered `git apply --cached` patch): f7a9af7 snapshots, 6d1255c ONVIF events,
  96c6f58 watchdog, 65f77cc vNICs.
- Deploy 10:20-10:28 AEST: scp of the five files into the server repo, then
  `docker compose up -d --build` (7 min; the YOLO layer re-downloaded). New image
  78ac74c07e64, container started 10:27:57.
- Verification (all passed): in-container md5 of all five files = local; three
  "Snapshot cache thread started"; ffmpeg processes show `-skip_frame nokey`; vNIC IPs
  unchanged; Protect readers back on all six MediaMTX paths; ONVIF fetch of all three
  cameras returned in 0.0-0.1 s with stddev 48-59 (clean) and 2.6-3.2 MB.
- Side effects observed: Tapo zoom and wide now subscribe to events (PullPoint at
  192.168.5.70:1030 / :1032) — the dict-form `GetCapabilities` returns the real events
  XAddr. Reolink 8K snapshots recompress 7.0 MB → 2.99 MB at JPEG quality 35 to fit under
  the 3.5 MB cap (soft but usable; see follow-ups). `sysctl: permission denied` on the
  per-vNIC `arp_ignore`/`arp_announce` keys is pre-existing (container can't write
  /proc/sys) and only visible because global debugMode is on.

### 2026-10-03 (session 2) — investigation of recurrence
- No prior memory existed; reconstructed context from the uncommitted diff, the vault note `03 Resources/Homelab Services/ONVIF Camera Stack (PROD).md`, and the live server.
- md5 comparison: local modified files ≠ server repo files = in-container files (stock). Running ffmpeg snapshot process has no `-ss 1`. Image built Sep 13.
- Protect polls each camera's snapshot every ~15 s; each poll spawns a cold ffmpeg against the warm MediaMTX path (sources never close because Protect reads them continuously).
- Tapo event forwarding fails continuously (HTTP 400 on CreatePullPointSubscription at 192.168.5.70:2020) — separate issue, not in scope here, noted for later.
- Reolink PullMessages returns 400 every ~15 s and resubscribes — this is the short-default-subscription issue the uncommitted `InitialTerminationTime PT1H` change addresses. Also not deployed.
- Tests run this session: see "Evidence" below.
- Deploy attempt (scp + `docker compose up -d --build`) was **blocked by the auto-mode classifier** as a production deploy. Server repo and container untouched. Test JPEGs cleaned out of the container.
- **Tower shfs hang discovered while publishing this note.** Creating the vault folder through `/mnt/user` hung in kernel D state (`filename_create`). `ps` showed shfs/FUSE requests stuck since ~15:13 on 2026-10-02 (`rm -R temp`, `exportfs -ra`, `lsof -Fn /mnt/disk1..8`, three bash shells) and `mkdir -p /mnt/user/appdata/aitoolkit/...` since ~23:24. Reads of existing paths still work; creates and some lookups do not. Load average ~25-32. No dmesg errors. Two of this session's `mkdir` processes (pids 554395, 581348) are stuck the same way and cannot be killed; they clear when shfs recovers or the box reboots. **Ben to decide on array restart / reboot.**
- Vault copy of this file was therefore written via the cache pool path `/mnt/cache/appdata/obsidian/config/Vault/...` (bypasses FUSE; appdata lives entirely on cache). Khoj indexed via PATCH. **CouchDB/LiveSync propagation not confirmed**: a direct-to-disk write raises no inotify event through FUSE, so Obsidian may not pick it up until a rescan or shfs recovery. Re-verify with `_all_docs` on `obsidiandb` next session.
- **Fix chosen:** `-skip_frame nokey` placed before `-i` in `capture_snapshot` (deterministic: first emitted frame is always a decoded keyframe). Kept the background cache from the previous session as the delivery mechanism, tightened its grey check to the measured fingerprint (stddev < 35 AND 115 < mean < 145), and changed the fallback so a flat-but-legitimate scene is served rather than replaced by the sub stream. Capture timeout 10 s → 15 s, cache loop sleep 2 s → 3 s.

### Evidence (2026-10-03, all run inside the container on Tower against the warm MediaMTX paths)

**Keyframe interval** (ffprobe, 8 s window): all three main streams emit a keyframe every **2.0 s**
(Tapo zoom at 1.5/3.5/5.5/7.5 s, Tapo wide at 1.0/3.0/5.0/7.0 s, Reolink at 1.2/3.2/5.2/7.2 s). ffmpeg in the image is 7.1.5.

**Capture comparison** (3 captures per variant per camera, greyscale stddev on a 400 px thumbnail; clean ≈ 56-79, grey ≈ 0.6-9):

| ffmpeg variant | Tapo zoom 4K H265 | Reolink 8K H265 | Tapo wide 4K H265 | Capture time |
|---|---|---|---|---|
| stock (no option) | 0/3 clean | 0/3 clean | 0/3 clean | 3.0-4.9 s |
| `-ss 1` after `-i` (previous session's fix) | 2/3 clean | 1/3 clean | 0/3 clean | 3.1-5.7 s |
| `-skip_frame nokey` after `-i` (wrong placement, applies to encoder) | 1/3 | 0/3 | 0/3 | same as stock |
| **`-skip_frame nokey` before `-i`** | **3/3 clean** | **3/3 clean** | **3/3 clean** | 3.5-5.2 s |
| `-ss 2.5` after `-i` | 3/3 clean | 3/3 clean | 3/3 clean | 4.4-7.5 s, still logs "Could not find ref" |
| stock on Tapo zoom **sub** stream (H264) | 3/3 clean | — | — | 3.7-4.0 s |

**Live ONVIF endpoint** (HTTP Basic, as Protect fetches it): 6/6 grey (stddev 0.7-3.8) on the stock code. This is what Protect was receiving.

**Grey-frame fingerprint** over all 54 samples: every grey frame has greyscale **mean 130.0-130.8** (decoder fills missing references with sample value 128) and stddev ≤ 9; every clean frame has stddev ≥ 56 and mean 99-129. The corruption check now requires both low stddev *and* mid-grey mean, so dark night frames are not rejected.

**Why H265 only:** ffmpeg's H264 decoder suppresses output before the first keyframe by default; the HEVC decoder does not, it synthesises grey reference pictures and emits the P-frame. Every main stream here is H265.

**Hardware note:** RTX 3060 is visible in the container with `hevc_cuvid` available. Not needed for this fix; the keyframe-only decode is cheap on CPU.

## Deploy procedure (NOT yet run - awaiting Ben)

Expect the three cameras to drop out of Protect for about a minute while the container
comes back. The image rebuild itself is fast: only the `COPY . .` layer and later are
invalidated, the PyTorch/YOLO layers are cached.

```bash
# From F:\Projects\Tonys-Onvf-RTSP-Server (Git Bash / PowerShell with OpenSSH)
scp app/camera.py app/ffmpeg_manager.py app/linux_network.py app/manager.py app/onvif_service.py     root@192.168.1.51:/mnt/user/appdata/onvif-rtsp/app/

ssh root@192.168.1.51 'cd /mnt/user/appdata/onvif-rtsp   && ip -br addr | grep vnic_ > /tmp/vnic_before.txt   && docker compose up -d --build'
```

Local checksums of the files being deployed (compare against the container after the build):

| File | md5 (local, 2026-10-03) |
|---|---|
| app/camera.py | 5cfe0637be86621fbdd83934022c20a1 |
| app/ffmpeg_manager.py | 41059743484d005495ba139222057fc0 |
| app/linux_network.py | 92a27c83f4b2e53709c50680ad5c4371 |
| app/manager.py | 1a90fcdd620fb26152262a2af2e6f99a |
| app/onvif_service.py | 51ae62b282d6a99e0ffca8c04be350c0 |
## Verification after deploy

```bash
# 1. The new code is actually in the image (md5 must match local `md5sum app/ffmpeg_manager.py app/camera.py`)
ssh root@192.168.1.51 'docker exec onvif-rtsp-server md5sum /app/app/ffmpeg_manager.py /app/app/camera.py'

# 2. Cache threads started, no tracebacks, no grey-frame warnings
ssh root@192.168.1.51 'docker logs onvif-rtsp-server --since 5m 2>&1 | grep -E "Snapshot cache|Traceback|grey-frame|refresh failed"'

# 3. ffmpeg captures now carry the new flag
ssh root@192.168.1.51 'docker exec onvif-rtsp-server ps -eo args | grep -E "ffmpeg.*skip_frame" | head -3'

# 4. vNIC IPs unchanged (Protect identifies cameras by IP)
ssh root@192.168.1.51 'diff /tmp/vnic_before.txt <(ip -br addr | grep vnic_) && echo IPs unchanged'

# 5. Protect's RTSP readers are back on all six paths
ssh root@192.168.1.51 'curl -s localhost:9997/v3/paths/list | grep -o "\"name\":\"[a-z0-9_]*\"\|\"readers\":\[[^]]*\]"'
```

Then fetch through the ONVIF endpoint the way Protect does (script in the appendix): responses
should come back in well under a second (served from cache) with greyscale stddev > 35.

## Rollback

The previous image `23dc2441ce3d` (stock v9.2.5, built 2026-09-13) becomes a dangling
`<none>` image after the rebuild. Either:

```bash
ssh root@192.168.1.51 'cd /mnt/user/appdata/onvif-rtsp && docker tag 23dc2441ce3d onvif-rtsp-onvif-rtsp-server:latest && docker compose up -d'
# or: git checkout -- app/ in the server repo, then docker compose up -d --build
```

## Open questions / follow-ups

- **CPU cost of the snapshot cache** (measured 10:31 AEST, 3 min after deploy): container
  87-136% of one core on the 56-core host; `python` ~90% in bursts (31% averaged since
  start), `ffmpeg` itself negligible (it mostly waits for a keyframe). Cadence is one
  capture every ~2.2 s across the three cameras (~6.7 s per camera). The cost is PIL:
  decoding a 4K/8K JPEG for the grey check, and for the Reolink re-encoding a 7 MB 8K JPEG
  up to four times to get under 3.5 MB. Cheap remedies for the next deploy, in order:
  (1) `img.draft('L', (400, 400))` before `convert` in `grey_blob_signature` (decodes at
  1/8 scale, ~50x cheaper); (2) in `_shrink_jpeg_if_needed` try a 50% downscale at
  quality 70 first instead of walking the quality ladder at full size; (3) raise the
  loop sleep from 3 s to 5 s; (4) capture with `-q:v 4` so the Reolink lands nearer the
  cap without recompression. None of these change correctness.
- **Push** the six commits to the vindicare3162 fork so Tower could `git pull` instead of scp.
- Watch Protect for a day: do motion-event thumbnails now appear for all three cameras?
- Reolink 8K snapshots land at JPEG quality 35 after recompression. If thumbnails look too
  soft, prefer a 50% downscale at quality 70 over quality 35 at full size (edit the ladder
  in `_shrink_jpeg_if_needed`).
- Reolink PullPoint with `InitialTerminationTime PT1H`: confirm over an hour that
  "PullMessages returned status 400" no longer appears every ~15 s. (None seen in the first
  minutes after deploy.)
- `sysctl: permission denied` for per-vNIC ARP keys inside the container (pre-existing):
  either `privileged: true` or `sysctls:` in compose if ARP flapping is ever observed.
- `ERROR: driverInitFileInfo 599 result=11` at startup: NVIDIA/CUDA init noise from the
  nvidia runtime; not investigated, not new.
- Tower shfs hung for creates on 2026-10-02/03 (see part 1 log). Resolved by reboot; cause
  unknown.
## How to resume

1. Read this file top to bottom.
2. `git status` / `git diff --stat` locally to see what is still uncommitted.
3. On Tower: `docker inspect onvif-rtsp-server --format '{{.Image}} {{.Created}}'` and compare against `docker images` to see whether the image was rebuilt after the fix.
4. `md5sum app/ffmpeg_manager.py` locally vs `docker exec onvif-rtsp-server md5sum /app/app/ffmpeg_manager.py` — mismatch means the running image is behind the repo.
5. Before creating anything on `/mnt/user`, check for D-state processes (`ps -eo pid,stat,wchan:28,etime,args | awk '$2 ~ /D/'`); if shfs is wedged, write appdata via `/mnt/cache/...` and tell Ben.

## Appendix: ONVIF fetch test (run inside the container)

```bash
ssh root@192.168.1.51 "docker exec -i onvif-rtsp-server python -" <<'PY'
import json, requests, time
from PIL import Image, ImageStat
cfg = json.load(open('/app/data/camera_config.json'))
cams = {c['name']: c for c in cfg['cameras']}
ips = {'Tapo C575D Zoom': ('192.168.2.158', 8002), 'Reolink Elite Pro Floodlight': ('192.168.2.233', 8003), 'Tapo C575D Wide': ('192.168.2.230', 8004)}
for name, (ip, port) in ips.items():
    c = cams[name]; t = time.time()
    r = requests.get(f'http://{ip}:{port}/onvif/snapshot', auth=(c['onvifUsername'], c['onvifPassword']), timeout=60)
    dt = round(time.time()-t, 2); open('/tmp/s.jpg', 'wb').write(r.content)
    g = Image.open('/tmp/s.jpg').convert('L'); g.thumbnail((400, 400)); st = ImageStat.Stat(g)
    print(f'{name:30s} status={r.status_code} dt={dt}s bytes={len(r.content)} mean={st.mean[0]:.0f} stddev={st.stddev[0]:.1f}  <- grey if stddev<35 and mean~130')
PY
```
