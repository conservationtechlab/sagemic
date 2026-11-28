"""SageMic station agent: tells the SageMic hub the station is online and
records the clips the hub confirmed.

Runs on each Pi (systemd service sagemic_agent). Talks to the SageMic hub
through the hub's MQTT broker (Mosquitto); MQTT: BROKER must be "hub":

    <BASE_TOPIC>/_ctl/<DEVICE>/online   station -> hub   "online" / "offline" (retained)
    <BASE_TOPIC>/_ctl/<DEVICE>/describe station -> hub   what the control page shows for this
                                        station (retained): the "describe" part of
                                        station/manifest.json - app name, status
                                        columns, settings yes/no, actions, data
                                        packages (models)
    <BASE_TOPIC>/_ctl/<DEVICE>/status   station -> hub   short status JSON (retained):
                                        name, IP address, detector running
    <BASE_TOPIC>/_ctl/<DEVICE>/ack      hub -> station   {"file"}  clip saved on the hub
                                        (logged to delivered_clips.log, so
                                        sagemic_scansend.py does not send it again)
    <BASE_TOPIC>/_ctl/<DEVICE>/cmd      hub -> station   answered on .../reply with
                                        "not available": this version has no
                                        remote settings or commands

Software and model updates are handled by sagemic_update.py, which is kept
separate from this agent.

Usage:
    python sagemic_agent.py --config config/config.yaml
"""

import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import yaml

try:
    import fcntl
except ImportError:   # not on Windows (tests only)
    fcntl = None

VERSION = "2026.11.1-ota"
STATUS_INTERVAL_S = 300
MANIFEST = os.path.join(os.path.dirname(os.path.realpath(__file__)), "manifest.json")


def description():
    """The "describe" part of station/manifest.json (None if there is none)."""
    try:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            d = json.load(f).get("describe")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(d, dict):
        return None
    return dict(d, agent_version=VERSION)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def log_lock(base_path):
    """Exclusive lock shared with sagemic_scansend.py for sent_clips.log and
    delivered_clips.log."""
    f = open(os.path.join(base_path, ".clip_logs.lock"), "a", encoding="utf-8")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f   # closing it releases the lock


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    except Exception:  # pylint: disable=broad-except
        return ""


class Agent:
    def __init__(self, config_path):
        cfg = load_config(config_path)
        mq = cfg.get("MQTT") or {}
        if not is_hub(mq.get("BROKER", "hub")):
            raise SystemExit("MQTT: BROKER must be \"hub\" - the agent works with a SageMic hub only.")
        self.device = mq.get("DEVICE", socket.gethostname())
        base = mq.get("BASE_TOPIC", "sagemic")
        ctl = f"{base}/_ctl/{self.device}"
        self.t_status = f"{ctl}/status"
        self.t_online = f"{ctl}/online"
        self.t_describe = f"{ctl}/describe"
        self.t_cmd = f"{ctl}/cmd"
        self.t_reply = f"{ctl}/reply"
        self.t_ack = f"{ctl}/ack"
        self.base_path = (cfg.get("PATHS") or {}).get("BASE_PATH", "")
        self.ack_lock = threading.Lock()
        self.wake = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"sagemic-agent-{self.device}", clean_session=True)
        self.client.will_set(self.t_online, "offline", qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(1, 30)
        if mq.get("USERNAME"):                       # broker login (the hub's needs one)
            self.client.username_pw_set(str(mq["USERNAME"]), str(mq.get("PASSWORD", "")))
        self.broker = broker_address("hub")
        self.port = int(mq.get("PORT") or 1883)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        print(f"agent connected to {self.broker}:{self.port} ({reason_code})", flush=True)
        if reason_code.is_failure:
            return                     # e.g. wrong login - paho retries
        client.subscribe([(self.t_cmd, 1), (self.t_ack, 1)])
        client.publish(self.t_online, "online", qos=1, retain=True)
        d = description()
        if d:
            client.publish(self.t_describe, json.dumps(d), qos=1, retain=True)
        self.wake.set()

    def on_message(self, client, userdata, msg):
        try:
            if msg.topic == self.t_ack:
                self.record_delivery(msg.payload)
            elif msg.topic == self.t_cmd:
                req = json.loads(msg.payload)
                req = req if isinstance(req, dict) else {}
                payload = {"id": req.get("id"), "device": self.device, "cmd": req.get("cmd"),
                           "ok": False, "data": None, "time": int(time.time()),
                           "message": "not available on this station (software and model updates only)"}
                client.publish(self.t_reply, json.dumps(payload), qos=1)
        except Exception as e:  # pylint: disable=broad-except
            print(f"message error: {e}", flush=True)

    def record_delivery(self, payload):
        """The hub confirmed it saved a clip -> delivered_clips.log."""
        data = json.loads(payload)
        name = os.path.basename(str(data.get("file", "")))
        if not re.match(r"^\d{4}-\d{2}-\d{2}_.+\.flac$", name):
            return
        path = os.path.join(self.base_path, name[:10], name)
        with self.ack_lock, log_lock(self.base_path), \
                open(os.path.join(self.base_path, "delivered_clips.log"), "a", encoding="utf-8") as f:
            f.write(path + "\n")

    def publish_status(self):
        st = {"device": self.device, "hostname": socket.gethostname(), "agent_version": VERSION,
              "time": int(time.time()), "time_local": datetime.now().isoformat(timespec="seconds"),
              "ip": run(["hostname", "-I"]).split(), "mode": "hub",
              "services": {"sagemic_local": run(["systemctl", "is-active", "sagemic_local"]) or "unknown"}}
        self.client.publish(self.t_status, json.dumps(st), qos=1, retain=True)

    def run(self):
        self.client.connect_async(self.broker, self.port)
        self.client.loop_start()
        while True:
            connected = self.client.is_connected()
            if connected:
                try:
                    self.publish_status()
                except Exception as e:  # pylint: disable=broad-except
                    print(f"status failed: {e}", flush=True)
            else:                      # the hub's address may only be known later
                addr = broker_address("hub")
                if addr != self.broker:
                    print(f"broker address now {addr} (was {self.broker})", flush=True)
                    self.broker = addr
                    self.client.connect_async(self.broker, self.port)
            self.wake.wait(timeout=STATUS_INTERVAL_S if connected else 30)
            self.wake.clear()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    a = p.parse_args()
    Agent(a.config).run()


if __name__ == "__main__":
    main()
