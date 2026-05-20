# Sagemic Systemd Services

* `sagemic_local.service` helps run the continuous BirdNet listener (sagemic_local)
* `sagemic_scansend.timer` wakes up `sagemic_scansend.service` every 5 minutes

**For setup, they need to have hardcoded absolute paths in whatever machine you're using, so make sure to change them in each file!**
For ExecStart: `/home/<user>/sagemic/.sagemic/bin/python /home/<user>/sagemic/<code_file>
Working directory is just the directory of the repo
##To use these services:
1. copy them into your system directory: `sudo cp *.* /etc/systemd/system`
2. refresh systemd: `sudo systemctl daemon-reload`
3. enable and start `sagemic_local.service` and `sagemic_scansend.timer`: `sudo systemctl enable <filename>`, `sudo systemctl start <filename>'
4. can also make sure 'sagemic_scansend.service' is disabled with the same format as 3. 
5. you can check the status of the services by running `sudo systemctl status <filename>'
6. to stop the services, run `sudo systemctl stop <filename>` on both active services
