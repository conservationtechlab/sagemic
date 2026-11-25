"""Run BirdNET on streaming input from a microphone.

Uses the `birdnetlib` package to analyze an acoustic data stream captured
by `sounddevice` directly from a microphone attached to this machine.
The stream is chunked into 3-second blocks to match BirdNET’s expected
window size and detections above a confidence threshold are saved as clips.

The audio callback only checks the schedule and hands each block to a
worker thread, which runs BirdNET and saves the clips. A slow analysis or
SD card write then never holds up the microphone stream; if the worker falls
behind, the oldest waiting blocks are dropped (counted as dropped_blocks).

SETTINGS: USB_OFF_OUTSIDE_ACTIVE_HOURS: true (default false) saves power outside
ACTIVE_HOURS: the microphone stream is closed and the Pi's USB ports - and the
HaLow dongle powered from them - are switched off (sagemic-helper usb-power,
uhubctl) until the next active period, then switched on again and the
microphone opened once it is back. Never with less than 2 h of active hours a
day; and not within 30 min of a settings change (config.yaml changed).
systemd switches USB on again whenever the detector stops (ExecStopPost).

Model: the standard BirdNET model, unless the hub sent another one
(sagemic_update.py). ~/sagemic_models/current names the model folder in use:
  - a species list (one .txt file, "Scientific name_Common name" per line as in
    BirdNET's labels): the standard model, reporting only those species. It
    replaces the location filter (LATITUDE / LONGITUDE are not used then).
  - a custom classifier trained with BirdNET-Analyzer (one .tflite file and its
    labels .txt file).
If the model cannot be loaded, the detector uses the standard model and reports
the problem (model_error in its heartbeat).
    python sagemic_local.py --config config/config.yaml --test-model <folder>
loads a model folder and analyzes 3 s of silence with it (used by the updater
before a new model is switched on).
"""

import os  # added for scansend service
import json
import math
import queue
import shutil  # free-space check
import subprocess
import sys
import threading
import traceback
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

from sagemic.helpers import get_config, get_device_id


# callback defined here to use with config variables
# Don't save clips when the card is nearly full (keeps the OS usable).
# Override with SETTINGS: MIN_FREE_MB in config.yaml.
DEFAULT_MIN_FREE_MB = 200

# Heartbeat for the station agent's status report. /dev/shm is in RAM, so
# writing it every 30 s causes no SD card wear.
STATE_PATH = os.environ.get("SAGEMIC_STATE", "/dev/shm/sagemic_detector.json")
STATE = {"started": time.time(), "blocks": 0, "analyzed": 0, "detections": 0,
         "skipped_interval": 0, "skipped_hours": 0, "overflows": 0,
         "rms_dbfs": None, "in_active_hours": True, "paused": False,
         "analyze_interval_s": 0, "active_hours": "00:00-24:00", "updated": 0,
         "errors": 0, "last_error": "", "dropped_blocks": 0,
         "usb_power": None, "usb_error": "",
         "model": None, "model_wanted": "", "model_error": ""}
# Models sent from the hub (sagemic_update.py); "current" names the one in use
MODELS_DIR = os.path.expanduser(os.environ.get("SAGEMIC_MODELS", "~/sagemic_models"))
HELPER = os.environ.get("SAGEMIC_HELPER", "/usr/local/sbin/sagemic-helper")
USB_OFF_FLAG = "/run/sagemic_usb_off"   # sagemic-helper: USB switched off by SageMic
USB_MIN_ACTIVE_MIN = 120      # less active time a day than this: USB stays on
USB_GRACE_S = 1800            # no USB off this long after config.yaml changed
_LAST_WRITE = [-1e9]          # monotonic: unaffected by clock changes
_LAST_CALLBACK = [time.monotonic()]
_LAST_SUMMARY = [time.monotonic(), 0, 0]   # time, analyzed, detections at last summary


def hourly_summary():
    """One log line per hour instead of one per 3 s block (less SD writing)."""
    mono = time.monotonic()
    if mono - _LAST_SUMMARY[0] < 3600:
        return
    analyzed = STATE["analyzed"] - _LAST_SUMMARY[1]
    found = STATE["detections"] - _LAST_SUMMARY[2]
    print(f"Last hour: {analyzed} blocks analyzed, {found} clip(s) saved, "
          f"mic {STATE['rms_dbfs']} dBFS, {STATE['errors']} error(s) total, "
          f"{'in' if STATE['in_active_hours'] else 'outside'} active hours")
    _LAST_SUMMARY[:] = [mono, STATE["analyzed"], STATE["detections"]]
_LAST_ANALYZED = [-1e9]
_WINDOWS = [(0, 1440)]
# blocks waiting for the worker: (audio, time recorded). 3 blocks = 9 s of audio.
BLOCKS = queue.Queue(maxsize=3)


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


def parse_active_hours(text):
    """'05:00-10:00,16:00-20:00' -> [(300, 600), (960, 1200)] in minutes."""
    windows = []
    for part in str(text or "00:00-24:00").replace(" ", "").split(","):
        start, end = part.split("-")
        to_min = lambda t: int(t[:2]) * 60 + int(t[3:])
        windows.append((to_min(start), to_min(end)))
    return windows


def active_minutes(windows):
    """Minutes per day inside the active hours (overlaps counted once)."""
    minutes = set()
    for start, end in windows:
        if start <= end:
            minutes.update(range(start, end))
        else:                                           # e.g. 20:00-06:00
            minutes.update(range(start, 1440))
            minutes.update(range(0, end))
    return len(minutes)


def is_true(value):
    return value is True or str(value).strip().lower() in ("true", "yes", "on")


def in_active_hours(windows, now):
    m = now.hour * 60 + now.minute
    for start, end in windows:
        if start <= end and start <= m < end:
            return True
        if start > end and (m >= start or m < end):   # e.g. 20:00-06:00
            return True
    return False


def helper(*args):
    """Run a sagemic-helper action (root, through its sudo rule): (ok, message)."""
    cmd = [HELPER, *[str(a) for a in args]]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)


def on_usb_storage(path):
    """True if path is on a USB drive (switching USB power off would cut it off)."""
    def out(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    try:
        src = out("findmnt", "-no", "SOURCE", "--target", path)
        disk = (out("lsblk", "-no", "PKNAME", src).split() or [os.path.basename(src)])[0]
        return out("lsblk", "-ndo", "TRAN", "/dev/" + disk) == "usb"
    except Exception:  # pylint: disable=broad-except
        return False


def usb_blocked(settings, windows):
    """Why USB must stay on although USB_OFF_OUTSIDE_ACTIVE_HOURS is true ("" = may go off)."""
    if active_minutes(windows) < USB_MIN_ACTIVE_MIN:
        return (f"ACTIVE_HOURS give less than {USB_MIN_ACTIVE_MIN // 60} h a day - USB stays on "
                "(too short a window to reach the station)")
    return ""


def recently_changed(path, now=None):
    """True if the file was modified less than USB_GRACE_S ago (or the time is unknown)."""
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(path)
    except OSError:
        return False
    return age < USB_GRACE_S


class UsbPower:
    """SETTINGS: USB_OFF_OUTSIDE_ACTIVE_HOURS - the Pi's USB ports off outside the
    active hours. SETTINGS: USB_POWER_LOCATION picks the hub(s) for uhubctl
    (empty = all the USB ports of a Pi 4 or Pi 5). Refused (warning, USB stays on) with
    under 2 h of active hours; held
    back for 30 min after config.yaml changed."""

    GRACE = f"settings changed less than {USB_GRACE_S // 60} min ago - USB stays on for now"

    def __init__(self, settings, base_path, config_path, windows):
        wanted = is_true(settings.get("USB_OFF_OUTSIDE_ACTIVE_HOURS"))
        self.blocked = usb_blocked(settings, windows) if wanted else ""
        self.enabled = wanted and not self.blocked
        self.location = str(settings.get("USB_POWER_LOCATION") or "").strip()
        self.base_path = base_path
        self.config_path = config_path
        self.is_off = os.path.exists(USB_OFF_FLAG)
        if wanted or self.is_off:
            STATE["usb_power"] = "off" if self.is_off else "on"
        if self.blocked:
            self._error(self.blocked)

    def _error(self, msg):
        if STATE.get("usb_error") != msg:
            print(f"!! USB power: {msg}")
        STATE["usb_error"] = msg

    def off(self):
        """Switch USB off. Returns True if it should be tried again later
        (held back after a settings change)."""
        if self.is_off or not self.enabled:
            return False
        if recently_changed(self.config_path):
            self._error(self.GRACE)
            return True
        if on_usb_storage(self.base_path):
            self._error("clips are stored on a USB drive (BASE_PATH) - USB stays on")
            return False
        ok, msg = helper("usb-power", "off", self.location)
        if not ok:
            self._error(msg or "switching off failed - USB stays on")
            return False
        self.is_off = True
        STATE["usb_power"], STATE["usb_error"] = "off", ""
        print("USB power off until the active hours start")
        return False

    def on(self):
        if not self.is_off:
            if STATE.get("usb_error") == self.GRACE:   # was never switched off
                STATE["usb_error"] = self.blocked
            return
        ok, msg = helper("usb-power", "on", self.location)
        if not ok:
            self._error(msg or "switching on failed")
            return
        self.is_off = False
        STATE["usb_power"], STATE["usb_error"] = "on", self.blocked
        print("USB power on")


def wait_for_device(name, sample_rate, timeout=120):
    """After USB power comes back: rescan the audio devices until the
    microphone is listed again, then look it up as at start-up."""
    end = time.monotonic() + timeout
    while True:
        try:   # PortAudio keeps the device list from start-up; rescan it
            sd._terminate()        # pylint: disable=protected-access
            sd._initialize()       # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            pass
        if any(d["name"].split(":")[0] == name for d in sd.query_devices()):
            break
        if time.monotonic() > end:
            print(f"!! {name} not back {timeout} s after USB power on")
            break
        write_state()
        time.sleep(3)
    return get_device_id(name, sample_rate)   # exits (systemd restarts) if still missing


def block_rms_dbfs(indata):
    """Audio level of a block in dBFS (about -150 = silence / dead mic)."""
    x = indata.reshape(-1).astype(np.float32, copy=False)
    if not x.size:
        return -150.0
    rms = math.sqrt(float(np.dot(x, x)) / x.size)
    if np.issubdtype(indata.dtype, np.integer):
        rms /= np.iinfo(indata.dtype).max
    return round(20 * math.log10(rms), 1) if rms > 0 else -150.0


def audio_callback(indata, frames, time_obj, status, tz=None, config=None):
    """Schedule checks, then hand the block to the worker (never blocks).

    Runs in the audio library's own thread for every block. It must return
    quickly and must never raise: sounddevice stops calling it for good after
    an exception, which would silently stop all detection.
    """
    _LAST_CALLBACK[0] = time.monotonic()
    try:
        STATE["blocks"] += 1
        if status is not None and getattr(status, "input_overflow", False):
            STATE["overflows"] += 1
        STATE["rms_dbfs"] = block_rms_dbfs(indata)
        now = datetime.now(tz)
        active = in_active_hours(_WINDOWS, now)
        STATE["in_active_hours"] = active
        if not active:
            STATE["skipped_hours"] += 1
            return
        interval = float(config["SETTINGS"].get("ANALYZE_INTERVAL_S", 0) or 0)
        mono = time.monotonic()
        if interval > 0 and mono - _LAST_ANALYZED[0] < interval - 0.5:
            STATE["skipped_interval"] += 1
            return
        _LAST_ANALYZED[0] = mono
        item = (indata.copy(), now)          # sounddevice reuses indata
        try:
            BLOCKS.put_nowait(item)
        except queue.Full:                   # worker behind: drop the oldest block
            try:
                BLOCKS.get_nowait()
                STATE["dropped_blocks"] += 1
            except queue.Empty:
                pass
            BLOCKS.put_nowait(item)
    except Exception as e:  # pylint: disable=broad-except
        STATE["errors"] += 1
        STATE["last_error"] = f"{type(e).__name__}: {e}"[:200]
        print("Error in the audio callback (still listening):")
        traceback.print_exc()


def worker(recording_buffer, config):
    """Analyze queued blocks and save clips (own thread; keeps going on errors)."""
    while True:
        audio, timestamp = BLOCKS.get()
        try:
            saved = _process_block(audio, timestamp, recording_buffer, config)
            STATE["analyzed"] += 1
            STATE["detections"] += saved or 0
        except Exception as e:  # pylint: disable=broad-except
            STATE["errors"] += 1
            STATE["last_error"] = f"{type(e).__name__}: {e}"[:200]
            print("Error while processing audio block (still listening):")
            traceback.print_exc()


def _process_block(indata, timestamp, recording_buffer, config):
    """Run BirdNET on one block and save a clip for each detection above
    CONFIDENCE_THRESHOLD. Returns the number of clips saved.

    Args:
        indata (numpy.ndarray): Audio block with shape (frames, channels).
            For this script, channels == 1.
        timestamp (datetime): when the block was recorded (local time zone);
            names the clip and its date folder.
        recording_buffer: birdnetlib RecordingBuffer (used by this thread only).
    """
    base_path = config["PATHS"]["BASE_PATH"]
    confidence_threshold = config["SETTINGS"]["CONFIDENCE_THRESHOLD"]
    sample_rate = config["SETTINGS"]["SAMPLERATE"]

    path = os.path.join(base_path, timestamp.strftime('%Y-%m-%d'))

    # Flatten the data to a 1D array as expected by birdnetlib
    audio_data = indata.flatten()

    # Add data to the buffer, specifying the samplerate here
    recording_buffer.buffer = audio_data


    # Analyze the buffer and get detections
    recording_buffer.analyze()
    detections = recording_buffer.detections
    saved = 0

    if detections:
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

                min_free_mb = config["SETTINGS"].get(
                    "MIN_FREE_MB", DEFAULT_MIN_FREE_MB)
                os.makedirs(path, exist_ok=True)   # only once there is a clip
                free_mb = shutil.disk_usage(path).free / 1e6
                if free_mb < min_free_mb:
                    print(f"!! Disk nearly full ({free_mb:.0f} MB free) - "
                          f"not saving {os.path.basename(final_filename)}")
                    continue

                try:
                    sf.write(temp_filename, indata, sample_rate,
                             format='FLAC')
                    os.rename(
                        temp_filename, final_filename
                    )  # to .flac for scansend when done
                    saved += 1
                except OSError as e:
                    print(f"!! Could not save {final_filename}: {e}")
                    try:
                        os.remove(temp_filename)
                    except OSError:
                        pass
    return saved


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
    settings = config["SETTINGS"]
    if args.test_model:
        test_model(config, args.test_model)
        return
    model = current_model()
    STATE["model_wanted"] = model or "standard"

    # Remote-controlled settings (see sagemic_agent.py)
    STATE["analyze_interval_s"] = settings.get("ANALYZE_INTERVAL_S", 0)
    STATE["active_hours"] = settings.get("ACTIVE_HOURS", "00:00-24:00")
    try:
        _WINDOWS[:] = parse_active_hours(STATE["active_hours"])
    except (ValueError, IndexError):
        print(f"!! Bad ACTIVE_HOURS '{STATE['active_hours']}' - listening all day")
        STATE["active_hours"] = "00:00-24:00"
        _WINDOWS[:] = [(0, 1440)]
    tz = ZoneInfo(settings["LOCAL_TZ"])

    def active_now():
        return in_active_hours(_WINDOWS, datetime.now(tz))

    usb = UsbPower(settings, config["PATHS"]["BASE_PATH"], args.config, _WINDOWS)
    if usb.is_off and (not usb.enabled or active_now()):
        usb.on()     # left off (setting just turned off, or a restart): power first

    if not settings.get("DETECTION_ENABLED", True):
        STATE["paused"] = True
        print("Detection is PAUSED (SETTINGS.DETECTION_ENABLED: false). "
              "Resume it from the hub (Node-RED). Microphone not opened.")
        try:
            while True:
                if usb.enabled:          # the USB schedule still applies
                    usb.on() if active_now() else usb.off()
                write_state(force=True)
                time.sleep(60)
        except KeyboardInterrupt:
            return

    def rest_until_active():
        """USB_OFF_OUTSIDE_ACTIVE_HOURS: USB off (the stream is closed) until the next
        active period; the heartbeat keeps going, so the agent sees a live detector."""
        STATE["in_active_hours"] = False
        retry = usb.off()
        while not active_now():
            if retry:            # held back after a settings change: try again
                retry = usb.off()
            STATE["skipped_hours"] += 1
            write_state()
            hourly_summary()
            time.sleep(20)
        usb.on()
        STATE["in_active_hours"] = True

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

    if usb.enabled and not active_now():
        rest_until_active()      # a USB microphone is unpowered until then

    # Find the mic first: if it is missing, fail fast instead of loading
    # the BirdNET model on every systemd restart.
    print("Scanning for audio devices")
    device_id = (wait_for_device(audio_device, sample_rate) if usb.enabled
                 else get_device_id(audio_device, sample_rate))

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

    recording_buffer = RecordingBuffer(
        analyzer=analyzer,
        lat=latitude if use_location else None,
        lon=longitude if use_location else None,
        rate=sample_rate, buffer=audio_buffer,
        # birdnetlib drops everything under 0.25 by default, which would
        # override a lower CONFIDENCE_THRESHOLD
        min_conf=float(config["SETTINGS"]["CONFIDENCE_THRESHOLD"])
    )

    arg_callback = partial(
        audio_callback,
        tz=tz,
        config=config
    )
    analysis = threading.Thread(target=worker, args=(recording_buffer, config),
                                name="birdnet", daemon=True)
    analysis.start()

    print(f"Active hours: {STATE['active_hours']}; "
          f"bird check interval: {STATE['analyze_interval_s']} s (0 = every block)"
          + ("; USB power off outside active hours" if usb.enabled else ""))
    while True:
        # start listener
        sd.default.device = device_id

        write_state(force=True)
        print("\nListening for birds...")
        print(f"Input device: {sd.query_devices(sd.default.device)['name']}")
        print("Press Ctrl+C to stop.")

        _LAST_CALLBACK[0] = time.monotonic()   # model loading time doesn't count
        try:
            with sd.InputStream(
                callback=arg_callback,
                samplerate=sample_rate,
                channels=1,
                blocksize=block_size,
                dtype=audio_dtype
            ) as stream:
                while True:
                    time.sleep(5)
                    write_state()
                    hourly_summary()
                    if usb.enabled and not active_now():
                        break        # close the stream before the USB power goes off
                    # If the audio stream dies (e.g. USB reset after a brownout)
                    # or the analysis thread is gone, exit so systemd restarts
                    # the detector.
                    stalled = time.monotonic() - _LAST_CALLBACK[0]
                    if not stream.active or stalled > 60 or not analysis.is_alive():
                        print(f"!! Audio stream stopped (active={stream.active}, "
                              f"no audio for {stalled:.0f} s, analysis "
                              f"{'running' if analysis.is_alive() else 'stopped'}) - exiting for restart")
                        write_state(force=True)
                        sys.exit(1)
        except KeyboardInterrupt:
            print("\nReceived keyboard interrupt. Quitting.")
            return
        print("Active hours over - microphone closed")
        rest_until_active()
        device_id = wait_for_device(audio_device, sample_rate)


if __name__ == "__main__":
    main()
