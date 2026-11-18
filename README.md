# sagemic
Real-time detection and classification of bioacoustic events on field devices built around microphone-enabled single-board computers, with capabilities to do inference on-device and via streaming data to a remote server.

SageMic is an acoustic inference device that runs BirdNET continuously on a Raspberry Pi, saves each
detection as a short FLAC clip and sends the clips through an MQTT broker.

Can use different microphones, but has been tested with an AudioMoth set to 48000 Hz, or an INMP441 mic (additional setup instructions below).

## Ways to run SageMic

| Option | What it is | Sends clips to | Use it for |
|---|---|---|---|
| **A. SageMic** | The SageMic programs in the top folder of this repo (`sagemic_local.py`, `sagemic_scansend.py`) with the services in `systemd/` | A cloud broker over MQTTS (certificates) | A single Pi with internet |
| **B. SageMic Station** | The programs in `station/`, connected to a SageMic hub | The site's SageMic hub over HaLow (plain MQTT with a station login); the hub uploads them to the cloud | Field sites: the hub confirms every clip, and stations are managed and updated from the hub |

**What B adds to A:** the hub confirms every clip it saves (unconfirmed clips are resent,
confirmed clips are deleted after 7 days), the station gets its address and time from the hub over
HaLow, and status, settings, commands, software updates and the model (the standard BirdNET model, a
species list, or a custom classifier) come from the hub's control page. The
station programs also add active hours and pause/resume, BirdNET in its own thread (the microphone
never stalls), a free-space limit, a mic-level heartbeat, and USB power off outside the active hours
(switches the HaLow dongle off to save power).

Option A starts with the [Installation](#installation) steps. Option B has its own installation in
[Option B: SageMic Station](#option-b-sagemic-station).

| | A. SageMic | B. SageMic Station |
|---|---|---|
| Programs | top folder | `station/` |
| `MQTT: BROKER` | cloud broker address | `hub` |
| Connection | MQTTS with certificates | plain MQTT on HaLow, station login |
| Services | `systemd/` (copied by hand) | written by `station/install_agent.sh` |
| Needs a SageMic hub | no | yes, set up first |

## Hardware
- Raspberry Pi 4B (4 GB or more) and its power supply
- microSD card, 32 GB or more (high-endurance recommended)
- Microphone: AudioMoth (USB, set to 48 kHz, see [AudioMoth Setup](#audiomoth-setup)) or INMP441 (wired to the Pi, see [INMP441 Setup](#inmp441-setup))
- Internet for the installation (Wi-Fi: office, phone hotspot or Starlink). 
- Option B only: a Heltec HT-HD01 HaLow dongle and an Ethernet cable, and a SageMic hub ([Hub Setup](#hub-setup))

## Installation
These steps are for option A, and for option B's
[Step by step](#step-by-step-instead-of-quick-install) installation. Option B's
[Quick install](#quick-install) automates these steps.

#### 1. Flash the OS (64-bit is required)
Flash **Raspberry Pi OS (64-bit) Bookworm** for a Raspberry Pi 4B. In Raspberry Pi Imager, Bookworm
is listed under "Raspberry Pi OS (other)" as a Legacy option, since Trixie is now the default.
Desktop or Lite both work; Lite uses less RAM and power and is a good fit for a headless field device.
In Imager's settings set a hostname (anything for now, e.g. sagemic - option B renames it later), the user `pi`
with a new password, a Wi-Fi network with internet, your time zone, and enable SSH. Then put the
card in the Pi, power it up, wait 2 minutes and connect: `ssh pi@<hostname>.local`

**The 32-bit OS will not work.** TensorFlow only publishes Linux ARM wheels for 64-bit (aarch64),
and the MediaMTX build used for Birbler is arm64-only.

Trixie and Pi 5 are also compatible, but Trixie requires 1 extra step (see step 3).

Place the SD Card into a Pi 4B and turn on power.
Connect the pi to a keyboard, mouse and monitor, or from a computer connected to the same wi-fi
used in the pi imager program.

To connect from a computer (e.g. `ssh pi@sagemic.local`):
Note: if you get a WARNING message, use the command `ssh-keygen -R sagemic.local` to remove previous keys.

```
ssh pi@<host-name>.local
```
if it asks to continue, select yes.

Confirm the OS is 64-bit. 
```
dpkg --print-architecture   # must print: arm64
getconf LONG_BIT            # must print: 64
python3 --version           # Bookworm: 3.11.x
```

#### 2. Install system packages
Install these **before** the Python packages. `sounddevice` does not bundle PortAudio on Linux; it
loads the system `libportaudio2` when imported, and without it every script fails with
`OSError: PortAudio library not found`.
```
sudo apt update
sudo apt install -y git python3-venv python3-pip libportaudio2 libsndfile1 alsa-utils
```
- `libportaudio2`: audio capture backend for `sounddevice`
- `libsndfile1`: backend for `soundfile`, used to write .flac clips
- `alsa-utils`: provides `arecord`/`aplay` for testing microphones

#### 3. (Trixie only) Build Python 3.11
If using Bookworm, skip to step 4.

If using Trixie, Python3.11 needs to be
installed because Tensorflow is not yet fully compatible with Python3.13, which is what Trixie has.

To create a non-replacing install of Python3.11, run these commands to build an alternate install of Python3.11 we can use in our venv.

```
su - root
apt install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev libxml2-dev libxslt1-dev
cd ~ && wget https://www.python.org/ftp/python/3.11.13/Python-3.11.13.tar.xz
tar xvf Python-3.11.13.tar.xz && cd Python-3.11.13
./configure --enable-optimizations --with-ensurepip=install
make -j $(nproc) && make altinstall
exit
```

#### 4. Clone the repo and create the virtual environment
Clone first (as `pi`, not with sudo), then create the `.sagemic` virtual environment **inside the
repo**. The services expect the environment at `~/sagemic/.sagemic/bin/python`.

```
cd ~
git clone -b dev https://github.com/conservationtechlab/sagemic.git
cd ~/sagemic
```

If using Bookworm, you can just use python3:
```
python3 -m venv .sagemic
```

If using Trixie, and you did the above altinstall, use python3.11:
```
python3.11 -m venv .sagemic
```

Bookworm and Trixie instructions converge here:

```
source .sagemic/bin/activate        # your prompt should now start with (.sagemic)
which python                        # must print /home/pi/sagemic/.sagemic/bin/python
pip install --upgrade pip setuptools wheel
pip install -e .
```
TensorFlow is a large download, so this step can take a while on a Pi.
If `which python` prints `/home/pi/.sagemic/...`, the environment was made in the home folder by
mistake: `deactivate`, `rm -r ~/.sagemic`, and create it again inside `~/sagemic`.

#### 5. Verify sounddevice / PortAudio
**Connect the microphone first:**
- AudioMoth: plug it into a USB port. Update the config.yaml file, see [AudioMoth Setup](#audiomoth-setup).
- INMP441 (I2S): it does not appear until I2S is enabled in `/boot/firmware/config.txt` and the
  Pi is rebooted. Complete the [INMP441 Setup](#inmp441-setup) section now if you have not already.
  The INMP441 setup also modifies the config.yaml file.

Activate the .sagemic environment and confirm that PortAudio loads and your microphone is listed:
```
cd ~/sagemic && source .sagemic/bin/activate
python -c "import sounddevice as sd; print(sd.get_portaudio_version())"
python -m sounddevice
```
#### 6. Test with run_birdnet_on_microphone_stream.py
The test script must be run with the `.sagemic` environment's Python, otherwise imports such as
`birdnetlib` and `sounddevice` will fail. Activate the environment first:
```
cd ~/sagemic
source .sagemic/bin/activate        # prompt should show (.sagemic)
cd feature_tests
python run_birdnet_on_microphone_stream.py
```
Or call the environment's Python directly without activating:
```
~/sagemic/.sagemic/bin/python ~/sagemic/feature_tests/run_birdnet_on_microphone_stream.py
```
The script lists audio devices and asks which to use; pick the AudioMoth or `googlevoicehat` device.
For the INMP441, selecting the default sound device may also work.
Press Ctrl+C to stop, and run `deactivate` to leave the environment. If unsure which Python is
active, `which python` should point inside `~/sagemic/.sagemic/bin`.

Notes:
- The latitude/longitude in this script are hardcoded to San Diego. Edit `LATITUDE`/`LONGITUDE` at the top of the script if you are elsewhere.
- ALSA only allows one program to use the microphone at a time. If `sagemic_local.service` is already
  running, stop it first or the test will fail with errors like `Invalid number of channels`:
  `sudo systemctl stop sagemic_local.service`

Then continue with your option: [A](#option-a-sagemic) or [B](#option-b-sagemic-station).

## Option A: SageMic

#### 7. Run continuously on boot
### Scan Send
See the readme in /systemd for instructions to enable the scansend service that will periodically check for new audio clips and transmit them over
MQTTS. See this repo [SageBRUSH-Xylem](https://github.com/conservationtechlab/sagebrush-xylem) for instructions on how to set up the MQTT Broker service and generate new keys needed to paste in the config file to allow for secure transmission
of audio clips.

Edit config.yaml with your paths, location, and broker (audio edits were done previously):
```
cp config/example_config.yaml config/config.yaml
nano config/config.yaml
```
Then follow the instructions inside /systemd to have sagemic_local.service
run the sagemic software continuously on boots.

## Option B: SageMic Station
The station sends its clips over HaLow to the site's SageMic hub. The hub gives the station its
address and time, runs the MQTT broker (Mosquitto) and Node-RED, which saves and confirms every
clip, uploads it to the cloud and shows the station on its control page. Status, settings, commands
and software updates all come from the hub.

Checking and looking after a station once it is installed:
[docs/STATION_OPERATIONS.md](docs/STATION_OPERATIONS.md).

### Hub Setup
The site's SageMic hub and its HaLow gate are set up by following the hub's own documents in the
SageMic hub repository (`<hub repository address>`):

The hub installation makes two keys that the station needs in its last step, connecting to the
hub: the station MQTT password and the update key. They were saved when the hub was installed.
See the hub documentation for retrieving the keys. 

### Microphone setup
Connect the microphone before the install - the install looks for it.
- AudioMoth: set it to 48 kHz and plug it into a USB port.
- INMP441: See ([INMP441 Setup](#inmp441-setup), wiring only). Config.txt is updated automatically 
  with the installation.

### Flash an SD card
Flash **Raspberry Pi OS (64-bit) Bookworm** (Quick install needs Bookworm), put the card in the Pi,
power it up and connect over SSH. The Imager settings and the 64-bit check are in
[Installation step 1](#1-flash-the-os-64-bit-is-required).

**Next, use the [Quick install](#quick-install) or [Step by step](#step-by-step-instead-of-quick-install)
instructions for the installation.**
Quick Install uses a script to run each command.  Step by step allows a user to run each individually.

### Quick install
One command, `station/install_station.sh`. It automates
[Installation](#installation) steps 2, 4 and 5 (system packages, Python environment, microphone
check), writes config.yaml, installs the station services and connects the station to the hub. It does not
build Python 3.11 on Trixie (step 3 - use Bookworm, or do step 3 first) and does not run the
BirdNET test (step 6).

You need: the microphone
connected ([Microphone setup](#microphone-setup)), the SD card flashed and the Pi reachable over SSH
([Flash an SD card](#flash-an-sd-card)) and internet on the station (Wi-Fi).

1. Get SageMic from GitHub. From a terminal on the computer (such as PowerShell), SSH into the
   Pi, using the hostname set in Raspberry Pi Imager (e.g. `sagemic-station`):
   ```
   ssh pi@sagemic-station.local
   ```
   (If it refuses with "REMOTE HOST IDENTIFICATION HAS CHANGED" - the computer remembers an
   earlier card with that name - run `ssh-keygen -R sagemic-station.local` on the computer
   first.) Then, on the Pi:
   ```
   sudo apt update && sudo apt install -y git
   git clone -b dev https://github.com/conservationtechlab/sagemic.git ~/sagemic
   ```
2. Start the install, with the station's name in place of `<station-name>`:
   ```
   sudo bash ~/sagemic/station/install_station.sh <station-name>
   ```
   `<station-name>` is the station's name, e.g. `canyon_north` (letters, digits, `_` and `-`). It is
   saved in `config.yaml` as `MQTT: DEVICE`, and the hub files the station's clips under it.
3. Answer its questions, all asked at the start: the microphone (1 AudioMoth, 2 INMP441), the
   station's latitude, longitude and time zone. Enter keeps the value shown in `[ ]`; a wrong
   answer is asked again. (They can also be given on the command line: `--mic=audiomoth` or
   `--mic=inmp441`, `--lat=32.7157 --lon=-117.1611 --tz=America/Los_Angeles`.)
   Then it runs by itself, step by step:
   1. checks the OS (64-bit, Python 3.11) and the internet connection;
   2. installs the system packages (also uhubctl, and tmux for later maintenance over SSH);
   3. creates the Python environment `~/sagemic/.sagemic` and installs BirdNET and TensorFlow
      (the long part);
   4. lists the audio devices and checks that the microphone is there;
   5. writes `~/sagemic/config/config.yaml` (station name, location, time zone, microphone,
      `BROKER: hub`);
   6. installs and starts the station services (`station/install_agent.sh`);pi
   

   If the SSH connection drops, the install stops: SSH in again and run the same command -
   finished steps are quick. Everything it prints is also in `~/sagemic_install.log`
   (`less ~/sagemic_install.log` to read it).

   Reboot the Pi with `sudo reboot` to initialize the microphone.
4. **Connect the station to the hub.** 
   - Set up the station's dongle ([HaLow dongle](#halow-dongle)) and cable it to the Pi's
     Ethernet port.
   - The hub must be set up, with its HaLow gate ([Hub Setup](#hub-setup)).
   - On the station, put the station MQTT password and update key from the hub
     ([Hub Setup](#hub-setup)) into the command below:
     ```
     sudo bash ~/sagemic/station/setup_station.sh <station-name> --mqtt-password=<password> --update-key=<key>
     ```
     To confirm the connection, verify that the station is shown on the hub's control page
     (`http://sagemic-hub.local:1880/sagemic/control`).

Then follow [Before the station goes to the field](docs/STATION_OPERATIONS.md#before-the-station-goes-to-the-field-option-b).

### Step by step (instead of Quick install)
First do steps 1-6 of [Installation](#installation) (flash the OS, system packages, Python 3.11 on
Trixie only, clone and environment, microphone check, BirdNET test), then B1-B3 below.

#### B1. config.yaml
Create the station's settings file in the nano editor (as `pi`, without sudo, so the file
belongs to `pi`):
```
nano ~/sagemic/config/config.yaml
```
Paste these lines into it and change the ones marked `<-- CHANGE`. Save with Ctrl+O, Enter, and
exit with Ctrl+X.
```
PATHS:
  BASE_PATH: "/home/pi/sagemic_clips"   # clips are saved here
SETTINGS:
  LATITUDE: 32.7157                  # <-- CHANGE: the station's location
  LONGITUDE: -117.1611               # <-- CHANGE
  LOCAL_TZ: "America/Los_Angeles"
  SAMPLERATE: 48000
  BLOCK_DURATION: 3
  CONFIDENCE_THRESHOLD: 0.5
  AUDIO_DEVICE: "48kHz AudioMoth USB Microphone"   # <-- CHANGE: the name from step 5
  AUDIO_DTYPE: "int16"
    # AudioMoth:  "48kHz AudioMoth USB Microphone"   and  "int16"
    # INMP441:    "snd_rpi_googlevoicehat_soundcar"  and  "int32"  (no final "d")
MQTT:
  DEVICE: canyon_north               # <-- CHANGE: the station name (<station-name>)
  BROKER: hub
  PORT: 1883
  BASE_TOPIC: sagemic
```
You don't need to add anything else now: B2 adds the other station settings, and B3 adds the
hub login.

Test the detector (Ctrl+C to stop; play a bird call - a detection prints "** ... Detected" and saves
a clip in `BASE_PATH`):
```
cd ~/sagemic && .sagemic/bin/python station/sagemic_local.py --config config/config.yaml
```

#### B2. Install the station services
First the MQTT library the station programs need (version 2 or newer), then the services:
```
cd ~/sagemic
.sagemic/bin/pip install -U "paho-mqtt>=2"
sudo bash station/install_agent.sh config/config.yaml
```

[USB power off outside active hours](docs/STATION_OPERATIONS.md#option-for-usb-power-off-outside-active-hours-option-b),
uses `uhubctl` (the tool that switches the USB ports):
```
sudo apt install -y uhubctl
```

#### B3. Connect to the hub
Set up the station's [HaLow dongle](#halow-dongle) and cable it to the Pi's Ethernet port.
The hub must be set up, with its HaLow gate ([Hub Setup](#hub-setup)). Then, on the station,
with `<station-name>` the name entered in the config file (B1):
```
sudo bash ~/sagemic/station/setup_station.sh <station-name>
```
It asks for the station MQTT password and the update key ([Hub Setup](#hub-setup)). The station
name is how the hub files its clips: keep the name a station already has (`MQTT: DEVICE` in its
`config.yaml`).
For a station named `canyon_north`, `setup_station.sh` sets the Pi's name (`canyon-north`), DHCP
on eth0, time from the hub, and in `config.yaml` (readable only by pi - it holds the MQTT password):
```
MQTT:
  DEVICE: canyon_north          # the station name
  BROKER: hub                   # = the DHCP server that gave eth0 its address
  PORT: 1883
  BASE_TOPIC: sagemic
  USERNAME: sagemic-station
  PASSWORD: ...
```
Check that the station shows on the hub's control page: `http://sagemic-hub.local:1880/sagemic/control`

Then follow [Before the station goes to the field](docs/STATION_OPERATIONS.md#before-the-station-goes-to-the-field-option-b).

### HaLow dongle
Set up the station's Heltec HT-HD01 as a Mesh Point, following `docs/HALOW_DONGLES.txt` in the
SageMic hub repository ([Hub Setup](#hub-setup)), "Mesh Point (each
station)":
- Hostname `mp-<station name>`, e.g. `mp-canyon-north` (the hub then gives the dongle a fixed
  address from .200 up);
- Traffic Mode **Bridge**;
- the same Country, Mesh ID, Passphrase, Bandwidth and Channel as the hub's gate.

Cable it to the Pi's Ethernet port (eth0) and power it over USB-C. The light is solid green once
it has joined the mesh (blinking green: not joined - check the Mesh ID, Passphrase, Channel and
Country). 

## Birbler
Currently: sagemic_stream.py (runs on remote server, with instruction steps for pi below)
Device streams its audio to a local server where inference runs there.

ffmpeg is required on **both** the Pi (to capture and encode the stream) and the remote machine (to decode it for inference).

Installation (on Pi):

```
sudo apt install ffmpeg
cd ~
wget https://github.com/bluenviron/mediamtx/releases/download/v1.8.3/mediamtx_v1.8.3_linux_arm64v8.tar.gz
tar xzvf mediamtx_v1.8.3_linux_arm64v8.tar.gz 
sudo mv mediamtx /usr/local/bin/
```
The arm64v8 build requires the 64-bit OS (see Installation step 1).

#### Check ffmpeg and the microphone
Confirm ffmpeg is installed with MP3 support:
```
ffmpeg -version
ffmpeg -hide_banner -encoders | grep libmp3lame
```
Find the name of your capture card:
```
arecord -l      # card numbers
arecord -L      # stable card names, e.g. hw:CARD=sndrpigooglevoi,DEV=0
```
Using the `CARD=` name instead of a number like `hw:3,0` keeps the command working if card numbers
change (for example, when a USB device is plugged in).

Test a 5 second capture with ffmpeg before setting up the stream. For an AudioMoth (mono):
```
ffmpeg -f alsa -channels 1 -sample_rate 48000 -i hw:CARD=<your card>,DEV=0 -t 5 ffmpeg_test.wav
```
For an INMP441, see "Setting up with INMP441 (I2S, stereo capture)" below. If the capture fails with a channel or format
error, the device does not support that format directly; try `plughw:` instead of `hw:` to let ALSA convert.

#### Configure MediaMTX
The tarball extracts `mediamtx.yml` into your home directory. Edit it (no sudo needed):
```
nano ~/mediamtx.yml
```
Add the `paths:` block for your microphone to the end of the file. Use only one of the two
sections below, depending on which mic is attached. Both stream the same mono 48 kHz MP3.

##### Setting up with AudioMoth (USB, mono)
The AudioMoth is a mono USB mic, so it can be captured as 1 channel directly:
```
paths:
  stream:
    runOnInit: ffmpeg -f alsa -channels 1 -sample_rate 48000 -i hw:CARD=<your card>,DEV=0 -c:a libmp3lame -b:a 128k -f rtsp -rtsp_transport tcp rtsp://localhost:8554/stream
    runOnInitRestart: yes
```

##### Setting up with INMP441 (I2S, stereo capture)
The `googlevoicehat-soundcard` overlay only exposes a stereo, 32-bit format, so the INMP441 must be
captured as 2 channels. With L/R tied to GND, all audio is on the **left** channel and the right is
silent. Use the `pan` filter to keep only the left channel at full level (downmixing with `-ac 1`
would average in the silent channel and halve the signal):
```
paths:
  stream:
    runOnInit: ffmpeg -f alsa -c:a pcm_s32le -channels 2 -sample_rate 48000 -i hw:CARD=sndrpigooglevoi,DEV=0 -af "pan=mono|c0=c0" -c:a libmp3lame -b:a 128k -f rtsp -rtsp_transport tcp rtsp://localhost:8554/stream
    runOnInitRestart: yes
```
Confirm the card name with `arecord -L`. To check which channel carries the audio, record a short
stereo clip while talking near the mic and compare channel levels:
```
ffmpeg -f alsa -c:a pcm_s32le -channels 2 -sample_rate 48000 -i hw:CARD=sndrpigooglevoi,DEV=0 -t 5 stereo_test.wav
ffmpeg -i stereo_test.wav -af astats=measure_overall=none -f null - 2>&1 | grep -E "Channel|RMS level"
```
One channel should show a real RMS level and the other should be near `-inf`. If the audio is on
the right channel (L/R wired to 3.3V), use `pan=mono|c0=c1` instead.

The INMP441 is fairly quiet. If needed, add gain in the same filter, e.g.
`-af "pan=mono|c0=c0,volume=12dB"`, increasing gradually and listening for clipping.

#### Start the stream
Start server with:
```
mediamtx ~/mediamtx.yml
```
If you'd like the stream to start automatically, after reboots
make it a systemd service:
```
sudo nano /etc/systemd/system/mediamtx.service
```
Paste this into the opened file:
```
[Unit]
Description=MediaMTX RTSP/RTMP Server
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mediamtx <HOME DIR>/mediamtx.yml
Restart=on-failure
RestartSec=5
User=<YOUR USER>
WorkingDirectory=<HOME DIR>

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
Replace your user and your home directory (e.g. `/home/pi`), save and close.
```
sudo systemctl daemon-reload
sudo systemctl enable mediamtx
sudo systemctl start mediamtx
```
Check that it is running with `sudo systemctl status mediamtx`, and view ffmpeg errors with
`journalctl -u mediamtx -f`.

To test your stream, on your local machine run:
```
ffplay -rtsp_transport tcp rtsp://<pi ip>:8554/stream
```
Listen with headphones if the recorder is in the same room, otherwise you'll hear
feedback from the playback.

Installation (on remote machine or local machine running inference):

Install ffmpeg (`sudo apt install ffmpeg` on Debian/Ubuntu, `brew install ffmpeg` on macOS) and
confirm it is on your PATH with `ffmpeg -version`; `sagemic_stream.py` calls it directly.
Then clone this repo, create a virtual environment and install the package as in Installation steps 4 and 5
(Python 3.9 to 3.11 is required).

Input your ip of your pi and other params set for the stream as
the STREAM_URL in sagemic_stream.py, and set the BASE_PATH to what
directory you'd like detections saved to. Run sagemic_stream.py on
your local machine (with the environment active) and it will run inference on the rtsp stream
of audio provided by the remote pi.
Note, birdnetlib expects 48kHz input.

References:
rtsp streaming: https://github.com/tphakala/birdnet-go/discussions/224#discussioncomment-9837887
downgrading python version on Trixie for tensorflow compatibility: https://github.com/open-webui/open-webui/discussions/17994

MICROPHONE SETUP FOR Option A and Option B.
(AudioMoth or INMP441)

## AudioMoth Setup

Update the config.yaml file:
In the home/pi/sagemic/config directory is a file named example_config.yaml.
Copy example_config.yaml to config.yaml and open config.yaml for editing:
```
cd ~/sagemic
cp config/example_config.yaml config/config.yaml
nano config/config.yaml
```
For the Audiomoth, set these two lines in `config.yaml`:
```
  AUDIO_DEVICE: "48kHz AudioMoth USB Microphone"
  AUDIO_DTYPE: "int16"
```
Check that it's there with:
```
cd ~/sagemic
source .sagemic/bin/activate
python -m sounddevice
```

## INMP441 Setup
Need to update config.txt and config.yaml.
(Note that Option B's `install_station.sh` does the config.txt and config.yaml modifications automatically 
when you select the INMP441; only the wiring is needed for Option B auto installation)

Pin configurations (INMP441 - 40 pin Pi)
SCK - Pin 12 (GPIO 18 PCM Clock)
SD - Pin 38 (GPIO 20, PCM In)
WS - Pin 35 (GPIO 19, PCM FS)
L/R - GND (For left channel)
VDD - 3.3V
GND - gnd

On Bookworm and later the file is `/boot/firmware/config.txt` (older guides use `/boot/config.txt`).
First check whether I2S is already present, possibly commented out with `#`:
```
grep -nE "i2s|googlevoicehat" /boot/firmware/config.txt
```
If `#dtparam=i2s=on` is there, remove the `#` rather than adding a second copy. Then edit the file:
```
sudo nano /boot/firmware/config.txt
```
Add the following at the very end of the file, under the `[all]` section (lines placed under a
filter such as `[cm4]` or `[pi5]` only apply to that board and are ignored otherwise):
```
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```

Then reboot your pi:
`sudo reboot`

After rebooting, confirm the mic is detected and records audio.
First connect to the Pi with `ssh pi@<station-name>.local`, then:
```
arecord -l                  # should list a googlevoicehat card
arecord -D plughw:CARD=sndrpigooglevoi,DEV=0 -c 1 -r 48000 -f S32_LE -d 5 test.wav
aplay test.wav              # or copy the file off the Pi and listen
```
Use `arecord -L` to confirm the card name if it differs. A silent or flat recording usually means a
wiring problem, most often SD on the wrong pin or L/R not tied to GND.

Update the config.yaml file:
In the home/pi/sagemic/config directory is a file named example_config.yaml.
Copy example_config.yaml to config.yaml and open config.yaml for editing:
```
cd ~/sagemic
cp config/example_config.yaml config/config.yaml
nano config/config.yaml
```
For the INMP441, set these two lines in `config.yaml`:
```
  AUDIO_DEVICE: "snd_rpi_googlevoicehat_soundcar"
  AUDIO_DTYPE: "int32"
```
Check that it's there with:
```
cd ~/sagemic
source .sagemic/bin/activate
python -m sounddevice
```
Then you are all setup for using INMP441 :)

References:
https://learn.adafruit.com/adafruit-i2s-mems-microphone-breakout/raspberry-pi-wiring-test
