"""Tools needed by both Birbler and SageMic.
"""
import os
import sounddevice as sd
import yaml


def check_path(date, base_path):
    """Create new folder for date to store detections.

    Args:
        date (str): Current date in YYYY-MM-DD.

    Returns:
        str: The path for where the detections will be stored that day.
    """
    new_path = os.path.join(base_path, date)
    print(new_path)
    if not os.path.exists(new_path):
        os.makedirs(new_path)
        print(f"Directory created: {os.path.abspath(new_path)}")

    return new_path

def get_device_id():
    """Checks which hardware is there before running

        Returns:
            returns the device id of either audiomoth or inmp441
    """

    devices = sd.query_devices()

    for i, dev in enumerate(devices):
        if 'audiomoth' in dev['name'].lower():
            print(f"Using Audiomoth")
            return i

    for i, dev in enumerate(devices):
        if "googlevoicehat" in dev['name'].lower():
            print(f"Using INMP441")
            return i
    print(f"Neither audiomoth nor INMP441 found. Using sysdefault (2)")
    return 2

def get_config(config_file):
    """Reads config file, return as python dict.

    Args:
        config_file(str): Filepath of the config file.

    Returns:
        dict: Dictionary of filepaths & custom MQTT info.
    """

    # check if file exists first
    if not os.path.exists(config_file):
        raise FileNotFoundError(
            "Config file missing!"
        )

    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
