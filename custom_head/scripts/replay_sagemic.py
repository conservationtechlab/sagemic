"""replay_sagemic.py: feed WAV files through Katie's real sagemic audio_callback, 3 s at a time.

  python replay_sagemic.py --sagemic ~/work/sdzwa/sagemic --model sagemic_head_C.joblib \
      ../data/toys/session_1/stellers_jay/*.wav

It imports audio_callback from YOUR CHECKOUT of the sagemic branch (not a copy of its logic),
sets it up exactly as sagemic_local.main() does (BirdNET analyzer with all tensors kept,
RecordingBuffer, joblib model), and calls it once per 3 s block, as the microphone stream would.
Audio is read as raw integers (--dtype int32 by default), like the live stream delivers it.

For every block it records what sagemic reported (bird + confidence, or nothing) and how long the
call took. Detection clips that sagemic saves go to a temporary folder, not your data.

Optional --dataset: compare each block with your labels (dataset.parquet) and print agreement.
Writes replay_results.csv.
"""
import argparse, contextlib, io, re, sys, tempfile, time
from pathlib import Path
import numpy as np, pandas as pd, soundfile as sf, joblib

LABELS = ["haemorhous_mexicanus", "sturnella_neglecta", "cyanocitta_stelleri",
          "callipepla_californica", "noise"]
COMMON = dict(zip(LABELS, ["house_finch", "western_meadowlark", "stellers_jay", "california_quail", "noise"]))

ap = argparse.ArgumentParser()
ap.add_argument("wavs", nargs="+")
ap.add_argument("--sagemic", required=True, help="path to your checkout of the sagemic branch")
ap.add_argument("--model", required=True)
ap.add_argument("--custom-conf", type=float, default=0.0)
ap.add_argument("--dtype", default="int32", choices=["int16", "int32", "float32"],
                help="sample format handed to sagemic (the live stream uses AUDIO_DTYPE)")
ap.add_argument("--dataset", default="", help="dataset.parquet, to compare with your labels")
ap.add_argument("--out", default="replay_results.csv")
args = ap.parse_args()

sys.path.insert(0, str(Path(args.sagemic).expanduser().resolve()))
try:
    import sounddevice  # noqa: F401  (sagemic_local imports it; a replay never opens a microphone)
except OSError:          # no PortAudio on this machine: give sagemic_local a stand-in module
    import types
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")
    print("(PortAudio not found: using a stand-in sounddevice module; fine for replay)")
import sagemic_local                      # Katie's code, from the branch you checked out
from birdnetlib import RecordingBuffer
from birdnetlib.analyzer import Analyzer

SR = 48000
clip_dir = tempfile.mkdtemp(prefix="sagemic_replay_")
config = {"PATHS": {"BASE_PATH": clip_dir},
          "SETTINGS": {"LOCAL_TZ": "America/Los_Angeles", "CONFIDENCE_THRESHOLD": 0.1,
                       "SAMPLERATE": SR, "MODEL_TYPE": "custom",
                       "CUSTOM_CONF": args.custom_conf, "CUSTOM_LABELS": LABELS}}

# --- same setup as sagemic_local.main() -------------------------------------------------------
with contextlib.redirect_stdout(io.StringIO()):
    analyzer = Analyzer()
    custom_model = joblib.load(args.model)
    analyzer.interpreter = type(analyzer.interpreter)(
        model_path=analyzer.model_path, num_threads=1, experimental_preserve_all_tensors=True)
    analyzer.interpreter.allocate_tensors()
    rb = RecordingBuffer(analyzer=analyzer, lat=32.7157, lon=-117.1611, rate=SR,
                         buffer=np.zeros(3 * SR, dtype="float32"))

DET = re.compile(r"\*\* (\S+) Detected w/ \(Confidence: (-?[\d.]+)\)")
rows = []
for wav in args.wavs:
    info = sf.info(wav)
    if info.samplerate != SR:
        print(f"skip {wav}: {info.samplerate} Hz (sagemic runs at {SR} Hz)")
        continue
    y, _ = sf.read(wav, dtype=args.dtype, always_2d=True)
    y = y[:, :1]                                  # mono, shape (frames, 1) like sounddevice
    for k in range(len(y) // (3 * SR)):          # whole 3 s blocks, aligned at 0 like embed.py
        block = np.ascontiguousarray(y[k * 3 * SR:(k + 1) * 3 * SR])
        out = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(out):
            sagemic_local.audio_callback(block, len(block), None, None,
                                         recording_buffer=rb, config=config, custom_model=custom_model)
        ms = (time.perf_counter() - t0) * 1000
        m = DET.search(out.getvalue())
        rows.append({"file": wav, "start_s": float(k * 3), "reported": m.group(1) if m else "",
                     "confidence": float(m.group(2)) if m else np.nan, "ms": round(ms, 1)})
    print(f"{Path(wav).name}: {len([r for r in rows if r['file'] == wav])} blocks", flush=True)

res = pd.DataFrame(rows)
if res.empty:
    sys.exit("no blocks replayed")
res.to_csv(args.out, index=False)
n_clips = len(list(Path(clip_dir).rglob("*.flac")))
print(f"\n{len(res)} blocks; sagemic reported a bird in {int((res.reported != '').sum())}; "
      f"saved {n_clips} detection clips (in {clip_dir})")
warm = res.ms.iloc[1:] if len(res) > 1 else res.ms   # block 1 includes TensorFlow warm-up
print(f"time per 3 s block: first (warm-up) {res.ms.iloc[0]:.0f} ms; after that median {warm.median():.0f} ms, "
      f"95th pct {warm.quantile(0.95):.0f} ms, max {warm.max():.0f} ms  (must stay well under 3000 ms)")
print("\nwhat sagemic reported:")
print(res.reported.replace("", "(nothing)").map(lambda s: COMMON.get(s, s)).value_counts().to_string())

if args.dataset:
    d = pd.read_parquet(args.dataset, columns=["file", "start_s", "label"])
    d["key"] = d.file.map(lambda f: str(Path(f).resolve())) + "|" + d.start_s.round(2).astype(str)
    res["key"] = res.file.map(lambda f: str(Path(f).resolve())) + "|" + res.start_s.round(2).astype(str)
    j = res.merge(d[["key", "label"]], on="key", how="left")
    j = j[j.label.isin(list(COMMON.values()))]
    if len(j):
        j["said"] = j.reported.map(lambda s: COMMON.get(s, "noise") if s else "noise")
        print(f"\nagreement with your labels ({len(j)} labeled blocks): {(j.said == j.label).mean():.1%}")
        print(pd.crosstab(j.label, j.said).to_string())
print(f"\nwrote {args.out}")
