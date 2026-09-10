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
import soundfile as sf

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

from sagemic.helpers import check_path, get_config, get_device_id


# callback defined here to use with config variables
def audio_callback(
    indata,
    frames,
    time_obj,
    status,
    recording_buffer,
    config=None
):
    """Process one audio block from the input stream and print detections.

    Called by `sounddevice` for each incoming audio block. Flattens the
    audio into a 1-D array, updates the global `recording_buffer`, runs
    BirdNET analysis, and prints detections whose confidence exceeds
    `CONFIDENCE_THRESHOLD`.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        frames (int): Number of frames in `indata`.
        time_obj: Stream timing information provided by `sounddevice`
            (implementation-specific; not used here).
        status (sounddevice.CallbackFlags): Callback status flags; printed
            if any non-OK condition is reported.

    Side Effects:
        Updates the global `recording_buffer.buffer` and writes detection
        summaries to stdout.
    """
    if status:
        print(status)

    local_tz = ZoneInfo(config["SETTINGS"]["LOCAL_TZ"])
    base_path = config["PATHS"]["BASE_PATH"]
    confidence_threshold = config["SETTINGS"]["CONFIDENCE_THRESHOLD"]
    sample_rate = config["SETTINGS"]["SAMPLERATE"]

    timestamp = datetime.now(local_tz)
    date = timestamp.strftime('%Y-%m-%d')
    path = check_path(date, base_path)

    # Flatten the data to a 1D array as expected by birdnetlib
    audio_data = indata.flatten()

    # Add data to the buffer, specifying the samplerate here
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
                    f"{path}/{date_time}_{name}_{confidence:.2f}.flac"
                )
                temp_filename = final_filename + ".tmp"
                sf.write(temp_filename, indata, sample_rate, format='FLAC')
                os.rename(
                    temp_filename, final_filename
                )  # to .flac for scansend when done
    else:
        print("No detections")


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

    arg_callback = partial(
        audio_callback,
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
