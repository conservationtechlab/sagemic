"""band_check.py: in which pitch bands does a toy recording rise above the room?

  python band_check.py ../data/toys/session_1/california_quail/quail_session_1in_many.wav

For each band it compares the toy file's loud moments (95th percentile of 100 ms frames)
with the ambient room recordings' typical level (median). A big difference = the toy's
calls live in that band. A small difference = that band is mostly room hum / noise.
"""
import sys, glob
import numpy as np, librosa

SR = 48000
BANDS = [(0, 150), (150, 300), (300, 600), (600, 1200), (1200, 2400), (2400, 4800),
         (4800, 9600), (9600, 20000)]

def band_levels(path):
    y, _ = librosa.load(path, sr=SR, mono=True)
    y = y[SR:]  # skip the first second (start-up thump)
    S = np.abs(librosa.stft(y, n_fft=4800, hop_length=4800)) ** 2  # 100 ms frames
    f = librosa.fft_frequencies(sr=SR, n_fft=4800)
    return np.array([10 * np.log10(S[(f >= lo) & (f < hi)].sum(axis=0) + 1e-12) for lo, hi in BANDS])

toy = band_levels(sys.argv[1])
amb_files = sorted(glob.glob("../data/ambient_room/**/*.wav", recursive=True))
if not amb_files:
    sys.exit("No ambient recordings found under ../data/ambient_room (run this from the birdnet folder)")
amb = np.concatenate([band_levels(p) for p in amb_files], axis=1)
print(f"{'band (Hz)':>14s} {'toy loud':>9s} {'room':>7s} {'diff':>6s}")
for (lo, hi), t, a in zip(BANDS, toy, amb):
    d = np.percentile(t, 95) - np.median(a)
    print(f"{lo:>6d}-{hi:<7d} {np.percentile(t,95):9.1f} {np.median(a):7.1f} {d:6.1f}  " + "#" * max(0, int(d / 2)))
