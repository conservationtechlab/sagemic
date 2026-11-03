"""mix_toys.py: synthetic "toy under crowd / music" examples, embedded with BirdNET.

  python mix_toys.py --dataset ../embeddings/dataset.parquet --data ../data \
      --out ../embeddings/mixes.parquet
  python mix_toys.py ... --limit 3       # quick trial: 3 toy windows per species

What it does
  1. Takes every toy-alone CALL window from dataset.parquet (the 3 s windows labeled as a species).
  2. For each window and each loudness ratio (--snr, default 10 5 0 -5 dB), adds a random 3 s
     stretch of crowd noise and of music, in the waveform, before BirdNET.
       * training mixes use only the TRAINING-split noise/music (data/noise/train, data/music/train)
       * test mixes use only the TEST-split noise/music (data/noise/test, data/music/test)
  3. Embeds each mix with BirdNET (same code path as embed.py) and writes one parquet row per mix.

Loudness ratio ("SNR"), measured only in the toys' pitch band (--band, default 1200-9600 Hz):
  toy level   = average power of the window's loudest 30% of 100 ms frames (the call itself, not the
                gaps inside the window)
  noise level = average power of the 3 s noise stretch
  +10 dB: toy clearly louder than the noise.  0 dB: equal.  -5 dB: noise louder than the toy.

Each row keeps the toy window's file and start time, so train_eval.py can keep a toy file's clean
and mixed windows on the same side of every train/test split.
"""
import argparse, contextlib, io, sys
from pathlib import Path
import numpy as np, pandas as pd, librosa, soundfile as sf
from scipy.signal import butter, sosfiltfilt

from bn import make_analyzer
from embed import embed_array

SR = 48000
WIN = 3 * SR
FRAME = SR // 10
SPECIES = ["house_finch", "western_meadowlark", "stellers_jay", "california_quail"]
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}

ap = argparse.ArgumentParser()
ap.add_argument("--dataset", default="../embeddings/dataset.parquet")
ap.add_argument("--data", default="../data")
ap.add_argument("--out", default="../embeddings/mixes.parquet")
ap.add_argument("--snr", type=float, nargs="+", default=[10, 5, 0, -5])
ap.add_argument("--per-train", type=int, default=2, help="noise stretches per window/ratio/kind (training)")
ap.add_argument("--per-test", type=int, default=1, help="noise stretches per window/ratio/kind (test)")
ap.add_argument("--pool", type=int, default=200, help="noise stretches kept per kind/split")
ap.add_argument("--band", type=float, nargs=2, default=[1200, 9600])
ap.add_argument("--limit", type=int, default=0, help="trial run: only N toy windows PER SPECIES")
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--save-audio", default="", help="folder: also save some mixes as WAV files to listen to")
ap.add_argument("--save-n", type=int, default=64,
                help="max WAVs with --save-audio: one per species x noise kind x ratio x split")
args = ap.parse_args()
rng = np.random.default_rng(args.seed)
SOS = butter(4, args.band, btype="bandpass", fs=SR, output="sos")
data = Path(args.data)

def band_power(y):
    return float(np.mean(sosfiltfilt(SOS, y) ** 2)) + 1e-20

def call_power(y):
    yb = sosfiltfilt(SOS, y)
    n = len(yb) // FRAME
    p = np.mean(yb[: n * FRAME].reshape(n, FRAME) ** 2, axis=1)
    top = np.sort(p)[-max(1, int(0.3 * n)):]
    return float(top.mean()) + 1e-20

# ---- noise pools: (kind, split) -> list of (file, 3 s array) -------------------------------------
def build_pool(folder):
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    if not files:
        sys.exit(f"no audio files in {folder}")
    order = rng.permutation(len(files))
    per_file = max(1, int(np.ceil(args.pool / len(files))) + 1)
    pool = []
    for i in order:
        try:
            y, _ = librosa.load(files[i], sr=SR, mono=True, duration=180)
        except Exception as exc:
            print(f"  skip {files[i].name}: {exc}")
            continue
        if len(y) < WIN:
            continue
        for _ in range(per_file * 3):                      # a few tries per file
            s = int(rng.integers(0, len(y) - WIN + 1))
            seg = y[s:s + WIN]
            if band_power(seg) > 1e-8:                     # skip near-silent stretches
                pool.append((str(files[i]), round(s / SR, 2), seg.astype(np.float32)))
            if sum(1 for f, _, _ in pool if f == str(files[i])) >= per_file:
                break
        if len(pool) >= args.pool:
            break
    return pool

pools = {}
for kind, sub in [("crowd", "noise"), ("music", "music")]:
    for split in ["train", "test"]:
        pools[(kind, split)] = build_pool(data / sub / split)
        print(f"noise pool {kind}/{split}: {len(pools[(kind, split)])} stretches", flush=True)

# ---- toy call windows -----------------------------------------------------------------------------
d = pd.read_parquet(args.dataset, columns=["file", "start_s", "end_s", "kind", "label", "role", "species", "toy_dist"])
calls = d[(d.kind == "toy_alone") & d.label.isin(SPECIES) & (d.role == "trainable")].reset_index(drop=True)
if args.limit:  # per species, so a trial always covers all four birds
    calls = pd.concat([calls[calls.species == sp].sample(min(args.limit, int((calls.species == sp).sum())),
                                                         random_state=args.seed)
                       for sp in SPECIES if (calls.species == sp).any()], ignore_index=True)
print("toy call windows per species: " + ", ".join(f"{k} {v}" for k, v in calls.species.value_counts().items()))
print(f"{len(calls)} toy call windows x {len(args.snr)} ratios x 2 noise kinds "
      f"x ({args.per_train} train + {args.per_test} test) = "
      f"{len(calls) * len(args.snr) * 2 * (args.per_train + args.per_test)} mixes", flush=True)

with contextlib.redirect_stdout(io.StringIO()):
    analyzer = make_analyzer()
saved = set()   # (species, noise kind, ratio, split) combinations already saved as WAV
if args.save_audio:
    Path(args.save_audio).mkdir(parents=True, exist_ok=True)
audio_cache = {}
rows = []
for n, c in calls.iterrows():
    if c.file not in audio_cache:
        audio_cache[c.file] = librosa.load(c.file, sr=SR, mono=True)[0]
    a = int(round(c.start_s * SR))
    toy = audio_cache[c.file][a:a + WIN]
    if len(toy) < WIN:
        continue
    p_call = call_power(toy)
    for split, per in [("train", args.per_train), ("test", args.per_test)]:
        for kind in ["crowd", "music"]:
            pool = pools[(kind, split)]
            for snr in args.snr:
                for _ in range(per):
                    nf, ns, noise = pool[int(rng.integers(len(pool)))]
                    g = np.sqrt(p_call / band_power(noise) / 10 ** (snr / 10))
                    mix = toy + g * noise
                    peak = np.abs(mix).max()
                    if peak > 0.99:
                        mix = mix * (0.99 / peak)              # BirdNET ignores overall level
                    r = embed_array(analyzer, mix.astype(np.float32))[0]
                    combo = (c.species, kind, snr, split)
                    if args.save_audio and combo not in saved and len(saved) < args.save_n:
                        saved.add(combo)
                        name = (f"{c.species}_{kind}_{snr:+.0f}dB_{split}_{Path(c.file).stem}"
                                f"_{c.start_s:.0f}s_{len(rows):05d}.wav")
                        sf.write(Path(args.save_audio) / name, mix.astype(np.float32), SR)
                    r.update(set=f"mix_{split}", kind="toy_mix", label=c.label, species=c.species,
                             toy_dist=c.toy_dist, file=c.file, source_id=Path(c.file).stem,
                             start_s=float(c.start_s), end_s=float(c.end_s),
                             role="trainable" if split == "train" else "test_only",
                             label_src="mix", group=f"toys:{Path(c.file).stem}", own=0.0,
                             noise_kind=kind, noise_file=nf, noise_start_s=ns, snr_db=float(snr))
                    rows.append(r)
    if (n + 1) % 10 == 0 or n + 1 == len(calls):
        print(f"  {n + 1}/{len(calls)} toy windows done ({len(rows)} mixes)", flush=True)

out = pd.DataFrame(rows)
E = [c for c in out.columns if c.startswith("e") and c[1:].isdigit()]
meta = [c for c in out.columns if c not in E]
out = out[meta + E]
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
out.to_parquet(args.out, index=False)
print(f"\nwrote {args.out}: {len(out)} mixes" + (f"; saved {len(saved)} WAVs to {args.save_audio}" if args.save_audio else ""))
print(pd.crosstab([out.set, out.noise_kind], out.snr_db).to_string())
