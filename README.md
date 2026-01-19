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
start server with:
```
mediamtx mediamtx.yml
```
Make a systemd service to start the service on boot.

To test your stream, on your local machine run:
```
ffplay -rtsp_transport tcp rtsp://<pi ip>:8554/stream
```
Listen with headphones if the recorder is in the same room, otherwise you'll hear
feedback from the playback.

Input your ip of your pi and other params set for the stream as
the STREAM_URL in sagemic_stream.py. Run sagemic_stream.py on
your local machine and it will run inference on the rtsp stream
of audio provided by the remote pi. 
Note, birdnetlib expects 48kHz input.



