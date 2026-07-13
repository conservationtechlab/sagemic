"""Tools needed by both Birbler and SageMic.
"""
import os
import sys
import sounddevice as sd
import yaml
import sqlite3
import platform
from importlib.metadata import version
import numpy as np


def insert_database(base_path, filepath, sample_rate, bitrate):
    """
    Inserts detection entries into the database.
    Called by sagemic_local.

    Args:
        base_path (str): path to file  storing directory
        filepath (str): path to detection audio file
        sample_rate (integer): user defined in config file
        bitrate (real): audio bitrate calculated in main

    """

    try:
        with open("/sys/firmware/devicetree/base/model", "r") as f:
            hardware = f.read().strip('\x00')
    except FileNotFoundError:
        hardware = "Unknown"

    firmware = platform.release().split('+')[0]

    model = f"birdnetlib_v{version('birdnetlib')}"

    db_path = os.path.join(base_path, "detections.db")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO detections "
            "(sent, filepath, hardware, firmware, model, sample_rate, bitrate)"
            "VALUES (0, ?, ?, ?, ?, ?, ?)",
            (filepath, hardware, firmware, model, sample_rate, bitrate)
        )


def create_database(base_path):
    """
    Checks if database exists, if not then creates detections database.

    Called by sagemic_local and sagemic_scansend

    Args:
        base_path (str): path to file storing directory
    """
    db_path = os.path.join(base_path, "detections.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS detections (
                sent INTEGER DEFAULT 0,
                filepath TEXT PRIMARY KEY,
                hardware TEXT,
                firmware TEXT,
                model TEXT,
                sample_rate INTEGER,
                bitrate INTEGER
            )
        ''')


def check_path(date, base_path):
    """Create new folder for date to store detections.

    Called by sagemic_local.py.

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


def inmp441_check(device_id, samplerate):
    """ Checks if inmp441 is connected and listening

    Called by get_device_id

    Args:
        device_id (int): id of inmp441 found from get_device_id()
        samplerate (int): samplerate of audio device from config

    """

    print("Checking INMP441")
    recording = sd.rec(
        int(0.5 * samplerate),
        samplerate=samplerate,
        channels=1,
        dtype='int32',
        device=device_id
    )
    sd.wait()

    peak_to_peak = np.ptp(recording)

    if peak_to_peak == 0:
        sys.exit("Flatline detected. INMP441 not connected properly!")

    print(f"INMP441 is connected. Peak-to-peak: {peak_to_peak})")


def get_device_id(pref_name, samplerate):
    """Checks which hardware is there before running

        Args:
            pref_device: string, name of audio device in config file
            samplerate: int, samplerate from config file
            - used for inmp441 error checking

        Returns:
            int: i
            - returns id for audio device
    """

    devices = sd.query_devices()

    device_id = -1

    # search for id number of device
    for i, dev in enumerate(devices):
        curr_device = dev['name'].split(':', 1)[0]
        # search exact match
        if pref_name == curr_device:
            device_id = i
            break

    # error checking for none inmp441
    if "googlevoicehat" not in pref_name:
        if device_id == -1:
            sys.exit(f"{pref_name} not found! Check connections")
    else:  # error checking for inmp441
        if device_id == -1:
            sys.exit("Check INMP441 device tree overlay! Not found")
        else:
            inmp441_check(device_id, samplerate)

    print(f"Using {pref_name}")
    return device_id


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
