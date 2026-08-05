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
If using the Sixfab LTE hat, you will need to use the lte setup systemd service
file so that it configures the connection properly on each reboot. 

Prior to that, you will need to conduct a 1 time setup step to configure
the proper APN in the modem hardware.

If using the recommended EIoT Club SIM within the US, the APN will be "america.bics"
 
Move the shell script and move the systemd service file.
```
cd ~/sagemic/systemd
sudo cp lte-up.sh /usr/local/bin/lte-up.sh
chmod +x /usr/local/bin/lte-up.sh
sudo cp lte-up.service /etc/systemd/system
```

Install modem packages and set up the APN manually in the modem, only needs to be done once.
```
sudo apt install modemmanager libqmi-utils minicom
mmcli -L
```
You are looking for the number after ../Modem/#. That # is the modem ID we will use later. 
It will probably be 0. But we should check for it because there's a chance it is 1, 2...

```
mmcli -m <id>
```
You should see a few /dev/ttyUSB# listed. Look for one that says (at). There may be multiple. Pick one for now.


Replace that number in the command below where the # is. 
```
sudo minicom -D /dev/ttyUSB#
```

You will now be in an AT interface, interfacing directly with the modem. Commands look a little different.


To see if you chose the correct USB# port, try the following command.
```
AT
```

If you see an 'OK' continue. If not, exit this and try the other USB# that said (at).

```
AT+CGDCONT=1,"IP","america.bics"
```

Verify the APN took with:
```
AT+CGDCONT?
```

You should see the APN we just set.

To exit minicom: Ctrl + A, X

```
reboot
```

Now that your modem knows the correct APN, you can enable the lte-setup service
and reboot one more time.

```
sudo systemctl daemon-reload
sudo systemctl enable lte-up.service
sudo systemctl start lte-up.service
reboot
```

When you are once again inside the pi, you should be able to ping google using
the wwan0 (LTE) connection. If this pings correctly, congrats, you set up LTE.

```
sudo ping -I wwan0 google.com
```

*If configuring LTE while sshed in, you will need to re-ssh in after reboots, if this is annoying, use
a monitor/keyboard if you have a desktop OS.
**mmcli -L may not see board initially if modemmanager was installed after modem was plugged in. If this happens, reboot while modem is plugged in and try mmcli -L again.

### LTE Watchdog
To prevent our modems from spamming cell towers when network drops and ensure a graceful restart of the LTE and other network services...
```
sudo cp lte-watchdog.sh /usr/local/bin/lte-watchdog.sh
sudo cp lte-watchdog.service /etc/systemd/system/lte-watchdog.service
sudo chmod +x /usr/local/bin/lte-watchdog.sh

sudo systemctl daemon-reload
sudo systemctl enable lte-watchdog.service
sudo systemctl start lte-watchdog.service
```

You can also ping every 5 minutes to ensure our carrier does not see us as inactive.
```
crontab -e
```
Go to the end of the file, and add:
```
*/5 * * * * /bin/ping -c 3 8.8.8.8 > /dev/null 2>&1
```

To permanently bind to T-Mobile (in the case of other carriers blacklisting)...
```
sudo systemctl stop lte-up.service
sudo systemctl stop ModemManager

sudo minicom -D /dev/ttyUSB2

AT+COPS=2        					// detach modems from all towers
AT+CRSM=214,28539,0,0,12,"FFFFFFFFFFFFFFFFFFFFFFFF"	// wipe blacklist
AT+QCFG="band",0,3FFFFFFF,0,1				// see all towers
AT+QICSGP=1,1,"america.bics","","",0
AT+CGDCONT=1,"IP","america.bics"
AT+CFUN=1
AT+COPS=1,2,310260,7					// force connection to tmobile
AT+QENG="servingcell"					// you should see 310  260, which is tmobile
```

