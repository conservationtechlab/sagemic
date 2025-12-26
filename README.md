# sagemic
Real-time detection and classification of bioacoustic events on field devices built around microphone-enabled single-board computers, with capabilities to do inference on-device and via streaming data to a remote server. 

## SageMic Local
Inference runs on board the remote device (raspberry pi).


## SageMic Streaming
Device streams its audio to a local server where inference runs there. 

Installation:
Install icecast2 and darkice onto raspberry pi
Set up the icecast server with your configs
create a /etc/darkice.cfg file and fill it with some params.
Start the darkice server with 'sudo darkice'

Input your ip of your pi and other params set for the darkice stream as
the STREAM_URL in sagemic_stream.py. Run sagemic_stream.py on
your local machine and it will run inference on the http stream
of audio provided by the remote pi. 

