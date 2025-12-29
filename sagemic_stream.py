"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are printed.

"""
import time
from datetime import datetime
import subprocess
import sys
import numpy as np
from scipy.io.wavfile import write

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

LATITUDE = 32.7157
LONGITUDE = -117.1611
SAMPLERATE = 48000
CONFIDENCE_THRESHOLD = 0.1

# The BirdNET model expects clips of at least 3 seconds for analysis
BLOCK_DURATION = 3
BLOCKSIZE = BLOCK_DURATION * SAMPLERATE

audio_buffer = np.zeros(BLOCKSIZE, dtype='float32')

STREAM_URL = "http://10.24.21.165:8000/mystream"

analyzer = Analyzer()

recording_buffer = RecordingBuffer(
    analyzer=analyzer,
    lat=LATITUDE,
    lon=LONGITUDE,
    rate=SAMPLERATE,
    buffer=audio_buffer
)


def audio_callback(indata, _frames, _time_obj, status):
    """Process one audio block from the input stream and print detections.

    Called by `sounddevice` for each incoming audio block. Flattens the
    audio into a 1-D array, updates the global `recording_buffer`, runs
    BirdNET analysis, and prints detections whose confidence exceeds
    `CONFIDENCE_THRESHOLD`.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        _frames (int): Number of frames in `indata`.
        _time_obj: Stream timing information provided by `sounddevice`
            (implementation-specific; not used here).
        status (sounddevice.CallbackFlags): Callback status flags; printed
            if any non-OK condition is reported.

    Side Effects:
        Updates the global `recording_buffer.buffer` and writes detection
        summaries to stdout.
    """
    if status:
        print(status)

    timestamp = datetime.now()

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
            if detection['confidence'] > CONFIDENCE_THRESHOLD:
                name = detection['common_name']
                confidence = detection['confidence']
                print(f"** {name} Detected w/ (Confidence: {confidence:.2f})")
                write(f"{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}_{name}_{confidence:.2f}.wav", 48000, indata)
    else:
        print("No detections")



def _start_ffmpeg_stream(url: str) -> subprocess.Popen:
    """
    Start ffmpeg reading from a URL and writing raw float32 mono PCM at SAMPLERATE to stdout.
    """
    # -reconnect* flags help for some HTTP sources; harmless if unsupported for your input type.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLERATE),
        "-f", "f32le",
        "pipe:1",
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # keep for debugging
        bufsize=0,
    )


def _read_exactly(pipe, nbytes: int) -> bytes:
    """
    Read exactly nbytes from a pipe unless EOF occurs.
    """
    chunks = []
    got = 0
    while got < nbytes:
        part = pipe.read(nbytes - got)
        if not part:
            break
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)


def main():
    """
    Open the HTTP audio stream and run inference on rolling 3-second blocks.
    """
    bytes_per_sample = 4  # float32
    block_bytes = BLOCKSIZE * bytes_per_sample

    while True:
        try:
            print(f"Connecting to stream: {STREAM_URL}")
            proc = _start_ffmpeg_stream(STREAM_URL)

            while True:
                raw = _read_exactly(proc.stdout, block_bytes)

                # Convert bytes -> numpy float32 vector
                block = np.frombuffer(raw, dtype=np.float32)

                # Feed into your existing inference path
                audio_callback(block, _frames=BLOCKSIZE, _time_obj=None, status=None)

        except KeyboardInterrupt:
            print("\nStopping.")
            break

if __name__ == '__main__':
    main()
