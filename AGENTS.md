# Agent Guidance

## Run the app
- **Docker**: `docker compose up --build -d`
- **Local**: `pip install -r requirements.txt`, then set env vars and run `python3 app/main.py`.
  Set `CONFIG_DIR` to a writable path (it defaults to the container path `/app/config`).

## Key env vars
- `CAMERA_URL` (required): URL to fetch camera snapshots
- `CONFIG_DIR`: where `crop.json`, `overrides.json` and `references/` live (default `/app/config`)
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`: MQTT connection
- `MQTT_TOPIC`: **base** topic — `state`, `availability`, `camera`, `camera_cropped`, `command/*` are appended
- `REFRESH_RATE=5`: detection loop interval in seconds
- `DETECTOR_DEBUG=1`: Enable debug output
- `MIN_CONFIDENCE=0.7`: Threshold below which state becomes "unknown"
- `OFFLINE_GRACE_SECONDS=30`: How long the camera may be unreachable before availability flips to `offline` (0 = report immediately)
- `ALIGN_SEARCH_PIXELS=15`: How far around the crop the reference may be found when matching (0 disables alignment)
- `DENOISE_STRENGTH=0`: OpenCV fastNlMeansDenoising strength (0=off, 10=mild, higher=stronger)
- `CLAHE_CLIP_LIMIT=2.0`: CLAHE contrast enhancement clip limit (0=off, ~2=moderate, higher=more local contrast)

All tunables are declared in the `TUNABLES` table at the top of `app/detector.py`
and can also be overridden at runtime from the WebUI config modal (persisted to
`$CONFIG_DIR/overrides.json`).

## Architecture
- Entry point: `app/main.py`
- Detection logic: `app/detector.py`
- WebUI: `app/webui.py`
- Reference images stored in `$CONFIG_DIR/references/{locked,unlocked}/`

### Reference images
- References are stored **uncropped**; the crop is applied when they are loaded
  (`_load_and_crop`). Never store a pre-cropped reference — it would be cropped a
  second time on load and compare the wrong region. Legacy pre-cropped files are
  detected (the crop rect cannot fit inside them) and used as-is.
- `get_frame(full=True)` refreshes `last_full_frame` (published to MQTT) and
  `last_cropped_frame` (the tight crop). `detection_window()` returns the crop
  widened by `ALIGN_SEARCH_PIXELS` plus the offset of the crop inside it, which
  `compare()` uses to bound the cross-correlation search.
- MQTT callbacks run on paho's network thread: never let an exception escape
  them, and make sure anything they reference already exists before
  `loop_start()`.
- Availability is owned by two things, and they must not fight: the MQTT **last
  will** (broker-side, covers the process dying) and the grace period in
  `detection_loop` (covers camera outages, see `OFFLINE_GRACE_SECONDS`). Do not
  publish `offline` from `on_disconnect` — paho queues it while disconnected and
  flushes it *after* the next connect's `online`, which leaves the retained
  availability stuck on `offline` until the next real outage.

## GitHub workflow
- `.github/workflows/docker-image.yml` auto-tags images
- main branch → `latest`, `main`, SHA
- feature branches → branch name + SHA
