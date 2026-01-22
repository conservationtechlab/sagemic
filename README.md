# sagemic
Real-time detection and classification of bioacoustic events on field devices built around microphone-enabled single-board computers, with capabilities to do inference on-device and via streaming data to a remote server. 

## SageMic Local
Inference runs on board the remote device (raspberry pi).


## SageMic Streaming
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
