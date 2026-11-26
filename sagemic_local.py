"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are printed.

With a SageMic hub (MQTT: BROKER: hub) the hub can also send a model
(station/sagemic_update.py). ~/sagemic_models/current names the model folder in use:
  - a species list (one .txt file, "Scientific name_Common name" per line as in
    BirdNET's labels): the standard model, reporting only those species. It
    replaces the location filter (LATITUDE / LONGITUDE are not used then).
  - a custom classifier trained with BirdNET-Analyzer (one .tflite file and its
    labels .txt file).
If the model cannot be loaded, the detector uses the standard model and reports
the problem (model_error in /dev/shm/sagemic_detector.json, which the updater
reads to check that a new model or a software update started).
    python sagemic_local.py --config config/config.yaml --test-model <folder>
loads a model folder and analyzes 3 s of silence with it (used by the updater
before a new model is switched on).
"""

import os  # added for scansend service
import json
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

# ------------------------------------------------ SageMic hub: models
# Models sent from the hub (sagemic_update.py); "current" names the one in use
MODELS_DIR = os.path.expanduser(os.environ.get("SAGEMIC_MODELS", "~/sagemic_models"))
# Heartbeat for the updater. /dev/shm is in RAM, so writing it every 30 s
# causes no SD card wear.
STATE_PATH = os.environ.get("SAGEMIC_STATE", "/dev/shm/sagemic_detector.json")
STATE = {"started": time.time(), "updated": 0,
         "model": None, "model_wanted": "", "model_error": ""}
_LAST_WRITE = [-1e9]          # monotonic: unaffected by clock changes


def is_hub(broker):
    """True for BROKER: hub (the SageMic hub), the default when BROKER is missing."""
    return str(broker if broker is not None else "hub").strip().lower() == "hub"


def write_state(force=False):
    """Write the heartbeat file at most every 30 s (atomic)."""
    mono = time.monotonic()
    if not force and mono - _LAST_WRITE[0] < 30:
        return
    STATE["updated"] = time.time()
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
        os.replace(tmp, STATE_PATH)
        _LAST_WRITE[0] = mono
    except OSError:
        pass


def read_lines(path):
    """Non-empty lines of a text file, without a byte-order mark (Notepad,
    PowerShell), Windows line endings or spaces at either end."""
    with open(path, "r", encoding="utf-8-sig") as f:
        return [line.strip() for line in f if line.strip()]


def model_files(folder):
    """The model in a folder -> (kind, files).
    kind "classifier": files["model"] (.tflite) and files["labels"] (.txt);
    kind "species": files["species"] (.txt species list)."""
    names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    tflite = [n for n in names if n.lower().endswith(".tflite")]
    txt = [n for n in names if n.lower().endswith(".txt")]
    if tflite:
        if len(tflite) != 1 or len(txt) != 1:
            raise ValueError("a custom classifier is one .tflite file and one labels .txt file "
                             f"(found {len(tflite)} .tflite and {len(txt)} .txt)")
        return "classifier", {"model": os.path.join(folder, tflite[0]),
                              "labels": os.path.join(folder, txt[0])}
    if len(txt) != 1:
        raise ValueError(f"a species list is one .txt file (found {len(txt)})")
    return "species", {"species": os.path.join(folder, txt[0])}


def current_model():
    """Folder name of the model in use (~/sagemic_models/current), "" = standard BirdNET."""
    try:
        with open(os.path.join(MODELS_DIR, "current"), "r", encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return ""
    return "" if name in ("", "standard") else name


def make_analyzer(folder):
    """BirdNET analyzer for a model folder ("" = the standard model).
    Returns (analyzer, use_location, description). birdnetlib does not allow a
    species list together with the location filter, and a custom classifier
    has its own species, so LATITUDE / LONGITUDE are used only by the standard model."""
    if not folder:
        return Analyzer(), True, "standard BirdNET"
    kind, files = model_files(folder)
    if kind == "classifier":
        labels = read_lines(files["labels"])
        if not labels:
            raise ValueError(f"{os.path.basename(files['labels'])} is empty")
        analyzer = Analyzer(classifier_model_path=files["model"],
                            classifier_labels_path=files["labels"])
        return analyzer, False, f"custom classifier, {len(labels)} labels"
    species = read_lines(files["species"])
    if not species:
        raise ValueError(f"{os.path.basename(files['species'])} is empty")
    # the cleaned lines, not the file: birdnetlib reads the file as it is, so a
    # byte-order mark or Windows line ending would make a name never match
    analyzer = Analyzer(custom_species_list=species)
    # a name BirdNET does not know would silently never match: refuse it
    known = set(getattr(analyzer, "labels", None) or [])
    unknown = [s for s in species if known and s not in known]
    if unknown:
        raise ValueError(f"{len(unknown)} of {len(species)} lines are not BirdNET species names, e.g. "
                         f"\"{unknown[0]}\" - each line must be Scientific name_Common name, "
                         "exactly as in BirdNET's labels (e.g. Turdus migratorius_American Robin)")
    return analyzer, False, f"species list, {len(species)} species"


def test_model(config, folder):
    """--test-model: load a model folder and analyze 3 s of silence with it."""
    rate = int(config["SETTINGS"]["SAMPLERATE"])
    analyzer, use_location, what = make_analyzer(folder)
    buf = RecordingBuffer(analyzer=analyzer, buffer=np.zeros(3 * rate, dtype="float32"), rate=rate,
                          lat=config["SETTINGS"]["LATITUDE"] if use_location else None,
                          lon=config["SETTINGS"]["LONGITUDE"] if use_location else None,
                          min_conf=0.5)
    buf.analyze()
    print(f"model OK: {what}")


def load_analyzer():
    """SageMic hub: the analyzer for the model in use, or the standard model
    if it cannot be loaded. Returns (analyzer, use_location)."""
    model = current_model()
    STATE["model_wanted"] = model or "standard"
    try:
        analyzer, use_location, what = make_analyzer(os.path.join(MODELS_DIR, model) if model else "")
        STATE["model"] = model or "standard"
    except Exception as e:  # pylint: disable=broad-except
        print(f"!! Model {model} could not be loaded ({e}) - using the standard BirdNET model")
        STATE["model_error"] = f"{model}: {e}"[:300]
        analyzer, use_location, what = make_analyzer("")
        STATE["model"] = "standard"
    print(f"Model: {model or 'standard'} ({what})")
    write_state(force=True)
    return analyzer, use_location



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
    parser.add_argument("--test-model", metavar="FOLDER",
                        help="load this model folder, analyze 3 s of silence and exit")
    args = parser.parse_args()

    config = get_config(args.config)
    if args.test_model:
        test_model(config, args.test_model)
        return
    # MQTT: BROKER: hub - SageMic Station with a SageMic hub (models from the hub)
    hub = is_hub((config.get("MQTT") or {}).get("BROKER", "hub"))

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

    if hub:
        analyzer, use_location = load_analyzer()
    else:
        analyzer, use_location = Analyzer(), True

    recording_buffer = RecordingBuffer(
        analyzer=analyzer,
        lat=latitude if use_location else None,
        lon=longitude if use_location else None,
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
                if hub:
                    write_state()   # heartbeat for the updater
    except KeyboardInterrupt:
        print("\nReceived keyboard interrupt. Quitting.")


if __name__ == "__main__":
    main()
