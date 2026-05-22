"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are printed.

"""

import os  # added for scansend service
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

LATITUDE = 32.7157
LONGITUDE = -117.1611
SAMPLERATE = 48000
CONFIDENCE_THRESHOLD = 0.1

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Directory to store detected clips by date.
BASE_PATH = "/path"  # ADD HERE!

# The BirdNET model expects clips of at least 3 seconds for analysis
BLOCK_DURATION = 3
BLOCKSIZE = BLOCK_DURATION * SAMPLERATE

audio_buffer = np.zeros(BLOCKSIZE, dtype="float32")

analyzer = Analyzer()

recording_buffer = RecordingBuffer(
    analyzer=analyzer, lat=LATITUDE, lon=LONGITUDE,
    rate=SAMPLERATE, buffer=audio_buffer
)


def audio_callback(indata, frames, time_obj, status):
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

    timestamp = datetime.now(LOCAL_TZ)

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
            if detection["confidence"] > CONFIDENCE_THRESHOLD:
                name = detection["scientific_name"].replace(" ", "_").lower()
                confidence = detection["confidence"]

                # new for filenames w/ data + time
+               date_time = timestamp.strftime("%Y-%m-%d_%H-%M-%S")

                print(f"** {name} Detected w/ (Confidence: {confidence:.2f})")

                # added to track complete files
                final_filename = f"{BASE_PATH}/{date_time}_{name}_{confidence:.2f}.wav"
                temp_filename = final_filename + ".tmp"
                write(temp_filename, 48000, indata)
                os.rename(
                    temp_filename, final_filename
                )  # to .wav for scansend when done
    else:
        print("No detections")


def main():
    """Open the audio input stream and run continuous BirdNET analysis.

    Lists available audio devices, opens a mono input stream at the configured
    sample rate and block size, and keeps the main thread alive while the
    `audio_callback` performs analysis on each block. Press Ctrl+C to stop.
    """
    devices = sd.query_devices()
    print("Available audio devices:")
    for i, dev in enumerate(devices):
        print(f"  {i}: {dev['name']}")

    sd.default.device = 1
    print("\nListening for birds...")
    print(f"Input device: {sd.query_devices(sd.default.device)['name']}")
    print("Press Ctrl+C to stop.")

    try:
        with sd.InputStream(
            callback=audio_callback,
            samplerate=SAMPLERATE,
            channels=1,
            blocksize=BLOCKSIZE,
        ):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Quitting.")


if __name__ == "__main__":
    main()
