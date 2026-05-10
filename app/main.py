#!/usr/bin/env python3
import os
import sys
import time
import threading
import json
import paho.mqtt.client as mqtt
from detector import DeadboltDetector, compute_published_state
import cv2
from webui import run_webui

# Configuration from environment
CAMERA_URL = os.getenv('CAMERA_URL')
MQTT_HOST = os.getenv('MQTT_HOST', 'mqtt')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
MQTT_USER = os.getenv('MQTT_USER', '')
MQTT_PASS = os.getenv('MQTT_PASS', '')
MQTT_TOPIC = os.getenv('MQTT_TOPIC', 'home/deadbolt')
REFRESH_RATE = int(os.getenv('REFRESH_RATE', '5'))
MQTT_DISCOVERY_PREFIX = os.getenv('MQTT_DISCOVERY_PREFIX', 'homeassistant')
MQTT_DEVICE_NAME = os.getenv('MQTT_DEVICE_NAME', 'Deadbolt Detector')
MQTT_DEVICE_ID = os.getenv('MQTT_DEVICE_ID', '')

def main():
    print("=" * 50)
    print("Deadbolt Detector Starting")
    print("=" * 50)

    if not CAMERA_URL:
        print("ERROR: CAMERA_URL environment variable not set")
        sys.exit(1)

    print(f"Camera URL: {CAMERA_URL}")
    print(f"Refresh Rate: {REFRESH_RATE}s")
    print(f"MQTT: {MQTT_HOST}:{MQTT_PORT}")
    print(f"Topic: {MQTT_TOPIC}")
    print(f"MQTT Auth: {'enabled' if MQTT_USER else 'disabled'}")

    # Setup MQTT client
    mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        print(f"MQTT username: {MQTT_USER}")

    # FIXED: Paho v2 callback signatures
    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0 or (hasattr(reason_code, 'is_failure') and not reason_code.is_failure):
            print(f"MQTT connected to {MQTT_HOST}")
            # Publish Home Assistant discovery config and availability
            try:
                # Build identifiers
                dev_id = MQTT_DEVICE_ID if MQTT_DEVICE_ID else MQTT_TOPIC.replace('/', '_')
                base_state_topic = f"{MQTT_TOPIC}/state"
                availability_topic = f"{MQTT_TOPIC}/availability"

                device = {
                    "identifiers": [dev_id],
                    "name": MQTT_DEVICE_NAME,
                    "model": "deadbolt-detector",
                    "manufacturer": "deadbolt-detector"
                }

                # Sensor for lock state (text) — replaces previous binary_sensor
                state_sensor_payload = {
                    "name": f"{MQTT_DEVICE_NAME} Lock State",
                    "state_topic": base_state_topic,
                    "value_template": "{{ value_json.state }}",
                    "unique_id": f"{dev_id}_state",
                    "json_attributes_topic": base_state_topic,
                    "availability_topic": availability_topic,
                    "device": device,
                    "icon": "mdi:lock"
                }
                state_sensor_topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/{dev_id}_state/config"
                client.publish(state_sensor_topic, json.dumps(state_sensor_payload), qos=1, retain=True)

                # Sensor for confidence
                sensor_payload = {
                    "name": f"{MQTT_DEVICE_NAME} Confidence",
                    "state_topic": base_state_topic,
                    "value_template": "{{ value_json.confidence }}",
                    "unit_of_measurement": "%",
                    "unique_id": f"{dev_id}_confidence",
                    "json_attributes_topic": base_state_topic,
                    "availability_topic": availability_topic,
                    "device": device
                }
                sensor_topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/{dev_id}_confidence/config"
                client.publish(sensor_topic, json.dumps(sensor_payload), qos=1, retain=True)

                # (previously created a duplicate text sensor here; replaced by the lock state sensor above)

                # Camera discovery (MQTT camera expects binary JPEG payloads on the topic)
                try:
                    camera_payload = {
                        "name": f"{MQTT_DEVICE_NAME} Camera",
                        "topic": f"{MQTT_TOPIC}/camera",
                        "unique_id": f"{dev_id}_camera",
                        "availability_topic": availability_topic,
                        "device": device
                    }
                    camera_topic = f"{MQTT_DISCOVERY_PREFIX}/camera/{dev_id}/config"
                    client.publish(camera_topic, json.dumps(camera_payload), qos=1, retain=True)
                except Exception as e:
                    print(f"Failed to publish camera discovery: {e}")

                # Button to capture locked reference
                try:
                    capture_locked_payload = {
                        "name": f"{MQTT_DEVICE_NAME} Capture Locked",
                        "command_topic": f"{MQTT_TOPIC}/command/capture_locked",
                        "unique_id": f"{dev_id}_capture_locked",
                        "availability_topic": availability_topic,
                        "device": device,
                        "icon": "mdi:camera"
                    }
                    capture_locked_topic = f"{MQTT_DISCOVERY_PREFIX}/button/{dev_id}_capture_locked/config"
                    client.publish(capture_locked_topic, json.dumps(capture_locked_payload), qos=1, retain=True)
                except Exception as e:
                    print(f"Failed to publish capture_locked button: {e}")

                # Button to capture unlocked reference
                try:
                    capture_unlocked_payload = {
                        "name": f"{MQTT_DEVICE_NAME} Capture Unlocked",
                        "command_topic": f"{MQTT_TOPIC}/command/capture_unlocked",
                        "unique_id": f"{dev_id}_capture_unlocked",
                        "availability_topic": availability_topic,
                        "device": device,
                        "icon": "mdi:camera"
                    }
                    capture_unlocked_topic = f"{MQTT_DISCOVERY_PREFIX}/button/{dev_id}_capture_unlocked/config"
                    client.publish(capture_unlocked_topic, json.dumps(capture_unlocked_payload), qos=1, retain=True)
                except Exception as e:
                    print(f"Failed to publish capture_unlocked button: {e}")

                # Publish retained availability online
                client.publish(availability_topic, "online", qos=1, retain=True)

                # Subscribe to command topics
                client.subscribe(f"{MQTT_TOPIC}/command/capture_locked", qos=1)
                client.subscribe(f"{MQTT_TOPIC}/command/capture_unlocked", qos=1)
            except Exception as e:
                print(f"Failed to publish Home Assistant discovery: {e}")
        else:
            print(f"MQTT connection failed: {reason_code}")

    def on_disconnect(client, userdata, disconnect_flags, rc, properties):
        print(f"MQTT disconnected (rc={rc}), will retry...")
        # Publish retained offline availability so Home Assistant marks the entity unavailable
        try:
            availability_topic = f"{MQTT_TOPIC}/availability"
            client.publish(availability_topic, "offline", qos=1, retain=True)
        except Exception:
            pass

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect

    def on_message(client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode('utf-8') if msg.payload else ""
        print(f"MQTT message: {topic} = {payload}")
        
        if topic == f"{MQTT_TOPIC}/command/capture_locked":
            filepath = detector.capture_reference("locked")
            if filepath:
                client.publish(f"{MQTT_TOPIC}/command/result", f"Captured locked: {filepath}", qos=1)
                print(f"Captured locked reference: {filepath}")
            else:
                client.publish(f"{MQTT_TOPIC}/command/result", "Failed to capture locked", qos=1)
        
        elif topic == f"{MQTT_TOPIC}/command/capture_unlocked":
            filepath = detector.capture_reference("unlocked")
            if filepath:
                client.publish(f"{MQTT_TOPIC}/command/result", f"Captured unlocked: {filepath}", qos=1)
                print(f"Captured unlocked reference: {filepath}")
            else:
                client.publish(f"{MQTT_TOPIC}/command/result", "Failed to capture unlocked", qos=1)

    mqtt_client.on_message = on_message

    # Connect with retry
    connected = False
    for attempt in range(30):
        try:
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
            mqtt_client.loop_start()
            connected = True
            break
        except Exception as e:
            print(f"MQTT connection attempt {attempt+1}/30 failed: {e}")
            time.sleep(2)

    if not connected:
        print("WARNING: Could not connect to MQTT, continuing without publishing")
        mqtt_client = None

    # Initialize detector with refresh rate
    print("Initializing detector...")
    detector = DeadboltDetector(
        mqtt_client=mqtt_client,
        refresh_rate=REFRESH_RATE
    )

    # Start detection loop in background thread
    def detection_loop():
        print(f"Detection loop started ({REFRESH_RATE}s interval)")
        while True:
            try:
                state, confidence = detector.detect()
                if state and mqtt_client and state != "unconfigured":
                    # Compute published state honoring confidence threshold
                    publish_state = compute_published_state(state, confidence)

                    # Convert to percentage for MQTT
                    confidence_pct = round(confidence * 100, 1)
                    payload_dict = {
                        'state': publish_state.title(),
                        'confidence': confidence_pct,
                        'confidence_pct': f"{confidence_pct}%",
                        'timestamp': time.time()
                    }
                    # Save last published payload on the detector for UI/debugging
                    try:
                        detector.last_published = payload_dict
                    except Exception:
                        pass

                    mqtt_client.publish(f"{MQTT_TOPIC}/state", json.dumps(payload_dict), qos=1, retain=True)
                    print(f"Detected: {state} -> published: {publish_state} (confidence: {confidence_pct}% )")
                    # Publish camera image (raw JPEG) to the camera topic so Home Assistant camera can display it
                    try:
                        if detector.last_full_frame is not None:
                            _, imgbuf = cv2.imencode('.jpg', detector.last_full_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                            mqtt_client.publish(f"{MQTT_TOPIC}/camera", imgbuf.tobytes(), qos=0, retain=False)
                    except Exception as e:
                        print(f"Failed to publish camera image: {e}")
                time.sleep(REFRESH_RATE)
            except Exception as e:
                print(f"Detection error: {e}")
                time.sleep(max(REFRESH_RATE, 10))

    # Only start detection if we have references
    if detector.has_references():
        detection_thread = threading.Thread(target=detection_loop, daemon=True)
        detection_thread.start()
        print("Auto-detection enabled")
    else:
        print("No reference images - detection disabled until configured via WebUI")

    # Start WebUI (blocking)
    print("Starting WebUI on http://0.0.0.0:5000")
    run_webui(detector, host='0.0.0.0', port=5000)

if __name__ == '__main__':
    main()
