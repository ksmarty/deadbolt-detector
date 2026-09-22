import glob
import json
import math
import os
import ssl
import time

import cv2
import numpy as np
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)

# Config directory holds the crop, the persisted setting overrides and the
# reference images. Defaults to the path used inside the container, but can be
# pointed somewhere writable when running locally.
CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "crop.json")
OVERRIDES_PATH = os.path.join(CONFIG_DIR, "overrides.json")
REFS_DIR = os.path.join(CONFIG_DIR, "references")


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(os.path.join(REFS_DIR, "locked"), exist_ok=True)
    os.makedirs(os.path.join(REFS_DIR, "unlocked"), exist_ok=True)


def _to_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# Settings that can be tuned at runtime (from the WebUI config modal) as well as
# from the environment. Maps setting name -> (env var, caster, default).
TUNABLES = {
    "min_confidence": ("MIN_CONFIDENCE", float, 0.7),
    "conf_alpha": ("CONF_ALPHA", float, 50.0),
    "conf_power": ("CONF_POWER", float, 0.75),
    "align_search_pixels": ("ALIGN_SEARCH_PIXELS", int, 15),
    "denoise_strength": ("DENOISE_STRENGTH", int, 0),
    "clahe_clip_limit": ("CLAHE_CLIP_LIMIT", float, 2.0),
    "detector_debug": ("DETECTOR_DEBUG", _to_bool, False),
}


def coerce_setting(name, value):
    """Parse `value` for `name`, returning None if it is empty or unparseable.

    Empty values are a common way for a docker-compose `${VAR:-}`
    interpolation to reach us, and those must not raise.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        return TUNABLES[name][1](value)
    except (TypeError, ValueError):
        return None


def env_setting(name):
    """Value of `name` from the environment, falling back to its default."""
    env_var, _, default = TUNABLES[name]
    value = coerce_setting(name, os.getenv(env_var))
    return default if value is None else value


def resolve_settings(overrides=None):
    """Merge environment defaults with runtime overrides.

    An override that cannot be parsed is ignored, leaving the environment value
    (not the hard-coded default) in effect.
    """
    settings = {name: env_setting(name) for name in TUNABLES}
    for name, value in (overrides or {}).items():
        parsed = coerce_setting(name, value)
        if parsed is not None:
            settings[name] = parsed
    return settings


def load_overrides():
    """Read persisted setting overrides from disk (best effort)."""
    try:
        with open(OVERRIDES_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in TUNABLES}


def save_overrides(overrides):
    ensure_dirs()
    with open(OVERRIDES_PATH, "w") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)


class WeakSSLAdapter(HTTPAdapter):
    """Custom adapter that allows weak/self-signed SSL certificates."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers('ALL:@SECLEVEL=0')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


class DeadboltDetector:
    def __init__(self, refresh_rate=5):
        self.refresh_rate = refresh_rate
        self.camera_url = os.getenv('CAMERA_URL')

        self.session = requests.Session()
        self.session.mount('https://', WeakSSLAdapter())
        self.session.mount('http://', HTTPAdapter())
        self.session.verify = False

        self.overrides = load_overrides()
        self.settings = resolve_settings(self.overrides)
        self.clahe = self._build_clahe()

        self.crop = self._load_crop()
        self.ref_images = {'locked': [], 'unlocked': []}
        self.last_full_frame = None
        self.last_cropped_frame = None
        self.camera_online = True
        self.last_detection = None

        ensure_dirs()
        self._load_all_references()

    # ------------------------------------------------------------------ config

    def _build_clahe(self):
        clip = self.settings['clahe_clip_limit']
        return cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)) if clip > 0 else None

    def set_overrides(self, overrides):
        """Replace the runtime overrides, persist them and re-apply them.

        Returns the resulting effective settings. Raises ValueError if a value
        is present but cannot be parsed as that setting's type.
        """
        clean = {}
        invalid = []
        for name, value in (overrides or {}).items():
            if name not in TUNABLES:
                continue
            if value is None or str(value).strip() == "":
                continue
            if coerce_setting(name, value) is None:
                invalid.append(name)
            else:
                clean[name] = str(value).strip()

        if invalid:
            raise ValueError(f"Invalid value for: {', '.join(sorted(invalid))}")

        previous = self.settings
        self.overrides = clean
        self.settings = resolve_settings(clean)
        save_overrides(clean)

        preprocessing_changed = (
            self.settings['clahe_clip_limit'] != previous['clahe_clip_limit']
            or self.settings['denoise_strength'] != previous['denoise_strength']
        )
        if self.settings['clahe_clip_limit'] != previous['clahe_clip_limit']:
            self.clahe = self._build_clahe()
        if preprocessing_changed:
            # References are preprocessed when they are loaded, so they have to
            # be reloaded for the new preprocessing to take effect.
            self._load_all_references()

        return self.settings

    def _load_crop(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r') as f:
                    coords = json.load(f).get('coords', [])
                coords = [int(c) for c in coords]
            except (OSError, ValueError, TypeError) as e:
                print(f"Could not read crop config: {e}")
                return None
            if len(coords) == 4:
                print(f"Loaded crop: {coords}")
                return tuple(coords)
        return None

    def _save_crop(self):
        ensure_dirs()
        data = {'coords': list(self.crop) if self.crop else []}
        with open(CONFIG_PATH, 'w') as f:
            json.dump(data, f)
        print(f"Saved crop: {self.crop}")

    # ------------------------------------------------------------------ images

    def _crop_fits(self, shape):
        """True if the configured crop rect lies fully inside an image."""
        if not self.crop:
            return False
        x1, y1, x2, y2 = self.crop
        h, w = shape[:2]
        return 0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h

    def apply_crop(self, img, margin=0):
        """Crop `img` to the configured region, widened by `margin` pixels."""
        if not self.crop:
            return img

        x1, y1, x2, y2 = self.crop
        h, w = img.shape[:2]
        x1 = max(0, min(x1, w) - margin)
        y1 = max(0, min(y1, h) - margin)
        x2 = max(0, min(x2, w) + margin)
        y2 = max(0, min(y2, h) + margin)
        if x2 > x1 and y2 > y1:
            return img[y1:y2, x1:x2]
        return img

    def _load_all_references(self):
        """Load all reference images from directories."""
        for state in ['locked', 'unlocked']:
            self.ref_images[state] = []
            pattern = os.path.join(REFS_DIR, state, "*.jpg")
            for path in sorted(glob.glob(pattern)):
                img = self._load_and_crop(path)
                if img is not None:
                    self.ref_images[state].append({'path': path, 'image': img})

            count = len(self.ref_images[state])
            print(f"{state}: {count} reference(s) loaded")

    def _denoise(self, img):
        strength = int(self.settings['denoise_strength'])
        if strength > 0:
            return cv2.fastNlMeansDenoising(img, h=strength)
        return img

    def _load_and_crop(self, path):
        """Load a reference image and apply the current crop and preprocessing.

        References are stored uncropped, so the crop is applied here. Images
        stored pre-cropped by older versions are used as-is: cropping those
        again would compare the wrong region (and the crop rect cannot fit
        inside an image that is already just the crop).
        """
        if not os.path.exists(path):
            return None
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None

        if self._crop_fits(img.shape):
            img = self.apply_crop(img)
        img = self._denoise(img)
        if self.clahe is not None:
            img = self.clahe.apply(img)
        return img

    def has_references(self):
        """Check if we have at least one reference for each state."""
        return len(self.ref_images['locked']) > 0 and len(self.ref_images['unlocked']) > 0

    def get_frame(self, full=False):
        """Fetch a frame from the camera.

        Always refreshes `last_full_frame`, and `last_cropped_frame` when a crop
        is configured. Returns the full frame, or the cropped one unless
        `full=True`.
        """
        try:
            response = self.session.get(self.camera_url, timeout=max(10, self.refresh_rate))
            response.raise_for_status()
            frame = cv2.imdecode(
                np.frombuffer(response.content, dtype=np.uint8),
                cv2.IMREAD_COLOR
            )
        except Exception as e:
            print(f"Camera error: {e}")
            self.camera_online = False
            return None

        if frame is None:
            print("Failed to decode JPEG from camera")
            self.camera_online = False
            return None

        self.camera_online = True
        self.last_full_frame = frame.copy()
        # The tight crop is what the WebUI and MQTT publish.
        self.last_cropped_frame = self.apply_crop(frame).copy() if self.crop else None

        return frame if (full or not self.crop) else self.last_cropped_frame

    def detection_window(self):
        """Region of the last frame that detection searches for the reference.

        This is the configured crop widened by `align_search_pixels` so that
        `compare()` has room to find small camera or door shifts. Returns
        `(window, offset)`, where offset is where the reference (the tight crop)
        is expected to sit inside the window — asymmetric when the crop is
        against an image edge and there is no room to widen on that side.
        """
        frame = self.last_full_frame
        if frame is None:
            return None, (0, 0)

        margin = int(self.settings['align_search_pixels'])
        if not self.crop or margin <= 0:
            return self.apply_crop(frame), (0, 0)

        x1, y1, x2, y2 = self.crop
        h, w = frame.shape[:2]
        x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
        y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))

        wx1, wy1 = max(0, x1 - margin), max(0, y1 - margin)
        wx2, wy2 = min(w, x2 + margin), min(h, y2 + margin)
        if wx2 <= wx1 or wy2 <= wy1:
            return self.apply_crop(frame), (0, 0)

        return frame[wy1:wy2, wx1:wx2], (x1 - wx1, y1 - wy1)

    def compare(self, frame, reference, offset=(0, 0)):
        """Calculate normalized similarity score (0-1, higher is better match).

        The frame is preprocessed exactly like the references are, then the
        reference is located inside the frame with normalized cross-correlation
        so that small camera shifts or door position changes do not tank the
        score. `offset` is where the reference is expected to be within the
        frame (see `detection_window`).
        """
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        ref_h, ref_w = reference.shape

        # matchTemplate needs the frame to be at least as large as the reference.
        # Only scale when it is *smaller* (a reference uploaded at a different
        # resolution). Resizing whenever the shapes differ would squash the
        # alignment window back down to the reference size, which silently
        # defeats the whole search and tanks every score.
        if gray.shape[0] < ref_h or gray.shape[1] < ref_w:
            gray = cv2.resize(gray, (ref_w, ref_h))

        gray = self._denoise(gray)
        if self.clahe is not None:
            gray = self.clahe.apply(gray)
        gray = self._normalize_lighting(gray, reference)

        search = int(self.settings['align_search_pixels'])

        # A constant reference has no variance to correlate against, and
        # alignment can be switched off with align_search_pixels=0.
        if search > 0 and float(np.std(reference)) > 0:
            result = cv2.matchTemplate(gray, reference, cv2.TM_CCOEFF_NORMED)
            # matchTemplate indexes by the reference's top-left corner; only the
            # positions within +/- search of where the crop actually is are
            # candidates, clamped to what fits.
            cy, cx = offset
            y0, y1 = max(0, cy - search), min(result.shape[0], cy + search + 1)
            x0, x1 = max(0, cx - search), min(result.shape[1], cx + search + 1)
            if y1 > y0 and x1 > x0:
                best = float(result[y0:y1, x0:x1].max())
                return min(1.0, max(0.0, best)) ** 0.5

        diff = cv2.absdiff(gray, reference)
        mae = float(np.mean(diff))
        return 1.0 - (mae / 255.0)

    def _normalize_lighting(self, img, reference):
        """Normalize img to match reference's histogram for lighting invariance."""
        ref_mean = np.mean(reference)
        ref_std = np.std(reference)
        
        img_mean = np.mean(img)
        img_std = np.std(img)
        
        if img_std > 0:
            normalized = ((img - img_mean) / img_std) * ref_std + ref_mean
            normalized = np.clip(normalized, 0, 255).astype(np.uint8)
            return normalized
        return img

    def detect(self):
        """Run detection comparing against all reference images."""
        settings = self.settings
        frame = self.get_frame(full=True)
        if frame is None:
            return None, 0.0

        if not self.has_references():
            return "unconfigured", 0.0

        window, offset = self.detection_window()

        # Use best (highest) similarity for each state
        best = {
            state: max(
                (self.compare(window, ref['image'], offset) for ref in self.ref_images[state]),
                default=0.0,
            )
            for state in ('locked', 'unlocked')
        }

        # Determine state (which side had the better best-match)
        state = 'locked' if best['locked'] > best['unlocked'] else 'unlocked'
        chosen = best[state]
        other = best['unlocked' if state == 'locked' else 'locked']

        # Confidence formula:
        # confidence = (chosen ^ power) * sigmoid(alpha * delta)
        # - Power boosts the raw similarity score (0.9 -> ~0.95 with power=0.7)
        # - Sigmoid uses difference (delta) for margin sensitivity
        # Tunable via CONF_ALPHA (default 50.0) and CONF_POWER (default 0.75)
        delta = chosen - other
        exponent = -settings['conf_alpha'] * delta
        try:
            p = 1.0 / (1.0 + math.exp(exponent))
        except OverflowError:
            p = 0.0 if exponent > 0 else 1.0

        boosted = chosen ** settings['conf_power']
        confidence = float(max(0.0, min(1.0, boosted * p)))

        # Debug output when enabled
        if settings['detector_debug']:
            print(
                f"detect: locked={best['locked']:.4f}, unlocked={best['unlocked']:.4f}, "
                f"chosen={chosen:.4f}, other={other:.4f}, p={p:.4f}, conf={confidence:.4f}"
            )

        # Kept for the WebUI so the score breakdown behind a confidence is visible.
        self.last_detection = {
            'locked_score': round(best['locked'], 4),
            'unlocked_score': round(best['unlocked'], 4),
            'chosen': round(chosen, 4),
            'other': round(other, 4),
            'margin': round(delta, 4),
            'margin_probability': round(p, 4),
        }

        return state, confidence

    def new_reference_path(self, state):
        """Pick an unused path for a new reference image.

        Timestamps only have second resolution, so a suffix is added rather
        than letting two captures in the same second overwrite each other.
        """
        ensure_dirs()
        stamp = int(time.time())
        path = os.path.join(REFS_DIR, state, f"{state}_{stamp}.jpg")
        suffix = 1
        while os.path.exists(path):
            path = os.path.join(REFS_DIR, state, f"{state}_{stamp}_{suffix}.jpg")
            suffix += 1
        return path

    def capture_reference(self, state, frame=None):
        """Capture the current frame as a new reference image.

        The uncropped frame is stored so that changing the crop later re-crops
        existing references instead of cropping them a second time.
        """
        if frame is None:
            frame = self.get_frame(full=True)
        if frame is None:
            return None

        # Convert to grayscale and save
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

        filepath = self.new_reference_path(state)
        cv2.imwrite(filepath, gray)

        # Reload references
        self._load_all_references()

        return filepath

    def delete_reference(self, state, filename):
        """Delete a reference image. Only plain .jpg names inside the state
        directory are accepted."""
        name = os.path.basename(filename)
        if name != filename or not name.lower().endswith('.jpg'):
            return False
        filepath = os.path.join(REFS_DIR, state, name)
        if os.path.exists(filepath):
            os.remove(filepath)
            self._load_all_references()
            return True
        return False

    def reload_config(self):
        """Reload crop and references."""
        new_crop = self._load_crop()
        if new_crop != self.crop:
            print(f"Crop changed: {self.crop} -> {new_crop}")
            self.crop = new_crop
            self._load_all_references()
            return True
        return False


def compute_published_state(state: str, confidence: float, min_confidence: float = None) -> str:
    """Compute the state to publish to MQTT/WebUI.

    If the detector reports `locked` or `unlocked` but the confidence is
    below the configured minimum (setting `min_confidence`, env
    `MIN_CONFIDENCE`, default 0.7), return "unknown". Otherwise preserve the
    historical published semantics by delegating to `map_state_for_publish`.
    """
    if min_confidence is None:
        min_confidence = env_setting("min_confidence")

    if state in ("locked", "unlocked"):
        if confidence < min_confidence:
            return "unknown"
        return map_state_for_publish(state)
    return state


def map_state_for_publish(state: str) -> str:
    """Map internal detector state to the published state semantics.

    This function ensures consistent state naming between internal detection
    and external consumers (MQTT, WebUI). Currently passes through unchanged
    since detector state already matches desired semantics.
    """
    # Pass through unchanged - detector state is already correct
    return state
