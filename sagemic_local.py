"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are printed.

"""

import os  # added for scansend service
import time
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import partial  # for config argument in callback

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

from sagemic.helpers import check_path, get_config, get_device_id


def run_inference(indata, recording_buffer, config):
    """Process one audio block from the input stream and print detections.

    Called by `audio_callback()` for each audio block. Flattens the
    audio into a 1-D array, updates the global `recording_buffer`, runs
    BirdNET analysis, prints detections whose confidence exceeds
    `CONFIDENCE_THRESHOLD`, and saves audio files locally.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        recording_buffer (RecordingBuffer instance):
            Holds raw audio data/coordinates and handles analysis pipeline.
        config (dict): Holds custom user configuration values for the script.

    Returns:
        (bool): True if there is a detection, False otherwise

    Side Effects:
        Updates the global `recording_buffer.buffer` and writes detection
        summaries to stdout.

        Saves audio clips with inferences into specified directory, by date.

    """
    confidence_threshold = config["SETTINGS"]["CONFIDENCE_THRESHOLD"]
    sample_rate = config["SETTINGS"]["SAMPLERATE"]

    local_tz = ZoneInfo(config["SETTINGS"]["LOCAL_TZ"])
    timestamp = datetime.now(local_tz)

    base_path = config["PATHS"]["BASE_PATH"]
    date = timestamp.strftime('%Y-%m-%d')
    path = check_path(date, base_path)

    audio_data = indata.flatten()

    recording_buffer.buffer = audio_data

    print(f"\nProcessing audio chunk at {timestamp.strftime('%H:%M:%S')}...")

    # Analyze the buffer and get detections
    recording_buffer.analyze()
    detections = recording_buffer.detections

    if detections:
        print("At least one detection.")
        for detection in detections:
            if detection["confidence"] > confidence_threshold:
                name = detection["scientific_name"].replace(" ", "_").lower()
                confidence = detection["confidence"]

                # new for filenames w/ data + time
                date_time = timestamp.strftime("%Y-%m-%d_%H-%M-%S")

                print(f"** {name} Detected w/ (Confidence: {confidence:.2f})")

                # added to track complete files
                final_filename = (
                    f"{path}/{date_time}_{name}_{confidence:.2f}.wav"
                )
                temp_filename = final_filename + ".tmp"
                write(temp_filename, sample_rate, indata)
                os.rename(
                    temp_filename, final_filename
                )  # to .wav for scansend when done
        return True

    print("No detections")
    return False


def audio_callback_raw(
    indata,
    frames,
    time_obj,
    status,
    recording_buffer,
    config=None
):

    """Audio callback for continuous inference.

    Called by 'sounddevice' for each incoming audio block.
    Calls run_inference to perform birdcall inference.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        frames (int): Number of frames in `indata`.
        time_obj: Stream timing information provided by `sounddevice`
            (implementation-specific; not used here).
        status (sounddevice.CallbackFlags): Callback status flags; printed
            if any non-OK condition is reported.
        recording_buffer (RecordingBuffer instance):
            Holds audio data, configs, and coordinates.
            Handles analysis pipeline.
        config (dict): Holds custom user configuration values for the script.

    """
    if status:
        print(status)

    run_inference(indata, recording_buffer, config)


def update_ambientrms(old_ambient, curr_rms, config, rms_dict):
    """
    Updates ambient noise floor based on given current RMS

    Called by audio_callback_rms.

    Args:
        old_ambient (float): current ambient noise floor
        curr_rms (float): rms of current audio block being processed
    """
    alpha = config["SETTINGS"]["EMA_ALPHA"]
    rms_dict["ambient_rms"] = (alpha * curr_rms) + ((1 - alpha) * old_ambient)


def audio_callback_rms(
    indata,
    frames,
    time_obj,
    status,
    recording_buffer,
    config=None,
    rms_dict=None
):

    """Audio callback for AC RMS Pre-filtering

    Called by 'sounddevice' for each incoming audio block.

    If RMS of indata is above trigger threshold, process prev & curr chunks.
    Then if there is no detection, use indata to update ambient.

    If RMS of indata is below trigger thresh, use indata to update ambient.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        frames (int): Number of frames in `indata`.
        time_obj: Stream timing information provided by `sounddevice`
            (implementation-specific; not used here).
        status (sounddevice.CallbackFlags): Callback status flags; printed
            if any non-OK condition is reported.
        recording_buffer (RecordingBuffer instance):
            Holds raw audio data, configs, and coordinates.
            Handles analysis pipeline.
        config (dict): Holds custom user configuration values for the script.
        rms_dict (dict): Stores data and valus needed for RMS filtering.

    Side Effects:
        Updates global rms_dict values.
    """

    if status:
        print(status)

    current_rms = np.std(indata)

    # initialize for first run
    if rms_dict["ambient_rms"] == 0.0:
        rms_dict["ambient_rms"] = current_rms
        rms_dict["prev_block"] = indata.copy()
        return

    ambient_multiplier = config["SETTINGS"]["THRESH_MULTIPLIER"]
    curr_ambientrms = rms_dict["ambient_rms"]
    prev_processed = rms_dict["prev_processed"]

    trigger_threshold = curr_ambientrms * ambient_multiplier

    if current_rms > trigger_threshold:

        if not prev_processed:
            print("Processing pre-trigger recording")
            run_inference(rms_dict["prev_block"], recording_buffer, config)

        # if no detection, use to update ambient floor
        if not run_inference(indata, recording_buffer, config):
            update_ambientrms(curr_ambientrms, current_rms, config, rms_dict)

        rms_dict["prev_processed"] = True

    else:
        print(f"RMS: {current_rms:.5f}, Trig Thresh: {trigger_threshold:.5f}")
        update_ambientrms(curr_ambientrms, current_rms, config, rms_dict)
        rms_dict["prev_processed"] = False

    rms_dict["prev_block"] = indata.copy()


def main():
    """ Parses config filepath and initalizes audio variables.

    Lists available audio devices, opens a mono input stream at the configured
    sample rate and block size, and keeps the main thread alive while the
    `audio_callback` performs analysis on each block. Press Ctrl+C to stop.
    """

    # parse given config filepath
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config file")
    args = parser.parse_args()

    config = get_config(args.config)

    # audio variables
    latitude = config["SETTINGS"]["LATITUDE"]
    longitude = config["SETTINGS"]["LONGITUDE"]
    sample_rate = config["SETTINGS"]["SAMPLERATE"]
    audio_device = config["SETTINGS"]["AUDIO_DEVICE"]
    audio_dtype = config["SETTINGS"]["AUDIO_DTYPE"]

    # The BirdNET model expects clips of at least 3 seconds for analysis
    block_duration = config["SETTINGS"]["BLOCK_DURATION"]
    block_size = block_duration * sample_rate

    audio_buffer = np.zeros(block_size, dtype="float32")

    analyzer = Analyzer()

    recording_buffer = RecordingBuffer(
        analyzer=analyzer, lat=latitude, lon=longitude,
        rate=sample_rate, buffer=audio_buffer
    )

    if config["SETTINGS"]["RMS_FILTER"] == 1:

        rms_dict = {
            "ambient_rms": 0.0,
            "prev_block": None,
            "prev_processed": False
        }

        arg_callback = partial(
            audio_callback_rms,
            recording_buffer=recording_buffer,
            config=config,
            rms_dict=rms_dict
        )
    else:
        arg_callback = partial(
            audio_callback_raw,
            recording_buffer=recording_buffer,
            config=config
        )

    # start listener
    print("Scanning for audio devices")

    sd.default.device = get_device_id(audio_device, sample_rate)

    print("\nListening for birds...")
    print(f"Input device: {sd.query_devices(sd.default.device)['name']}")
    print("Press Ctrl+C to stop.")

    try:
        with sd.InputStream(
            callback=arg_callback,
            samplerate=sample_rate,
            channels=1,
            blocksize=block_size,
            dtype=audio_dtype
        ):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Quitting.")


if __name__ == "__main__":
    main()
