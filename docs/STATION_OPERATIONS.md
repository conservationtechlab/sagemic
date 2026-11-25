# SageMic station - operations

Checking and looking after a station (option B) once it is installed. Installing a
station: the [README](../README.md).

- [Before the station goes to the field](#before-the-station-goes-to-the-field-option-b)
- [Is the station running?](#is-the-station-running) and [the station's log](#the-stations-log)
- [Reaching a station](#reaching-a-station-option-b)
- [Station settings](#station-settings-option-b)
- [USB power off outside active hours](#option-for-usb-power-off-outside-active-hours-option-b)
- [Delivery confirmation](#delivery-confirmation-option-b)
- [Station software updates](#station-software-updates-option-b)
- [The model](#the-model-option-b)

## Before the station goes to the field (option B)
Once the station is connected to the hub, on the station:
1. Delete the test clips recorded on the bench, so they are not sent to the hub and uploaded to
   the NAS with the field recordings (`~/sagemic_clips` is the `BASE_PATH` folder). Do this
   after connecting the station: the sender runs every minute, and clips it has already
   sent cannot be called back (the hub may already have them in its site folders). Stop the
   sender first, delete, then start it again (the last line runs the sender once, so its timer
   keeps going):
   ```
   sudo systemctl stop sagemic_scansend.timer
   rm -r ~/sagemic_clips/*
   sudo systemctl start sagemic_scansend.timer sagemic_scansend.service
   ```
2. Turn the station Wi-Fi off (HaLow only, from the next power-up):
   ```
   sudo bash ~/sagemic/station/setup_station.sh --wifi off
   ```

## Is the station running?
1. **Restart the station** by cycling power and wait about **3 minutes**.

2. **Log in again** (`ssh pi@<station-name>.local`) and **check the services**:
   ```
   systemctl is-active sagemic_local sagemic_agent sagemic_update
   ```
   Pass: each line says `active`.

   | Service | What it does |
   |---|---|
   | `sagemic_local` | the detector: listens to the microphone and saves clips |
   | `sagemic_agent` | sends the station's status, takes settings and commands from the hub |
   | `sagemic_update` | installs station software updates sent from the hub |

3. **Check the timers.** The sender and the clean-up run on timers (every minute and every hour),
   so they are not listed as running above:
   ```
   systemctl list-timers 'sagemic_*' --no-pager
   ```
   Pass: both `sagemic_scansend.timer` and `sagemic_cleanup.timer` show a time under **NEXT**.
   LAST can show `-` until the timer's first timed run - that is normal.
4. **Option B: check the hub's control page.** Pass: the station is **online**, with a recent last
   report, and the Detector column says `running`.

Status of the timer can be checked with:
```
systemctl status sagemic_scansend.timer --no-pager`.
```
## The station's log
The agent's last messages (connection to the hub or broker, status, commands):
```
journalctl -u sagemic_agent -n 20 --no-pager
```
The other services the same way, e.g. `journalctl -u sagemic_local -n 20 --no-pager` for the
stations main program.

## Reaching a station (option B)
SSH can be used to access a station through the hub, on site or from the office: see the hub repository's
`docs/HUB_OPERATIONS.md`, "Reaching a station (SSH, and copying files)".

## Station settings (option B)
In option B (Hub/Station Configuration) all of these can also be changed from the hub's control page.
```
SETTINGS:
  DETECTION_ENABLED: true        # pause/resume detection
  ANALYZE_INTERVAL_S: 0          # seconds between analyzed 3 s blocks (0 = continuous)
  ACTIVE_HOURS: "00:00-24:00"    # one period "05:00-10:00"; several, comma-separated:
                                 # "05:00-10:00,16:00-20:00"; past midnight "20:00-06:00";
                                 # at least 2 hours a day
  MIN_FREE_MB: 200               # stop saving clips below this free space
  USB_OFF_OUTSIDE_ACTIVE_HOURS: false  # true = USB ports (and the HaLow dongle) off outside ACTIVE_HOURS
TIMERS:
  SEND_INTERVAL_S: 60            # how often clips are sent (seconds)
  CLEANUP_INTERVAL_S: 3600       # how often old sent clips are deleted
  RESEND_AFTER_S: 600            # resend clips not confirmed this long after sending
CLEANUP:
  KEEP_SENT_DAYS: 7              # confirmed clips stay on the station 7 days
                                 # (a spare copy while the hub uploads them)
AGENT:
  STATUS_INTERVAL_S: 300         # how often status is sent (5 min)
```
From the command line (keeps comments, backup `config.yaml.bak`):
```
cd ~/sagemic && .sagemic/bin/python station/sagemic_agent.py --config config/config.yaml --set AGENT.STATUS_INTERVAL_S=120
```
Only detections, errors and an hourly summary are logged. BirdNET runs in
its own thread, so a slow analysis never holds up the microphone; if it falls behind, the oldest
waiting blocks are skipped (control page warning "analysis falling behind").

## Option for USB power off outside active hours (option B)
There is an option to turn on the USB power which switches off power to the HaLow dongle only during ACTIVE_HOURS.  This 
saves power during inactive hours. When the dongle turns back on it takes about 2 minutes to come back online.

While the dongle is off, the station is offline:
- it shows as offline on the hub's control page;
- it can't be reached for commands or updates;
- any clips that were not sent wait on the Pi and are sent when the active hours start.

With `ACTIVE_HOURS` "00:00-24:00" nothing is switched. In order to be able to reach it from the hub
for update, `ACTIVE_HOURS` must add upt to at least 2 hours a day.  After each setting change power stays on
for 30 minutes, even if the setting is to turn it off.  This way, the user still has access in case
an error was made during entry.
For example: It's 9:00 am and the setting is ACTIVE HOURS begins at 10:00.  The dongle will stay on until 9:30 am, turn off then on again at 10:00 am.
Settings can be for multiple times, for example 09:00 - 10:00, 11:00-12:00.
 The selection can be done from the hub's control page or by using the command:
```
cd ~/sagemic && .sagemic/bin/python station/sagemic_agent.py --config config/config.yaml --set SETTINGS.USB_OFF_OUTSIDE_ACTIVE_HOURS=true
```
Before turning it on:
- Pi 4 and Pi 5: **all** the Pi's USB ports are switched off together.
- It refuses to switch off when the clips (`BASE_PATH`) or the system disk are on a USB drive.

## Delivery confirmation (option B)
The hub confirms every clip it saves; the Pi records these in `delivered_clips.log` and cleanup
deletes only confirmed clips.

Both logs are in the clips folder (`BASE_PATH`):

| Log | What it holds |
|---|---|
| `sent_clips.log` | the clips sent to the hub that are still on the Pi |
| `delivered_clips.log` | the clips the hub confirmed that are still on the Pi |
| `delivered_clips_archive.log` | every clip the hub confirmed, also after the clip was deleted from the Pi - a permanent record (clip and time of confirmation), never shortened |

When the hourly cleanup deletes a confirmed clip, it moves that clip's line from
`delivered_clips.log` to `delivered_clips_archive.log`, so the two working logs (read by the sender
every minute) stay short while nothing confirmed is forgotten.

## Station software updates (option B)
New station software is installed from the **hub's control page**: upload the zip, then install it
on one station, then on all of them. How to make the zip and the steps on the control page: the hub
repository's `docs/STATION_UPDATES.txt`, and `docs/HUB_OPERATIONS.md`, "Update software on the
Stations".

**What the station does with an update** - the control page shows each step as it happens:
1. **verify** - checks that the update came from the hub and arrived complete.
2. **check** - tries the new programs with the station's own settings before installing anything.
   Any problem stops the update here, with the reason on the control page; nothing has been changed.
3. **backup** - keeps a copy of the current programs (the last 3 updates).
4. **install** - puts the new programs in place and restarts the station's services.
5. **health** - waits up to 4 minutes for everything to run properly.
6. **done.** If the health check fails, the station puts the backup back by itself and the control
   page shows "rollback". If an update gets stuck, the station also puts the backup back by itself
   after 30 minutes.

Settings (`config.yaml`), clips and logs are never changed by an update.

**Checking the update on the control page:** in the stations table, the station's Version shows the
new version marked **up to date**, and the station is **online** with a recent report.

## The model (option B)
The station uses one of:
- the **standard BirdNET model** (all species, filtered by the station's `LATITUDE` and `LONGITUDE`);
- a **species list** - the standard model, but only the listed species are reported (the location
  filter is not used then);
- a **custom classifier** trained with BirdNET-Analyzer (its own species).

All stations use the same model. It is sent from the **hub's control page**, section "Model on the
Stations", to one station at a time - see the hub repository's `docs/MODEL_UPDATES.txt`.

**What the station does with a new model:** receives it in parts (an interrupted transfer carries on
where it stopped when the model is sent again), checks it arrived complete, tests it (loads it and
analyzes 3 seconds of silence; a species list is also checked against BirdNET's species names), then
restarts the detector with it. If the detector does not start with the new model, the station goes
back to the model it had before. The model in use and the 2 before it are kept in `~/sagemic_models/`.

**Checking it:** the control page's **Model** column shows the station's model. On the station:
```bash
cat ~/sagemic_models/current                     # the model in use ("standard" = standard BirdNET)
journalctl -u sagemic_local | grep Model:        # what the detector loaded
```
If the detector cannot load its model when it starts, it uses the standard BirdNET model and the
control page shows the warning "model not loaded, standard BirdNET in use: ...".
