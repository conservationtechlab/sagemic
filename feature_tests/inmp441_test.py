""" Record a 3 second audio through INMP441

    Before running this script, add the following to config.txt:
    dtoverlay=googlevoicehat-soundcard
    dtparam=i2s=on
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

BASE_PATH = "/home/ellinaho/test_files"
SAMPLERATE = 48000 # inmp441 & birdnet specific

BLOCK_DURATION = 3

def main():
    """ Records audio, does dynamic file naming, writes to machine 
    """
    devices = sd.query_devices()
    print("Available audio devices:")
    for i, dev in enumerate(devices):
        print(f" {i}: {dev['name']}")

    sd.default.device = 1

    print(f"\n--- Starting INMP441 Test ---")
    print(f"Input Device = {sd.query_devices(sd.default.device)['name']}")

    # Dynamic file naming

    timestamp = datetime.now(LOCAL_TZ)
    date_time = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    final_filename = f"{BASE_PATH}/inmp411_{date_time}.wav"
    temp_filename = final_filename + ".tmp"

    print(f"Recording for 3 seconds!")

    recording = sd.rec(
        frames=int(BLOCK_DURATION * SAMPLERATE),
        samplerate = SAMPLERATE,
        channels=2,
        dtype='int32' # all inmp441 specific
    )

    sd.wait()
    print("Recording done! Stop streaming...")

    recording_mono = recording[:,0]

    print(f"Saving to {final_filename}...")
    write(temp_filename, SAMPLERATE, recording)
    os.rename(temp_filename, final_filename)

    print("Done saving!")

if __name__ == "__main__":
    main()
