"""Send a test file over MQTTS.

This script will test a device publish message to our
TBMQ server.
"""
import paho.mqtt.client as mqtt
import ssl


PORT = 8883
BROKER = "<ip or domain of broker>" # must match the SAN/IP of the server certificate
TOPIC = "test/demo"
MESSAGE = "Hello from Sagemic!"
PATH_TO_CA_PEM = "</path/to/root_ca.pem>"
SESSION_ID = "sagemic-test-pub"
USER = "sagemic-test-pub"
PASS = "<unique password>"

client = mqtt.Client(client_id=SESSION_ID)

client.username_pw_set(USER, PASS)

client.tls_set(ca_certs=PATH_TO_CA_PEM,
               certfile=None,
               keyfile=None,
               cert_reqs=ssl.CERT_REQUIRED,
               tls_version=ssl.PROTOCOL_TLS)

client.connect(BROKER, PORT)

client.loop_start()
client.publish(TOPIC, MESSAGE, qos=1)
client.loop_stop()
client.disconnect()

print("sent")
