"""
Service to check new BirdNet Detection Files and send them via MQTTS.
"""

# libraries
import os
import argparse
import ssl
import time  # for sending delays
import bisect
import paho.mqtt.client as mqtt

from sagemic.helpers import get_config


# function to write sent filepaths to log file
def write_log_file(filepath, log_file):
    """Logs a successfully sent file with its filepath.


    Args:
        filepath (str): Path to the .flac file written by sagemic_local.
        - Also means the file has been sent by this script.

        log_file (str): Path to the log file, defined in the config file.
    """

    with open(log_file, "a", encoding='utf-8') as f:  # append to bottom
        f.write(f"{filepath}\n")


def search_unsent(base_path, log_file):
    """ Searches parent directory and date folders for unsent files.

    Args:
        base_path (str): Path to the base directory to audio files.
        - Defined in config.

        log_file (str): Path to the log file, defined in the config file.

    Returns:
        dict: Returns an dictionary of filepaths of unsent files.
    """
    sent_files = set()
    files_to_send = []
    start_folder = ""
    start_file = ""

    # Read the log file to get entire set and latest date read
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if lines:
                sent_files = set(line.strip() for line in lines)
                last_line = lines[-1].strip()
                start_folder = os.path.basename(os.path.dirname(last_line))
                start_file = os.path.basename(last_line)

    # filter out folders only in basepath
    try:
        folders = []
        for f in os.listdir(base_path):
            # check if item is a folder
            # can be eliminated late for efficiency
            folder_path = os.path.join(base_path, f)
            if os.path.isdir(folder_path):
                folders.append(f)
        all_folders = sorted(folders)  # sorted for for loop later
    except FileNotFoundError:
        all_folders = []

    # trim all_folders to start at start_folder
    if not start_folder:
        search_folders = all_folders
    else:
        # Using this instead of for loop and compare O(N) so its O(logN)
        index = bisect.bisect_left(all_folders, start_folder)
        search_folders = all_folders[index:]

    # search in relevant folders
    for folder in search_folders:
        folder_path = os.path.join(base_path, folder)
        flacs = []

        for file in os.listdir(folder_path):
            if file.endswith(".flac"):
                flacs.append(file)
        flac_files = sorted(flacs)

        if folder == start_folder and start_file:
            index = bisect.bisect_right(flac_files, start_file)
            flac_files = flac_files[index:]

        for file in flac_files:
            filepath = os.path.join(folder_path, file)

            if filepath not in sent_files:
                files_to_send.append(filepath)

    files_to_send.sort()  # so we send chronologically

    return files_to_send


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

    # log file to track clips that have alr been sent (tracker)
    log_file = os.path.join(base_path, "sent_clips.log")

    port = config["MQTT"]["PORT"]
    broker = config["MQTT"]["BROKER"]
    topic = config["MQTT"]["BASE_TOPIC"] + "/" + config["MQTT"]["DEVICE"]
    path_to_ca_pem = config["PATHS"]["PATH_TO_CA_PEM"]
    path_to_crt = config["PATHS"]["PATH_TO_CRT"]
    path_to_key = config["PATHS"]["PATH_TO_KEY"]

    files_to_send = search_unsent(base_path, log_file)

    if not files_to_send:
        print("No new clips we need to send")
        return

    # ensure only sending completed clip, (done in sagemic_local.py)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    # use certificate.pem to authenticate msg with port 8883
    client.tls_set(
        ca_certs=path_to_ca_pem,
        certfile=path_to_crt,
        keyfile=path_to_key,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )

    client.connect(broker, port)

    client.loop_start()

    # send new clips to broker over mqtt
    for filepath in files_to_send:
        # add print statements here if needed later

        # get .flac filename from filepath
        filename = os.path.basename(filepath)

        # make dynamic topic
        dynamic_topic = f"{topic}/{filename}"

        try:
            with open(filepath, "rb") as flac_file:  # open in raw binary mode
                flac_data = flac_file.read()
                result = client.publish(
                    dynamic_topic,
                    bytearray(flac_data),
                    qos=1
                )
                result.wait_for_publish()

                write_log_file(filepath, log_file)
                print(f"Sent {filepath}")
                time.sleep(0.5)  # to prevent network flood

        except Exception as e:  # pylint: disable=broad-except
            print(f"Failed to send {filepath}: {e}")

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
