# Agent Guidance

## Run the app
- **Docker**: `docker compose up --build -d`
- **Local**: `pip install -r requirements.txt` then set env vars and run `python3 app/main.py`

## Key env vars
- `CAMERA_URL` (required): URL to fetch camera snapshots
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`: MQTT connection
- `DETECTOR_DEBUG=1`: Enable debug output
- `MIN_CONFIDENCE=0.7`: Threshold below which state becomes "unknown"
- `ALIGN_SEARCH_PIXELS=15`: Search range for NCC alignment (handles camera shifts/door position variation; higher=more tolerance but slower)
- `DENOISE_STRENGTH=0`: OpenCV fastNlMeansDenoising strength (0=off, 10=mild, higher=stronger)
- `CLAHE_CLIP_LIMIT=2.0`: CLAHE contrast enhancement clip limit (0=off, ~2=moderate, higher=more local contrast)

## Architecture
- Entry point: `app/main.py`
- Detection logic: `app/detector.py`
- WebUI: `app/webui.py`
- Reference images stored in `/app/config/references/{locked,unlocked}/`

## GitHub workflow
- `.github/workflows/docker-image.yml` auto-tags images
- main branch → `latest`, `main`, SHA
- feature branches → branch name + SHA