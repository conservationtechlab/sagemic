#!/bin/bash
# Install / update the SageMic station services for use with a SageMic hub:
# detector (sagemic_local), sender (sagemic_scansend), agent (sagemic_agent)
# and updater (sagemic_update - software and model updates from the hub).
# Run from the sagemic folder:   sudo bash station/install_agent.sh [config/config.yaml]
# Needs MQTT: BROKER: hub in config.yaml.
# The services it writes replace the systemd/ units of the same names.
#
# What it does:
#   - installs the root helper (/usr/local/sbin/sagemic-helper) and a sudo rule
#     for it only (/etc/sudoers.d/sagemic);
#   - writes and starts the services: detector (sagemic_local.py), sender
#     (sagemic_scansend.py, timer every minute), agent and updater;
#   - ends by listing each service as active/failed.
# Safe to run again (software updates from the hub run it too).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo bash station/install_agent.sh"; exit 1; }
USER_NAME="${SUDO_USER:-pi}"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"      # agent, updater, helper (sagemic/station)
SAGEMIC_DIR="$(dirname "$APP_DIR")"           # the sagemic folder: sagemic_local.py,
                                              # sagemic_scansend.py, config/, .sagemic/
CONFIG="$(realpath "${1:-$SAGEMIC_DIR/config/config.yaml}")"
# SageMic's Python environment: inside the sagemic folder (the SageMic README),
# or ~/.sagemic on Pis installed the older way
PY="$SAGEMIC_DIR/.sagemic/bin/python"
[[ -x "$PY" ]] || PY="$USER_HOME/.sagemic/bin/python"

echo "User:    $USER_NAME"
echo "Folder:  $SAGEMIC_DIR"
echo "Config:  $CONFIG"
[[ -f "$CONFIG" ]] || { echo "Config not found: $CONFIG"; exit 1; }
[[ -x "$PY" ]] || { echo "Python environment not found: $PY"; exit 1; }
for f in station/sagemic_agent.py station/sagemic_update.py station/sagemic-helper sagemic_local.py sagemic_scansend.py; do
  [[ -f "$SAGEMIC_DIR/$f" ]] || { echo "Missing $f in $SAGEMIC_DIR"; exit 1; }
done
sudo -u "$USER_NAME" "$PY" -c "import yaml, paho.mqtt.client as m; m.CallbackAPIVersion" 2>/dev/null || {
  echo "paho-mqtt 2.x missing - run:  ${PY%/python}/pip install -U 'paho-mqtt>=2'   (needs internet)"; exit 1; }

getcfg() {   # SECTION.KEY -> value ("" if missing)
  sudo -u "$USER_NAME" "$PY" -c "import sys,yaml; c=yaml.safe_load(open(sys.argv[1])) or {}; s,k=sys.argv[2].split('.',1); v=(c.get(s) or {}).get(k); print('' if v is None else v)" "$CONFIG" "$1"
}
BROKER="$(getcfg MQTT.BROKER)"
if [[ -n "$BROKER" && "${BROKER,,}" != hub ]]; then
  echo "FAIL  MQTT: BROKER is \"$BROKER\" - these services are for a station with a SageMic hub"
  echo "      (BROKER: hub). To send clips straight to the cloud broker, use the systemd/"
  echo "      units instead (systemd/README.md)."
  exit 1
fi
[[ -n "$(getcfg MQTT.USERNAME)" ]] || echo "  WARN  MQTT: USERNAME / PASSWORD not set - the hub's broker will refuse the station"
[[ -n "$(getcfg UPDATE.KEY)" ]] || echo "  WARN  UPDATE: KEY not set - updates from the hub are refused until it is"

echo "== Root helper and sudo rule"
install -o root -g root -m 755 "$APP_DIR/sagemic-helper" /usr/local/sbin/sagemic-helper
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/sagemic-helper" > /etc/sudoers.d/sagemic.tmp
chmod 440 /etc/sudoers.d/sagemic.tmp
visudo -cf /etc/sudoers.d/sagemic.tmp >/dev/null || { rm -f /etc/sudoers.d/sagemic.tmp; echo "sudo rule check failed"; exit 1; }
mv /etc/sudoers.d/sagemic.tmp /etc/sudoers.d/sagemic

echo "== Services"
unit() {   # name, description, type, program (in the sagemic folder), extra
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
ExecStart="$PY" "$SAGEMIC_DIR/$4" --config "$CONFIG"
$5
EOF
  if [[ "$3" == "simple" ]]; then printf '\n[Install]\nWantedBy=multi-user.target\n' >> "/etc/systemd/system/$1"; fi
}
unit sagemic_agent.service    "SageMic station agent (online + clip confirmations)" simple  station/sagemic_agent.py  $'Restart=always\nRestartSec=10'
unit sagemic_local.service    "SageMic Local Audio Listener"                        simple  sagemic_local.py          $'Restart=always\nRestartSec=10'
unit sagemic_scansend.service "SageMic MQTT Scan & Send"                            oneshot sagemic_scansend.py       ""
unit sagemic_update.service   "SageMic update agent (packages sent over MQTT)"      simple  station/sagemic_update.py $'Restart=always\nRestartSec=10'
# A timer started after boot never fires by itself (OnBootSec has passed, and
# OnUnitActiveSec needs an earlier run), so the sender is also run once below.
cat > /etc/systemd/system/sagemic_scansend.timer <<EOF
[Unit]
Description=Run SageMic Scan & Send

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
Unit=sagemic_scansend.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now sagemic_local.service sagemic_scansend.timer sagemic_agent.service sagemic_update.service
systemctl restart sagemic_local.service sagemic_agent.service sagemic_update.service
systemctl start --no-block sagemic_scansend.service   # first run, so the timer keeps going
sleep 3
echo
systemctl is-active sagemic_local sagemic_scansend.timer sagemic_agent sagemic_update \
  | paste -d' ' <(printf '%s\n' sagemic_local sagemic_scansend.timer sagemic_agent sagemic_update) - || true
echo
echo "Done. Logs:  journalctl -u sagemic_update -n 20 --no-pager   (and sagemic_agent, sagemic_local)"
