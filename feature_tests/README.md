### send_file.py

This script is to test the ability of the end-device (sagemic) to successfully publish a message to the TBMQ broker. 
One will need to head over to the site to create the session ID, username, and password of the client device to input in this script.

You will need the public certificate on the device to validate the TLS communication with the remote host. Contact the system admin for TBMQ to obtain.

### run_birdnet_on_microphone_stream.py

This script validates the functionality of the sagemic device's connectivity and proper configuration of the microphone, and the 
package dependency installs. It will allow you to select the sounddevice (microphone) to use, and will run inference using birdnet. 
If the environment is properly configured, this will show that the device is ready to run Sagemic (local inference) or Birbler (audio streaming).

### Setting up the TBMQ broker
We are using TBMQ (ThingsBoard MQTT Broker) to recieve and publish messages
from our Sagemics.

This service will need to be hosted on a remote server where port 8883 can
be opened for ingress. 

TBMQ docker will run on this remote host.


### MQTTS (MQTT over TLS)

Requires:
[TBMQ Server](https://thingsboard.io/docs/mqtt-broker/installation/docker/)
	- Port 8883 open for ingress

*Access to server is done through port forwarding over SSH. 
Unless you changed the port (default 8080), you can access it on your browser at

```
localhost:8080
```
by SSHing in with

```
ssh -L 8080:Localhost:8080 user@<ip of tbmq host>
```

For secure communication between devices and TBMQ, we will create a
certificate that the devices will use to conduct the 
TLS handshake with our TBMQ server, and that Node-Red will use to authenticate
its subscription to the TBMQ topic.

Create a root-ca.pem, and root-ca.key in a secure machine, ideally storing the private key 
completely offline if possible.

Create an intermediate-ca.key, and submit a .csr to be signed by the root-ca.key.

Transfer the intermediate-ca.key, the resulting intermediate-ca.pem to a different
machine that will be used to create our server certificate, and our future device
certificates. 

Create a tbmq-server.key, include the SAN with the ip address or domain name of the
server where tbmq is hosted. Submit the csr to be signed by the intermediate-ca.key.
Create a chain containing the intermediate-ca.pem and the tbmq-server.pem for a server-chain.pem
Transfer the tbmq-server.key and the server-chain.pem to the machine hosting tbmq in a folder called
/certs or something similar.

Transfer the root-ca.pem to the devices and the node-red instance where they can access the
file. This file is what we will include when we send and receive data to confirm the
identity of the tbmq server(TLS handshake), and enable encrypted communications. 

### TODO: X.509 Two-way TLS (client certificates)
Create client certificates using the intermediate-ca.key, give each device a pair. 



