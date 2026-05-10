import os
import json
import cv2
import numpy as np
import urllib3
import ssl
import requests
import glob
import math
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from urllib3.exceptions import InsecureRequestWarning

urllib3.disable_warnings(InsecureRequestWarning)

CONFIG_PATH = "/app/config/crop.json"
REFS_DIR = "/app/config/references"

def ensure_dirs():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    os.makedirs(os.path.join(REFS_DIR, "locked"), exist_ok=True)
    os.makedirs(os.path.join(REFS_DIR, "unlocked"), exist_ok=True)

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
    def __init__(self, mqtt_client=None, refresh_rate=5):
        self.mqtt = mqtt_client
        self.refresh_rate = refresh_rate
        self.camera_url = os.getenv('CAMERA_URL')

        self.session = requests.Session()
        self.session.mount('https://', WeakSSLAdapter())
        self.session.mount('http://', HTTPAdapter())
        self.session.verify = False

        self.crop = self._load_crop()
        self.ref_images = {'locked': [], 'unlocked': []}
        self.last_full_frame = None
        self.last_cropped_frame = None

        ensure_dirs()
        self._load_all_references()

    def _load_crop(self):
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                data = json.load(f)
                coords = data.get('coords', [])
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

    def _load_and_crop(self, path):
        """Load image and apply current crop."""
        if not os.path.exists(path):
            return None
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None

        if self.crop:
            x1, y1, x2, y2 = self.crop
            h, w = img.shape
            x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
            y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
            if x2 > x1 and y2 > y1:
                img = img[y1:y2, x1:x2]
        return img

    def has_references(self):
        """Check if we have at least one reference for each state."""
        return len(self.ref_images['locked']) > 0 and len(self.ref_images['unlocked']) > 0

    def get_frame(self, full=False):
        """Fetch frame from camera."""
        try:
            response = self.session.get(self.camera_url, timeout=30)
            response.raise_for_status()

            frame = cv2.imdecode(
                np.frombuffer(response.content, dtype=np.uint8),
                cv2.IMREAD_COLOR
            )

            if frame is None:
                print("Failed to decode JPEG from camera")
                return None

            self.last_full_frame = frame.copy()

            if not full and self.crop:
                x1, y1, x2, y2 = self.crop
                h, w = frame.shape[:2]
                x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
                y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
                if x2 > x1 and y2 > y1:
                    cropped = frame[y1:y2, x1:x2]
                    self.last_cropped_frame = cropped.copy()
                    frame = cropped

            return frame

        except Exception as e:
            print(f"Camera error: {e}")
            return None

    def compare(self, frame, reference):
        """Calculate normalized similarity score (0-1, higher is better match).
        
        Uses normalized cross-correlation with histogram normalization to handle
        both slight camera position changes and lighting variations.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if gray.shape != reference.shape:
            gray = cv2.resize(gray, (reference.shape[1], reference.shape[0]))

        gray = self._normalize_lighting(gray, reference)
        reference = self._normalize_lighting(reference, reference)

        ref_h, ref_w = reference.shape
        search_range = int(os.getenv('ALIGN_SEARCH_PIXELS', '10'))

        if search_range > 0 and gray.shape[0] > ref_h + 2*search_range and gray.shape[1] > ref_w + 2*search_range:
            best_score = -1
            h, w = gray.shape

            for dy in range(-search_range, search_range + 1):
                for dx in range(-search_range, search_range + 1):
                    y_start = search_range + dy
                    y_end = y_start + ref_h
                    x_start = search_range + dx
                    x_end = x_start + ref_w

                    if y_end <= h and x_end <= w:
                        window = gray[y_start:y_end, x_start:x_end]

                        mean_ref = np.mean(reference)
                        mean_win = np.mean(window)

                        ref_centered = reference.astype(np.float32) - mean_ref
                        win_centered = window.astype(np.float32) - mean_win

                        norm_ref = np.sqrt(np.sum(ref_centered ** 2))
                        norm_win = np.sqrt(np.sum(win_centered ** 2))

                        if norm_ref > 0 and norm_win > 0:
                            ncc = np.sum(ref_centered * win_centered) / (norm_ref * norm_win)
                            ncc = max(0, ncc)
                            if ncc > best_score:
                                best_score = ncc

            if best_score < 0:
                return 0.0

            score = best_score ** 0.5
            return score

        diff = cv2.absdiff(gray, reference)
        mae = np.mean(diff)
        similarity = 1.0 - (mae / 255.0)
        return similarity

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
        frame = self.get_frame(full=False)
        if frame is None:
            return None, 0

        if not self.has_references():
            return "unconfigured", 0

        # Compare against all references, use best match for each state
        locked_scores = []
        for ref in self.ref_images['locked']:
            score = self.compare(frame, ref['image'])
            locked_scores.append(score)

        unlocked_scores = []
        for ref in self.ref_images['unlocked']:
            score = self.compare(frame, ref['image'])
            unlocked_scores.append(score)

        # Use best (highest) similarity for each state
        best_locked = max(locked_scores) if locked_scores else 0
        best_unlocked = max(unlocked_scores) if unlocked_scores else 0

        # Determine state (which side had the better best-match)
        if best_locked > best_unlocked:
            state = "locked"
            chosen = best_locked
            other = best_unlocked
        else:
            state = "unlocked"
            chosen = best_unlocked
            other = best_locked

        # Confidence formula:
        # confidence = (chosen ^ power) * sigmoid(alpha * delta)
        # - Power boosts the raw similarity score (0.9 -> ~0.95 with power=0.7)
        # - Sigmoid uses difference (delta) for margin sensitivity
        # Tunable via CONF_ALPHA (default 50.0) and CONF_POWER (default 0.75)
        alpha = float(os.getenv('CONF_ALPHA', '50.0'))
        power = float(os.getenv('CONF_POWER', '0.75'))
        delta = float(chosen) - float(other)
        try:
            p = 1.0 / (1.0 + math.exp(-alpha * delta))
        except OverflowError:
            p = 0.0 if (alpha * delta) < 0 else 1.0

        boosted = float(chosen) ** power
        confidence = boosted * float(p)

        # Debug output when enabled
        if os.getenv('DETECTOR_DEBUG') == '1':
            print(
                f"detect: locked={best_locked:.4f}, unlocked={best_unlocked:.4f}, chosen={chosen:.4f}, other={other:.4f}, p={p:.4f}, conf={confidence:.4f}"
            )

        # Clamp to [0,1]
        confidence = float(max(0.0, min(1.0, confidence)))

        return state, confidence

    def capture_reference(self, state, frame=None):
        """Capture current frame as new reference image."""
        if frame is None:
            frame = self.get_frame(full=True)
        if frame is None:
            return None

        # Apply crop if set
        if self.crop:
            x1, y1, x2, y2 = self.crop
            h, w = frame.shape[:2]
            x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
            y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
            if x2 > x1 and y2 > y1:
                frame = frame[y1:y2, x1:x2]

        # Convert to grayscale and save
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame

        # Generate filename with timestamp
        import time
        timestamp = int(time.time())
        filename = f"{state}_{timestamp}.jpg"
        filepath = os.path.join(REFS_DIR, state, filename)

        cv2.imwrite(filepath, gray)

        # Reload references
        self._load_all_references()

        return filepath

    def delete_reference(self, state, filename):
        """Delete a reference image."""
        filepath = os.path.join(REFS_DIR, state, filename)
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


def compute_published_state(state: str, confidence: float) -> str:
    """Compute the state to publish to MQTT/WebUI.

    If the detector reports `locked` or `unlocked` but the confidence is
    below the configured minimum (env `MIN_CONFIDENCE`, default 0.7),
    return "unknown". Otherwise preserve the historical published
    semantics by delegating to `map_state_for_publish`.
    """
    try:
        min_conf = float(os.getenv("MIN_CONFIDENCE", "0.7"))
    except Exception:
        min_conf = 0.7

    if state in ("locked", "unlocked"):
        if confidence < min_conf:
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

