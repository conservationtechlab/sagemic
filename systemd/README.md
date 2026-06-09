# Sagemic Systemd Services

* `sagemic_local.service`  helps run the continuous BirdNet listener `sagemic_local.py`  
* `sagemic_scansend.timer` wakes up (triggers) `sagemic_scansend.service` every 5 minutes  
 
**For setup, they need to have hardcoded absolute paths in whatever machine you're using, so make sure to change them in each file!**  

For ExecStart:
```
/home/<user>/sagemic/.sagemic/bin/python /home/<user>/sagemic/<code_file>
```
Working directory is just the directory of the repo.

## To use these services:
1. Copy them into your system directory: `sudo cp *.* /etc/systemd/system`
2. Refresh systemd: `sudo systemctl daemon-reload`
3. You can enable to start programs automatically at boot:
```
sudo systemctl enable sagemic_local.service
sudo systemctl enable sagemic_scansend.timer
```
Or disable so they will never run at boot:
```
sudo systemctl disable sagemic_local.service
sudo systemctl enable sagemic_scansend.timer
```
4. To start the programs:
```
sudo systemctl start sagemic_local.service
sudo systemctl start sagemic_scansend.timer
```
5. you can also make sure `sagemic_scansend.service` is disabled with the same format as 3. 
6. you can check the status of the service/timer by running `sudo systemctl status <filename>`
7. to stop the services, run `sudo systemctl stop <filename>` on both service/timer

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
You can fix this by stopping or disabling services like in step 3 or 7. 
