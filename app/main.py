#!/usr/bin/env python3
import os
import sys
import time
import threading
import json
import numpy as np
import paho.mqtt.client as mqtt
from detector import DeadboltDetector, compute_published_state
import cv2
from webui import run_webui


def env_int(name, default):
    """Read an int from the environment, tolerating unset/empty/garbage values."""
    try:
        return int(os.getenv(name, ''))
    except (TypeError, ValueError):
        return default


# Configuration from environment
CAMERA_URL = os.getenv('CAMERA_URL')
MQTT_HOST = os.getenv('MQTT_HOST', 'mqtt')
MQTT_PORT = env_int('MQTT_PORT', 1883)
MQTT_USER = os.getenv('MQTT_USER', '')
MQTT_PASS = os.getenv('MQTT_PASS', '')
MQTT_TOPIC = os.getenv('MQTT_TOPIC', 'home/deadbolt')
REFRESH_RATE = max(1, env_int('REFRESH_RATE', 5))
MQTT_DISCOVERY_PREFIX = os.getenv('MQTT_DISCOVERY_PREFIX', 'homeassistant')
MQTT_DEVICE_NAME = os.getenv('MQTT_DEVICE_NAME', 'Deadbolt Detector')
MQTT_DEVICE_ID = os.getenv('MQTT_DEVICE_ID', '')


def create_placeholder_image(width=640, height=480):
    img = np.full((height, width, 3), (64, 64, 64), dtype=np.uint8)
    text = "Camera Offline"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 1.0
    thickness = 2
    text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]
    text_x = (width - text_size[0]) // 2
    text_y = (height + text_size[1]) // 2
    cv2.putText(img, text, (text_x, text_y), font, font_scale, (255, 255, 255), thickness)
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 50])
    return buf.tobytes()


def publish_discovery(client, topic, payload, label):
    """Publish one retained Home Assistant discovery message."""
    try:
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
    except Exception as e:
        print(f"Failed to publish {label} discovery: {e}")


def build_discovery_messages(dev_id):
    """Build the (topic, payload, label) Home Assistant discovery messages."""
    base_state_topic = f"{MQTT_TOPIC}/state"
    availability_topic = f"{MQTT_TOPIC}/availability"

    device = {
        "identifiers": [dev_id],
        "name": MQTT_DEVICE_NAME,
        "model": "deadbolt-detector",
        "manufacturer": "deadbolt-detector"
    }

    common = {
        "availability_topic": availability_topic,
        "device": device,
    }

    messages = [
        # Sensor for lock state (text) — replaces the previous binary_sensor
        (
            f"{MQTT_DISCOVERY_PREFIX}/sensor/{dev_id}_state/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Lock State",
                "state_topic": base_state_topic,
                "value_template": "{{ value_json.state }}",
                "unique_id": f"{dev_id}_state",
                "json_attributes_topic": base_state_topic,
                "icon": "mdi:lock",
                **common,
            },
            "lock state sensor",
        ),
        # Sensor for confidence
        (
            f"{MQTT_DISCOVERY_PREFIX}/sensor/{dev_id}_confidence/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Confidence",
                "state_topic": base_state_topic,
                "value_template": "{{ value_json.confidence }}",
                "unit_of_measurement": "%",
                "unique_id": f"{dev_id}_confidence",
                "json_attributes_topic": base_state_topic,
                **common,
            },
            "confidence sensor",
        ),
        # Camera discovery (MQTT camera expects binary JPEG payloads on the topic)
        (
            f"{MQTT_DISCOVERY_PREFIX}/camera/{dev_id}/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Camera",
                "topic": f"{MQTT_TOPIC}/camera",
                "unique_id": f"{dev_id}_camera",
                **common,
            },
            "camera",
        ),
        # Cropped camera discovery
        (
            f"{MQTT_DISCOVERY_PREFIX}/camera/{dev_id}_cropped/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Camera Cropped",
                "topic": f"{MQTT_TOPIC}/camera_cropped",
                "unique_id": f"{dev_id}_camera_cropped",
                **common,
            },
            "cropped camera",
        ),
        # Button to capture locked reference
        (
            f"{MQTT_DISCOVERY_PREFIX}/button/{dev_id}_capture_locked/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Capture Locked",
                "command_topic": f"{MQTT_TOPIC}/command/capture_locked",
                "unique_id": f"{dev_id}_capture_locked",
                "icon": "mdi:camera",
                **common,
            },
            "capture_locked button",
        ),
        # Button to capture unlocked reference
        (
            f"{MQTT_DISCOVERY_PREFIX}/button/{dev_id}_capture_unlocked/config",
            {
                "name": f"{MQTT_DEVICE_NAME} Capture Unlocked",
                "command_topic": f"{MQTT_TOPIC}/command/capture_unlocked",
                "unique_id": f"{dev_id}_capture_unlocked",
                "icon": "mdi:camera",
                **common,
            },
            "capture_unlocked button",
        ),
    ]
    return messages


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

    # Build the detector before the MQTT client: the MQTT callbacks below
    # reference it, and retained command messages can be delivered as soon as
    # the network loop starts.
    print("Initializing detector...")
    detector = DeadboltDetector(refresh_rate=REFRESH_RATE)

    availability_topic = f"{MQTT_TOPIC}/availability"

    # Availability is debounced: a camera that misses a frame or two (a Wi-Fi
    # hiccup, a camera reboot) should not flap the entities in Home Assistant.
    # The retained "offline" is only published once the camera has been
    # unreachable for `offline_grace_seconds`.
    availability = {"offline_since": None, "unavailable": False}

    # Setup MQTT client
    mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    # Last will: if this process dies or the network drops, the *broker* marks the
    # device unavailable. Doing it here instead of in on_disconnect matters:
    # a retained "offline" published while disconnected gets queued by paho and
    # flushed *after* the reconnect's "online", which left the device stuck
    # showing offline until the next real outage.
    mqtt_client.will_set(availability_topic, "offline", qos=1, retain=True)

    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        print(f"MQTT username: {MQTT_USER}")

    def publish_availability(online):
        mqtt_client.publish(availability_topic, "online" if online else "offline", qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0 or (hasattr(reason_code, 'is_failure') and not reason_code.is_failure):
            print(f"MQTT connected to {MQTT_HOST}")
            # Publish Home Assistant discovery config and availability
            dev_id = MQTT_DEVICE_ID if MQTT_DEVICE_ID else MQTT_TOPIC.replace('/', '_')
            try:
                messages = build_discovery_messages(dev_id)
                for topic, payload, label in messages:
                    publish_discovery(client, topic, payload, label)

                # Republish availability, reflecting an outage that is still in
                # progress rather than blindly claiming to be online.
                publish_availability(not availability["unavailable"])

                # Subscribe to command topics
                client.subscribe(f"{MQTT_TOPIC}/command/capture_locked", qos=1)
                client.subscribe(f"{MQTT_TOPIC}/command/capture_unlocked", qos=1)
            except Exception as e:
                print(f"Failed to publish Home Assistant discovery: {e}")
        else:
            print(f"MQTT connection failed: {reason_code}")

    def on_disconnect(client, userdata, disconnect_flags, rc, properties):
        # Availability is owned by the last will plus the grace period in the
        # detection loop; publishing "offline" from here would flap on a brief
        # reconnect (see the will_set comment above).
        print(f"MQTT disconnected (rc={rc}), will retry...")

    def on_message(client, userdata, msg):
        """Handle capture commands.

        Never let an exception escape: paho runs this on its network thread and
        an unhandled error there tears down the whole MQTT loop.
        """
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8') if msg.payload else ""
            print(f"MQTT message: {topic} = {payload}")

            for state, command in (("locked", "capture_locked"), ("unlocked", "capture_unlocked")):
                if topic != f"{MQTT_TOPIC}/command/{command}":
                    continue

                filepath = detector.capture_reference(state)
                if filepath:
                    client.publish(f"{MQTT_TOPIC}/command/result", f"Captured {state}: {filepath}", qos=1)
                    print(f"Captured {state} reference: {filepath}")
                else:
                    client.publish(f"{MQTT_TOPIC}/command/result", f"Failed to capture {state}", qos=1)
        except Exception as e:
            print(f"Error handling MQTT message: {e}")

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
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

    # Start detection loop in background thread
    def detection_loop():
        print(f"Detection loop started ({REFRESH_RATE}s interval)")
        while True:
            try:
                # Read each cycle so the WebUI override applies without a restart.
                grace = max(0, int(detector.settings['offline_grace_seconds']))
                state, confidence = detector.detect()

                if mqtt_client:
                    if not detector.camera_online:
                        if availability["offline_since"] is None:
                            availability["offline_since"] = time.monotonic()
                            print(f"Camera unreachable - waiting up to {grace}s before marking unavailable")

                        down_for = time.monotonic() - availability["offline_since"]
                        if not availability["unavailable"] and down_for >= grace:
                            print(f"Camera unreachable for {down_for:.0f}s - marking entities unavailable")
                            publish_availability(False)
                            try:
                                placeholder = create_placeholder_image()
                                mqtt_client.publish(f"{MQTT_TOPIC}/camera", placeholder, qos=0, retain=False)
                                mqtt_client.publish(f"{MQTT_TOPIC}/camera_cropped", placeholder, qos=0, retain=False)
                            except Exception as e:
                                print(f"Failed to publish placeholder image: {e}")
                            availability["unavailable"] = True

                        time.sleep(REFRESH_RATE)
                        continue

                    if availability["offline_since"] is not None:
                        down_for = time.monotonic() - availability["offline_since"]
                        availability["offline_since"] = None
                        if availability["unavailable"]:
                            print(f"Camera back after {down_for:.0f}s - marking entities available")
                            publish_availability(True)
                            availability["unavailable"] = False
                        else:
                            print(f"Camera recovered after {down_for:.0f}s - within grace, availability unchanged")

                if state and mqtt_client and state != "unconfigured":
                    # Compute published state honoring confidence threshold
                    publish_state = compute_published_state(
                        state, confidence, detector.settings['min_confidence']
                    )

                    # Convert to percentage for MQTT
                    confidence_pct = round(confidence * 100, 1)
                    payload_dict = {
                        'state': publish_state.title(),
                        'confidence': confidence_pct,
                        'confidence_pct': f"{confidence_pct}%",
                        'timestamp': time.time()
                    }

                    mqtt_client.publish(f"{MQTT_TOPIC}/state", json.dumps(payload_dict), qos=1, retain=True)
                    print(f"Detected: {state} -> published: {publish_state} (confidence: {confidence_pct}% )")
                    # Publish camera image (raw JPEG) to the camera topic so Home Assistant camera can display it
                    try:
                        if detector.last_full_frame is not None:
                            _, imgbuf = cv2.imencode('.jpg', detector.last_full_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                            mqtt_client.publish(f"{MQTT_TOPIC}/camera", imgbuf.tobytes(), qos=0, retain=False)
                    except Exception as e:
                        print(f"Failed to publish camera image: {e}")

                    # Publish cropped camera image
                    try:
                        if detector.last_cropped_frame is not None:
                            _, imgbuf = cv2.imencode('.jpg', detector.last_cropped_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                            mqtt_client.publish(f"{MQTT_TOPIC}/camera_cropped", imgbuf.tobytes(), qos=0, retain=False)
                    except Exception as e:
                        print(f"Failed to publish cropped camera image: {e}")
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
