#!/bin/bash
# Brand-new SageMic station: everything after writing the SD card, in one go.
# Clone SageMic into the Pi's home folder (as the user pi, not with sudo), then:
#
#   git clone -b dev https://github.com/conservationtechlab/sagemic.git ~/sagemic
#   sudo bash ~/sagemic/station/install_station.sh <station-name>
#   sudo bash ~/sagemic/station/install_station.sh <station-name> --mqtt-password=... --update-key=...
#
# The second form is printed by the hub (sudo bash setup_hub.sh show, under
# "New station"): then it also connects the station to the hub at the end,
# without asking for the password and key.
# Other options (asked when missing):
#   --mic=audiomoth|inmp441   --lat=32.7157   --lon=-117.1611   --tz=America/Los_Angeles
#
# What it does (README.md, Option B: Quick install):
#   checks 64-bit OS and Python 3.11, installs the system packages, makes
#   SageMic's Python environment ~/sagemic/.sagemic, turns on the INMP441 (if
#   chosen), writes config/config.yaml, installs the station services
#   (install_agent.sh) and, if the HaLow link to the hub is up, runs
#   setup_station.sh. The services run the programs in ~/sagemic/station.
# Run it on the bench: it needs internet (Wi-Fi) and takes 1-2 hours. If the
# SSH connection drops, run it again: finished steps are quick; a config.yaml
# already connected to a hub is kept. Everything it prints is also in
# ~/sagemic_install.log.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo bash $0 <station-name>"; exit 1; }
USER_NAME="${SUDO_USER:-pi}"
export SUDO_USER="$USER_NAME"   # for install_agent.sh / setup_station.sh
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"      # the station programs (sagemic/station)
SAGEMIC_DIR="$(dirname "$APP_DIR")"           # the SageMic clone: config/, .sagemic/
CONFIG="$SAGEMIC_DIR/config/config.yaml"
VENV="$SAGEMIC_DIR/.sagemic"
REPO=https://github.com/conservationtechlab/sagemic.git
LOG="$USER_HOME/sagemic_install.log"
STATION_FILES="sagemic_local.py sagemic_scansend.py sagemic_cleanup.py sagemic_agent.py
  sagemic_update.py sagemic-helper install_agent.sh setup_station.sh install_station.sh"

ok()   { echo "  OK    $*"; }
warn() { echo "  WARN  $*"; }
fail() { echo "  FAIL  $*"; }
title() { echo; echo "===== $*"; }
as_user() { sudo -u "$USER_NAME" -H "$@"; }
num='^-?[0-9]+(\.[0-9]+)?$'
ask() {   # prompt, default -> answer (Enter = default; the default when there is no terminal)
  local a="" junk
  if [[ -t 0 ]]; then
    while read -r -t 0.1 -n 4096 junk; do :; done   # drop anything typed or sent to the terminal earlier
    read -r -p "$1" a || a=""
  fi
  a="${a//$'\r'/}"; a="${a#"${a%%[![:space:]]*}"}"; a="${a%"${a##*[![:space:]]}"}"   # trim
  echo "${a:-${2:-}}"
}
ask_until() {   # prompt, default, test (a bash function name), hint -> asks again until the test passes
  local a
  while true; do
    a="$(ask "$1" "$2")"
    if "$3" "$a" || [[ ! -t 0 ]]; then echo "$a"; return; fi
    echo "  '$a' is not valid - $4. Press Enter for the default ($2)." >&2
  done
}
is_num()  { [[ "$1" =~ $num ]]; }
is_mic()  { [[ "$1" == 1 || "$1" == 2 ]]; }
is_tz()   { [[ -n "$1" && -f "/usr/share/zoneinfo/$1" ]]; }
getcfg() {   # SECTION.KEY -> value from config.yaml ("" if missing, or before the Python environment exists)
  [[ -x "$VENV/bin/python" && -f "$CONFIG" ]] || { echo ""; return 0; }
  as_user "$VENV/bin/python" -c "import sys,yaml; c=yaml.safe_load(open(sys.argv[1])) or {}; s,k=sys.argv[2].split('.',1); v=(c.get(s) or {}).get(k); print('' if v is None else v)" "$CONFIG" "$1"
}
boot_config() { if [[ -f /boot/firmware/config.txt ]]; then echo /boot/firmware/config.txt; else echo /boot/config.txt; fi; }
boot_line_active() {   # line -> true if it is in config.txt, not commented, outside any [pi5]/[cm4]... filter
  awk -v want="$1" '/^\[/ { sec = tolower($0) } $0 == want && (sec == "" || sec == "[all]") { f = 1 } END { exit !f }' "$(boot_config)"
}

# ------------------------------------------------------------------ options
NAME="" MIC="" LAT="" LON="" TZ_IN="" HUB_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --mqtt-password=*|--update-key=*) HUB_ARGS+=("$arg") ;;
    --mic=*) MIC="${arg#*=}" ;;
    --lat=*) LAT="${arg#*=}" ;;
    --lon=*) LON="${arg#*=}" ;;
    --tz=*)  TZ_IN="${arg#*=}" ;;
    -*) echo "Unknown option: $arg"; exit 1 ;;
    *)  [[ -z "$NAME" ]] || { echo "Give only one station name (got '$NAME' and '$arg')"; exit 1; }
        NAME="$arg" ;;
  esac
done
if [[ ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{1,30}$ || "$NAME" == receiver ]]; then
  echo "Usage: sudo bash $0 <station-name> [--mqtt-password=... --update-key=...]"
  echo "  station-name: letters, digits, _ and -, e.g. canyon_north (how the hub files its clips)"
  exit 1
fi
[[ -z "$MIC" || "$MIC" == audiomoth || "$MIC" == inmp441 ]] || { echo "--mic is audiomoth or inmp441"; exit 1; }
if [[ "$(basename "$APP_DIR")" != station || ! -d "$SAGEMIC_DIR/.git" || ! -f "$SAGEMIC_DIR/pyproject.toml" ]]; then
  echo "Run this from a clone of SageMic (it is its station folder), as the user $USER_NAME:"
  echo "  git clone -b dev $REPO ~/sagemic"
  echo "  sudo bash ~/sagemic/station/install_station.sh $NAME"
  exit 1
fi
for f in $STATION_FILES; do
  [[ -f "$APP_DIR/$f" ]] || { echo "Missing $f in $APP_DIR - the clone is incomplete: cd $SAGEMIC_DIR && git status"; exit 1; }
done
[[ "$(stat -c %U "$SAGEMIC_DIR")" == "$USER_NAME" ]] || {
  echo "$SAGEMIC_DIR belongs to $(stat -c %U "$SAGEMIC_DIR"), not $USER_NAME - clone it without sudo"; exit 1; }

exec > >(tee -a "$LOG") 2>&1
echo; echo "######## SageMic station install $(date '+%F %T')  (log: $LOG)"

# ------------------------------------------------------------------ questions (all at the start, then it runs by itself)
title "Questions (all at the start - then it runs by itself)"
if [[ -z "$MIC" ]]; then
  if grep -qi audiomoth /proc/asound/cards 2>/dev/null; then def=1
  elif grep -qi googlevoi /proc/asound/cards 2>/dev/null || boot_line_active "dtoverlay=googlevoicehat-soundcard"; then def=2
  else def=1; fi
  echo
  echo "  Microphone:  1) AudioMoth (USB, set to 48 kHz)   2) INMP441 (wired to the Pi)"
  [[ "$(ask_until "  Which one? [$def] " "$def" is_mic "type 1 or 2")" == 2 ]] && MIC=inmp441 || MIC=audiomoth
fi

# ------------------------------------------------------------------ station location (asked now, used in step 5)
KEEP_CONFIG=0
if [[ "$(getcfg MQTT.BROKER)" == hub && -n "$(getcfg MQTT.PASSWORD)" ]]; then
  KEEP_CONFIG=1   # a re-run: config.yaml is already connected to a hub and is kept as it is
else
  TZ_DEF="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [[ -z "$TZ_DEF" || "$TZ_DEF" == Etc/UTC ]] && TZ_DEF=America/Los_Angeles
  LAT_DEF=32.7157 LON_DEF=-117.1611
  c="$(getcfg SETTINGS.LATITUDE)";  [[ "$c" =~ $num && "$c" != 0 && "$c" != 0.0 ]] && LAT_DEF="$c"   # a re-run: offer the saved location
  c="$(getcfg SETTINGS.LONGITUDE)"; [[ "$c" =~ $num && "$c" != 0 && "$c" != 0.0 ]] && LON_DEF="$c"
  is_num "$LAT"  || LAT="$(ask_until "  Station latitude  [$LAT_DEF]: " "$LAT_DEF" is_num "use a number, e.g. 32.7157")"
  is_num "$LON"  || LON="$(ask_until "  Station longitude [$LON_DEF]: " "$LON_DEF" is_num "use a number, e.g. -117.1611")"
  is_tz "$TZ_IN" || TZ_IN="$(ask_until "  Time zone [$TZ_DEF]: " "$TZ_DEF" is_tz "use a name like America/Los_Angeles")"
  is_num "$LAT" && is_num "$LON" || { fail "latitude/longitude must be numbers (e.g. 32.7157 -117.1611)"; exit 1; }
  is_tz "$TZ_IN" || { fail "unknown time zone: $TZ_IN (e.g. America/Los_Angeles)"; exit 1; }
  echo "  The rest runs by itself (1-2 hours)."
fi

# ------------------------------------------------------------------ 1. system
title "1. Operating system"
ARCH="$(dpkg --print-architecture)"; BITS="$(getconf LONG_BIT)"
if [[ "$ARCH" == arm64 && "$BITS" == 64 ]]; then ok "64-bit ($ARCH)"
else fail "this is a $BITS-bit system ($ARCH) - BirdNET/TensorFlow needs the 64-bit Raspberry Pi OS. Write the card again (README.md, Installation step 1)"; exit 1; fi
PYBASE=""
for p in python3.11 python3; do
  if command -v "$p" >/dev/null && [[ "$("$p" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" == 3.11 ]]; then
    PYBASE="$(command -v "$p")"; break
  fi
done
if [[ -n "$PYBASE" ]]; then ok "Python 3.11 ($PYBASE)"
else fail "Python 3.11 not found (this OS has $(python3 --version 2>&1)). Use Bookworm, or build Python 3.11 first (SageMic README, step 3)"; exit 1; fi
if getent hosts github.com >/dev/null && curl -fsS -m 15 -o /dev/null https://github.com; then ok "internet"
else fail "no internet - connect the Pi to Wi-Fi with internet first"; exit 1; fi

# ------------------------------------------------------------------ 2. packages
title "2. System packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3-venv python3-pip libportaudio2 libsndfile1 alsa-utils tmux uhubctl
ok "packages installed"

REBOOT=0
if [[ "$MIC" == inmp441 ]]; then
  title "INMP441 microphone (I2S in $(boot_config))"
  cfg="$(boot_config)"
  for line in dtparam=i2s=on dtoverlay=googlevoicehat-soundcard; do
    if boot_line_active "$line"; then ok "$line already on"
    else
      grep -q '^\[all\]' "$cfg" && [[ "$(grep '^\[' "$cfg" | tail -1)" == "[all]" ]] || printf '\n[all]\n' >> "$cfg"
      echo "$line" >> "$cfg"; REBOOT=1; ok "$line added"
    fi
  done
  AUDIO_DEVICE="snd_rpi_googlevoicehat_soundcar"; AUDIO_DTYPE=int32
else
  AUDIO_DEVICE="48kHz AudioMoth USB Microphone"; AUDIO_DTYPE=int16
fi

# ------------------------------------------------------------------ 3. SageMic + BirdNET
title "3. SageMic and BirdNET ($SAGEMIC_DIR, environment $VENV)"
ok "SageMic clone: $SAGEMIC_DIR (branch $(as_user git -C "$SAGEMIC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown), $(as_user git -C "$SAGEMIC_DIR" log -1 --format='%h %cs' 2>/dev/null || echo '?'))"
[[ -x "$VENV/bin/python" ]] || { as_user "$PYBASE" -m venv "$VENV"; ok "environment created"; }
[[ -d "$USER_HOME/.sagemic" ]] && warn "$USER_HOME/.sagemic is a second environment (made in the home folder by mistake?) - not used; remove it:  rm -r ~/.sagemic"
echo "  installing the Python packages (TensorFlow is large: this is the long part) ..."
as_user bash -c "cd '$SAGEMIC_DIR' && '$VENV/bin/pip' install --upgrade pip setuptools wheel \
  && '$VENV/bin/pip' install -e . && '$VENV/bin/pip' install -U 'paho-mqtt>=2'"
as_user "$VENV/bin/python" -c "import sounddevice, birdnetlib, yaml, paho.mqtt.client as m; m.CallbackAPIVersion" \
  && ok "BirdNET, sounddevice and paho-mqtt import" || { fail "the Python packages did not install - see the pip output above"; exit 1; }

# ------------------------------------------------------------------ 4. microphone check
title "4. Microphone"
DEVICES="$(as_user "$VENV/bin/python" -m sounddevice 2>&1 || true)"
echo "$DEVICES" | sed 's/^/        /'
if [[ "$MIC" == audiomoth ]]; then
  found="$(echo "$DEVICES" | grep -i audiomoth | head -1 | sed -E 's/^[ >*<]*[0-9]+ +//; s/:.*//' || true)"
  if [[ -n "$found" ]]; then AUDIO_DEVICE="$found"; ok "AudioMoth found: $found"
  else warn "no AudioMoth in the list - plug it in (set to 48 kHz); config.yaml gets \"$AUDIO_DEVICE\""; fi
  [[ -z "$found" || "$found" == 48kHz* ]] || warn "the AudioMoth is not at 48 kHz - set it to 48 kHz and fix AUDIO_DEVICE in config.yaml"
elif echo "$DEVICES" | grep -q googlevoicehat; then ok "INMP441 found (googlevoicehat)"
elif (( REBOOT )); then ok "the INMP441 shows up after the restart at the end"
else warn "no googlevoicehat device - check the wiring and $(boot_config) (README.md, INMP441 Setup)"; fi

# ------------------------------------------------------------------ 5. config.yaml
title "5. config.yaml"
if (( KEEP_CONFIG )) || [[ "$(getcfg MQTT.BROKER)" == hub && -n "$(getcfg MQTT.PASSWORD)" ]]; then
  ok "$CONFIG is already set up for a hub (station $(getcfg MQTT.DEVICE)) - kept"
else
  if [[ -f "$CONFIG" ]]; then
    # the config.yaml that comes with SageMic: set the station's values in it
    as_user "$VENV/bin/python" "$APP_DIR/sagemic_agent.py" --config "$CONFIG" --set \
      "PATHS.BASE_PATH=$USER_HOME/sagemic_clips" "SETTINGS.LATITUDE=$LAT" "SETTINGS.LONGITUDE=$LON" \
      "SETTINGS.LOCAL_TZ=$TZ_IN" "SETTINGS.SAMPLERATE=48000" "SETTINGS.BLOCK_DURATION=3" \
      "SETTINGS.AUDIO_DEVICE=$AUDIO_DEVICE" "SETTINGS.AUDIO_DTYPE=$AUDIO_DTYPE" \
      "MQTT.DEVICE=$NAME" "MQTT.BROKER=hub" "MQTT.PORT=1883" "MQTT.BASE_TOPIC=sagemic" "MQTT.USE_TLS=false" >/dev/null \
      || { fail "could not change $CONFIG - correct it by hand (README.md, B1)"; exit 1; }
    [[ -n "$(getcfg SETTINGS.CONFIDENCE_THRESHOLD)" ]] || \
      as_user "$VENV/bin/python" "$APP_DIR/sagemic_agent.py" --config "$CONFIG" --set "SETTINGS.CONFIDENCE_THRESHOLD=0.5" >/dev/null
    grep -qiE 'PATH_TO|\.(crt|key|pem)' "$CONFIG" && \
      warn "config.yaml keeps its certificate lines (used by the original SageMic, option A) - not used by the station"
    chmod 600 "$CONFIG"
    ok "SageMic's config.yaml updated (before: config.yaml.bak): station $NAME, $LAT $LON, $TZ_IN, microphone \"$AUDIO_DEVICE\" ($AUDIO_DTYPE)"
  else
  as_user mkdir -p "$SAGEMIC_DIR/config"
  as_user tee "$CONFIG" >/dev/null <<EOF
PATHS:
  BASE_PATH: "$USER_HOME/sagemic_clips"   # clips are saved here
SETTINGS:
  LATITUDE: $LAT
  LONGITUDE: $LON
  LOCAL_TZ: "$TZ_IN"
  SAMPLERATE: 48000
  BLOCK_DURATION: 3
  CONFIDENCE_THRESHOLD: 0.5
  AUDIO_DEVICE: "$AUDIO_DEVICE"
  AUDIO_DTYPE: "$AUDIO_DTYPE"
MQTT:
  DEVICE: $NAME
  BROKER: hub
  PORT: 1883
  BASE_TOPIC: sagemic
EOF
  chmod 600 "$CONFIG"
  ok "written: station $NAME, $LAT $LON, $TZ_IN, microphone \"$AUDIO_DEVICE\" ($AUDIO_DTYPE)"
  fi
fi

# ------------------------------------------------------------------ 6. services
title "6. Station services (install_agent.sh)"
bash "$APP_DIR/install_agent.sh" "$CONFIG"

# ------------------------------------------------------------------ 7. hub
title "7. Hub"
HUB_IP="$(nmcli -g DHCP4.OPTION device show eth0 2>/dev/null | tr '|' '\n' \
          | awk -F= '$1 ~ /dhcp_server_identifier/ {gsub(/ /, "", $2); print $2; exit}' || true)"
NEXT="sudo bash $APP_DIR/setup_station.sh $NAME"
if [[ -n "$HUB_IP" ]]; then
  ok "hub found on eth0 ($HUB_IP) - connecting the station"
  bash "$APP_DIR/setup_station.sh" "$NAME" "${HUB_ARGS[@]}" || warn "setup_station.sh reported a problem - fix it, then:  $NEXT"
else
  echo "  Not connected to the hub"   # README.md, Quick install step 4: connecting it
fi

echo
echo "Install finished. Log: $LOG"
if (( REBOOT )); then
  echo "The INMP441 needs a restart to be turned on."
  if [[ ! -t 0 ]]; then   # started by SageMic_Setup on the PC: it restarts the Pi once it has the result
    echo "Restart it with:  sudo reboot"; echo "SAGEMIC_REBOOT_NEEDED=1"
  else
    [[ "$(ask "Restart now? [Y/n] " y)" == [nN]* ]] && echo "Restart later with:  sudo reboot" || { sleep 2; reboot; }
  fi
fi
