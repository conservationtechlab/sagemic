# Custom head for sagemic: 4 species + noise

A small classifier that sits on top of BirdNET for the donor demo. BirdNET (2.4, via
`birdnetlib`) turns each 3-second block of audio into 1,024 numbers (an "embedding"); this head
reads those numbers and answers one of five classes:

| Class | Label in config | Notes |
|---|---|---|
| 0 | `haemorhous_mexicanus` | house finch |
| 1 | `sturnella_neglecta` | western meadowlark |
| 2 | `cyanocitta_stelleri` | Steller's jay |
| 3 | `callipepla_californica` | California quail |
| 4 | `noise` | anything else: room, crowd, talking, music. Never reported. |

The model is a scikit-learn pipeline (scaler + linear SVM) saved as `sagemic_head.joblib`
(65 KB). It plugs into the custom-model path on the `82_svm` branch with no code changes.

## Demo day

From the repo root on the demo Pi, on this branch:

1. `git pull`
2. Open `config/demo_custom_head.yaml` and fill in the three values marked
   `FROM CURRENT DEMO CONFIG` (`BASE_PATH` = the folder the lightbox watches, `AUDIO_DEVICE`,
   `AUDIO_DTYPE`), copied from the demo Pi's existing `config.yaml`. Everything else is set.
3. Run sagemic with this config (or point `sagemic_local.service`'s `--config` at it and restart):
   `python sagemic_local.py --config config/demo_custom_head.yaml`
   If the model file won't load, see "Install on a sagemic Pi" below.
4. Press a toy.

## Config

`config/demo_custom_head.yaml` has everything set except three device values. The model lines are:

```yaml
PATHS:
  CUSTOM_MODEL_PATH: "custom_head/sagemic_head.joblib"   # relative to the repo root
SETTINGS:
  MODEL_TYPE: "custom"
  CUSTOM_CONF: 0.0
  CUSTOM_LABELS: [haemorhous_mexicanus, sturnella_neglecta, cyanocitta_stelleri, callipepla_californica, noise]
```

The `CUSTOM_LABELS` order must stay exactly as above: the model outputs class numbers 0-4 and
sagemic looks the name up by position. The names match what stock BirdNET mode already writes
(lowercase scientific names), so detection filenames look the same as before.

## Install on a sagemic Pi

The model was saved with **Python 3.11, scikit-learn 1.9.1, joblib 1.6.0, numpy 2.4.6**. Loading it
with a different scikit-learn version can fail or behave differently, so install from the lock file
in a fresh environment rather than into an existing one:

```bash
cd ~/sagemic                      # this branch
python3.11 -m venv .venv-head
source .venv-head/bin/activate
pip install -e .
pip install -r custom_head/requirements-lock.txt
```

`requirements-lock.txt` was captured on a Raspberry Pi 5 (64-bit Raspberry Pi OS, Python 3.11) and
should suit a Pi 4B as well. `pyproject.toml` now also lists `scikit-learn` and `joblib`, which the
custom-model path needs but weren't declared before.

## Test it without toys

`scripts/replay_sagemic.py` feeds WAV files through the real `audio_callback` from
`sagemic_local.py`, 3 seconds at a time, as raw integers like the live stream. It reports what
sagemic would have detected and how long each block took. Detection clips go to a temp folder.

```bash
python custom_head/scripts/replay_sagemic.py --config config/demo_custom_head.yaml path/to/recordings/*.wav
```

With `--config` it uses the config's labels, model path and `CUSTOM_CONF`, and refuses to run if
the number of labels doesn't match the model.

| Machine | Time per 3 s block (after a ~0.5 s warm-up) |
|---|---|
| MacBook Pro | ~30 ms |
| Raspberry Pi 5 | ~86 ms |
| Raspberry Pi 4B | not measured yet; same command will tell |

The Mac and the Pi 5 gave identical detections on the same files.

## Results

All toy and noise recordings are from one lab session (2026-10-28) with the `sagemic` mic.

**Scored exams** (`train_eval.py`, at `CUSTOM_CONF: 0.0`; toy-alone files were tested with heads
that had not trained on that file):

| Setup | Toy calls caught |
|---|---|
| Toy alone, quiet room | 126 / 127 |
| Toy 1 ft from mic, crowd playback 1 m | 17 / 17 |
| Toy 1 m from mic, crowd playback 1 ft | 4 / 16 |
| Toy with music (Jurassic Park) or people talking | 8 / 8 |

- **False alarms: 0 of 4,086 windows with no toy**: empty room, Freesound crowd (held-out
  uploaders), our crowd playback, real conversation, and 1,526 windows of held-out party-style
  music chosen to overlap bird pitches (flute, piccolo, violin, clarinet, trumpet, whistling,
  glass clinks).
- **Wrong species reported: 0.** Every miss was silence, never the wrong bird.
- **Held-out Xeno-canto field recordings: 100%.**

**End-to-end through `sagemic_local.audio_callback`** (`replay_sagemic.py`): 156 of 167 labeled
toy-call blocks reported correctly (93%), 0 of 167 no-toy blocks reported.

**Why train on the toys at all:** a head trained only on Xeno-canto birds caught 2 of 13 Steller's
jay toy windows and called the other 11 **house finch**. Adding the toy recordings fixed that
(13 / 13) without adding false alarms.

## Demo placement

- **Keep every toy closer to the mic than the guests.** With the toy closer, it caught every call
  we tested; with people closer than the toy, about a quarter.
- **Put the meadowlark closest.** Behind a close crowd it was never detected (0 of 14 blocks),
  probably because its call (1.2-4.8 kHz) sits where speech is loudest.
- Misses are silent: a buried toy just doesn't light up; it won't light the wrong bird.

## Lightbox notes

- Clip names are unchanged in form: `{date}_{time}_{scientific_name}_{confidence}.flac`.
- **The confidence number has a different scale.** With stock BirdNET it is a 0-1 score. With this
  head it is the SVM's score: typically about 0 to 2.5, not a probability. If the lightbox uses
  the number (a minimum, brightness, etc.), it needs adjusting.
- `noise` never produces a file, so there is no "nothing heard" message; lights need their own
  time-out.

## Tuning `CUSTOM_CONF`

A bird is reported only if its score is above `CUSTOM_CONF` (one value, or a list per class).
`0.0` is recommended: no setting from -1 to 2 produced a false alarm in our tests, and 0.0 also
blocks weak, odd wins of the kind an earlier version made on near-silent audio. If false alarms
show up at the venue, raising it trades calls for safety:

| `CUSTOM_CONF` | Quiet room | Toy closer than crowd | Crowd closer than toy | Music / talking |
|---|---|---|---|---|
| 0.0 | 126/127 | 17/17 | 4/16 | 8/8 |
| 0.5 | 125/127 | 17/17 | 3/16 | 7/8 |
| 1.0 | 108/127 | 14/17 | 0/16 | 7/8 |

## How it was built

Training data:

- **Real birds:** Xeno-canto recordings of the four species (1,571 recordings), keeping 3 s windows
  where stock BirdNET scored that species at 0.7 or higher.
- **Toys:** our lab recordings of each toy at 1 in, 1 ft and 1 m (plus "above the mic" positions).
  Call windows were labeled by loudness in the toys' pitch band (1.2-9.6 kHz, at least 1 s of call
  per window); room tone between presses is `noise`.
- **Noise:** Freesound crowd and party ambience (training uploaders), two of three ambient lab
  recordings, and Freesound music (training uploaders).

Final model: 13,002 windows (5,569 noise; 1,426-2,061 per species), linear SVM, balanced class
weights.

Leakage rules followed throughout: train/test splits by recording (Xeno-canto) or uploader
(Freesound); our mic recordings of crowd playback stay on the same side as the Freesound file
that was played (all were test files, so they are test-only); holdout recordings never trained on.

Pipeline (`scripts/`):

| Script | Does |
|---|---|
| `bn.py` | BirdNET analyzer that keeps the embedding tensors (needed for `extract_embeddings`) |
| `embed.py` | audio folders -> one row of 1,024 numbers per 3 s window (parquet) |
| `build_dataset.py --margin 3` | merges everything, labels windows, marks train/test roles; applies hand labels from `labels/label_overrides.csv` |
| `train_eval.py` | trains candidate heads and runs every exam, incl. the `CUSTOM_CONF` table |
| `package_head.py` | trains the final model and checks it reproduces the exam predictions exactly |
| `replay_sagemic.py` | runs WAVs through sagemic's own `audio_callback` |
| `band_check.py` | shows which pitch bands a recording rises above the room in |
| `noise_freesound.py`, `music_freesound.py` | download the Freesound noise and music (needs a Freesound API key) |

Audio and embeddings are not in the repo (size and Freesound licenses); they live on Jeff's
machine and the CTL remote server, with Freesound sources and licenses in `sources.csv`.

## Known limits

- **One session, one room, one mic.** The venue is untested; a quick rehearsal there is the best
  next check.
- **Small toy counts**, especially the jay (13 training windows from the toys alone) and no
  recording of the quail with crowd noise.
- **Crowd closer than the toy** is the weak case (about 25% caught).
- Toy labels are partly automatic (loudness rules); test files were checked by ear.
- Replaying recordings faster than real time can overwrite clips that share the same second,
  species and confidence in their filename. Not an issue live (blocks are 3 s apart).

## Next steps

- Synthetic training mixes of toy calls over crowd and music at low levels, aimed at the
  crowd-closer case.
- A second recording session (more jay presses at 1 ft and 1 m, quail with crowd) and a venue
  rehearsal.
- Pi 4B timing with `replay_sagemic.py`.

Questions: Jeff Wang (jeff@jeff-wang.com)
