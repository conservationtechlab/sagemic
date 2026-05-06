"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are printed.

"""
from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess
import os
import numpy as np
import time
import selectors
from scipy.io.wavfile import write

from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

from sagemic.helpers import check_path


LATITUDE = 32.7157
LONGITUDE = -117.1611
SAMPLERATE = 48000
CONFIDENCE_THRESHOLD = 0.1

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Directory to store detected clips by date.
BASE_PATH = '<path to store detections>'

# The BirdNET model expects clips of at least 3 seconds for analysis.
BLOCK_DURATION = 3
BLOCKSIZE = BLOCK_DURATION * SAMPLERATE

audio_buffer = np.zeros(BLOCKSIZE, dtype='float32')

# Your stream url.
STREAM_URL = "rtsp://<pi ip>:8554/stream"

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

    timestamp = datetime.now(LOCAL_TZ)
    date = timestamp.strftime('%Y-%m-%d')
    path = check_path(date, BASE_PATH)
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
                name = detection['scientific_name'].replace(" ", "_").lower()
                confidence = detection['confidence']
                time = timestamp.strftime('%H-%M-%S')
                print(f"** {name} Detected w/ (Confidence: {confidence:.2f})")
                write(
                      f"{path}/{time}_{name}_{confidence:.2f}.wav",
                      48000,
                      indata
                     )
    else:
        print("No detections")


def _start_ffmpeg_stream(url: str) -> subprocess.Popen:
    """Start ffmpeg stream.

    Start ffmpeg reading from a URL and writing raw float32
    mono PCM at SAMPLERATE to stdout.

    Args:
        url (string): URL of the audio stream from the pi.

    Returns:
        subprocess.Popen: Raw stream output.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-analyzeduration", "0",
        "-probesize", "32",
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


def _read_exactly(pipe, nbytes: int, stall_timeout_s: float = 1.0) -> bytes:
    """Read bytes.

    Read exactly nbytes from a pipe unless EOF occurs.

    Args:
        pipe (subprocess.Popen): Raw output of ffmpeg stream.
        nbytes (int): Number of bytes needed from stream for
                      a single 3s recording.

    Returns:
        bytes: Bytes grabbed from stream.
    """
    sel = selectors.DefaultSelector()
    sel.register(pipe, selectors.EVENT_READ)

    chunks = []
    got = 0
    while got < nbytes:
        events = sel.select(timeout=stall_timeout_s)
        if not events:
            # stalled: nothing became readable within timeout
            break
        part = pipe.read(nbytes - got)
        if not part:
            break
        chunks.append(part)
        got += len(part)

    try:
        sel.unregister(pipe)
    except Exception:
        pass

    return b"".join(chunks)


def main():
    """Grab stream in chunked intervals for inference.

    Open the RTSP audio stream and run inference on rolling 3-second blocks.
    """
    bytes_per_sample = 4  # float32
    block_bytes = BLOCKSIZE * bytes_per_sample
    print(f"block_bytes expected per grab: {block_bytes}")
    while True:
        try:
            print(f"Connecting to stream: {STREAM_URL}")
            proc = _start_ffmpeg_stream(STREAM_URL)

            while True:
                start_time = time.perf_counter()
                raw = _read_exactly(proc.stdout, block_bytes, stall_timeout_s=1.1)
                end_time = time.perf_counter()
                print(f"elapsed time for read exactly: {end_time - start_time}")
                grab_s = end_time - start_time
                # If we stalled/lagged, skip inference and reconnect
                if len(raw) != block_bytes or grab_s > 3.7:
                    print("[stream] lag/stall -> restarting ffmpeg (skipping inference on this block)")
                    proc.kill()
                    proc.wait(timeout=1)
                    time.sleep(0.5)
                    break

                # Convert bytes -> numpy float32 vector
                block = np.frombuffer(raw, dtype=np.float32)

                # Feed into your existing inference path
                start_time = time.perf_counter()
                audio_callback(block,
                               _frames=BLOCKSIZE,
                               _time_obj=None,
                               status=None)
                end_time = time.perf_counter()
                print(f"elapsed time for inference: {end_time - start_time}")

        except KeyboardInterrupt:
            print("\nStopping.")
            break


if __name__ == '__main__':
    main()
