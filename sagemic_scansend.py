
"""
Service to check new BirdNet Detection Files and send them via MQTTS.
"""

# libraries
import os
import argparse
import ssl
import time  # for sending delays
import sqlite3
import paho.mqtt.client as mqtt

from sagemic.helpers import get_config, create_database


# script only runs every few minutes (loop)
def main():
    """
    Parses config filepath argument and initalizes variables.

    Main execution loop for scanning unsent files and sending them.
    """

    # parse given config filepath
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config file")
    args = parser.parse_args()

    config = get_config(args.config)

    base_path = config["PATHS"]["BASE_PATH"]

    port = config["MQTT"]["PORT"]
    broker = config["MQTT"]["BROKER"]
    topic = config["MQTT"]["BASE_TOPIC"] + "/" + config["MQTT"]["DEVICE"]
    path_to_ca_pem = config["PATHS"]["PATH_TO_CA_PEM"]
    session_id = config["MQTT"]["SESSION_ID"]
    user = config["MQTT"]["USER"]
    password = config["MQTT"]["PASS"]

    db_path = os.path.join(base_path, "detections.db")

    # get files to send
    create_database(base_path)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath FROM detections WHERE sent = 0 ORDER BY timestamp ASC")
        files_to_send = [row[0] for row in cursor.fetchall()]

    if not files_to_send:
        print("No new clips we need to send")
        return

    # ensure only sending completed clip, (done in sagemic_local.py)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=session_id)

    client.username_pw_set(user, password)

    # use certificate.pem to authenticate msg with port 8883
    client.tls_set(
        ca_certs=path_to_ca_pem,
        certfile=None,
        keyfile=None,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )

    client.connect(broker, port)

    client.loop_start()

    # send new clips to broker over mqtt
    for filepath in files_to_send:
        if not os.path.exists(filepath):
            print(f"Warning: {filepath} found in DB but missing on disk.")
            continue

        # get .wav filename from filepath
        filename = os.path.basename(filepath)

        # make dynamic topic
        dynamic_topic = f"{topic}/{filename}"

        try:
            with open(filepath, "rb") as wav_file:  # open in raw binary mode
                wav_data = wav_file.read()
                result = client.publish(
                    dynamic_topic,
                    bytearray(wav_data),
                    qos=1
                )
                result.wait_for_publish()

                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "UPDATE detections SET sent = 1 WHERE filepath = ?",
                        (filepath,)
                    )

                print(f"Successfully sent and logged: {filename}")
                time.sleep(0.5)  # to prevent network flood

        except Exception as e:  # pylint: disable=broad-except
            print(f"Failed to send {filepath}: {e}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
