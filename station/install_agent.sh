#!/bin/bash
# Install / update the SageMic station software (detector, sender, cleanup, and the
# agent for status, settings and control from the SageMic hub).
# Run from the sagemic folder:   sudo bash station/install_agent.sh [config/config.yaml]
# Needs MQTT: BROKER: hub in config.yaml (the station works with a SageMic hub only).
# The services it writes replace the GitHub systemd/ units of the same names.
#
# What it does:
#   - installs the root helper (/usr/local/sbin/sagemic-helper) and a sudo rule
#     for it only (/etc/sudoers.d/sagemic);
#   - adds any missing station settings to config.yaml (backup: config.yaml.bak);
#   - the station runs no MQTT broker of its own - if Mosquitto is installed it
#     is stopped and disabled;
#   - with SETTINGS: USB_OFF_OUTSIDE_ACTIVE_HOURS: true, installs uhubctl if missing;
#   - the detector's service switches USB power on again whenever it stops
#     (ExecStopPost, hub(s) from SETTINGS: USB_POWER_LOCATION);
#   - writes and starts the services: detector (sagemic_local), sender
#     (sagemic_scansend, timer every minute), cleanup (sagemic_cleanup, timer
#     every hour), agent (sagemic_agent) and updater (sagemic_update);
#   - caps the system log (journal) at 200 MB;
#   - ends by listing each service as active/failed.
# Safe to run again (software updates from the hub run it too).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo bash station/install_agent.sh"; exit 1; }
USER_NAME="${SUDO_USER:-pi}"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"      # the station programs (sagemic/station)
SAGEMIC_DIR="$(dirname "$APP_DIR")"           # the SageMic repository: config/, .sagemic/
CONFIG="$(realpath "${1:-$SAGEMIC_DIR/config/config.yaml}")"
# SageMic's Python environment: inside the sagemic folder (the SageMic README),
# or ~/.sagemic on Pis installed the older way
PY="$SAGEMIC_DIR/.sagemic/bin/python"
[[ -x "$PY" ]] || PY="$USER_HOME/.sagemic/bin/python"

echo "User:    $USER_NAME"
echo "Folder:  $APP_DIR"
echo "Config:  $CONFIG"
[[ -f "$CONFIG" ]] || { echo "Config not found: $CONFIG"; exit 1; }
[[ -x "$PY" ]] || { echo "Python environment not found: $PY"; exit 1; }
for f in sagemic_agent.py sagemic_update.py sagemic-helper sagemic_local.py sagemic_scansend.py sagemic_cleanup.py; do
  [[ -f "$APP_DIR/$f" ]] || { echo "Missing $f in $APP_DIR"; exit 1; }
done
sudo -u "$USER_NAME" "$PY" -c "import yaml, paho.mqtt.client as m; m.CallbackAPIVersion" 2>/dev/null || {
  echo "paho-mqtt 2.x missing - run:  ${PY%/python}/pip install -U 'paho-mqtt>=2'   (needs internet)"; exit 1; }

echo "== Root helper and sudo rule"
install -o root -g root -m 755 "$APP_DIR/sagemic-helper" /usr/local/sbin/sagemic-helper
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/sagemic-helper" > /etc/sudoers.d/sagemic.tmp
chmod 440 /etc/sudoers.d/sagemic.tmp
visudo -cf /etc/sudoers.d/sagemic.tmp >/dev/null || { rm -f /etc/sudoers.d/sagemic.tmp; echo "sudo rule check failed"; exit 1; }
mv /etc/sudoers.d/sagemic.tmp /etc/sudoers.d/sagemic

echo "== Adding any missing settings to config.yaml (backup: config.yaml.bak)"
sudo -u "$USER_NAME" "$PY" "$APP_DIR/sagemic_agent.py" --config "$CONFIG" --init-config

getcfg() {   # SECTION.KEY -> value ("" if missing)
  sudo -u "$USER_NAME" "$PY" -c "import sys,yaml; c=yaml.safe_load(open(sys.argv[1])) or {}; s,k=sys.argv[2].split('.',1); v=(c.get(s) or {}).get(k); print('' if v is None else v)" "$CONFIG" "$1"
}
BROKER="$(getcfg MQTT.BROKER)"
if [[ -z "$BROKER" || "${BROKER,,}" == hub ]]; then
  echo "== Mode: hub (MQTT to the SageMic hub over HaLow)"
  echo "== Station MQTT broker off (the station uses the hub's broker)"
  # The station runs no MQTT broker of its own. If Mosquitto is installed (it
  # listened on the HaLow network), stop it and remove its listener file.
  rm -f /etc/mosquitto/conf.d/halow.conf /etc/mosquitto/conf.d/halow.conf.bak
  if systemctl list-unit-files mosquitto.service >/dev/null 2>&1; then
    systemctl disable --now mosquitto >/dev/null 2>&1 || true
    echo "  mosquitto stopped and disabled"
  fi
else
  echo "FAIL  MQTT: BROKER is \"$BROKER\" - the SageMic Station works with a SageMic hub only"
  echo "      (BROKER: hub). To send clips straight to the cloud broker, use the original"
  echo "      SageMic (option A in README.md)."
  exit 1
fi

if [[ "$(getcfg SETTINGS.USB_OFF_OUTSIDE_ACTIVE_HOURS)" == True ]]; then
  echo "== USB power switching (SETTINGS: USB_OFF_OUTSIDE_ACTIVE_HOURS: true)"
  if command -v uhubctl >/dev/null; then echo "  uhubctl installed"
  elif DEBIAN_FRONTEND=noninteractive apt-get install -y uhubctl >/dev/null 2>&1; then echo "  uhubctl installed now"
  else echo "  uhubctl could not be installed (needs internet once: sudo apt install uhubctl) - USB stays on"; fi
fi
# USB power back on whenever the detector stops or crashes (sagemic-helper
# usb-power on; empty location = all the ports of a Pi 4 or Pi 5). A failure there is ignored (-).
USB_LOC="$(getcfg SETTINGS.USB_POWER_LOCATION)"
if [[ -n "$USB_LOC" && ! "$USB_LOC" =~ ^[0-9]+(-[0-9]+(\.[0-9]+)*)?(,[0-9]+(-[0-9]+(\.[0-9]+)*)?)*$ ]]; then
  echo "  WARN  SETTINGS: USB_POWER_LOCATION \"$USB_LOC\" is not a hub location (e.g. 1-1 or 1,3) - not used"
  USB_LOC=""
fi
USB_ON="ExecStopPost=-/usr/bin/sudo -n /usr/local/sbin/sagemic-helper usb-power on${USB_LOC:+ $USB_LOC}"

echo "== Services"
unit() {   # name, description, type, exec, extra
  cat > "/etc/systemd/system/$1" <<EOF
[Unit]
Description=$2
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=$3
User=$USER_NAME
WorkingDirectory=$SAGEMIC_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart="$PY" "$APP_DIR/$4" --config "$CONFIG"
$5
EOF
  if [[ "$3" == "simple" ]]; then printf '\n[Install]\nWantedBy=multi-user.target\n' >> "/etc/systemd/system/$1"; fi
}
unit sagemic_agent.service    "SageMic station agent (status + remote control)" simple  sagemic_agent.py    $'Restart=always\nRestartSec=10'
unit sagemic_local.service    "SageMic Local Audio Listener"                     simple  sagemic_local.py    $'Restart=always\nRestartSec=10\n'"$USB_ON"
unit sagemic_scansend.service "SageMic MQTT Scan & Send"                         oneshot sagemic_scansend.py ""
unit sagemic_cleanup.service  "SageMic delete sent clips"                        oneshot sagemic_cleanup.py  ""
unit sagemic_update.service   "SageMic update agent (packages sent over MQTT)"   simple  sagemic_update.py   $'Restart=always\nRestartSec=10'
# A timer started after boot never fires by itself (OnBootSec has passed, and
# OnUnitActiveSec needs an earlier run), so the services are also run once below.
# The interval set from the hub is kept in a drop-in (sagemic-helper set-interval).
timer() {  # name, description, interval
  cat > "/etc/systemd/system/$1.timer" <<EOF
[Unit]
Description=$2

[Timer]
OnBootSec=2min
OnUnitActiveSec=$3
Unit=$1.service

[Install]
WantedBy=timers.target
EOF
}
timer sagemic_scansend "Run SageMic Scan & Send" 1min
timer sagemic_cleanup  "Run SageMic cleanup"     1h
# An interval set from the hub (drop-in interval.conf) written by an older version reset
# the timer's OnBootSec as well, so the timer never started after a boot: rewrite it.
for t in sagemic_scansend sagemic_cleanup; do
  f="/etc/systemd/system/$t.timer.d/interval.conf"
  secs=$(sed -n 's/^OnUnitActiveSec=\([0-9][0-9]*\)s$/\1/p' "$f" 2>/dev/null | tail -1)
  if [[ -n "$secs" ]] && ! grep -q '^OnBootSec=' "$f"; then
    printf '[Timer]\nOnUnitActiveSec=\nOnBootSec=2min\nOnUnitActiveSec=%ss\n' "$secs" > "$f"
    echo "  $t: interval ${secs}s kept, start after boot restored"
  fi
done

# Journal size cap (FMEA S-28)
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/sagemic.conf
systemctl restart systemd-journald

systemctl daemon-reload
systemctl enable --now sagemic_local.service sagemic_scansend.timer sagemic_cleanup.timer sagemic_agent.service sagemic_update.service
systemctl restart sagemic_local.service sagemic_agent.service sagemic_update.service
systemctl start --no-block sagemic_scansend.service sagemic_cleanup.service   # first run, so the timers keep going
sleep 3
echo
systemctl is-active sagemic_local sagemic_scansend.timer sagemic_cleanup.timer sagemic_agent sagemic_update \
  | paste -d' ' <(printf '%s\n' sagemic_local sagemic_scansend.timer sagemic_cleanup.timer sagemic_agent sagemic_update) - || true
echo
echo "Done. Agent log:  journalctl -u sagemic_agent -n 20 --no-pager"
