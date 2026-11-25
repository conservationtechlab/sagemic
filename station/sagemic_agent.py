"""SageMic station agent: status reports, remote settings and station control.

Runs on each Pi (systemd service sagemic_agent). Talks to the SageMic hub
through the hub's MQTT broker (Mosquitto); MQTT: BROKER must be "hub". Every
control topic carries the station name, so stations don't see each other's
commands and confirmations:

    <BASE_TOPIC>/_ctl/<DEVICE>/status   station -> hub   status JSON (retained)
    <BASE_TOPIC>/_ctl/<DEVICE>/online   station -> hub   "online" / "offline" (retained)
    <BASE_TOPIC>/_ctl/<DEVICE>/cmd      hub -> station   {"id", "cmd", "args"}
    <BASE_TOPIC>/_ctl/<DEVICE>/reply    station -> hub   {"id", "ok", "message", "data"}
    <BASE_TOPIC>/_ctl/<DEVICE>/ack      hub -> station   {"file"}  clip saved on the hub
                                        (logged to delivered_clips.log; cleanup deletes only those)

<BASE_TOPIC>/_ctl/receiver/+ is shared: the hub announces itself there.

Commands:  status | get_config | set {KEY: value, ...} | action {"name": ...}

Root-level actions (restart services, timer intervals, clock, reboot) go
through /usr/local/sbin/sagemic-helper, the only command the agent may run
with sudo.

Usage:
    python sagemic_agent.py --config config/config.yaml
    python sagemic_agent.py --config config/config.yaml --init-config
    python sagemic_agent.py --config config/config.yaml --set AGENT.STATUS_INTERVAL_S=120 ...
"""

import argparse
import collections
import copy
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import yaml

try:
    import fcntl
except ImportError:   # not on Windows (tests only)
    fcntl = None

VERSION = "2026.11.1"
HELPER = os.environ.get("SAGEMIC_HELPER", "/usr/local/sbin/sagemic-helper")
DETECTOR_STATE = os.environ.get("SAGEMIC_STATE", "/dev/shm/sagemic_detector.json")
ACTIVE_HOURS_RE = re.compile(r"^\d{2}:\d{2}-\d{2}:\d{2}(,\d{2}:\d{2}-\d{2}:\d{2})*$")

# ---------------------------------------------------------------- settings
# key, type, min, max, default, apply, label, description
# apply: detector = restart detector | agent = applied live |
#        next_run = used next time the job runs | timer:<name> = systemd timer
SCHEMA = [
    ("SETTINGS.CONFIDENCE_THRESHOLD", "float", 0.01, 0.99, 0.5, "detector",
     "Confidence threshold", "Minimum BirdNET confidence for a clip to be saved"),
    ("SETTINGS.ANALYZE_INTERVAL_S", "int", 0, 3600, 0, "detector",
     "Bird check interval (s)", "Seconds between analyzed 3 s blocks; 0 = every block (continuous)"),
    ("SETTINGS.ACTIVE_HOURS", "hours", None, None, "00:00-24:00", "detector",
     "Active hours", "When to listen, e.g. 05:00-10:00,16:00-20:00 (00:00-24:00 = always; at least 2 hours a day)"),
    ("SETTINGS.LATITUDE", "float", -90, 90, 32.7157, "detector",
     "Latitude", "Station latitude (BirdNET species filter)"),
    ("SETTINGS.LONGITUDE", "float", -180, 180, -117.1611, "detector",
     "Longitude", "Station longitude (BirdNET species filter)"),
    ("SETTINGS.LOCAL_TZ", "tz", None, None, "America/Los_Angeles", "detector",
     "Time zone", "Time zone for clip names and date folders"),
    ("SETTINGS.MIN_FREE_MB", "int", 50, 100000, 200, "detector",
     "Stop saving below (MB)", "Detector stops saving clips below this free space"),
    ("SETTINGS.USB_OFF_OUTSIDE_ACTIVE_HOURS", "bool", None, None, False, "detector",
     "USB power off outside active hours",
     "TRUE = USB (HaLow) powers off outside Active Hours\n"
     "FALSE = USB (HaLow) always on"),
    ("TIMERS.SEND_INTERVAL_S", "int", 15, 86400, 60, "timer:scansend",
     "Send interval (s)", "How often new clips are sent to the hub"),
    ("TIMERS.CLEANUP_INTERVAL_S", "int", 300, 604800, 3600, "timer:cleanup",
     "Cleanup interval (s)", "How often old sent clips are deleted"),
    ("TIMERS.RESEND_AFTER_S", "int", 120, 86400, 600, "next_run",
     "Resend unconfirmed after (s)", "Clips the hub has not confirmed this long after sending are sent again"),
    ("CLEANUP.KEEP_SENT_DAYS", "float", 0.04, 365, 7, "next_run",
     "Keep sent clips (days)", "Days the Pi keeps clips after sending them"),
    ("CLEANUP.MIN_FREE_GB", "float", 0.2, 500, 2, "next_run",
     "Minimum free space (GB)", "Cleanup deletes oldest sent clips below this"),
    ("AGENT.STATUS_INTERVAL_S", "int", 30, 86400, 300, "agent",
     "Status interval (s)", "How often this station sends its status"),
]
SCHEMA_BY_KEY = {s[0]: s for s in SCHEMA}
# settings that are always text (--set keeps them as typed)
TEXT_KEYS = {"MQTT.DEVICE", "MQTT.BROKER", "MQTT.BASE_TOPIC", "MQTT.USERNAME", "MQTT.PASSWORD",
             "UPDATE.KEY", "SETTINGS.USB_POWER_LOCATION",
             "PATHS.PATH_TO_CA_PEM", "PATHS.PATH_TO_CRT", "PATHS.PATH_TO_KEY"}
BOOL_WORDS = {"true": True, "on": True, "yes": True, "1": True,
              "false": False, "off": False, "no": False, "0": False}

ACTIONS = {
    "pause_detection": "Pause detection (stays paused after reboot)",
    "resume_detection": "Resume detection",
    "restart_detector": "Restart detector",
    "restart_sender": "Restart send timer",
    "send_now": "Send waiting clips now",
    "cleanup_now": "Run cleanup now",
    "set_clock": "Set Pi clock (args: epoch)",
    "resend_undelivered": "Resend clips the hub never confirmed",
    "reboot": "Reboot the Pi",
    "shutdown": "Shut down the Pi (needs a power cycle to restart)",
}


# ------------------------------------------------------------- config file
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_value(cfg, key, default=None):
    sec, name = key.split(".", 1)
    return (cfg.get(sec) or {}).get(name, default)


def fmt_yaml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value))   # double-quoted, valid YAML


def validate(key, raw):
    """Return (ok, value_or_error) for one setting."""
    if key == "SETTINGS.DETECTION_ENABLED":
        if isinstance(raw, bool):
            return True, raw
        return False, "must be true or false"
    if key not in SCHEMA_BY_KEY:
        return False, f"{key} is not a remotely editable setting"
    _, typ, lo, hi, _, _, _, _ = SCHEMA_BY_KEY[key]
    try:
        if typ == "int":
            v = int(float(raw))
            if float(raw) != v:
                return False, "must be a whole number"
        elif typ == "float":
            v = float(raw)
        elif typ == "bool":            # the hub's settings page sends text
            if isinstance(raw, bool):
                v = raw
            elif str(raw).strip().lower() in BOOL_WORDS:
                v = BOOL_WORDS[str(raw).strip().lower()]
            else:
                return False, "must be true or false"
        elif typ == "tz":
            v = str(raw).strip()
            ZoneInfo(v)
        elif typ == "hours":
            v = str(raw).replace(" ", "")
            if not ACTIVE_HOURS_RE.match(v):
                return False, "use HH:MM-HH:MM, comma-separated"
            for part in v.split(","):
                if part.split("-")[0] == part.split("-")[1]:
                    return False, f"{part}: start and end are the same (never active)"
                for t in part.split("-"):
                    h, m = int(t[:2]), int(t[3:])
                    if h > 24 or m > 59 or (h == 24 and m):
                        return False, f"bad time {t}"
            listening = set()           # minutes of the day inside the active hours
            for part in v.split(","):
                a, b = (int(t[:2]) * 60 + int(t[3:]) for t in part.split("-"))
                listening.update(range(a, b) if a < b else list(range(a, 1440)) + list(range(0, b)))
            if len(listening) < 120:
                mins = len(listening) % 60
                return False, (f"{len(listening) // 60} h{f' {mins} minutes' if mins else ''} a day entered - "
                               "minimum of 2 hours a day required")
        else:
            return False, "unknown type"
    except Exception as e:  # pylint: disable=broad-except
        return False, f"invalid value ({e})"
    if lo is not None and not (lo <= v <= hi):
        return False, f"must be between {lo} and {hi}"
    return True, v


def set_yaml_values(path, updates):
    """Change KEY: value lines in config.yaml, keeping comments and layout.

    updates: {"SECTION.KEY": value}. The edited text must parse to exactly the
    original settings plus the requested changes (nothing else lost or
    duplicated); otherwise nothing is written. Keeps config.yaml.bak.
    """
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()
    before = yaml.safe_load(original) or {}
    lines = original.split("\n")
    for full_key, value in updates.items():
        sec, key = full_key.split(".", 1)
        val = fmt_yaml(value)
        headers = [i for i, l in enumerate(lines) if re.match(rf"^{re.escape(sec)}\s*:", l)]
        if len(headers) > 1:
            raise ValueError(f"section {sec} appears more than once in config.yaml")
        if headers and not re.match(rf"^{re.escape(sec)}\s*:\s*(#.*)?$", lines[headers[0]]):
            raise ValueError(f"section {sec} is not in block style - edit config.yaml by hand")
        if not headers:
            while lines and lines[-1].strip() == "":
                lines.pop()
            lines += ["", f"{sec}:", f"  {key}: {val}", ""]
            continue
        sec_idx = headers[0]
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#"):
                end = i
                break
        body = [l for l in lines[sec_idx + 1:end] if l.strip() and not l.lstrip().startswith("#")]
        indent = (len(body[0]) - len(body[0].lstrip())) if body else 2
        pat = re.compile(rf"^( {{{indent}}}{re.escape(key)}\s*:\s*)(.*?)(\s+#.*)?$")
        for i in range(sec_idx + 1, end):
            m = pat.match(lines[i])
            if m:
                lines[i] = m.group(1) + val + (m.group(3) or "")
                break
        else:
            lines.insert(sec_idx + 1, " " * indent + f"{key}: {val}")
    text = "\n".join(lines)
    expected = copy.deepcopy(before)
    for full_key, value in updates.items():
        sec, key = full_key.split(".", 1)
        if not isinstance(expected.get(sec), dict):
            expected[sec] = {}
        expected[sec][key] = value
    if yaml.safe_load(text) != expected:
        raise ValueError("config check failed - config.yaml not changed")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    shutil.copy2(path, path + ".bak")
    os.replace(tmp, path)


def init_config(path):
    """Add any missing remote-editable settings with their defaults."""
    cfg = load_config(path)
    missing = {s[0]: s[4] for s in SCHEMA if get_value(cfg, s[0]) is None}
    if get_value(cfg, "SETTINGS.DETECTION_ENABLED") is None:
        missing["SETTINGS.DETECTION_ENABLED"] = True
    if get_value(cfg, "CLEANUP.REQUIRE_DELIVERY") is None:
        missing["CLEANUP.REQUIRE_DELIVERY"] = True
    if get_value(cfg, "UPDATE.KEY") is None:
        missing["UPDATE.KEY"] = ""   # remote updates stay off until set (sagemic_update.py)
    if missing:
        set_yaml_values(path, missing)
    return missing


# ------------------------------------------------------------ system calls
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


NOT_HUB = ("MQTT: BROKER must be \"hub\" - the SageMic Station works with a SageMic hub only. "
           "To send clips straight to the cloud broker, use the original SageMic (option A).")


def require_delivery(cfg):
    """Does the sender wait for the hub and resend unconfirmed clips?
    CLEANUP: REQUIRE_DELIVERY (on unless set to false)."""
    return get_value(cfg, "CLEANUP.REQUIRE_DELIVERY") is not False


def log_lock(base_path):
    """Exclusive lock shared by everything that writes sent_clips.log or
    delivered_clips.log (sender, agent, cleanup), so cleanup can rewrite a
    log without losing a line appended at the same moment."""
    f = open(os.path.join(base_path, ".clip_logs.lock"), "a", encoding="utf-8")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f   # closing it releases the lock


def helper(*args):
    cmd = [HELPER, *[str(a) for a in args]]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              check=False).stdout.strip()
    except Exception:  # pylint: disable=broad-except
        return ""


def cpu_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:  # pylint: disable=broad-except
        out = run(["vcgencmd", "measure_temp"])   # temp=48.3'C
        m = re.search(r"([\d.]+)", out)
        return float(m.group(1)) if m else None


THROTTLE_BITS = {0: "under-voltage now", 1: "CPU capped now", 2: "throttled now",
                 3: "temperature limit now", 16: "under-voltage since boot",
                 17: "CPU capped since boot", 18: "throttled since boot",
                 19: "temperature limit since boot"}


def throttled():
    out = run(["vcgencmd", "get_throttled"])      # throttled=0x50005
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    if not m:
        return None, []
    bits = int(m.group(1), 16)
    return hex(bits), [txt for b, txt in THROTTLE_BITS.items() if bits & (1 << b)]


def services():
    units = ["sagemic_local", "sagemic_scansend.timer", "sagemic_cleanup.timer",
             "sagemic_update"]
    out = run(["systemctl", "is-active", *units]).split("\n")
    return {u: (out[i] if i < len(out) and out[i] else "unknown") for i, u in enumerate(units)}


def clip_stats(base_path, tz_name=None):
    sent = set()
    log = os.path.join(base_path, "sent_clips.log")
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8") as f:
            sent = {l.split("\t")[0].strip() for l in f if l.strip()}
    delivered = set()
    dlog = os.path.join(base_path, "delivered_clips.log")
    if os.path.exists(dlog):
        with open(dlog, "r", encoding="utf-8") as f:
            delivered = {l.split("\t")[0].strip() for l in f if l.strip()}
    try:   # date folders are named in the detector's time zone
        tz = ZoneInfo(tz_name) if tz_name else None
    except Exception:  # pylint: disable=broad-except
        tz = None
    today = datetime.now(tz).strftime("%Y-%m-%d")
    total = unsent = undelivered = today_n = 0
    newest = None
    if os.path.isdir(base_path):
        for folder in os.listdir(base_path):
            fp = os.path.join(base_path, folder)
            if not os.path.isdir(fp):
                continue
            for name in os.listdir(fp):
                if not name.endswith(".flac"):
                    continue
                total += 1
                p = os.path.join(fp, name)
                if p not in sent:
                    unsent += 1
                elif p not in delivered:
                    undelivered += 1
                if folder == today:
                    today_n += 1
                if newest is None or name > newest:
                    newest = name
    last = None
    if newest:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_(.+)_(\d+\.\d+)\.flac$", newest)
        last = ({"time": f"{m[1]} {m[2]}:{m[3]}:{m[4]}", "species": m[5], "confidence": float(m[6])}
                if m else {"file": newest})
    return {"clips_on_pi": total, "clips_today": today_n, "unsent": unsent,
            "undelivered": undelivered, "last_detection": last}


def detector_state():
    try:
        with open(DETECTOR_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
        st["age_s"] = round(time.time() - st.get("updated", 0))
        return st
    except Exception:  # pylint: disable=broad-except
        return None


def gather_status(cfg, device):
    base = (cfg.get("PATHS") or {}).get("BASE_PATH", "/")
    usage = shutil.disk_usage(base if os.path.isdir(base) else "/")
    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            uptime_s = int(float(f.read().split()[0]))
    except Exception:  # pylint: disable=broad-except
        uptime_s = None
    temp = cpu_temp_c()
    thr_hex, thr = throttled()
    svc = services()
    det = detector_state()
    clips = clip_stats(base, get_value(cfg, "SETTINGS.LOCAL_TZ"))
    settings = {s[0]: get_value(cfg, s[0], s[4]) for s in SCHEMA}
    settings["SETTINGS.DETECTION_ENABLED"] = get_value(cfg, "SETTINGS.DETECTION_ENABLED", True)
    free_gb = round(usage.free / 1e9, 2)

    warnings = []
    if temp is not None and temp >= 80:
        warnings.append(f"CPU {temp} C - throttling likely")
    elif temp is not None and temp >= 70:
        warnings.append(f"CPU {temp} C - hot")
    warnings += [t for t in thr if t.endswith("now")]
    if free_gb < float(settings["CLEANUP.MIN_FREE_GB"]):
        warnings.append(f"low disk: {free_gb} GB free")
    for u, s in svc.items():
        if s not in ("active", "unknown") and not (u == "sagemic_local" and not settings["SETTINGS.DETECTION_ENABLED"]):
            warnings.append(f"{u} {s}")
    if not settings["SETTINGS.DETECTION_ENABLED"]:
        warnings.append("detection paused")
    elif det:
        if det.get("age_s", 0) > 120:
            warnings.append(f"detector silent for {det['age_s']} s")
        if det.get("errors"):
            warnings.append(f"{det['errors']} processing error(s), last: {det.get('last_error', '')}")
        if det.get("dropped_blocks"):
            warnings.append(f"analysis falling behind: {det['dropped_blocks']} audio block(s) "
                            "not analyzed since the detector started")
        rms = det.get("rms_dbfs")
        if rms is not None and rms < -90 and det.get("in_active_hours", True):
            warnings.append(f"mic level {rms} dBFS - possible mic failure")
    if det and det.get("usb_error"):
        warnings.append(f"USB power: {det['usb_error']}")
    if det and det.get("model_error"):
        warnings.append(f"model not loaded, standard BirdNET in use: {det['model_error']}")
    if clips["undelivered"] > 500 and require_delivery(cfg):
        warnings.append(f"{clips['undelivered']} clips not confirmed by the receiver")
    if datetime.now().year < 2025:
        warnings.append("Pi clock looks wrong - use set_clock")
    synced = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])

    return {
        "device": device, "hostname": socket.gethostname(), "agent_version": VERSION,
        "time": int(time.time()), "time_local": datetime.now().isoformat(timespec="seconds"),
        "uptime_s": uptime_s, "cpu_temp_c": temp, "throttled": thr_hex, "throttle_flags": thr,
        "load": [round(x, 2) for x in os.getloadavg()],
        "mem_avail_mb": round(mem.get("MemAvailable", 0) / 1024),
        "mem_total_mb": round(mem.get("MemTotal", 0) / 1024),
        "disk_free_gb": free_gb, "disk_total_gb": round(usage.total / 1e9, 1),
        "services": svc, "detector": det, **clips,
        "ip": run(["hostname", "-I"]).split(), "settings": settings, "warnings": warnings,
        "clock_synced": {"yes": True, "no": False}.get(synced),
        "mode": "hub",
        "usb_power": (det or {}).get("usb_power"),
    }


# -------------------------------------------------------------------- agent
class Agent:
    def __init__(self, config_path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        mq = self.cfg.get("MQTT") or {}
        self.device = mq.get("DEVICE", socket.gethostname())
        base = mq.get("BASE_TOPIC", "sagemic")
        ctl = f"{base}/_ctl/{self.device}"           # per station (see module docstring)
        self.t_status = f"{ctl}/status"
        self.t_online = f"{ctl}/online"
        self.t_cmd = f"{ctl}/cmd"
        self.t_reply = f"{ctl}/reply"
        self.t_ack = f"{ctl}/ack"
        self.base_path = (self.cfg.get("PATHS") or {}).get("BASE_PATH", "")
        self.ack_lock = threading.Lock()
        self.seen_ids = collections.deque(maxlen=500)
        self.t_receivers = f"{base}/_ctl/receiver/+"
        self.receivers = {}   # receiver id -> online?
        self.wake = threading.Event()
        self.jobs = queue.Queue()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"sagemic-agent-{self.device}", clean_session=True)
        # clean session: commands sent while the agent is down are dropped,
        # never executed later
        self.client.will_set(self.t_online, "offline", qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(1, 30)
        if mq.get("USERNAME"):                       # broker login (the hub's needs one)
            self.client.username_pw_set(str(mq["USERNAME"]), str(mq.get("PASSWORD", "")))
        if not is_hub(mq.get("BROKER", "hub")):
            raise SystemExit(NOT_HUB)
        self.broker = broker_address("hub")
        self.port = int(mq.get("PORT") or 1883)

    # MQTT callbacks (network thread) -------------------------------------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        print(f"agent connected to {self.broker}:{self.port} ({reason_code})", flush=True)
        if reason_code.is_failure:
            return                     # e.g. wrong login - paho retries
        client.subscribe([(self.t_cmd, 1), (self.t_ack, 1), (self.t_receivers, 1)])
        client.publish(self.t_online, "online", qos=1, retain=True)
        self.wake.set()

    def on_message(self, client, userdata, msg):
        try:
            if msg.topic == self.t_ack:
                self.record_delivery(msg.payload)
            elif "/_ctl/receiver/" in msg.topic:
                self.receivers[msg.topic.rsplit("/", 1)[-1]] = msg.payload == b"1"
            elif msg.topic == self.t_cmd:
                self.jobs.put(msg.payload)
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

    # helpers ----------------------------------------------------------------
    def reply(self, req, ok, message, data=None):
        payload = {"id": req.get("id"), "device": self.device, "cmd": req.get("cmd"),
                   "ok": ok, "message": message, "data": data, "time": int(time.time())}
        self.client.publish(self.t_reply, json.dumps(payload), qos=1)
        print(f"{req.get('cmd')}: {'OK' if ok else 'FAILED'} - {message}", flush=True)

    def publish_status(self):
        self.cfg = load_config(self.config_path)
        st = gather_status(self.cfg, self.device)
        st["receivers_online"] = sorted(k for k, v in self.receivers.items() if v)
        self.client.publish(self.t_status, json.dumps(st), qos=1, retain=True)
        return st

    def apply_timers(self):
        msgs = []
        for key, name in (("TIMERS.SEND_INTERVAL_S", "scansend"), ("TIMERS.CLEANUP_INTERVAL_S", "cleanup")):
            val = get_value(self.cfg, key)
            if val is not None:
                ok, out = helper("set-interval", name, int(val))
                msgs.append(f"{name} timer {'set to ' + str(val) + ' s' if ok else 'NOT set: ' + out}")
        return msgs

    # command handling (worker thread) ---------------------------------------
    def handle(self, raw):
        req = {}
        try:
            req = json.loads(raw)
            if not isinstance(req, dict):
                return
            rid = req.get("id")
            if rid in self.seen_ids:      # duplicate delivery of the same command
                return
            self.seen_ids.append(rid)
            self.cfg = load_config(self.config_path)
            cmd = req.get("cmd")
            args = req.get("args") or {}
            if cmd == "status":
                self.reply(req, True, "status sent", self.publish_status())
            elif cmd == "get_config":
                self.reply(req, True, "settings", self.config_data())
            elif cmd == "set":
                self.cmd_set(req, args)
            elif cmd == "action":
                self.cmd_action(req, args if isinstance(args, dict) else {})
            else:
                self.reply(req, False, f"unknown command {cmd}")
        except Exception as e:  # pylint: disable=broad-except
            try:
                self.reply(req if isinstance(req, dict) else {}, False, f"error: {e}")
            except Exception:  # pylint: disable=broad-except
                print(f"command error: {e}", flush=True)

    def config_data(self):
        return {"version": VERSION, "device": self.device, "actions": ACTIONS,
                "settings": [{"key": k, "type": t, "min": lo, "max": hi, "default": d,
                              "apply": a, "label": lab, "description": desc,
                              "value": get_value(self.cfg, k, d)}
                             for k, t, lo, hi, d, a, lab, desc in SCHEMA]}

    def cmd_set(self, req, args):
        if not isinstance(args, dict) or not args:
            self.reply(req, False, "nothing to set")
            return
        clean, errors = {}, []
        for k, v in args.items():
            ok, val = validate(k, v)
            if ok:
                clean[k] = val
            else:
                errors.append(f"{k}: {val}")
        if errors:
            self.reply(req, False, "not changed - " + "; ".join(errors))
            return
        set_yaml_values(self.config_path, clean)
        self.cfg = load_config(self.config_path)
        applies = {SCHEMA_BY_KEY[k][5] if k in SCHEMA_BY_KEY else "detector" for k in clean}
        notes = []
        for a in sorted(applies):
            if a.startswith("timer:"):
                name = a.split(":", 1)[1]
                key = "TIMERS.SEND_INTERVAL_S" if name == "scansend" else "TIMERS.CLEANUP_INTERVAL_S"
                ok, out = helper("set-interval", name, int(get_value(self.cfg, key)))
                notes.append(f"{name} timer {'updated' if ok else 'NOT updated: ' + out}")
            elif a == "detector":
                ok, out = helper("restart", "sagemic_local.service")
                notes.append("detector restarted" if ok else f"detector NOT restarted: {out}")
            elif a == "agent":
                notes.append("status interval applied")
                self.wake.set()
            elif a == "next_run":
                notes.append("used at next cleanup run")
        changed = ", ".join(f"{k}={v}" for k, v in clean.items())
        self.reply(req, True, f"saved {changed}; " + "; ".join(notes))
        self.publish_status()

    def cmd_action(self, req, args):
        name = args.get("name")
        if name not in ACTIONS:
            self.reply(req, False, f"unknown action {name}")
            return
        if name in ("pause_detection", "resume_detection"):
            enabled = name == "resume_detection"
            set_yaml_values(self.config_path, {"SETTINGS.DETECTION_ENABLED": enabled})
            ok, out = helper("restart", "sagemic_local.service")
            msg = ("detection resumed" if enabled else "detection paused") + ("" if ok else f" (restart failed: {out})")
        elif name == "restart_detector":
            ok, out = helper("restart", "sagemic_local.service"); msg = "detector restarted"
        elif name == "restart_sender":
            ok, out = helper("restart", "sagemic_scansend.timer"); msg = "send timer restarted"
        elif name == "send_now":
            ok, out = helper("start", "sagemic_scansend.service"); msg = "send started"
        elif name == "resend_undelivered":
            import sys
            subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "sagemic_scansend.py"), "--config", self.config_path, "--resend-undelivered"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ok, out, msg = True, "", "resending unconfirmed clips"
        elif name == "cleanup_now":
            ok, out = helper("start", "sagemic_cleanup.service"); msg = "cleanup started"
        elif name == "set_clock":
            epoch = int(args.get("epoch", 0))
            before = int(time.time())
            if abs(epoch - before) <= 2:
                self.reply(req, True, "clock already correct (within 2 s)")
                return
            ok, out = helper("set-time", epoch)
            msg = f"clock changed by {epoch - before:+d} s"
        elif name in ("reboot", "shutdown"):
            self.reply(req, True, f"{name} in 3 s")
            self.client.publish(self.t_online, "offline", qos=1, retain=True)
            helper("reboot" if name == "reboot" else "poweroff")
            return
        self.reply(req, ok, msg if ok else f"{msg} FAILED: {out}")
        self.publish_status()

    def worker(self):
        while True:
            raw = self.jobs.get()
            try:
                self.handle(raw)
            except Exception as e:  # pylint: disable=broad-except
                print(f"worker error: {e}", flush=True)

    def run(self):
        for note in self.apply_timers():
            print(note, flush=True)
        threading.Thread(target=self.worker, daemon=True).start()
        self.client.connect_async(self.broker, self.port)
        self.client.loop_start()
        while True:
            interval = int(get_value(self.cfg, "AGENT.STATUS_INTERVAL_S", 300) or 300)
            if self.client.is_connected():
                try:
                    self.publish_status()
                except Exception as e:  # pylint: disable=broad-except
                    print(f"status failed: {e}", flush=True)
            connected = self.client.is_connected()
            if not connected:          # BROKER: hub - the hub's address may only be known later
                addr = broker_address("hub")
                if addr != self.broker:
                    print(f"broker address now {addr} (was {self.broker})", flush=True)
                    self.broker = addr
                    self.client.connect_async(self.broker, self.port)
            self.wake.wait(timeout=max(30, interval) if connected else 30)
            self.wake.clear()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--init-config", action="store_true",
                   help="add missing remote-editable settings with defaults, then exit")
    p.add_argument("--set", nargs="+", metavar="SECTION.KEY=VALUE",
                   help="change settings in config.yaml (keeps comments), then exit; "
                        "VALUE is read as YAML: true, 1883, text")
    a = p.parse_args()
    if a.set:
        updates = {}
        for item in a.set:
            key, sep, raw = item.partition("=")
            key = key.strip()
            if not sep or "." not in key:
                p.error(f"--set needs SECTION.KEY=VALUE, got {item!r}")
            if key in TEXT_KEYS:       # names, passwords, keys: never numbers or true/false
                raw = raw.strip()
                updates[key] = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" else raw
            else:
                updates[key] = yaml.safe_load(raw) if raw.strip() else ""
        set_yaml_values(a.config, updates)
        print("set: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
        return
    if a.init_config:
        added = init_config(a.config)
        print("added: " + (", ".join(f"{k}={v}" for k, v in added.items()) if added else "nothing (all present)"))
        return
    Agent(a.config).run()


if __name__ == "__main__":
    main()
