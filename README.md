# Deadbolt Detector

Lightweight deadbolt lock-state detector that compares camera snapshots to reference images, publishes detection results and camera images over MQTT, and optionally auto-discovers sensors in Home Assistant.

Features
- Multi-reference image support for `locked` and `unlocked` states
- Publishes JSON state and confidence to MQTT (retained)
- Publishes camera JPEG frames to MQTT for Home Assistant camera display
- Home Assistant auto-discovery (sensor for state, sensor for confidence, camera, availability)
- Low-confidence mapping: detections below `MIN_CONFIDENCE` map to `unknown`

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

Or run locally:

```bash
pip install -r requirements.txt
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

- Camera JPEG frames are published raw to `{MQTT_TOPIC}/camera` (non-retained).
- Availability is published retained to `{MQTT_TOPIC}/availability` (`online` / `offline`).
- Home Assistant discovery messages are published retained under `{MQTT_DISCOVERY_PREFIX}` so HA can auto-create entities.

Environment variables

Below are all environment variables used by the application and their defaults (as read from the code):

| Variable | Default | Description |
|---|---:|---|
| CAMERA_URL | (required) | URL for camera snapshots (HTTP endpoint). Example: `https://camera.local/cgi-bin/currentpic.cgi` |
| MQTT_HOST | `mqtt` | MQTT broker host or IP |
| MQTT_PORT | `1883` | MQTT broker port |
| MQTT_USER | `` | MQTT username (optional) |
| MQTT_PASS | `` | MQTT password (optional) |
| MQTT_TOPIC | `home/deadbolt` | Base MQTT topic used for state, camera and availability |
| REFRESH_RATE | `5` | Detection loop interval in seconds |
| MQTT_DISCOVERY_PREFIX | `homeassistant` | MQTT discovery prefix used by Home Assistant |
| MQTT_DEVICE_NAME | `Deadbolt Detector` | Friendly device name used in discovery payloads |
| MQTT_DEVICE_ID | `` | Optional device identifier; defaults to a sanitized `MQTT_TOPIC` if empty |
| CONF_ALPHA | `50.0` | Alpha parameter used in the sigmoid confidence formula (tuning) |
| DETECTOR_DEBUG | (not set) | Set to `1` to enable detector debug output |
| MIN_CONFIDENCE | `0.7` | Minimum confidence [0:1]. Detections below this are mapped to `unknown` |

Notes for Home Assistant
- The app publishes a plain `sensor` for the lock state (text) and a separate `sensor` for confidence (percentage), plus an MQTT camera discovery entry and an availability topic. The lock sensor uses `value_json.state` to read the textual state.
- Published states are Title Case (e.g., `Locked`, `Unlocked`, `Unknown`).
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
docker run --rm eclipse-mosquitto mosquitto_sub -h <broker> -t 'homeassistant/#' -v
```

- If the app cannot connect to MQTT at startup it will continue running without publishing; check logs for connection attempts.

Developer notes
- Detection and publish mapping live in `app/detector.py` (see `compute_published_state()` and `map_state_for_publish()`), and the MQTT/discovery logic is in `app/main.py`.
- Do not change the mapping logic unless you want different HA semantics — the UI intentionally shares the same mapping function to keep behavior consistent.

License & Contributing
- PRs welcome. Please open issues for bugs or feature requests.
