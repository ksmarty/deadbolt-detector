# Agent Guidance

## Run the app
- **Docker**: `docker compose up --build -d`
- **Local**: `pip install -r requirements.txt` then set env vars and run `python3 app/main.py`

## Key env vars
- `CAMERA_URL` (required): URL to fetch camera snapshots
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`: MQTT connection
- `DETECTOR_DEBUG=1`: Enable debug output
- `MIN_CONFIDENCE=0.7`: Threshold below which state becomes "unknown"
- `ALIGN_SEARCH_PIXELS=10`: Search range for NCC alignment (handles camera shifts)

## Architecture
- Entry point: `app/main.py`
- Detection logic: `app/detector.py`
- WebUI: `app/webui.py`
- Reference images stored in `/app/config/references/{locked,unlocked}/`

## GitHub workflow
- `.github/workflows/docker-image.yml` auto-tags images
- main branch → `latest`, `main`, SHA
- feature branches → branch name + SHA