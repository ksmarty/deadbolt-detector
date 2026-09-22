# Deadbolt Detector

Lightweight deadbolt lock-state detector that compares camera snapshots to reference images, publishes detection results and camera images over MQTT, and optionally auto-discovers sensors in Home Assistant.

Features
- Multi-reference image support for `locked` and `unlocked` states
- Publishes JSON state and confidence to MQTT (retained)
- Publishes full and cropped camera JPEG frames to MQTT for Home Assistant camera display
- Home Assistant auto-discovery (sensor for state, sensor for confidence, camera, cropped camera, availability, capture buttons)
- Low-confidence mapping: detections below `MIN_CONFIDENCE` map to `unknown`
- Image denoising to reduce frame-to-frame confidence variance
- CLAHE local contrast enhancement for robustness to lighting drift
- NCC alignment window handles small camera bumps and door position variation: the reference is located inside a window around the configured crop rather than compared pixel-for-pixel
- Runtime-tunable settings from the WebUI config modal, applied live and persisted to `config/overrides.json`
- Camera health monitoring: marks entities unavailable and publishes a placeholder image when the camera is unreachable

References are stored **uncropped** and the crop is applied when they are loaded, so changing the crop re-crops every existing reference. Reference images written by older versions (which stored the already-cropped region) are still used as-is.

Quick start (Docker Compose)

1. Copy and edit the example environment file:

```bash
cp example.env .env
# Edit .env and set CAMERA_URL and MQTT_HOST (and any other vars)
```

2. Start the service:

```bash
docker compose up --build -d
```

3. Watch logs:

```bash
docker compose logs -f deadbolt-detector
```

Or run locally (point `CONFIG_DIR` somewhere writable — it defaults to the
container path `/app/config`):

```bash
pip install -r requirements.txt
export CONFIG_DIR="$PWD/config"
export CAMERA_URL="https://camera.local/current.jpg"
export MQTT_HOST="192.168.1.1"
python3 app/main.py
```

How it publishes
- Base topic: `MQTT_TOPIC` (default: `home/deadbolt`).
- State JSON published to `{MQTT_TOPIC}/state` (retained). Payload shape:

```json
{
  "state": "Locked",            // Title-cased published state
  "confidence": 95.2,            // numeric percentage
  "confidence_pct": "95.2%",   // human-readable
  "timestamp": 1670000000.0
}
```

- Full camera JPEG frames are published raw to `{MQTT_TOPIC}/camera` (non-retained).
- Cropped camera JPEG frames are published raw to `{MQTT_TOPIC}/camera_cropped` (non-retained). The crop region is configured via the WebUI.
- Availability is published retained to `{MQTT_TOPIC}/availability` (`online` / `offline`). When the camera is unreachable, availability flips to `offline` and both camera topics receive a "Camera Offline" placeholder image. It flips back to `online` when the camera recovers.
- Capture button commands are published to `{MQTT_TOPIC}/command/capture_locked` and `{MQTT_TOPIC}/command/capture_unlocked`. The result is published to `{MQTT_TOPIC}/command/result`.
- Home Assistant discovery messages are published retained under `{MQTT_DISCOVERY_PREFIX}` so HA can auto-create entities.

Environment variables

Below are all environment variables used by the application and their defaults (as read from the code):

| Variable | Default | Description |
|---|---:|---|
| CAMERA_URL | (required) | URL for camera snapshots (HTTP endpoint). Example: `https://camera.local/cgi-bin/currentpic.cgi` |
| CONFIG_DIR | `/app/config` | Directory for `crop.json`, `overrides.json` and `references/` |
| MQTT_HOST | `mqtt` | MQTT broker host or IP |
| MQTT_PORT | `1883` | MQTT broker port |
| MQTT_USER | `` | MQTT username (optional) |
| MQTT_PASS | `` | MQTT password (optional) |
| MQTT_TOPIC | `home/deadbolt` | **Base** MQTT topic; `state`, `availability`, `camera`, `camera_cropped` and `command/*` are appended to it |
| REFRESH_RATE | `5` | Detection loop interval in seconds (minimum 1) |
| MQTT_DISCOVERY_PREFIX | `homeassistant` | MQTT discovery prefix used by Home Assistant |
| MQTT_DEVICE_NAME | `Deadbolt Detector` | Friendly device name used in discovery payloads |
| MQTT_DEVICE_ID | `` | Optional device identifier; defaults to a sanitized `MQTT_TOPIC` if empty |
| CONF_ALPHA | `50.0` | Alpha parameter used in the sigmoid confidence formula (tuning) |
| CONF_POWER | `0.75` | Power boost applied to the chosen similarity score in confidence formula |
| ALIGN_SEARCH_PIXELS | `15` | How far (in pixels) around the crop the reference may be found when matching. `0` disables alignment and falls back to a direct pixel difference |
| DENOISE_STRENGTH | `0` | OpenCV fastNlMeansDenoising strength applied to frames and references (0=off, 10=mild, higher=stronger) |
| CLAHE_CLIP_LIMIT | `2.0` | CLAHE local contrast enhancement clip limit (0=off, ~2=moderate, higher=stronger local contrast) |
| DETECTOR_DEBUG | (not set) | Set to `1` to enable detector debug output |
| MIN_CONFIDENCE | `0.7` | Minimum confidence [0:1]. Detections below this are mapped to `unknown` |

Everything except the connection settings (`CAMERA_URL`, `MQTT_*`, `CONFIG_DIR`,
`REFRESH_RATE`) can also be overridden at runtime from the WebUI **Config**
button. Overrides take effect immediately, are written to
`$CONFIG_DIR/overrides.json` so they survive a restart, and can be cleared by
unticking *Override* to fall back to the env var. Env values that are empty or
unparseable fall back to the documented default instead of crashing.

### Tuning the confidence

The WebUI **Test detection** button reports the score of each reference set and
the margin between them, e.g.

```
locked 0.997 · unlocked 0.802
margin +0.195
```

Confidence is `chosen^CONF_POWER × sigmoid(CONF_ALPHA × margin)`, so two things
move it:

- **How well the winning reference matches at all** (`chosen`). A crop that
  mostly covers flat wall will match poorly no matter which state the door is in.
  Aim for `chosen` above ~0.8; tighten the crop onto the bolt if it is lower.
- **How much better the winner is than the loser** (the margin). When the two
  references score almost the same the margin goes to zero, the sigmoid returns
  0.5 and confidence halves — the UI flags this in amber. That means the crop
  does not contain anything that distinguishes locked from unlocked (or the two
  reference sets are the same picture), not that the maths is wrong.

If detections land just under `MIN_CONFIDENCE`, raise `CONF_ALPHA` (makes the
margin matter more) or lower `MIN_CONFIDENCE`, both live from the config modal.
Setting `DETECTOR_DEBUG=1` prints the same numbers to the log on every cycle.

Notes for Home Assistant
- The app publishes a plain `sensor` for the lock state (text) and a separate `sensor` for confidence (percentage), plus two MQTT cameras (full frame and cropped), an availability topic, and two button entities for capturing references. The lock sensor uses `value_json.state` to read the textual state.
- The cropped camera focuses on the region of interest (configured in the WebUI crop editor). It shows the same area used for detection.
- Two buttons (`Capture Locked` and `Capture Unlocked`) are available to capture reference images from Home Assistant.
- Published states are Title Case (e.g., `Locked`, `Unlocked`, `Unknown`).
- If the camera becomes unreachable, all entities are marked unavailable via the availability topic and a "Camera Offline" placeholder is published to both camera topics. Entities return to normal automatically when the camera recovers.
- If you see stale entities in HA after changing entity types, clear the retained discovery topics (or wait until the app reconnects and republishes discovery). To clear a retained topic:

```bash
# publish an empty retained payload to delete the retained message
mosquitto_pub -h <broker> -t 'homeassistant/sensor/your_device/config' -r -n
```

Troubleshooting
- If entities are not updating in Home Assistant:
  - Confirm `MQTT_HOST` is reachable from the server/container running the detector.
  - Check container logs for `MQTT connected to` or connection errors: `docker compose logs -f deadbolt-detector`.
  - Subscribe to topics directly to verify messages are being published:

```bash
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'home/deadbolt/state' -v
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'home/deadbolt/availability' -v
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'home/deadbolt/camera' -v
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'home/deadbolt/camera_cropped' -v
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'homeassistant/#' -v
```

- If the app cannot connect to MQTT at startup it will continue running without publishing; check logs for connection attempts.

Developer notes
- Detection and publish mapping live in `app/detector.py` (see `compute_published_state()` and `map_state_for_publish()`), and the MQTT/discovery logic is in `app/main.py`.
- `compute_published_state()` takes an optional `min_confidence`; both `main.py` and `webui.py` pass the detector's effective setting so runtime overrides apply consistently.
- Tunables live in the `TUNABLES` table at the top of `app/detector.py` (env var, caster, default). Adding an entry there makes it available to the env, the override file and the WebUI config modal.
- Do not change the mapping logic unless you want different HA semantics — the UI intentionally shares the same mapping function to keep behavior consistent.

Known limitations
- The reference is located with normalized cross-correlation, so its score is on a different scale than the plain pixel-difference fallback used when `ALIGN_SEARCH_PIXELS=0`. If you change that setting, re-check `MIN_CONFIDENCE`.
- Changing the crop re-crops existing references, which requires them to have been stored uncropped. References captured before that change are used as-is and will not follow crop edits.
- Reference images are matched against the raw frame; if the camera resolution changes, references are resized to match and scores drop until re-captured.
- The crop must contain something that actually differs between locked and unlocked. If both reference sets look the same inside the crop, confidence sits at 50% and the state is reported as `unknown` — the UI says so in amber.

License & Contributing
- PRs welcome. Please open issues for bugs or feature requests.
