"""build_dataset.py: merge all embedding files into ONE labeled table (dataset.parquet).

  python build_dataset.py                       # defaults: data=../data  emb=../embeddings
  python build_dataset.py --margin 10 --quiet 3 # loudness rules (see below)

What it does
  1. Loads xc_*.parquet, toys, holdout, ambient, noise_train, noise_test.
  2. Reads species / kind / crowd-source / distance from the toy FILENAMES.
  3. Measures each 3 s window's loudness (RMS dB) from the audio itself.
  4. Labels windows:
       xc            -> species from folder, group = XC recording id
       ambient/noise/music -> "noise"  (music_test and noise_test are test_only)
       crowd only    -> "noise", group = Freesound id
       crowd+toy     -> same idea as toy alone, but the floor is the crowd (per file), not the room:
                        call = >= --min-call s above the crowd's normal swing + --crowd-margin dB
       toy alone     -> loudness = the loudest 100 ms inside the 3 s window, measured only in
                        the toys' pitch band (--band, default 1200-9600 Hz) so room hum, the
                        start-up thump and handling rumble don't count
  The first window of every lab recording (toys, ambient, holdout) is dropped: it holds the
  recorder's start-up thump, which the continuous sagemic stream never has.
                        call    = at least --min-call seconds (default 1.0) of 100 ms frames
                                  at >= ambient_p95 + margin dB           -> species
                        quiet   = loudest frame < ambient_p95 + quiet dB  -> "noise"
                        partial = anything else (a call fragment at the window's edge, or a
                                  faint call): left out of training AND testing
       holdout with no species and no "toy" in its name (e.g. real_talking) -> "noise"
       music+toy, holdout with a toy -> "review" (label by ear)
  5. Writes label_review.csv with every "review" window. Fill the `label` column
     (a species name, or noise) for the ones you can judge, save as label_overrides.csv,
     and re-run: the overrides win over the automatic labels.

Output columns (besides embeddings e0..e1023 and the bn_* scores):
  set, kind, species, toy_dist, other_dist, many, label, label_src, group, role, own, rms_db

  role = "trainable" or "test_only". test_only covers: holdout, music+toy, noise_test,
         and any crowd playback whose Freesound source is in noise/test (or can't be found).
  own  = stock BirdNET's score for the window's own species (XC only), for thresholding later.
"""
import argparse, glob, re
from pathlib import Path
import numpy as np, pandas as pd, librosa
from scipy.signal import butter, sosfiltfilt

SR = 48000
SPECIES = ["house_finch", "western_meadowlark", "stellers_jay", "california_quail"]
SP_RE = re.compile("(house_finch|western_meadowlark|stellers_jay|stellars_jay|california_quail|quail)")

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="../data")
ap.add_argument("--emb", default="../embeddings")
ap.add_argument("--margin", type=float, default=6.0, help="dB above ambient p95 = toy is calling")
ap.add_argument("--quiet", type=float, default=3.0, help="dB above ambient p95 = still room tone")
ap.add_argument("--crowd-margin", type=float, default=3.0,
                help="dB above the crowd's normal swing = toy calling (crowd+toy files)")
ap.add_argument("--min-call", type=float, default=1.0,
                help="seconds of call needed in a 3 s window to label it the species")
ap.add_argument("--band", type=float, nargs=2, default=[1200, 9600],
                help="pitch band (Hz) where loudness is measured; band_check.py showed the toys live here")
args = ap.parse_args()
emb = Path(args.emb)

def load(pattern, set_name):
    files = sorted(glob.glob(str(emb / pattern)))
    if not files:
        print(f"  (no files match {pattern}; skipping {set_name})")
        return pd.DataFrame()
    d = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    d.insert(0, "set", set_name)
    return d

parts = [load("xc_*.parquet", "xc"), load("toys.parquet", "toys"), load("holdout.parquet", "holdout"),
         load("ambient.parquet", "ambient"), load("noise_train.parquet", "noise_train"),
         load("noise_test.parquet", "noise_test"),
         load("music_train.parquet", "music_train"), load("music_test.parquet", "music_test")]
df = pd.concat([p for p in parts if len(p)], ignore_index=True)
df["file"] = df["file"].str.replace("stellars_jay", "stellers_jay")
# Lab recordings start with a start-up thump; the live sagemic stream never has one. Drop window 0.
startup = df.set.isin(["toys", "ambient", "holdout"]) & (df.start_s == 0)
print(f"dropping {int(startup.sum())} first windows of lab recordings (start-up thump)")
df = df[~startup].reset_index(drop=True)
E = [c for c in df.columns if c.startswith("e") and c[1:].isdigit()]

# ---- metadata from filenames --------------------------------------------------
def parse(row):
    s, stem = row["set"], Path(row.name).stem.lower()
    path_l = str(Path(row.name)).lower()
    sp = SP_RE.search(stem) or SP_RE.search(path_l)   # filename first, then folder names
    sp = sp.group(1).replace("stellars", "stellers") if sp else ""
    if sp == "quail": sp = "california_quail"
    fs = re.search(r"fs(\d+)", stem)
    D = r"(?<![a-z0-9])(1in|1ft|1m)(?![a-z0-9])"
    m = SP_RE.search(stem)
    after = re.search(D, stem[m.end():]) if m else None
    before = re.search(D, stem[:m.start()]) if m else re.search(D, stem)
    toy_dist = after.group(1) if after else (before.group(1) if (before and not stem.startswith(("crowd", "jurassic"))) else "")
    other_dist = before.group(1) if (before and stem.startswith(("crowd", "jurassic"))) else ""
    out = dict(kind=s, species=sp, toy_dist=toy_dist, other_dist=other_dist, many="many" in stem,
               fs_id=fs.group(1) if fs else "")
    if s == "toys":
        if stem.startswith("jurassic"): out["kind"] = "music_toy"
        elif stem.startswith("crowd") and sp: out["kind"] = "crowd_toy"
        elif stem.startswith("crowd"): out["kind"] = "crowd_only"
        elif sp: out["kind"] = "toy_alone"
        else: out["kind"] = "unknown"
    group = f"fs{fs.group(1)}" if (fs and s == "toys") else (row["source_id"] if s == "xc" else stem)
    out["group"] = f"{s}:{group}"
    return pd.Series(out)

meta = df.drop_duplicates("file").set_index("file").apply(parse, axis=1)
df = df.join(meta, on="file")
if (df.kind == "unknown").any():
    print("WARNING unrecognized toy files:", df[df.kind == "unknown"].file.unique()[:5])

# ---- loudness per window --------------------------------------------------------
SOS = butter(4, args.band, btype="bandpass", fs=SR, output="sos")
rms = {}
for f in df[df.set != "xc"].file.unique():
    # the parquet may hold the pre-rename path; try both spellings
    p = Path(f)
    if not p.exists(): p = Path(f.replace("stellers_jay", "stellars_jay"))
    y, _ = librosa.load(p, sr=SR, mono=True)
    rms[f] = (y, sosfiltfilt(SOS, y))
FRAME = int(0.1 * SR)  # 100 ms
def window_db(r):
    """(broadband average level over the 3 s window, loudest 100 ms within the band) in dB"""
    got = rms.get(r["file"])
    if got is None: return (np.nan, np.nan, None)
    y, yb = got
    a, b = int(r["start_s"] * SR), int(r["end_s"] * SR)
    seg, segb = y[a:b], yb[a:b]
    if len(segb) < FRAME: return (np.nan, np.nan, None)
    n = len(segb) // FRAME
    frames = np.sqrt(np.mean(segb[: n * FRAME].reshape(n, FRAME) ** 2, axis=1))
    db = lambda v: 20 * np.log10(v + 1e-9)
    fdb = db(frames)
    return (db(np.sqrt(np.mean(seg ** 2))), fdb.max(), fdb)
levels = [window_db(r) for _, r in df.iterrows()]
df["rms_db"] = [l[0] for l in levels]
df["peak_db"] = [l[1] for l in levels]
frame_db = [l[2] for l in levels]

amb = df[df.set == "ambient"].peak_db.dropna()
floor = np.percentile(amb, 95) if len(amb) else np.nan
print(f"Loudness = loudest 100 ms in each 3 s window, measured only in {args.band[0]:.0f}-{args.band[1]:.0f} Hz (peak_db)")
print(f"ambient room: {len(amb)} windows, p5/p50/p95 = "
      f"{np.percentile(amb,5):.1f} / {np.median(amb):.1f} / {floor:.1f} dB" if len(amb) else "no ambient windows")
for k in ["toy_alone", "crowd_only", "crowd_toy", "music_toy"]:
    v = df[df.kind == k].peak_db.dropna()
    if len(v): print(f"{k:11s}: p5/p50/p95 = {np.percentile(v,5):.1f} / {np.median(v):.1f} / {np.percentile(v,95):.1f} dB")
if not np.isfinite(floor):
    raise SystemExit("Need ambient windows for the loudness floor. Is ambient.parquet there?")
call_level = floor + args.margin
df["call_s"] = [np.nan if f is None else float((f >= call_level).sum()) * 0.1 for f in frame_db]
fp = int((df[df.set == "ambient"].call_s >= args.min_call).sum())
print(f"call rule: >= {args.min_call:.1f} s of frames at >= {call_level:.1f} dB (room p95 + {args.margin:g} dB)")
print(f"  self-check: ambient-room windows that would count as a call = {fp} (should be 0)"
      + ("" if fp == 0 else "  <-- margin too low, raise --margin"))

# ---- crowd + toy: the crowd sets the floor, not the empty room ------------------------
# Each mix file has its own crowd level (crowd at 1 ft vs 1 m), so the floor is set per file:
#   base  = that file's 25th-percentile frame level (mostly crowd, between presses)
#   swing = how far crowd ALONE rises above its own 25th percentile (p95 - p25), measured on the
#           crowd-only recordings
#   call  = frames above base + swing + --crowd-margin   (louder than crowd alone ever gets)
#   quiet = loudest frame below base + swing             (nothing beyond normal crowd)
def frames_by_file(mask):
    out = {}
    for i in np.where(mask)[0]:
        if frame_db[i] is not None:
            out.setdefault(df.file.iat[i], []).append(frame_db[i])
    return {k: np.concatenate(v) for k, v in out.items()}
crowd_frames = frames_by_file((df.kind == "crowd_only").to_numpy())
mix_frames = frames_by_file((df.kind == "crowd_toy").to_numpy())
crowd_lv = {}
if crowd_frames:
    swing = float(np.median([np.percentile(v, 95) - np.percentile(v, 25) for v in crowd_frames.values()]))
    for f, v in {**crowd_frames, **mix_frames}.items():
        base = float(np.percentile(v, 25))
        crowd_lv[f] = (base + swing, base + swing + args.crowd_margin)
    for i in np.where(df.kind.isin(["crowd_only", "crowd_toy"]).to_numpy())[0]:
        f = df.file.iat[i]
        if frame_db[i] is not None and f in crowd_lv:
            df.iat[i, df.columns.get_loc("call_s")] = float((frame_db[i] >= crowd_lv[f][1]).sum()) * 0.1
    fp = int((df[df.kind == "crowd_only"].call_s >= args.min_call).sum())
    print(f"crowd rule: per file, >= {args.min_call:.1f} s of frames above (file p25 + crowd swing "
          f"{swing:.1f} dB + {args.crowd_margin:g} dB)")
    print(f"  self-check: crowd-only windows that would count as a call = {fp} (should be 0)"
          + ("" if fp == 0 else "  <-- raise --crowd-margin"))
elif mix_frames:
    print("no crowd-only recordings: crowd+toy windows stay 'review'")

# ---- labels ----------------------------------------------------------------------
label, src = [], []
for r in df.itertuples():
    if r.set == "xc": l, s = r.label, "xc_folder"
    elif r.set in ("ambient", "noise_train", "noise_test", "music_train", "music_test"): l, s = "noise", r.set
    elif r.kind == "crowd_only": l, s = "noise", "crowd_only"
    elif r.set == "holdout" and not r.species and "toy" not in Path(r.file).stem.lower():
        l, s = "noise", "holdout_no_toy"
    elif r.kind == "toy_alone":
        if r.call_s >= args.min_call: l, s = r.species, "loud"
        elif r.peak_db < floor + args.quiet: l, s = "noise", "quiet"
        else: l, s = "partial", "edge_or_faint"   # a bit of call, too little to trust: not used
    elif r.kind == "crowd_toy" and r.file in crowd_lv:
        if r.call_s >= args.min_call: l, s = r.species, "crowd_loud"
        elif r.peak_db < crowd_lv[r.file][0]: l, s = "noise", "crowd_quiet"
        else: l, s = "partial", "crowd_edge_or_faint"
    else: l, s = "review", r.kind
    label.append(l); src.append(s)
df["label"], df["label_src"] = label, src

df["own"] = 0.0
for sp in SPECIES:
    m = (df.set == "xc") & (df.label == sp)
    df.loc[m, "own"] = df.loc[m, f"bn_{sp}"]

# Which noise split does each crowd-playback source belong to? (leakage rule)
def source_split(fs_id):
    hits = {st for f, st in ((Path(f).stem, st) for st in ("noise_train", "noise_test")
                             for f in df[df.set == st].file.unique())
            if re.search(rf"(?<!\d){fs_id}(?!\d)", f)}
    return "noise_test" if "noise_test" in hits else ("noise_train" if hits else "not_found")
src_split = {fid: source_split(fid) for fid in df.loc[df.set == "toys", "fs_id"].unique() if fid}
for fid, where in src_split.items():
    print(f"crowd playback fs{fid}: source is in {where}" + ("" if where == "noise_train" else "  -> its recordings are test_only"))

def role(r):
    if r.set in ("holdout", "noise_test", "music_test") or r.kind == "music_toy": return "test_only"
    if r.set == "toys" and r.fs_id and src_split.get(r.fs_id) != "noise_train": return "test_only"
    return "trainable"
df["role"] = [role(r) for r in df.itertuples()]

ov = Path("label_overrides.csv")
if ov.exists():
    o = pd.read_csv(ov).dropna(subset=["label"])
    key = o.set_index(["file", "start_s"]).label.to_dict()
    hit = [(f, s) in key for f, s in zip(df.file, df.start_s.round(2))]
    for i, (f, s) in enumerate(zip(df.file, df.start_s.round(2))):
        if (f, s) in key: df.iloc[i, df.columns.get_loc("label")] = key[(f, s)]; df.iloc[i, df.columns.get_loc("label_src")] = "manual"
    print(f"applied {sum(hit)} overrides from {ov}")

rev = df[df.label == "review"][["file", "start_s", "end_s", "kind", "species", "toy_dist", "role", "peak_db", "rms_db"]].copy()
rev["label"] = ""
rev.round(2).to_csv("label_review.csv", index=False)

keep = ["set", "kind", "species", "toy_dist", "other_dist", "many", "label", "label_src", "group", "role", "own",
        "peak_db", "call_s", "rms_db", "file", "source_id",
        "start_s", "end_s", "top1_species", "top1_conf"] + [c for c in df.columns if c.startswith("bn_")] + E
df[keep].to_parquet(emb / "dataset.parquet", index=False)

print("\nwindows by set x label:")
print(pd.crosstab(df.set, df.label))
print("\nwindows by role x label:")
print(pd.crosstab(df.role, df.label))
t = df[df.kind == "toy_alone"]
print("\ntoy-alone windows per species (auto labels):")
print(pd.crosstab(t.species, t.label))
c = df[df.kind == "crowd_toy"]
if len(c):
    print("\ncrowd+toy windows by species, toy distance, crowd distance (auto labels, test only):")
    cc = c.assign(lab=np.where(c.label == c.species, "call", c.label))
    print(pd.crosstab([cc.species, cc.toy_dist, cc.other_dist], cc.lab))
print("\ntoy-alone windows by species and toy distance (call / partial / noise):")
tt = t.assign(lab=np.where(t.label == t.species, "call", t.label))
print(pd.crosstab([tt.species, tt.toy_dist], tt.lab))
print(f"\nwrote {emb/'dataset.parquet'} ({len(df)} windows); {len(rev)} windows need review -> label_review.csv")
