# Custom head: 4 bird species + noise

A small classifier on top of BirdNET for the donor demo. BirdNET (2.4, via `birdnetlib`) turns each
3-second block of audio into 1,024 numbers; this head reads them and picks one of five classes. It
plugs into the custom-model path on the `82_svm` branch with no code changes.

| Class | `CUSTOM_LABELS` name | |
|---|---|---|
| 0 | `haemorhous_mexicanus` | house finch |
| 1 | `sturnella_neglecta` | western meadowlark |
| 2 | `cyanocitta_stelleri` | Steller's jay |
| 3 | `callipepla_californica` | California quail |
| 4 | `noise` | room, crowd, talking, music: never reported |

The model is `sagemic_head.joblib`: a scikit-learn pipeline (scaler + linear SVM), 65 KB.

## Demo day

From the repo root on the demo Pi:

1. `git pull` (this branch).
2. In `config/demo_custom_head.yaml`, fill in the three values marked `FROM CURRENT DEMO CONFIG`,
   copied from the Pi's existing `config.yaml`:
   - `BASE_PATH`: the folder the lightbox watches
   - `AUDIO_DEVICE`, `AUDIO_DTYPE`

   Everything else is set.
3. Start sagemic with this config:
   `python sagemic_local.py --config config/demo_custom_head.yaml`
   (or point the service at it, see below). The log should show
   `Using custom model: custom_head/sagemic_head.joblib`, then `No detections` every 3 s.
4. Press a toy.

### Placement

- **Keep every toy closer to the mic than the guests.** That's where it's near-perfect.
- **Put the meadowlark closest.** It's the toy most easily covered by talking.
- A toy buried by people talking near the mic is usually missed, and a miss is silent: it doesn't
  light the wrong bird.

## Config

```yaml
PATHS:
  CUSTOM_MODEL_PATH: "custom_head/sagemic_head.joblib"   # relative to the repo root
SETTINGS:
  MODEL_TYPE: "custom"
  CUSTOM_CONF: 0.25
  CUSTOM_LABELS: [haemorhous_mexicanus, sturnella_neglecta, cyanocitta_stelleri, callipepla_californica, noise]
```

- Keep `CUSTOM_LABELS` in exactly this order: the model outputs class numbers 0-4.
- The names match what stock BirdNET mode writes, so clip filenames keep the same form:
  `{date}_{time}_{scientific_name}_{confidence}.flac`.
- **The confidence in the filename is on a different scale** from stock BirdNET: it's the SVM's
  score (correct calls typically 1-3), not a 0-1 probability. Worth checking if the lightbox uses it.
- `noise` never writes a file, so there is no "nothing heard" message.

### Tuning `CUSTOM_CONF`

A bird is reported only if its score is above `CUSTOM_CONF`. **0.25 is recommended**: it keeps a
safety margin above the strongest near-false-alarm we saw (-0.04). If false alarms appear at the
venue, raise it:

| `CUSTOM_CONF` | Toy alone | Toy closer than crowd | Crowd closer than toy | Toy + music / talking | False alarms |
|---|---|---|---|---|---|
| 0.0 | 126/127 | 17/17 | 11/16 | 8/8 | 0 |
| **0.25** | **126/127** | **17/17** | **10/16** | **8/8** | **0** |
| 0.5 | 126/127 | 17/17 | 9/16 | 8/8 | 0 |
| 1.0 | 126/127 | 17/17 | 6/16 | 7/8 | 0 |

## Install

The model was saved with **Python 3.11, scikit-learn 1.9.1, joblib 1.6.0, numpy 2.4.6**.

**Quick check** in sagemic's existing environment:

```bash
python -c "import joblib; print(joblib.load('custom_head/sagemic_head.joblib').classes_)"
```

If it prints `[0 1 2 3 4]`, nothing else is needed. If it fails (for example numpy 1.x), build a
separate environment from the lock file, which leaves the existing one untouched:

```bash
python3.11 -m venv .venv-head
source .venv-head/bin/activate
pip install -e .
pip install -r custom_head/requirements-lock.txt
```

and point the service at it with a reversible override (`sudo systemctl edit sagemic_local.service`):

```ini
[Service]
ExecStart=
ExecStart=/home/<user>/sagemic/.venv-head/bin/python /home/<user>/sagemic/sagemic_local.py --config /home/<user>/sagemic/config/demo_custom_head.yaml
WorkingDirectory=/home/<user>/sagemic
```

Then `sudo systemctl daemon-reload && sudo systemctl restart sagemic_local.service`.
Undo with `sudo systemctl revert sagemic_local.service`.

`requirements-lock.txt` was captured on a Raspberry Pi 5 (64-bit Raspberry Pi OS, Python 3.11).


## Results

All toy, crowd and music recordings come from one lab session (2026-10-28) with the `sagemic` mic,
at `CUSTOM_CONF: 0.25`:

| Test | Result |
|---|---|
| Toy alone, quiet room | 126 / 127 calls |
| Toy 1 ft from mic, crowd playback 1 m | 17 / 17 |
| Toy 1 m from mic, crowd playback 1 ft | 10 / 16 |
| Toy with music or people talking | 8 / 8 |
| **False alarms**: room, crowd, talking, and party-style music (flute, violin, clarinet, trumpet, whistling, glass clinks) the model never trained on | **0 / 4,086** windows |
| Wrong bird named | 0 |
| Real birds (held-out Xeno-canto recordings) | 100% |

Toy-alone results were scored with heads that had not trained on that recording.

### What made the difference

Each step was kept only if it helped without adding false alarms:

| Training data | Effect |
|---|---|
| Xeno-canto birds + noise | Real birds fine, but the Steller's jay toy was called **house finch** (11 of 13) |
| + toy recordings | Fixed the toys: jay 13/13, quail 26/27 |
| + party music as noise | Removed false alarms on near-silent audio |
| **+ synthetic toy-under-crowd/music mixes** | Toy behind a close crowd: **4/16 → 10/16**, no new false alarms |

**The mixes:** each toy call was mixed, as audio, with crowd and music at set loudness ratios, then
embedded with BirdNET. Training used only mixes where the toy is at least 5 dB louder than the noise
in its pitch band (1.2-9.6 kHz). Including quieter mixes, where the toy is effectively inaudible,
raised the hard case further but taught the head that crowd sound alone could be a quail
(28 false alarms). On held-out synthetic mixes with toy and noise equally loud, calls caught rose
from 35% to 83% (crowd) and 50% to 95% (music).

## How it was built

**Training data:**
- **Real birds:** Xeno-canto recordings of the four species (1,571 recordings), using 3 s windows
  where stock BirdNET scored that species at 0.7 or higher.
- **Toys:** lab recordings of each toy at 1 in, 1 ft and 1 m. Call windows were found by loudness in
  the toys' band (at least 1 s of call per window); room tone between presses is `noise`.
- **Noise:** Freesound crowd and party ambience, Freesound music, and lab room tone.
- **Mixes:** described above.

**Leakage rules:** train/test splits by recording (Xeno-canto) or uploader (Freesound). Our mic
recordings of crowd playback, and all test mixes, use only test-split Freesound files. A toy file's
clean and mixed windows always stay on the same side of a split.

**Scripts** (`scripts/`), in order:

| Script | Does |
|---|---|
| `embed.py` (+ `bn.py`) | audio → one row of 1,024 numbers per 3 s window |
| `build_dataset.py --margin 3` | merges everything, labels windows, applies hand labels from `labels/` |
| `mix_toys.py` | makes and embeds the toy-under-crowd/music mixes |
| `train_eval.py` | trains candidate heads and runs every test, incl. the `CUSTOM_CONF` table |
| `package_head.py` | trains the final model; checks it reproduces the test predictions exactly |
| `replay_sagemic.py` | runs WAV files through sagemic's own `audio_callback`, 3 s at a time |
| `band_check.py` | shows which pitch bands a recording rises above the room in |
| `noise_freesound.py`, `music_freesound.py` | download the Freesound audio (API key needed) |


Audio and embeddings aren't in the repo (size, and Freesound licenses); Freesound sources and
licenses are listed in each download folder's `sources.csv`.

## Known limits

- **One session, one room, one mic.** The venue is untested; a short rehearsal there is the best
  next check.
- **Crowd closer than the toy** is still the weak case (10 of 16), and that test is small.
- **No recording of the quail with crowd noise.** It improved on synthetic mixes but is untested on
  real audio.
- The model and `CUSTOM_CONF` were chosen after comparing a few variants on these same tests, so
  expect real-world results to be somewhat lower.
- Few toy recordings for the jay (13 call windows).
