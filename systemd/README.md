# Sagemic Systemd Services

* `sagemic_local.service` helps run the continuous BirdNet listener (sagemic_local)
* `sagemic_scansend.timer` wakes up (triggers) `sagemic_scansend.service` every 5 minutes

**For setup, they need to have hardcoded absolute paths in whatever machine you're using, so make sure to change them in each file!**
For ExecStart: `/home/<user>/sagemic/.sagemic/bin/python /home/<user>/sagemic/<code_file>
Working directory is just the directory of the repo

##To use these services:
1. copy them into your system directory: `sudo cp *.* /etc/systemd/system`
2. refresh systemd: `sudo systemctl daemon-reload`
3. you can enable to start programs automatically at boot:
```
sudo systemctl enable sagemic_local.service
sudo systemctl enable sagemic_scansend.timer
```
or disable so they will never run at boot:
```
sudo systemctl disable sagemic_local.service
sudo systemctl enable sagemic_scansend.timer
```
3. to start the programs:
```
sudo systemctl start sagemic_local.service
sudo systemctl start sagemic_scansend.timer
```
4. you can also make sure `sagemic_scansend.service` is disabled with the same format as 3. 
5. you can check the status of the service/timer by running `sudo systemctl status <filename>`
6. to stop the services, run `sudo systemctl stop <filename>` on both service/timer

### Gotchas: Audio Device Resource Allocations (?)
ALSA only allows one process/program to use the microphone at a time! 
If you are trying to run other scripts with the audio devices and you get errors like:
```
channelCount <= maxChans
```
or
```
Invalid number of channels
```
You can fix this by stopping or disabling services like in step 3 or 6.


## LTE Setup
If using the Sixfab LTE hat, you will need to create a NetworkManager profile with
some specific settings, and it should manage the connection for you.

If using the recommended EIoT Club SIM within the US, the APN will be "america.bics"

Install modem packages:
```
sudo apt install modemmanager libqmi-utils minicom
```

Configure an lte device in NetworkManager, change 'america.bics' using a SIM with a different APN:
```
sudo nmcli connection add \
    type gsm \
    ifname "*" \
    con-name lte \
    apn america.bics
```

Ensure it will automatically reconnect, and pull it up:
```
sudo nmcli connection modify lte connection.autoconnect yes
sudo nmcli connection up lte
```

Check that is connected properly with:
```
nmcli device status
```
You should be able to ping google using
the wwan0 (LTE) connection. If this pings correctly, congrats, you set up LTE.

```
sudo ping -I wwan0 google.com
```

You can check logs if something goes awry using:

```
journalctl -u NetworkManager
journalctl -u ModemManager
```

*If configuring LTE while sshed in, you will need to re-ssh in after reboots, if this is annoying, use
a monitor/keyboard if you have a desktop OS.
**mmcli -L may not see board initially if modemmanager was installed after modem was plugged in. If this happens, reboot while modem is plugged in and try mmcli -L again.
