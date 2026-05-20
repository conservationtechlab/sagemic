"""
Service to check new BirdNet Detection Files and send them via MQTTS
"""

# libraries
import os
import ssl
import paho.mqtt.client as mqtt

BASE_PATH = "/path"  # ADD HERE! Same as sagemic_local
# log file to track clips that have alr been sent (tracker)
LOG_FILE = os.path.join(BASE_PATH, "sent_clips.log")

PORT = 8883
BROKER = "132.249.238.9"
TOPIC = "test/demo"
MESSAGE = "Hello from Sagemic!"
PATH_TO_CA_PEM = "/path"  # ADD HERE!
SESSION_ID = "client id"  # ADD HERE!
USER = "user"  # ADD HERE!
PASS = "pass"  # ADD HERE!


# function to get log file of sent files
def get_log_file():
    """Reads the log file (sent filepaths) and puts in a set """
    if not os.path.exists(LOG_FILE):
        return set()  # nothing sent
    with open(LOG_FILE, "r", encoding='utf-8') as f:
        return set(line.strip() for line in f)  # return sent file paths as set


# function to write sent filepaths to log file
def write_log_file(filepath):
    """Logs a successfully sent file with its filepath"""
    with open(LOG_FILE, "a", encoding='utf-8') as f:  # append to bottom
        f.write(f"{filepath}\n")


# script only runs every few minutes (loop)
def main():
    """Main execution loop for scanning unsent files and sending them"""
    sent_files = get_log_file()
    files_to_send = []

    # check for newly stored clips in sagemic_local folder
    for root, _, files in os.walk(
        BASE_PATH
    ):  # go through each wav to see what hasn't been sent
        for file in files:
            if file.endswith(".wav"):
                filepath = os.path.join(root, file)
                if filepath not in sent_files:  # haven't sent this!
                    files_to_send.append(filepath)

    if not files_to_send:
        print("No new clips we need to send")
        return

    # ensure only sending completed clip, (done in sagemic_local.py)

    client = mqtt.Client(client_id=SESSION_ID)

    client.username_pw_set(USER, PASS)

    # use certificate.pem to authenticate msg with port 8883
    client.tls_set(
        ca_certs=PATH_TO_CA_PEM,
        certfile=None,
        keyfile=None,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )

    client.connect(BROKER, PORT)

    client.loop_start()

    # send new clips to broker over mqtt
    for filepath in files_to_send:
        # add print statements here if needed later
        try:
            with open(filepath, "rb") as wav_file:  # open in raw binary mode
                wav_data = wav_file.read()
                result = client.publish(TOPIC, bytearray(wav_data), qos=1)
                result.wait_for_publish()

                write_log_file(filepath)
        except Exception as e:  # pylint: disable=broad-except
            print(f"Failed to send {filepath}: {e}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
