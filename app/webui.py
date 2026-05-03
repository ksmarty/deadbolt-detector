import os
import json
import cv2
import numpy as np
import base64
from flask import Flask, render_template, jsonify, request
from detector import REFS_DIR, compute_published_state
import glob

app = Flask(__name__)
detector = None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/frame')
def get_frame():
    """Return current live frame with crop overlay."""
    detection_frame = detector.get_frame(full=False)

    if detector.last_full_frame is None:
        return jsonify({
            'error': 'No frame available',
            'crop': detector.crop,
            'resolution': (0, 0)
        }), 503

    display_frame = detector.last_full_frame.copy()

    if detector.crop:
        x1, y1, x2, y2 = detector.crop
        cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

    _, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    img_b64 = base64.b64encode(buffer).decode('utf-8')

    return jsonify({
        'image': f'data:image/jpeg;base64,{img_b64}',
        'crop': detector.crop,
        'resolution': display_frame.shape[:2]
    })

@app.route('/api/crop', methods=['GET', 'POST'])
def crop_endpoint():
    if request.method == 'GET':
        return jsonify({'coords': list(detector.crop) if detector.crop else None})

    data = request.get_json()
    coords = data.get('coords', [])

    if len(coords) == 4:
        detector.crop = tuple(coords)
        detector._save_crop()
        detector._load_all_references()
        return jsonify({'success': True, 'crop': coords})
    elif len(coords) == 0:
        detector.crop = None
        detector._save_crop()
        return jsonify({'success': True, 'crop': None})

    return jsonify({'success': False, 'error': 'Invalid coordinates'}), 400

@app.route('/api/references')
def get_references():
    """Return list of all reference images with thumbnails."""
    result = {'locked': [], 'unlocked': []}

    for state in ['locked', 'unlocked']:
        for ref in detector.ref_images.get(state, []):
            try:
                # Create thumbnail preserving aspect ratio and pad to target size
                def make_thumb(img, tw=160, th=120):
                    if img is None:
                        return None
                    h, w = img.shape[:2]
                    if w == 0 or h == 0:
                        return None
                    scale = min(tw / w, th / h)
                    new_w = max(1, int(round(w * scale)))
                    new_h = max(1, int(round(h * scale)))
                    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
                    # Create gray canvas and center the resized image
                    if resized.ndim == 2:
                        canvas = np.full((th, tw), 128, dtype=resized.dtype)
                        x = (tw - new_w) // 2
                        y = (th - new_h) // 2
                        canvas[y:y+new_h, x:x+new_w] = resized
                    else:
                        canvas = np.full((th, tw, resized.shape[2]), 128, dtype=resized.dtype)
                        x = (tw - new_w) // 2
                        y = (th - new_h) // 2
                        canvas[y:y+new_h, x:x+new_w, :] = resized
                    return canvas

                thumb = make_thumb(ref['image'], 160, 120)
                if thumb is None:
                    continue
                _, buffer = cv2.imencode('.jpg', thumb)
                thumb_b64 = base64.b64encode(buffer).decode('utf-8')

                result[state].append({
                    'filename': os.path.basename(ref['path']),
                    'thumbnail': f'data:image/jpeg;base64,{thumb_b64}'
                })
            except Exception as e:
                print(f"Error creating thumbnail: {e}")

    return jsonify(result)

@app.route('/api/capture', methods=['POST'])
def capture_reference():
    """Capture current frame as new reference."""
    data = request.get_json()
    state = data.get('type')

    if state not in ['locked', 'unlocked']:
        return jsonify({'success': False, 'error': 'Invalid type'}), 400

    filepath = detector.capture_reference(state)

    if filepath:
        return jsonify({
            'success': True,
            'path': filepath,
            'filename': os.path.basename(filepath)
        })
    else:
        return jsonify({'success': False, 'error': 'Failed to capture'}), 500

@app.route('/api/upload', methods=['POST'])
def upload_reference():
    """Upload reference image from file."""
    state = request.form.get('type')
    if state not in ['locked', 'unlocked']:
        return jsonify({'success': False, 'error': 'Invalid type'}), 400

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': 'No image'}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file'}), 400

    try:
        file_bytes = file.read()
        nparr = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return jsonify({'success': False, 'error': 'Invalid image'}), 400

        # Apply crop if set
        if detector.crop:
            x1, y1, x2, y2 = detector.crop
            h, w = img.shape[:2]
            x1, x2 = max(0, min(x1, w)), max(0, min(x2, w))
            y1, y2 = max(0, min(y1, h)), max(0, min(y2, h))
            if x2 > x1 and y2 > y1:
                img = img[y1:y2, x1:x2]

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Save with timestamp
        import time
        timestamp = int(time.time())
        filename = f"{state}_{timestamp}.jpg"
        filepath = os.path.join(REFS_DIR, state, filename)
        cv2.imwrite(filepath, gray)

        detector._load_all_references()
        return jsonify({'success': True, 'filename': filename})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reference/<state>/<filename>', methods=['DELETE'])
def delete_reference(state, filename):
    """Delete a reference image."""
    if state not in ['locked', 'unlocked']:
        return jsonify({'success': False, 'error': 'Invalid state'}), 400

    success = detector.delete_reference(state, filename)
    return jsonify({'success': success})

@app.route('/api/references/batch-delete', methods=['POST'])
def batch_delete_references():
    """Delete multiple reference images."""
    data = request.get_json()
    state = data.get('state')
    filenames = data.get('filenames', [])

    if state not in ['locked', 'unlocked']:
        return jsonify({'success': False, 'error': 'Invalid state'}), 400

    deleted = 0
    for filename in filenames:
        if detector.delete_reference(state, filename):
            deleted += 1

    return jsonify({'success': True, 'deleted': deleted})

@app.route('/api/config')
def get_config():
    """Return current configuration info."""
    import os
    config = {
        'camera_url': os.environ.get('CAMERA_URL', 'Not set'),
        'mqtt_host': os.environ.get('MQTT_HOST', 'Not set'),
        'mqtt_port': os.environ.get('MQTT_PORT', 'Not set'),
        'mqtt_user': os.environ.get('MQTT_USER', 'Not set'),
        'detector_debug': os.environ.get('DETECTOR_DEBUG', '0'),
        'min_confidence': os.environ.get('MIN_CONFIDENCE', '0.7'),
        'align_search_pixels': os.environ.get('ALIGN_SEARCH_PIXELS', '10'),
        'conf_alpha': os.environ.get('CONF_ALPHA', '50.0'),
        'conf_power': os.environ.get('CONF_POWER', '0.75'),
    }
    return jsonify(config)

@app.route('/api/detect')
def test_detect():
    """Run detection on current frame."""
    state, confidence = detector.detect()

    # Map internal state + confidence to the published state (unknown when low confidence)
    publish_state = compute_published_state(state, confidence)

    # Return decimal confidence (0-1) and a human readable percent
    confidence_pct = round(confidence * 100, 1)

    if state is None:
        return jsonify({
            'state': 'error',
            'confidence': 0.0,
            'confidence_pct': '0%',
            'error': 'Detection failed'
        })

    return jsonify({
        'state': publish_state,
        'confidence': confidence,
        'confidence_pct': f"{confidence_pct}%"
    })

def run_webui(detector_instance, host='0.0.0.0', port=5000):
    global detector
    detector = detector_instance
    app.run(host=host, port=port, threaded=True, debug=False)
