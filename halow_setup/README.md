## Configuring Halow Dongles for Mesh Network

### Configuring the Mesh Gate

Heltec has documentation for setting up the Mesh Gate configuration
on the dongle. [Heltec Wiki for Mesh Gate](https://wiki.heltec.org/docs/devices/wifi-halow/ht-hd01/meshgate?mg=mg)

Follow the instructions exactly except customize the following fields:
Mesh ID: arbitrary string all devices in the mesh must match
Mesh Passphrase:: password for mesh ID
Bandwidth (or Width): The smaller, the farther and less throughput, the larger the closer range and more throughput. For Sagemics, 2MHz is fine.
Channel: Your choice, we choose 26 (915MHz)

For Upstream Network, we choose Ethernet, and Bridge traffic mode.

*For plug-in, enable 2.4GHz Wifi AP, for battery; disable.

If configuring the wifi AP, you'll setup an SSID and password. Note that joining this network does not put you on the Halow network, because it's a different frequency.

For SageMics, about 5-8 hops through the mesh is reasonable before we expect bandwidth constraints.

### Configuring Mesh Points

Each SageMic Halow dongle should be set-up according to the Heltec Setup guide.
[Heltec Wiki for Mesh Gate](https://wiki.heltec.org/docs/devices/wifi-halow/ht-hd01/meshgate?mg=mp)

Follow the instructions exactly, but customize the following values:

Mesh ID: Should match the Mesh Gate ID
Mesh Passphrase: Should match the Mesh passphrase
Width: 2MHz
Channel: 26 (915 MHz) (can select whichever, but keep in mind interferance in the region with similar channels)

Upstream Network is Ethernet and Bridge like the Mesh Gate. 

Because most mesh points will be SageMics on battery, AP should be turned off. 
Should only be turned on if it's connected via outlet or there is another reason to do so.

## Configuring the Raspberry Pi as a DHCP Server

The Mesh Gate should be connected via Ethernet to a Raspberry Pi that acts as the DHCP Server and router. To configure this, paste the following commands in the Pi terminal.


