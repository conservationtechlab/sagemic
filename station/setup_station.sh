#!/bin/bash
# Connect a SageMic station Pi to a SageMic hub: its address, time and MQTT all
# come from the hub. Run after SageMic itself is installed (install_agent.sh
# has run once), with the Pi's Ethernet cabled to its HaLow dongle:
#
#   sudo bash ~/sagemic/station/setup_station.sh <station-name>      e.g. canyon_north
#   sudo bash ~/sagemic/station/setup_station.sh <station-name> --mqtt-password=... --update-key=...
#   sudo bash ~/sagemic/station/setup_station.sh --wifi off   no Wi-Fi at power-up (HaLow only); --wifi on undoes it
#
# The station name is how the hub files its clips. Keep the name the station
# already has (MQTT: DEVICE in config.yaml) unless you want a new one; the Pi's
# hostname becomes the same name with "_" written as "-".
# It needs the station MQTT password and the update key. The hub prints this
# command with both filled in (sudo bash setup_hub.sh show): copy it once,
# replace the station name for each station. Without them it asks.
# Safe to run again.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo:  sudo bash ~/sagemic/station/setup_station.sh <station-name>"; exit 1; }
USER_NAME="${SUDO_USER:-pi}"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"      # the station programs (sagemic/station)
SAGEMIC_DIR="$(dirname "$APP_DIR")"           # the SageMic repository: config/, .sagemic/
CONFIG="${CONFIG:-$SAGEMIC_DIR/config/config.yaml}"
# SageMic's Python environment: inside the sagemic folder (the SageMic README),
# or ~/.sagemic on Pis installed the older way
PY="$SAGEMIC_DIR/.sagemic/bin/python"
[[ -x "$PY" ]] || PY="$USER_HOME/.sagemic/bin/python"
MQTT_USER=sagemic-station

ok()   { echo "  OK    $*"; }
warn() { echo "  WARN  $*"; }
fail() { echo "  FAIL  $*"; }
title() { echo; echo "===== $*"; }
as_user() { sudo -u "$USER_NAME" "$@"; }
setcfg() { as_user "$PY" "$APP_DIR/sagemic_agent.py" --config "$CONFIG" --set "$@" >/dev/null; }
getcfg() {   # SECTION.KEY -> value ("" if missing)
  as_user "$PY" -c "import sys,yaml; c=yaml.safe_load(open(sys.argv[1])) or {}; s,k=sys.argv[2].split('.',1); v=(c.get(s) or {}).get(k); print('' if v is None else v)" "$CONFIG" "$1"
}
ask() {   # prompt, default -> answer (default when there is no terminal)
  local a=""
  if [[ -t 0 ]]; then read -r -p "$1" a || a=""; fi
  echo "${a:-${2:-}}"
}
# Wi-Fi chip on/off at power-up (dtoverlay=disable-wifi in the boot settings)
boot_config() { if [[ -f /boot/firmware/config.txt ]]; then echo /boot/firmware/config.txt; else echo /boot/config.txt; fi; }
ssh_iface() {   # interface of an SSH session into this Pi ("" if none)
  local a; a="$(ss -Htn state established '( sport = :22 )' 2>/dev/null | awk '{print $3}' \
                | sed -E 's/^\[(::ffff:)?//; s/\]?:22$//' | head -1)"
  [[ -n "$a" ]] && ip -4 -o addr show | awk -v a="$a" '$4 ~ "^"a"/" {print $2; exit}'
}
wifi_set() {   # off|on
  local cfg; cfg="$(boot_config)"
  if [[ "$1" == off ]]; then
    grep -q '^dtoverlay=disable-wifi' "$cfg" || printf '\n[all]\n# SageMic: no Wi-Fi (set by setup script; remove to turn it back on)\ndtoverlay=disable-wifi\n' >> "$cfg"
    if [[ "$(ssh_iface || true)" == wlan0 ]]; then
      echo "  OK    Wi-Fi is off from the next power-up (left on now: you are connected through it)"
    else
      nmcli radio wifi off 2>/dev/null || true
      echo "  OK    Wi-Fi off now and at every power-up"
    fi
  else
    sed -i '/^# SageMic: no Wi-Fi/d; /^dtoverlay=disable-wifi/d' "$cfg"
    nmcli radio wifi on 2>/dev/null || true
    echo "  OK    Wi-Fi allowed again - restart the Pi to switch the chip back on (sudo reboot)"
  fi
}
eth_profiles() {   # every Ethernet profile that can run on eth0 (active or not)
  local n
  while IFS=: read -r n t; do
    [[ "$t" == 802-3-ethernet ]] || continue
    case "$(nmcli -g connection.interface-name con show "$n" 2>/dev/null)" in eth0|"") echo "$n" ;; esac
  done < <(nmcli -t -f NAME,TYPE con show 2>/dev/null)
}

[[ -f "$CONFIG" ]] || { fail "config not found: $CONFIG"; exit 1; }
[[ -x "$PY" ]] || { fail "SageMic Python not found: $PY - install SageMic first"; exit 1; }
for f in sagemic_agent.py sagemic_update.py install_agent.sh; do
  [[ -f "$APP_DIR/$f" ]] || { fail "missing $f in $APP_DIR"; exit 1; }
done
grep -q 'def broker_address' "$APP_DIR/sagemic_agent.py" || {
  fail "sagemic_agent.py is from an older package - update the station software first (cd ~/sagemic && git pull)"; exit 1; }

# ------------------------------------------------------------------ wifi
if [[ "${1:-}" == --wifi ]]; then
  case "${2:-}" in
    off) title "Wi-Fi off at power-up"
         HUB_OK="$(nmcli -g DHCP4.OPTION device show eth0 2>/dev/null | grep -c dhcp_server_identifier || true)"
         (( HUB_OK > 0 )) || warn "eth0 has no address from the hub - without Wi-Fi this Pi is then only reachable with keyboard and screen"
         wifi_set off ;;
    on)  title "Wi-Fi on"; wifi_set on ;;
    *)   echo "Use:  sudo bash $0 --wifi off   (or: --wifi on)"; exit 1 ;;
  esac
  exit 0
fi

# ------------------------------------------------------------------ setup
NAME="" OPT_PW="" OPT_KEY=""
for arg in "$@"; do
  case "$arg" in
    --mqtt-password=*) OPT_PW="${arg#*=}" ;;
    --update-key=*)    OPT_KEY="${arg#*=}" ;;
    -*) echo "Unknown option: $arg"; exit 1 ;;
    *)  [[ -z "$NAME" ]] || { echo "Give only one station name (got '$NAME' and '$arg')"; exit 1; }
        NAME="$arg" ;;
  esac
done
CUR_DEVICE="$(getcfg MQTT.DEVICE)"
if [[ ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{1,30}$ || "$NAME" == receiver ]]; then
  echo "Usage: sudo bash ~/sagemic/station/setup_station.sh <station-name>"
  echo "  station-name: letters, digits, _ and -, e.g. canyon_north"
  [[ -n "$CUR_DEVICE" ]] && echo "  This station is currently named: $CUR_DEVICE  (use that to keep its clip folder)"
  exit 1
fi
HOST="$(tr 'A-Z_' 'a-z-' <<<"$NAME")"
SSH_IF="$(ssh_iface || true)"

if [[ -n "$CUR_DEVICE" && "$CUR_DEVICE" != "$NAME" ]]; then
  warn "this station is named \"$CUR_DEVICE\" now; it becomes \"$NAME\"."
  echo "        The hub files its clips under the new name from now on."
  [[ "$(ask "  Rename it? [y/N] " n)" == [yY]* ]] || { echo "Stopped. Run again with:  sudo bash ~/sagemic/station/setup_station.sh $CUR_DEVICE"; exit 1; }
fi

title "Station name: $NAME (Pi name $HOST)"
if [[ "$(hostname)" != "$HOST" ]]; then
  hostnamectl set-hostname "$HOST"
  if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOST/" /etc/hosts
  else printf '127.0.1.1\t%s\n' "$HOST" >> /etc/hosts; fi
  systemctl restart avahi-daemon 2>/dev/null || true
  ok "Pi renamed to $HOST"
else
  ok "Pi already named $HOST"
fi

title "Ethernet: address from the hub (DHCP)"
changed=0
while IFS= read -r prof; do
  [[ -n "$prof" ]] || continue
  if [[ "$(nmcli -g ipv4.method con show "$prof")" != auto ]]; then
    nmcli con mod "$prof" ipv4.method auto ipv4.addresses "" ipv4.gateway ""
    changed=1; ok "$prof: fixed address -> automatic"
  fi
  # keep asking for an address until the hub answers (e.g. the hub boots later)
  nmcli con mod "$prof" ipv4.dhcp-timeout infinity
done < <(eth_profiles)
if (( changed )); then
  if [[ "$SSH_IF" == eth0 ]]; then
    warn "you are connected through eth0 - the new address applies at the next restart (asked at the end)"
  else
    active="$(nmcli -g GENERAL.CONNECTION device show eth0 2>/dev/null || true)"
    if [[ -n "$active" ]]; then nmcli con up "$active" >/dev/null 2>&1 || true
    else nmcli dev connect eth0 >/dev/null 2>&1 || true; fi
    sleep 8
  fi
fi
MYIP="$(ip -4 -br addr show eth0 2>/dev/null | awk '{print $3}' | cut -d/ -f1)"
[[ -n "$MYIP" ]] && ok "eth0 address: $MYIP" || warn "eth0 has no address yet (dongle light solid green?)"

title "Finding the hub"
HUB_IP="$(nmcli -g DHCP4.OPTION device show eth0 2>/dev/null | tr '|' '\n' \
          | awk -F= '$1 ~ /dhcp_server_identifier/ {gsub(/ /, "", $2); print $2; exit}' || true)"
if [[ -n "$HUB_IP" ]]; then ok "hub at $HUB_IP (the DHCP server that gave eth0 its address)"
else warn "no address from a hub on eth0 yet - settings are written anyway; run this again once connected"; fi

title "Time from the hub"
if systemctl is-active --quiet chrony 2>/dev/null; then
  mkdir -p /etc/chrony/sources.d
  { [[ -n "$HUB_IP" ]] && echo "server $HUB_IP iburst"; echo "server sagemic-hub.local iburst"; } \
    > /etc/chrony/sources.d/sagemic-hub.sources
  chronyc reload sources >/dev/null 2>&1 || systemctl restart chrony
  ok "chrony uses the hub"
else
  mkdir -p /etc/systemd/timesyncd.conf.d
  printf '# Written by setup_station.sh\n[Time]\nNTP=%s sagemic-hub.local\n' "${HUB_IP:-}" \
    > /etc/systemd/timesyncd.conf.d/sagemic-hub.conf
  timedatectl set-ntp true
  systemctl restart systemd-timesyncd
  ok "systemd-timesyncd uses the hub"
fi

title "SageMic settings (config.yaml, backup config.yaml.bak)"
pw="$(getcfg MQTT.PASSWORD)"
if [[ -n "$OPT_PW" ]]; then
  pw_in="$OPT_PW"; ok "station MQTT password from the command line"
else
  echo "  On the hub,  sudo bash setup_hub.sh show  prints the values asked for here."
  pw_in="$(ask "  Station MQTT password [$([[ -n "$pw" ]] && echo "keep current" || echo none)]: " "")"
fi
[[ -n "$pw_in" ]] && pw="$pw_in"
[[ -n "$pw" ]] || warn "no MQTT password - the hub will refuse this station"
key="$(getcfg UPDATE.KEY)"
if [[ -n "$OPT_KEY" ]]; then
  key_in="$OPT_KEY"; ok "update key from the command line"
else
  key_in="$(ask "  Update key [$([[ ${#key} -ge 16 ]] && echo "keep current" || echo "none - remote updates off")]: " "")"
fi
if [[ -n "$key_in" ]]; then
  [[ "$key_in" =~ ^[A-Za-z0-9_-]{16,}$ ]] && key="$key_in" || warn "update key not changed (16+ letters, digits, - or _)"
fi
setcfg "MQTT.DEVICE=$NAME" "MQTT.BROKER=hub" "MQTT.PORT=1883" "MQTT.USE_TLS=false" \
       "MQTT.BASE_TOPIC=sagemic" "MQTT.USERNAME=$MQTT_USER" "MQTT.PASSWORD=$pw" \
       ${key:+"UPDATE.KEY=$key"}
chmod 600 "$CONFIG" "$CONFIG.bak" 2>/dev/null || true   # it holds the MQTT password now
ok "DEVICE $NAME, BROKER hub (${HUB_IP:-not found yet}):1883, login $MQTT_USER${key:+, update key set}"
# A clip confirmed by the hub may still be the hub's only copy until the hub has
# uploaded it (Starlink down for days): keep it on the station for a week.
keep="$(getcfg CLEANUP.KEEP_SENT_DAYS)"
if [[ -z "$keep" ]] || python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) < 7 else 1)" "$keep" 2>/dev/null; then
  setcfg "CLEANUP.KEEP_SENT_DAYS=7"
  ok "keeps clips 7 days after the hub confirmed them (was ${keep:-1}); free-space rule still applies"
fi

title "Restarting SageMic"
bash "$APP_DIR/install_agent.sh" "$CONFIG"

title "Check"
bad=0
if [[ -n "$HUB_IP" ]] && ping -c2 -W3 "$HUB_IP" >/dev/null 2>&1; then ok "hub answers ping ($HUB_IP)"
else fail "hub not reachable - dongle light solid green? eth0 address: ip -br addr"; bad=1; fi
if [[ -n "$HUB_IP" ]]; then
  if as_user "$PY" - "$HUB_IP" "$NAME" "$HOST" "$MQTT_USER" "$pw" "${CUR_DEVICE:-}" <<'PYEOF'
import json, sys, time
import paho.mqtt.client as mqtt
hub, name, host, user, pw, old = sys.argv[1:7]
seen, state = {}, {"rc": None}
def on_connect(c, u, flags, rc, props):
    state["rc"] = rc
    if not rc.is_failure:
        c.subscribe([("sagemic/_ctl/receiver/hub", 1), (f"sagemic/_ctl/{name}/status", 1)])
def on_message(c, u, m):
    seen[m.topic] = m.payload
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sagemic-check-{host}")
c.username_pw_set(user, pw)
c.on_connect, c.on_message = on_connect, on_message
try:
    c.connect(hub, 1883, 30)
except OSError as e:
    print(f"  FAIL  MQTT: cannot reach {hub}:1883 ({e})"); sys.exit(1)
c.loop_start()
end = time.time() + 10
while state["rc"] is None and time.time() < end:
    time.sleep(0.2)
if state["rc"] is None or state["rc"].is_failure:
    print(f"  FAIL  MQTT: the hub refused the login ({state['rc']}) - check the station MQTT password")
    sys.exit(1)
print("  OK    MQTT login accepted by the hub")
topic = f"sagemic/_ctl/{name}/status"
ok = True
end = time.time() + 30
while time.time() < end:
    st = None
    if topic in seen and seen[topic]:
        try:
            st = json.loads(seen[topic])
        except ValueError:
            st = None
    if seen.get("sagemic/_ctl/receiver/hub") == b"1" and st and st.get("hostname") == host \
            and abs(time.time() - float(st.get("time", 0))) < 600:
        break
    time.sleep(0.5)
if seen.get("sagemic/_ctl/receiver/hub") == b"1":
    print("  OK    hub receiver online - clips will be sent to the hub")
else:
    print("  FAIL  hub receiver not online - is Node-RED running on the hub?"); ok = False
st = None
try:
    st = json.loads(seen.get(topic) or b"null")
except ValueError:
    pass
if st and st.get("hostname") == host and abs(time.time() - float(st.get("time", 0))) < 600:
    print(f"  OK    this station's status reached the hub (agent {st.get('agent_version')})")
elif st and st.get("hostname") != host:
    print(f"  FAIL  another Pi ({st.get('hostname')}) also uses the name {name} - give each station its own name")
    ok = False
else:
    print("  WARN  no status from this station yet - check: journalctl -u sagemic_agent -n 20")
# renamed: clear the old name's retained messages so the hub forgets it
if old and old != name:
    for t in ("status", "online", "update/state"):
        c.publish(f"sagemic/_ctl/{old}/{t}", b"", qos=1, retain=True).wait_for_publish(5)
    print(f"  OK    old name {old} cleared on the hub (remove it from the control page with 'Forget a station' in the Node-RED editor)")
c.loop_stop(); c.disconnect()
sys.exit(0 if ok else 1)
PYEOF
  then :; else bad=1; fi
fi

echo
if [[ $bad == 0 ]]; then
  echo "Station $NAME is connected to the hub. It shows on the hub's control page:"
  echo "  http://sagemic-hub.local:1880/sagemic/control"
else
  echo "Some checks failed - see above. Fix, then run this again."
fi
if (( changed )) && [[ "$SSH_IF" == eth0 ]]; then
  echo "eth0 switches to an address from the hub at the next restart; the station then"
  echo "shows on the hub's control page with its new address."
  [[ "$(ask "Restart now? [y/N] " n)" == [yY]* ]] && { echo "Restarting..."; systemctl reboot; }
fi
if [[ $bad == 0 ]] && nmcli radio wifi 2>/dev/null | grep -q enabled; then
  if [[ "$SSH_IF" == wlan0 ]]; then
    echo "Wi-Fi is still on (you are connected through it). For the field, turn it off with"
    echo "  sudo bash ~/sagemic/station/setup_station.sh --wifi off     (takes effect at the next power-up)"
  elif [[ "$(ask "Turn this Pi's Wi-Fi off, now and at every power-up (HaLow only)? [y/N] " n)" == [yY]* ]]; then
    wifi_set off; echo "        Back on later:  sudo bash ~/sagemic/station/setup_station.sh --wifi on"
  fi
fi
exit $bad
