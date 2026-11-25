"""
Service to check new BirdNet Detection Files and send them over MQTT, as
<BASE_TOPIC>/<DEVICE>/<clip>.flac.

To the SageMic hub over HaLow (MQTT: BROKER: hub - required):
    Plain MQTT on port 1883 with the station login. Sends only while the hub's
    receiver is online (it announces itself on <BASE_TOPIC>/_ctl/receiver/<id>),
    so the broker never has to queue clips for an absent receiver. Clips sent
    but not confirmed by the receiver within TIMERS.RESEND_AFTER_S are sent
    again (the receiver skips duplicates). sent_clips.log keeps one clip path
    per line (with the time it was sent).

Settings (optional):
    MQTT: USERNAME / PASSWORD    the hub's station login
    CLEANUP: REQUIRE_DELIVERY    wait for the hub and resend unconfirmed clips
                                 (default true)

--dry-run loads the config, finds the clips to send and sets up the MQTT
client, then stops before connecting: nothing is sent or logged.
Exit code 0 if it got that far (used to check a software update before it is kept).
"""

# libraries
import os
import argparse
import re
import subprocess
import time  # for sending delays
import paho.mqtt.client as mqtt
import yaml

try:
    import fcntl
except ImportError:   # not on Windows (tests only)
    fcntl = None


def get_config(path):
    """config.yaml as a dict. Read directly: sagemic.helpers would also load
    the audio libraries, on every run of this timer."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def log_lock(base_path):
    """Exclusive lock shared by everything that writes sent_clips.log or
    delivered_clips.log (sender, agent, cleanup), so cleanup can rewrite a
    log without losing a line appended at the same moment."""
    f = open(os.path.join(base_path, ".clip_logs.lock"), "a", encoding="utf-8")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f   # closing it releases the lock


def broker_address(name):
    """MQTT: BROKER as an address. "hub" means the SageMic hub: the DHCP server
    that gave eth0 its address (falls back to sagemic-hub.local before eth0
    has an address)."""
    try:
        out = subprocess.run(["nmcli", "-g", "DHCP4.OPTION", "device", "show", "eth0"],
                             capture_output=True, text=True, timeout=10, check=False).stdout
    except Exception:  # pylint: disable=broad-except
        out = ""
    for part in re.split(r"[|\n]", out):
        key, _, value = part.partition("=")
        if key.strip() == "dhcp_server_identifier" and value.strip():
            return value.strip()
    return "sagemic-hub.local"


def is_hub(broker):
    """True for BROKER: hub (the SageMic hub), the default when BROKER is missing."""
    return str(broker if broker is not None else "hub").strip().lower() == "hub"


def require_delivery(config):
    """Wait for the hub and resend unconfirmed clips? CLEANUP: REQUIRE_DELIVERY
    (on unless set to false)."""
    return (config.get("CLEANUP") or {}).get("REQUIRE_DELIVERY") is not False


# function to write sent filepaths to log file
def write_log_file(filepath, log_file, with_time=True):
    """Logs a successfully sent file with its filepath.


    Args:
        filepath (str): Path to the .flac file written by sagemic_local.
        - Also means the file has been sent by this script.

        log_file (str): Path to the log file, defined in the config file.
        with_time (bool): add <TAB>epoch (needed to resend unconfirmed clips).
            Without it the line is just the path, as the GitHub version writes.
    """

    with log_lock(os.path.dirname(log_file)), \
            open(log_file, "a", encoding='utf-8') as f:  # append to bottom
        f.write(f"{filepath}\t{int(time.time())}\n" if with_time else f"{filepath}\n")


def read_log_times(path):
    """{clip path: last time logged} from a log ("path" or "path<TAB>epoch")."""
    times = {}
    if not os.path.exists(path):
        return times
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            clip, _, stamp = line.partition("\t")
            try:
                times[clip.strip()] = float(stamp) if stamp else 0.0
            except ValueError:
                times[clip.strip()] = 0.0
    return times


def read_log(path):
    """Set of clip paths listed in a log file."""
    return set(read_log_times(path))


def search_unsent(base_path, log_file, delivered_file=None, resend_undelivered=False,
                  resend_after=None):
    """ Find every clip not yet sent (or, with resend_undelivered, every clip
    sent but never confirmed by the hub).

    Every clip is checked against the log. The original version resumed after
    the last logged clip, which permanently skipped clips whose names sort
    earlier - e.g. the repeated hour when daylight saving time ends, or after
    the Pi's clock is set back.

    Args:
        base_path (str): Path to the base directory to audio files.
        log_file (str): sent_clips.log
        delivered_file (str): delivered_clips.log (receiver confirmations)
        resend_undelivered (bool): select sent-but-unconfirmed clips instead
        resend_after (float): also include clips sent this many seconds ago or
            more that the hub has still not confirmed

    Returns:
        list: file paths to send, oldest first.
    """
    sent_times = read_log_times(log_file)
    sent_files = set(sent_times)
    delivered = read_log(delivered_file) if delivered_file else set()
    now = time.time()
    files_to_send = []
    try:
        folders = sorted(f for f in os.listdir(base_path)
                         if os.path.isdir(os.path.join(base_path, f)))
    except FileNotFoundError:
        folders = []
    for folder in folders:
        folder_path = os.path.join(base_path, folder)
        for file in sorted(os.listdir(folder_path)):
            if not file.endswith(".flac"):
                continue
            filepath = os.path.join(folder_path, file)
            if resend_undelivered:
                if filepath in sent_files and filepath not in delivered:
                    files_to_send.append(filepath)
            elif filepath not in sent_files:
                files_to_send.append(filepath)
            elif (resend_after is not None and filepath not in delivered
                  and now - sent_times[filepath] >= resend_after):
                files_to_send.append(filepath)   # sent, never confirmed
    files_to_send.sort()  # so we send chronologically
    return files_to_send


def receiver_online(client, base_topic, wait=2.0):
    """True if the hub's receiver has announced itself on this broker.

    Receivers publish a retained "1" on <base>/_ctl/receiver/<id> while
    connected; the broker replaces it with "0" (their last will) when they go.
    """
    found = []
    def on_message(c, userdata, msg):
        if msg.payload == b"1":
            found.append(msg.topic)
    client.on_message = on_message
    client.subscribe(f"{base_topic}/_ctl/receiver/+", qos=1)
    end = time.time() + wait
    while time.time() < end and not found:
        time.sleep(0.1)
    client.unsubscribe(f"{base_topic}/_ctl/receiver/+")
    return bool(found)


# script only runs every few minutes (loop)
def main():
    """
    Parses config filepath argument and initalizes variables.

    Main execution loop for scanning unsent files and sending them.
    """

    # parse given config filepath
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config file")
    parser.add_argument("--resend-undelivered", action="store_true",
                        help="resend clips sent earlier but never confirmed by the hub")
    parser.add_argument("--dry-run", action="store_true",
                        help="load config and find clips, but do not connect or send")
    args = parser.parse_args()

    config = get_config(args.config)

    base_path = config["PATHS"]["BASE_PATH"]

    # log file to track clips that have alr been sent (tracker)
    log_file = os.path.join(base_path, "sent_clips.log")

    if not is_hub(config["MQTT"].get("BROKER", "hub")):
        raise SystemExit("MQTT: BROKER must be \"hub\" - the SageMic Station works with a SageMic hub "
                         "only. To send clips straight to the cloud broker, use the original "
                         "SageMic (option A).")
    broker = broker_address("hub")
    base_topic = config["MQTT"]["BASE_TOPIC"]
    topic = f"{base_topic}/{config['MQTT']['DEVICE']}"
    # With delivery confirmation on (the default), send only to an online
    # receiver and resend clips it has not confirmed after RESEND_AFTER_S.
    confirm = require_delivery(config)
    resend_after = float((config.get("TIMERS") or {}).get("RESEND_AFTER_S", 600))

    files_to_send = search_unsent(
        base_path, log_file, os.path.join(base_path, "delivered_clips.log"),
        resend_undelivered=args.resend_undelivered,
        resend_after=resend_after if confirm else None)

    if not files_to_send and not args.dry_run:
        return   # nothing to do (no log line every minute: less SD writing)

    # ensure only sending completed clip, (done in sagemic_local.py)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config["MQTT"].get("USERNAME"):          # broker login (the hub's needs one)
        client.username_pw_set(str(config["MQTT"]["USERNAME"]), str(config["MQTT"].get("PASSWORD", "")))
    port = int(config["MQTT"].get("PORT") or 1883)

    if args.dry_run:
        print(f"[dry run] {len(files_to_send)} clip(s) to send to {broker}:{port} "
              f"(topic {topic}/...) - not connecting, nothing sent")
        return

    try:
        client.connect(broker, port)
    except OSError as e:
        # broker not reachable right now (HaLow or uplink down); unsent clips
        # stay unlogged and are retried on the next timer run
        print(f"Broker {broker}:{port} unreachable ({e}); will retry later")
        return

    client.loop_start()
    for _ in range(100):               # connect() returns before the broker answers
        if client.is_connected():
            break
        time.sleep(0.1)
    if not client.is_connected():
        print(f"Broker {broker}:{port} did not accept the connection "
              "(wrong MQTT login?); will retry later")
        client.loop_stop()
        return

    if confirm and not receiver_online(client, base_topic):
        print(f"Hub receiver not online - keeping {len(files_to_send)} clip(s) "
              "on the Pi until it connects")
        client.loop_stop()
        client.disconnect()
        return

    # send new clips to broker over mqtt
    for filepath in files_to_send:
        # add print statements here if needed later

        # get .flac filename from filepath
        filename = os.path.basename(filepath)

        # make dynamic topic
        dynamic_topic = f"{topic}/{filename}"

        try:
            with open(filepath, "rb") as flac_file:  # open in raw binary mode
                flac_data = flac_file.read()
                result = client.publish(
                    dynamic_topic,
                    bytearray(flac_data),
                    qos=1
                )
                result.wait_for_publish(timeout=60)
                if not result.is_published():
                    raise RuntimeError("publish not acknowledged by broker")

                write_log_file(filepath, log_file, with_time=confirm)
                print(f"Sent {filepath}")
                time.sleep(0.5)  # to prevent network flood

        except Exception as e:  # pylint: disable=broad-except
            # Stop here and keep the order: the next run starts again
            # from this clip.
            print(f"Failed to send {filepath}: {e} - will retry next run")
            break

    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
