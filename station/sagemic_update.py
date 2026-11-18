"""SageMic update agent: installs a new SageMicStation_Pi.zip sent over MQTT.

Runs on each Pi (systemd service sagemic_update) next to sagemic_agent. It is
kept separate from the agent and imports nothing from it, so a package with a
faulty agent can still be rolled back remotely.

Topics, on the hub's MQTT broker (Mosquitto; MQTT: BROKER must be "hub"), per station:

    <BASE_TOPIC>/_ctl/<DEVICE>/update/request   hub -> station   {"id", "action", "time", "hmac", ...}
    <BASE_TOPIC>/_ctl/<DEVICE>/update/progress  station -> hub   {"id", "device", "stage", "ok", "message"}
    <BASE_TOPIC>/_ctl/<DEVICE>/update/state     station -> hub   installed version, last result (retained)

Actions:
    install   {"version", "sha256", "data": base64 zip, "force": optional}
    rollback  restore the files from before the last update
    model     {"version", "sha256", "size", "chunk_size", "chunks"}: receive a
              model zip from the hub in parts, test it, and use it (see below)
    model-use {"version"}: use a model already on the station, or "standard"
    state     publish the state again (needs no signature)

Models (all stations use the same one; the hub sends it to one station at a time):
    The zip holds a species list (one .txt file) or a custom classifier (one
    .tflite file and its labels .txt file) - see sagemic_local.py. The station
    asks for each part in turn (progress stage "chunk", "need": part number)
    and the hub answers on <BASE_TOPIC>/_ctl/<DEVICE>/model/chunk with
    {"id", "n", "data": base64}. An interrupted transfer carries on from the
    last part received when the model is sent again. The whole zip is checked
    against the signed sha256, unpacked to ~/sagemic_models/<version>/, and
    tested (sagemic_local.py --test-model). Then ~/sagemic_models/current is
    set and the detector restarted; if the detector does not start with the new
    model, the previous one is put back. The last few models are kept, so
    switching back to an earlier one needs no transfer.

install, rollback, model and model-use must be signed with the key in config.yaml (UPDATE: KEY):
    hmac = HMAC-SHA256(KEY, "<id>\\n<action>\\n<version>\\n<sha256>\\n<time>")   (hex)
The key never travels over the link. Updates are refused until it is set.

Install steps: check signature and checksum -> unpack and check the files
(syntax, the new updater's --self-test, import of the new agent, and dry runs
of the new sagemic_scansend.py and sagemic_cleanup.py against the real config)
-> back up the current files to ~/sagemic_backup/<date-time>/ -> copy the new
files in -> run install_agent.sh through sagemic-helper (in its own systemd
unit, so it keeps going when it restarts this service) -> check the services
-> keep the update, or restore the backup and reinstall it.
Progress is kept in update_state.json, so the update carries on after the
installer restarts this service. sagemic-helper run-installer also arms a
root-side guard (systemd timer, 30 min): if this file still shows an update
in progress by then, it restores the backup and reinstalls it. finish() stops
the guard.

Usage:
    python sagemic_update.py --config config/config.yaml
    python sagemic_update.py --config config/config.yaml --self-test
"""

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime

import paho.mqtt.client as mqtt
import yaml

VERSION = "2026.11.2"
HELPER = os.environ.get("SAGEMIC_HELPER", "/usr/local/sbin/sagemic-helper")
DETECTOR_STATE = os.environ.get("SAGEMIC_STATE", "/dev/shm/sagemic_detector.json")
INSTALL_RC = "/run/sagemic_install.rc"      # written by sagemic-helper run-installer
INSTALL_LOG = "/run/sagemic_install.log"
REQUIRED = ["install_agent.sh", "sagemic-helper", "sagemic_agent.py", "sagemic_update.py",
            "sagemic_local.py", "sagemic_scansend.py", "sagemic_cleanup.py"]
FILE_RE = re.compile(r"^(sagemic-helper|[A-Za-z0-9_][A-Za-z0-9_.-]*\.(py|sh|txt|md))$")
BACKUP_RE = re.compile(r"^\d{8}-\d{6}$")
MAX_ZIP = 5_000_000
MAX_UNPACKED = 20_000_000
INSTALL_TIMEOUT_S = 900
HEALTH_TIMEOUT_S = 240
KEEP_BACKUPS = 3
MAX_AGE_S = 86400
# models
MODELS_DIR = os.path.expanduser(os.environ.get("SAGEMIC_MODELS", "~/sagemic_models"))
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
MODEL_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .()-]*\.(tflite|txt)$", re.I)
MAX_MODEL = 200_000_000           # zip as sent
MAX_MODEL_UNPACKED = 400_000_000
MAX_CHUNK = 700_000               # one part, before base64 (the hub's broker takes 1 MB messages)
CHUNK_WAIT_S = 30                 # ask for a part again after this long ...
CHUNK_TRIES = 5                   # ... this many times, then give up
MODEL_TEST_S = 600
MODEL_HEALTH_S = 300
KEEP_MODELS = 3


class UpdateError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


# ------------------------------------------------------------- config file
# (same as sagemic_agent.py - copied, not imported, see module docstring)
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_value(cfg, key, default=None):
    sec, name = key.split(".", 1)
    return (cfg.get(sec) or {}).get(name, default)


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



def helper(*args):
    cmd = [HELPER, *[str(a) for a in args]]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)


def is_active(units):
    try:
        out = subprocess.run(["systemctl", "is-active", *units], capture_output=True,
                             text=True, timeout=10, check=False).stdout.split("\n")
    except Exception:  # pylint: disable=broad-except
        out = []
    return {u: (out[i] if i < len(out) and out[i] else "unknown") for i, u in enumerate(units)}


def write_atomic(path, data, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def file_mode(name):
    return 0o755 if name == "sagemic-helper" or name.endswith(".sh") else 0o644


def tail(path, n=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]).rstrip()
    except OSError:
        return ""


# ------------------------------------------------------------------ updater
class Updater:
    def __init__(self, config_path):
        self.config_path = os.path.realpath(config_path)
        self.dir = os.path.dirname(os.path.realpath(__file__))
        home = os.path.expanduser("~")
        self.backup_root = os.path.join(home, "sagemic_backup")
        self.staging = os.path.join(home, "sagemic_update_tmp")
        self.state_path = os.path.join(self.dir, "update_state.json")
        cfg = load_config(self.config_path)
        mq = cfg.get("MQTT") or {}
        self.device = mq.get("DEVICE", socket.gethostname())
        base = mq.get("BASE_TOPIC", "sagemic")
        ctl = f"{base}/_ctl/{self.device}"
        self.t_request = f"{ctl}/update/request"
        self.t_progress = f"{ctl}/update/progress"
        self.t_state = f"{ctl}/update/state"
        self.t_chunk = f"{ctl}/model/chunk"
        self.state = self.load_state()
        self.busy = threading.Event()
        self.activity = ""            # what the worker is doing (for "busy" answers)
        self.kind = "software"        # "model" while a model request runs
        self.jobs = queue.Queue()
        self.chunks = queue.Queue()   # model parts from the hub
        self.sagemic_dir = os.path.dirname(self.dir)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"sagemic-update-{self.device}", clean_session=True)
        # clean session: requests sent while this service is down are dropped
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(1, 30)
        if mq.get("USERNAME"):                       # broker login (the hub's needs one)
            self.client.username_pw_set(str(mq["USERNAME"]), str(mq.get("PASSWORD", "")))
        if not is_hub(mq.get("BROKER", "hub")):
            raise SystemExit("MQTT: BROKER must be \"hub\" - the SageMic Station works with a SageMic "
                             "hub only. To send clips straight to the cloud broker, use the original "
                             "SageMic (option A).")
        self.broker = broker_address("hub")
        self.port = int(mq.get("PORT") or 1883)

    # state file -------------------------------------------------------------
    def load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                st.setdefault("phase", "idle")
                return st
        except (OSError, ValueError):
            pass
        return {"phase": "idle", "installed": None, "last_result": None, "seen_ids": []}

    def save_state(self):
        write_atomic(self.state_path, json.dumps(self.state, indent=1).encode())

    # MQTT -------------------------------------------------------------------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        print(f"update agent connected to {self.broker}:{self.port} ({reason_code})", flush=True)
        if reason_code.is_failure:
            return
        client.subscribe([(self.t_request, 1), (self.t_chunk, 1)])
        self.publish_state()

    def on_message(self, client, userdata, msg):
        try:
            req = json.loads(msg.payload)
            if not isinstance(req, dict):
                return
        except ValueError:
            return
        if msg.topic == self.t_chunk:     # a model part: for the worker waiting for it
            self.chunks.put(req)
            return
        rid = str(req.get("id", ""))[:64]
        if self.busy.is_set() and req.get("action") != "state":
            # the answer belongs to the new request, not to the one running
            payload = {"id": rid, "device": self.device, "stage": "busy", "ok": False,
                       "kind": "model" if str(req.get("action", "")).startswith("model") else "software",
                       "message": f"busy ({self.activity or self.state.get('phase')}) - try again later",
                       "time": int(time.time())}
            self.client.publish(self.t_progress, json.dumps(payload), qos=1)
            return
        self.jobs.put(req)

    def wait_connected(self, timeout):
        """True once connected to the broker; gives up after timeout seconds."""
        end = time.time() + timeout
        while not self.client.is_connected() and time.time() < end:
            time.sleep(0.5)
        return self.client.is_connected()

    def progress(self, rid, stage, ok, message, **extra):
        payload = {"id": rid, "device": self.device, "stage": stage, "ok": ok, "kind": self.kind,
                   "message": message, "time": int(time.time()), **extra}
        # after a restart the connection may not be back yet: a report sent
        # before it is would be lost, so wait for it (briefly)
        self.wait_connected(60)
        self.client.publish(self.t_progress, json.dumps(payload), qos=1)
        mark = {True: "OK", False: "FAILED", None: ".."}[ok]
        print(f"[{stage}] {mark} {message}", flush=True)

    def publish_state(self):
        st = {k: v for k, v in self.state.items() if k != "seen_ids"}
        if isinstance(st.get("installed"), dict):
            st["installed"] = {k: v for k, v in st["installed"].items() if k != "previous"}
        st.update(device=self.device, updater_version=VERSION, time=int(time.time()),
                  model=self.model_info())
        self.client.publish(self.t_state, json.dumps(st), qos=1, retain=True)

    # requests (worker thread) ---------------------------------------------
    def handle(self, req):
        rid = str(req.get("id", ""))[:64]
        action = req.get("action")
        if action == "state":
            self.publish_state()
            inst = self.state.get("installed") or {}
            self.progress(rid, "state", True, f"installed: {inst.get('version', 'unknown')}, "
                                              f"phase: {self.state.get('phase')}")
            return
        if action not in ("install", "rollback", "model", "model-use"):
            self.progress(rid, "verify", False, f"unknown action {action}")
            return
        model = action.startswith("model")
        self.kind = "model" if model else "software"
        try:
            ok, why = self.check_signature(req, rid, action)
            if not ok:
                self.progress(rid, "verify", False, why)
                return
            if self.state.get("phase") != "idle":
                self.progress(rid, "busy", False, f"busy ({self.state.get('phase')})")
                return
            self.busy.set()
            self.activity = f"{action} {req.get('version') or ''}".strip()
            if action == "install":
                self.install(req, rid)
            elif action == "rollback":
                self.rollback_request(rid)
            elif action == "model":
                self.model_receive(req, rid)
            else:
                self.model_switch(rid, str(req.get("version", "")))
        except UpdateError as e:
            self.progress(rid, e.stage, False, str(e))
            self.state["model_result" if model else "last_result"] = {
                "id": rid, "ok": False, "message": f"{e.stage}: {e}", "time": int(time.time())}
            self.save_state()
            self.publish_state()
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)
            self.busy.clear()
            self.activity = ""
            self.kind = "software"

    def check_signature(self, req, rid, action):
        key = str(get_value(load_config(self.config_path), "UPDATE.KEY", "") or "")
        if len(key) < 16:
            return False, "updates disabled: set UPDATE: KEY (16+ characters) in config.yaml on the Pi"
        text = "\n".join([rid, action, str(req.get("version", "")), str(req.get("sha256", "")),
                          str(req.get("time", ""))])
        expected = hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(req.get("hmac", ""))):
            return False, "rejected: bad signature (update key differs between sender and Pi?)"
        if not rid or rid in self.state.get("seen_ids", []):
            return False, "rejected: request id missing or already used"
        try:
            age = abs(time.time() - float(req.get("time")))
        except (TypeError, ValueError):
            return False, "rejected: no time in request"
        if age > MAX_AGE_S and datetime.now().year >= 2025:
            return False, "rejected: request older than 24 h, or the Pi clock is wrong (use set_clock)"
        self.state["seen_ids"] = (self.state.get("seen_ids", []) + [rid])[-50:]
        self.save_state()
        return True, ""

    # install ---------------------------------------------------------------
    def install(self, req, rid):
        try:
            blob = base64.b64decode(str(req.get("data", "")), validate=True)
        except ValueError as e:
            raise UpdateError("verify", f"package data is not valid base64 ({e})") from e
        if not blob or len(blob) > MAX_ZIP:
            raise UpdateError("verify", f"package size {len(blob)} bytes not allowed")
        sha = hashlib.sha256(blob).hexdigest()
        if sha != str(req.get("sha256", "")).lower():
            raise UpdateError("verify", "checksum mismatch - package damaged in transfer")
        version = str(req.get("version") or sha[:8])[:40]
        inst = self.state.get("installed") or {}
        if inst.get("sha256") == sha and not req.get("force"):
            self.progress(rid, "done", True, f"{version} is already installed (send force: true to reinstall)")
            return
        self.progress(rid, "verify", True, f"package {version} ({len(blob)} bytes) received and verified")

        files = self.unpack(blob)
        self.check_files(files)
        self.progress(rid, "check", True, f"{len(files)} files checked: {', '.join(sorted(files))}")

        backup, new_files = self.backup(files)
        self.copy_in(files)
        self.state.update(phase="installing", id=rid, version=version, sha256=sha, backup=backup,
                          new_files=new_files, previous=self.state.get("installed"),
                          reason=None, started=int(time.time()))
        self.save_state()
        self.publish_state()
        self.progress(rid, "backup", True, f"old files saved to {backup}")
        self.start_installer()
        self.progress(rid, "install", None, "installer running - services restart and this "
                                            "connection drops for a few seconds")
        self.supervise()

    def unpack(self, blob):
        """Files from the zip folder that holds install_agent.sh -> {name: staged path}."""
        shutil.rmtree(self.staging, ignore_errors=True)
        os.makedirs(self.staging)
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as e:
            raise UpdateError("unpack", f"not a zip file ({e})") from e
        with z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise UpdateError("unpack", "package unpacks too large")
            for i in infos:
                parts = i.filename.replace("\\", "/").split("/")
                if i.filename.startswith(("/", "\\")) or ".." in parts:
                    raise UpdateError("unpack", f"unsafe path in zip: {i.filename}")
            installers = [i.filename.replace("\\", "/") for i in infos
                          if i.filename.replace("\\", "/").split("/")[-1] == "install_agent.sh"]
            if len(installers) != 1:
                raise UpdateError("unpack", "zip must contain exactly one install_agent.sh")
            prefix = installers[0][:-len("install_agent.sh")]
            files = {}
            for i in infos:
                path = i.filename.replace("\\", "/")
                name = path[len(prefix):]
                if not path.startswith(prefix) or "/" in name or not FILE_RE.match(name):
                    continue          # other folders, __MACOSX, config files, ...
                dest = os.path.join(self.staging, name)
                with open(dest, "wb") as f:
                    f.write(z.read(i))
                files[name] = dest
        return files

    def check_files(self, files):
        missing = [n for n in REQUIRED if n not in files]
        if missing:
            raise UpdateError("check", f"package is missing {', '.join(missing)}")
        for name, path in files.items():
            if name.endswith(".py"):
                try:
                    with open(path, "rb") as f:
                        compile(f.read(), name, "exec")
                except SyntaxError as e:
                    raise UpdateError("check", f"{name} line {e.lineno}: {e.msg}") from e
            elif name.endswith(".sh") or name == "sagemic-helper":
                r = subprocess.run(["bash", "-n", path], capture_output=True, text=True, check=False)
                if r.returncode:
                    raise UpdateError("check", f"{name}: {r.stderr.strip()}")
        # the new updater and agent must at least start, or no remote rollback is
        # possible; the new sender and cleanup must run with the real config
        # (--dry-run: nothing is sent or deleted)
        def staged(name, *args):
            return [sys.executable, os.path.join(self.staging, name), "--config", self.config_path, *args]
        tests = [("self-test of the new sagemic_update.py", staged("sagemic_update.py", "--self-test")),
                 ("import of the new sagemic_agent.py", [sys.executable, "-c", "import sagemic_agent"]),
                 ("dry run of the new sagemic_scansend.py", staged("sagemic_scansend.py", "--dry-run")),
                 ("dry run of the new sagemic_cleanup.py", staged("sagemic_cleanup.py", "--dry-run"))]
        for label, cmd in tests:
            try:
                r = subprocess.run(cmd, cwd=self.staging, capture_output=True, text=True,
                                   timeout=120, check=False)
            except subprocess.TimeoutExpired as e:
                raise UpdateError("check", f"{label} timed out (120 s)") from e
            if r.returncode:
                err = (r.stderr or r.stdout).strip().splitlines()
                raise UpdateError("check", f"{label} failed (exit {r.returncode}): "
                                           f"{err[-1] if err else 'no output'}")

    def backup(self, files):
        dest = os.path.join(self.backup_root, datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        new_files = []
        for name in files:
            cur = os.path.join(self.dir, name)
            if os.path.isfile(cur):
                shutil.copy2(cur, dest)
            else:
                new_files.append(name)
        # keep the newest few; the flat ~/sagemic_backup/* files from manual updates are left alone
        keep = {dest}
        inst = self.state.get("installed") or {}
        if inst.get("backup"):
            keep.add(inst["backup"])
        old = sorted(d for d in os.listdir(self.backup_root)
                     if BACKUP_RE.match(d) and os.path.isdir(os.path.join(self.backup_root, d)))
        for d in old[:-KEEP_BACKUPS]:
            if os.path.join(self.backup_root, d) not in keep:
                shutil.rmtree(os.path.join(self.backup_root, d), ignore_errors=True)
        return dest, new_files

    def copy_in(self, files):
        for name, src in files.items():
            with open(src, "rb") as f:
                write_atomic(os.path.join(self.dir, name), f.read(), file_mode(name))

    def restore(self, backup, new_files):
        if not backup or not os.path.isdir(backup):
            raise UpdateError("rollback", f"backup folder {backup} not found")
        for name in os.listdir(backup):
            if FILE_RE.match(name):
                with open(os.path.join(backup, name), "rb") as f:
                    write_atomic(os.path.join(self.dir, name), f.read(), file_mode(name))
        for name in new_files or []:
            if FILE_RE.match(name) and name != "sagemic_update.py":
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass

    def start_installer(self):
        self.state["install_started"] = time.time()
        self.save_state()
        ok, out = helper("run-installer", self.dir, self.config_path)
        if not ok:
            raise UpdateError("install", f"could not start the installer: {out}")

    # after the installer started (also resumed after this service restarts)
    def wait_installer(self):
        started = float(self.state.get("install_started", 0))
        while time.time() < started + INSTALL_TIMEOUT_S:
            try:
                if os.path.getmtime(INSTALL_RC) >= started - 2:
                    with open(INSTALL_RC, "r", encoding="utf-8") as f:
                        return int(f.read().strip() or -1)
            except (OSError, ValueError):
                pass
            time.sleep(2)
        return None

    def health_check(self):
        """Services up twice in a row 15 s apart, and a fresh detector heartbeat."""
        detection = get_value(load_config(self.config_path), "SETTINGS.DETECTION_ENABLED", True) is not False
        units = ["sagemic_agent", "sagemic_update", "sagemic_scansend.timer", "sagemic_cleanup.timer"]
        if detection:
            units.append("sagemic_local")
        try:
            since = os.path.getmtime(INSTALL_RC)
        except OSError:
            since = time.time()
        deadline = time.time() + HEALTH_TIMEOUT_S
        good, why = 0, "no check ran"
        while time.time() < deadline:
            bad = [f"{u} {s}" for u, s in is_active(units).items() if s != "active"]
            if detection and not bad:
                try:
                    with open(DETECTOR_STATE, "r", encoding="utf-8") as f:
                        beat = json.load(f).get("updated", 0)
                except (OSError, ValueError):
                    beat = 0
                if beat <= since:
                    bad.append("no detector heartbeat since the install")
            if bad:
                good, why = 0, ", ".join(bad)
            else:
                good += 1
                if good >= 2:
                    return True, ""
            time.sleep(15)
        return False, why

    def supervise(self):
        phase = self.state.get("phase")
        rid = self.state.get("id", "")
        rc = self.wait_installer()
        log = tail(INSTALL_LOG)
        if phase == "installing":
            if rc != 0:
                self.rollback(f"installer {'did not finish' if rc is None else f'failed (exit {rc})'}", log)
                return
            self.progress(rid, "health", None, "installer finished - checking services")
            ok, why = self.health_check()
            if not ok:
                self.rollback(f"health check failed: {why}", log)
                return
            prev = self.state.get("installed") or None
            if prev:
                prev = {k: v for k, v in prev.items() if k not in ("previous", "backup")}
            self.state["installed"] = {"version": self.state.get("version"),
                                       "sha256": self.state.get("sha256"), "time": int(time.time()),
                                       "backup": self.state.get("backup"), "previous": prev}
            msg = f"{self.state.get('version')} installed and running"
            self.finish(rid, True, msg, "done")
        elif phase == "rolling_back":
            ok, why = (rc == 0, "") if rc != 0 else self.health_check()
            reason = self.state.get("reason") or "rollback"
            if ok:
                self.state["installed"] = self.state.get("previous")
                prev = self.state["installed"] or {}
                msg = f"{reason} - rolled back to {prev.get('version', 'the previous files')}"
            else:
                msg = (f"{reason} - ROLLBACK FAILED ({'installer exit ' + str(rc) if rc != 0 else why}); "
                       "connect over SSH and see /run/sagemic_install.log")
            self.finish(rid, False if reason != "rollback requested" else ok, msg, "rollback", log=log)

    def finish(self, rid, ok, message, stage, **extra):
        self.state["last_result"] = {"id": rid, "ok": ok, "message": message, "time": int(time.time())}
        self.state["phase"] = "idle"
        self.save_state()
        ok_stop, out = helper("update-guard-stop")   # the root-side guard is not needed now
        if not ok_stop:
            print(f"update guard not stopped: {out}", flush=True)
        self.publish_state()
        self.progress(rid, stage, ok, message, **extra)

    def rollback(self, reason, log):
        rid = self.state.get("id", "")
        self.progress(rid, "rollback", None, f"{reason} - restoring the previous files", log=log)
        try:
            self.restore(self.state.get("backup"), self.state.get("new_files"))
            self.state.update(phase="rolling_back", reason=reason)
            self.save_state()
            self.publish_state()
            self.start_installer()
        except (UpdateError, OSError) as e:
            self.finish(rid, False, f"{reason} - ROLLBACK FAILED: {e}", "rollback")
            return
        self.supervise()

    def rollback_request(self, rid):
        inst = self.state.get("installed") or {}
        if not inst.get("backup") or not os.path.isdir(inst["backup"]):
            raise UpdateError("rollback", "no backup from the last update to roll back to")
        self.state.update(id=rid, version=None, sha256=None, backup=inst["backup"], new_files=[],
                          previous=inst.get("previous"))
        self.progress(rid, "rollback", None, f"restoring the files from before {inst.get('version')}")
        self.restore(inst["backup"], [])
        self.state.update(phase="rolling_back", reason="rollback requested")
        self.save_state()
        self.publish_state()
        self.start_installer()
        self.supervise()

    # models ------------------------------------------------------------------
    @staticmethod
    def model_current():
        """Folder name of the model in use, "" = standard BirdNET (as sagemic_local.py)."""
        try:
            with open(os.path.join(MODELS_DIR, "current"), "r", encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            return ""
        return "" if name in ("", "standard") else name

    @staticmethod
    def model_meta(name):
        try:
            with open(os.path.join(MODELS_DIR, name, "model.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta if isinstance(meta, dict) else None
        except (OSError, ValueError):
            return None

    def model_stored(self):
        """Models on the station, oldest first: [(received, name)]."""
        try:
            names = os.listdir(MODELS_DIR)
        except OSError:
            return []
        out = []
        for n in names:
            meta = MODEL_NAME_RE.match(n) and self.model_meta(n)
            if meta:
                out.append((meta.get("received", 0), n))
        return sorted(out)

    def model_info(self):
        cur = self.model_current()
        meta = self.model_meta(cur) if cur else None
        return {"current": cur or "standard", "about": (meta or {}).get("about", "standard BirdNET"),
                "stored": [n for _, n in self.model_stored()],
                "last_result": self.state.get("model_result")}

    def model_done(self, rid, ok, message, stage="done"):
        self.state["model_result"] = {"id": rid, "ok": ok, "message": message, "time": int(time.time())}
        self.save_state()
        self.publish_state()
        self.progress(rid, stage, ok, message)

    def model_receive(self, req, rid):
        version = str(req.get("version", ""))
        if not MODEL_NAME_RE.match(version) or version in ("standard", "current"):
            raise UpdateError("verify", f"model name \"{version}\" not allowed")
        sha = str(req.get("sha256", "")).lower()
        try:
            size, chunk, chunks = int(req["size"]), int(req["chunk_size"]), int(req["chunks"])
        except (KeyError, TypeError, ValueError) as e:
            raise UpdateError("verify", "model request without size, chunk_size or chunks") from e
        if (not re.fullmatch(r"[0-9a-f]{64}", sha) or not 0 < size <= MAX_MODEL
                or not 0 < chunk <= MAX_CHUNK or chunks != -(-size // chunk)):
            raise UpdateError("verify", "model size, part size or checksum not allowed")
        os.makedirs(MODELS_DIR, exist_ok=True)
        meta = self.model_meta(version)
        if meta and meta.get("sha256") == sha:
            self.progress(rid, "receive", True, f"{version} is already on the station - not sent again")
        else:
            if meta:
                raise UpdateError("verify", f"the station has a different model called {version} - "
                                            "give the new zip the next number")
            free = shutil.disk_usage(MODELS_DIR).free
            if free < 3 * size + 300_000_000:
                raise UpdateError("verify", f"not enough disk space for {version}: {free / 1e6:.0f} MB free, "
                                            f"{(3 * size + 300_000_000) / 1e6:.0f} MB needed")
            part = self.model_download(rid, version, sha, size, chunk, chunks)
            try:
                self.model_unpack(rid, part, version, sha, size)
            except UpdateError:
                try:
                    os.remove(part)           # a model that fails its test is not kept
                except OSError:
                    pass
                raise
            finally:
                shutil.rmtree(os.path.join(MODELS_DIR, ".staging"), ignore_errors=True)
        self.model_switch(rid, version)

    def request_chunk(self, rid, n):
        """Ask the hub for part n (not printed: there are many)."""
        self.wait_connected(60)
        self.client.publish(self.t_progress, json.dumps(
            {"id": rid, "device": self.device, "stage": "chunk", "kind": "model", "ok": None,
             "need": n, "message": f"part {n + 1}", "time": int(time.time())}), qos=1)

    def get_chunk(self, rid, n):
        for _ in range(CHUNK_TRIES):
            self.request_chunk(rid, n)
            end = time.time() + CHUNK_WAIT_S
            while time.time() < end:
                try:
                    m = self.chunks.get(timeout=max(0.1, end - time.time()))
                except queue.Empty:
                    break
                if m.get("id") != rid or m.get("n") != n:
                    continue          # an old or repeated part
                try:
                    return base64.b64decode(str(m.get("data", "")), validate=True)
                except ValueError:
                    break             # damaged: ask again
        raise UpdateError("receive", f"part {n + 1} did not arrive (hub or HaLow link down?) - send the "
                                     "model again; the station carries on from the parts it has")

    def model_download(self, rid, version, sha, size, chunk, chunks):
        part = os.path.join(MODELS_DIR, f".download-{sha[:16]}.part")
        for f in os.listdir(MODELS_DIR):      # other, older interrupted downloads
            if f.startswith(".download-") and os.path.join(MODELS_DIR, f) != part:
                try:
                    os.remove(os.path.join(MODELS_DIR, f))
                except OSError:
                    pass
        have = os.path.getsize(part) if os.path.exists(part) else 0
        n = min(have // chunk, chunks)        # an interrupted transfer carries on from here
        while True:                           # parts left over from an earlier request
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                break
        self.progress(rid, "receive", None, f"receiving {version}: {size / 1e6:.1f} MB in {chunks} parts"
                                            + (f", carrying on from part {n + 1}" if n else ""))
        with open(part, "r+b" if os.path.exists(part) else "wb") as f:
            f.truncate(n * chunk)
            f.seek(n * chunk)
            shown = n * 10 // chunks
            while n < chunks:
                data = self.get_chunk(rid, n)
                want = min(chunk, size - n * chunk)
                if len(data) != want:
                    raise UpdateError("receive", f"part {n + 1} has {len(data)} bytes, expected {want}")
                f.write(data)
                n += 1
                if n * 10 // chunks > shown and n < chunks:     # every 10 %
                    shown = n * 10 // chunks
                    f.flush()
                    self.progress(rid, "receive", None, f"received {shown * 10} % ({n} of {chunks} parts)")
            f.flush()
            os.fsync(f.fileno())
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != sha:
            os.remove(part)
            raise UpdateError("receive", "checksum mismatch - the model was damaged in transfer; send it again")
        self.progress(rid, "receive", True, f"{version} received and verified")
        return part

    def model_unpack(self, rid, part, version, sha, size):
        stage_dir = os.path.join(MODELS_DIR, ".staging")
        shutil.rmtree(stage_dir, ignore_errors=True)
        os.makedirs(stage_dir)
        try:
            with zipfile.ZipFile(part) as z:
                infos = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
                if sum(i.file_size for i in infos) > MAX_MODEL_UNPACKED:
                    raise UpdateError("check", "model unpacks too large")
                names = {}
                for i in infos:
                    base = i.filename.replace("\\", "/").split("/")[-1]
                    if base.startswith("."):
                        continue
                    if not MODEL_FILE_RE.match(base):
                        raise UpdateError("check", f"{i.filename}: a model zip holds only .tflite and .txt files")
                    if base in names:
                        raise UpdateError("check", f"{base} is in the zip twice")
                    names[base] = i
                for base, i in names.items():
                    with z.open(i) as src, open(os.path.join(stage_dir, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if base.lower().endswith(".txt"):
                        # files made on Windows (Notepad, PowerShell) may start with a
                        # byte-order mark and end lines with \r\n; birdnetlib reads the
                        # labels as they are, so the names would never match
                        p = os.path.join(stage_dir, base)
                        with open(p, "rb") as f:
                            raw = f.read()
                        clean = raw.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                        if clean != raw:
                            with open(p, "wb") as f:
                                f.write(clean)
        except zipfile.BadZipFile as e:
            raise UpdateError("check", f"not a zip file ({e})") from e
        self.progress(rid, "check", None, f"testing {version} ({', '.join(sorted(names))}) - "
                                          "this can take a few minutes")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            p for p in (self.sagemic_dir, os.environ.get("PYTHONPATH", "")) if p))
        cmd = [sys.executable, os.path.join(self.dir, "sagemic_local.py"), "--config", self.config_path,
               "--test-model", stage_dir]
        try:
            r = subprocess.run(cmd, cwd=self.sagemic_dir, env=env, capture_output=True, text=True,
                               timeout=MODEL_TEST_S, check=False)
        except subprocess.TimeoutExpired as e:
            raise UpdateError("check", f"testing {version} took over {MODEL_TEST_S // 60} min") from e
        out = [l for l in (r.stdout + "\n" + r.stderr).splitlines() if l.strip()]
        if r.returncode:
            err = [l for l in r.stderr.splitlines() if l.strip()]
            raise UpdateError("check", f"{version} does not load: {(err or out or ['no output'])[-1]}")
        about = next((l.split("model OK:", 1)[1].strip() for l in reversed(out) if "model OK:" in l), "")
        meta = {"version": version, "sha256": sha, "size": size, "about": about,
                "files": sorted(names), "received": int(time.time())}
        write_atomic(os.path.join(stage_dir, "model.json"), json.dumps(meta, indent=1).encode())
        folder = os.path.join(MODELS_DIR, version)
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(stage_dir, folder)
        os.remove(part)
        self.progress(rid, "check", True, f"{version} tested: {about}")

    def model_set(self, name):
        write_atomic(os.path.join(MODELS_DIR, "current"), ((name or "standard") + "\n").encode())

    def model_switch(self, rid, version):
        name = "" if version in ("", "standard") else version
        label = name or "standard BirdNET"
        if name and not (MODEL_NAME_RE.match(name) and self.model_meta(name)):
            raise UpdateError("switch", f"{name} is not on this station - send it first")
        os.makedirs(MODELS_DIR, exist_ok=True)
        prev = self.model_current()
        if prev == name:
            self.model_done(rid, True, f"{label} is already in use")
            return
        self.model_set(name)
        if get_value(load_config(self.config_path), "SETTINGS.DETECTION_ENABLED", True) is False:
            self.model_done(rid, True, f"{label} set - detection is paused; it is used when detection resumes")
            return
        since = time.time()
        self.progress(rid, "switch", None, f"restarting the detector with {label}")
        ok, out = helper("restart", "sagemic_local.service")
        if not ok:
            self.model_set(prev)
            raise UpdateError("switch", f"detector not restarted: {out}")
        ok, note = self.model_health(name or "standard", since)
        if ok:
            self.model_prune()
            self.model_done(rid, True, f"{label} in use{note}")
            return
        self.model_set(prev)
        helper("restart", "sagemic_local.service")
        self.model_done(rid, False, f"{label} did not start ({note}) - back to {prev or 'standard BirdNET'}",
                        stage="rollback")

    def model_health(self, want, since):
        """(True, note) once the restarted detector reports the model loaded."""
        deadline = time.time() + MODEL_HEALTH_S
        while time.time() < deadline:
            time.sleep(5)
            try:
                with open(DETECTOR_STATE, "r", encoding="utf-8") as f:
                    st = json.load(f)
            except (OSError, ValueError):
                continue
            if st.get("started", 0) < since or st.get("model_wanted") != want:
                continue                      # still the old detector
            if st.get("model") == want:
                return True, ""
            if st.get("model_error"):
                return False, st["model_error"]
            if st.get("model") is None and (st.get("paused") or not st.get("in_active_hours", True)):
                return True, " (tested; the detector loads it when the active hours start)"
        return False, f"the detector did not report the model within {MODEL_HEALTH_S // 60} min"

    def model_prune(self):
        """Keep the model in use and the newest few others."""
        cur = self.model_current()
        stored = [n for _, n in self.model_stored() if n != cur]
        for n in stored[:max(0, len(stored) - (KEEP_MODELS - (1 if cur else 0)))]:
            shutil.rmtree(os.path.join(MODELS_DIR, n), ignore_errors=True)

    # main loop ---------------------------------------------------------------
    def worker(self):
        if self.state.get("phase") != "idle":
            # restarted by the installer in the middle of an update: carry on
            phase = self.state.get("phase")
            if phase == "rolling_back":   # version is empty for a requested rollback
                what = f"rollback of {self.state.get('version') or 'the last update'}"
            else:
                what = f"{phase} of {self.state.get('version')}"
            print(f"resuming {what}", flush=True)
            if not self.wait_connected(120):
                print("broker not reachable yet - continuing; reports may be lost", flush=True)
            self.busy.set()
            try:
                self.supervise()
            except Exception as e:  # pylint: disable=broad-except
                self.finish(self.state.get("id", ""), False, f"update interrupted: {e}", "rollback")
            finally:
                self.busy.clear()
        while True:
            req = self.jobs.get()
            try:
                self.handle(req)
            except Exception as e:  # pylint: disable=broad-except
                print(f"update error: {e}", flush=True)
                try:
                    self.progress(str(req.get("id", ""))[:64], "error", False, f"error: {e}")
                except Exception:  # pylint: disable=broad-except
                    pass

    def follow_broker(self):
        """BROKER: hub - reconnect to the hub's address once it is known or changes."""
        while True:
            time.sleep(30)
            if not self.client.is_connected():
                addr = broker_address("hub")
                if addr != self.broker:
                    print(f"broker address now {addr} (was {self.broker})", flush=True)
                    self.broker = addr
                    self.client.connect_async(self.broker, self.port)

    def run(self):
        self.client.connect_async(self.broker, self.port)
        self.client.loop_start()
        threading.Thread(target=self.follow_broker, daemon=True).start()
        self.worker()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--self-test", action="store_true",
                   help="load the config and exit (used to check a new package before installing it)")
    a = p.parse_args()
    if a.self_test:
        Updater(a.config)
        print(f"sagemic_update {VERSION} ok")
        return
    Updater(a.config).run()


if __name__ == "__main__":
    main()
