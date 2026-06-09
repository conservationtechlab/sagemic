# sagemic
Real-time detection and classification of bioacoustic events on field devices built around microphone-enabled single-board computers, with capabilities to do inference on-device and via streaming data to a remote server. 

## SageMic (Pi 4B)
Currently: sagemic_local.py
Sagemic is an acoustic inference device that runs Birdnet actively using a USB microphone and stores detections locally. 

Current versions in development include LTE enabled and Wifi Halow versions where detections are sent to an MQTT Broker.

Clone this repo on your Pi 4B (Full Desktop version of Trixie OS)

In the repo, create a python environment. If using Trixie, Python3.11 needs to be
installed because Tensorflow is not yet fully comptaible with Python3.13, which is what Trixie has. 

To create a non-replacing install of Python3.11, run these commands to build an alternate install of Python3.11 we can use in our venv.

```
su - root
apt install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev libxml2-dev libxslt1-dev
cd ~ && wget https://www.python.org/ftp/python/3.11.13/Python-3.11.13.tar.xz
tar xvf Python-3.11.13.tar.xz && cd Python-3.11.13
./configure --enable-optimizations --with-ensurepip=install
make -j $(nproc) && make altinstall

python3.11 -m venv .sagemic

source .sagemic/bin/activate
```

```
cd ~/sagemic
pip install -e .
sudo apt-get install libportaudio2
```

Change the save path to either a mounted drive (if running on a local network) or the desired local storage.

##TODO Make method for periodic data transfer when service is intermittent.

Inference runs on board the remote device (raspberry pi).

## Pi Zero 2 Setup  

Flash the Pi with OS Lite (64-bit) Debian Trixie w/ No Desktop  
* A Desktop would eat too much into the given 512MB RAM
* The Python community hates the 32 bit system, downloading python & dependencies take forever + too much RAM nothing is really precompiled

Make sure to disable Trixie automatic updates by:  
```
sudo apt update
sudo apt install unattended-upgrades
sudo dpkg-reconfigure unattended-upgrades
```
Choose No, then press Enter.  

### Installing Python 3.11 & Dependencies 
```
su - root
apt install -y build-essential zlib1g-dev libncurses5-dev libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev libsqlite3-dev wget libbz2-dev libxml2-dev libxslt1-dev
cd ~ && wget https://www.python.org/ftp/python/3.11.13/Python-3.11.13.tar.xz
tar xvf Python-3.11.13.tar.xz && cd Python-3.11.13
./configure --enable-optimizations --with-ensurepip=install
```
Changes from previous sagemic README: 
* Simply running `make -j $(nproc)` will enable all 4 CPU cores on the zero2, which will eatup the zero2's 512MB of RAM, which will freeze everything
* We also need to configure our swap space for this heavy compilation.  
```
sudo apt update
sudo apt install -y dphys-swapfile
sudo nano /etc/dphys-swapfile
```
Change `CONF_SWAPSIZE` to 1024, then apply this new setting:  
```
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
make && make altinstall
```
* Swap space is cheap, expandable, but very slow. It will probably take more than an hour to install python, but it won't crash or freeze!  
* Once done, go back to `dphys-swapfile` and change it back to whatever it was (either empty or 100), then run the new setting commands again.  

```
exit
cd sagemic
python3.11 -m venv .sagemic
source .sagemic/bin/activate
```

The full Tensorflow used originally is for both inference and training, but it is too heavyweight and will cause problems with our limited RAM  
Instead, we will use Tensorflow Lite. The original Tensorflow dependency in pyproject.toml is already modified to lite.   

Changes from previous sagemic README: 
* We can't get birdnetlib with pip because the zero2's weak wifi will drop packets inevitably during the 60MB download. With pip's strict timeout, the download will fail.   
* So, we use wget! 

Copy the link address of the birdnetlib .whl file from [PyPI Files](https://pypi.org/project/birdnetlib/#files)  
```
wget -c <link address>
pip install <.whl file name> --no-cache-dir
```
```
pip install -e .
sudo apt-get install libportaudio2
```

### Setup Files and Libraries
1. Follow the README.md in `/sagemic/systemd`
2. //Add to config files for datapaths etc
3. 

## Birbler (Regular Pi)
Currently: sagemic_stream.py (runs on remote server, with instruction steps for pi below)
Device streams its audio to a local server where inference runs there. 

Installation (on Pi):

```
sudo apt install ffmpeg
wget https://github.com/bluenviron/mediamtx/releases/download/v1.8.3/mediamtx_v1.8.3_linux_arm64v8.tar.gz
tar xzvf mediamtx_v1.8.3_linux_arm64v8.tar.gz 
sudo mv mediamtx /usr/local/bin/
sudo nano mediamtx.yml
```
Add these lines to the end of the file:
```
paths:
  stream:
    runOnInit: ffmpeg -f alsa -channels 1 -i hw:3,0 -ar 48000 -acodec libmp3lame -f rtsp -rtsp_transport tcp rtsp://localhost:8554/stream
    runOnInitRestart: yes
```
If mic on pi is a different device (not hw:3,0), run:
```
arecord -l
```
to see which device to swap in that param.
Start server with:
```
mediamtx mediamtx.yml
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
ExecStart=/usr/local/bin/mediamtx /home/ctl/mediamtx.yml
Restart=on-failure
RestartSec=5
User=<YOUR USER>
WorkingDirectory=<HOME DIR>

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```
Replace your user and your home directory, save and close.
```
sudo systemctl daemon-reload
sudo systemctl enable mediamtx
sudo systemctl start mediamtx
```
To test your stream, on your local machine run:
```
ffplay -rtsp_transport tcp rtsp://<pi ip>:8554/stream
```
Listen with headphones if the recorder is in the same room, otherwise you'll hear
feedback from the playback.

Installation (on remote machine or local machine running inference):

Clone this repo. Install necessary packages.

Input your ip of your pi and other params set for the stream as
the STREAM_URL in sagemic_stream.py, and set the BASE_PATH to what
directory you'd like detections saved to. Run sagemic_stream.py on
your local machine and it will run inference on the rtsp stream
of audio provided by the remote pi. 
Note, birdnetlib expects 48kHz input.

References:
rtsp streaming: https://github.com/tphakala/birdnet-go/discussions/224#discussioncomment-9837887
downgrading python version on Trixie for tensorflow compatibility: https://github.com/open-webui/open-webui/discussions/17994
