"""embed.py: turn audio files into BirdNET embeddings, one row per 3-second window.

Each row holds:
  file, source_id, label, start_s, end_s       <- where the window came from
  top1_species, top1_conf                       <- stock BirdNET's best guess for the window
  bn_<species> for the 4 target species         <- stock BirdNET's score for each target (baseline)
  e0 ... e1023                                  <- the embedding the head trains on

Usage:
  python embed.py --input ../data/xc --out ../embeddings/xc.parquet --label-from folder
  python embed.py --input ../data/toys --out ../embeddings/toys.parquet --label-from none

  --label-from folder : label = name of the file's parent folder (e.g. data/xc/stellers_jay/XC1.wav)
  --label-from none   : label left empty (fill it in later, e.g. for toy recordings)

The same embed_array() function is used for files, toy recordings and noisy mixes,
so every kind of data goes through BirdNET identically.

Needs: bn.py (make_analyzer) in the same folder; pip install pandas pyarrow
"""
import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

import librosa
import numpy as np
import pandas as pd
from birdnetlib import RecordingBuffer

from bn import make_analyzer

SR = 48000  # BirdNET's sample rate; everything is resampled to this
TARGETS = {  # scientific name -> short column name
    "Haemorhous mexicanus": "house_finch",
    "Sturnella neglecta": "western_meadowlark",
    "Cyanocitta stelleri": "stellers_jay",
    "Callipepla californica": "california_quail",
}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def embed_array(analyzer, y):
    """Run BirdNET over a 48 kHz mono array. Returns one dict per 3 s window."""
    with contextlib.redirect_stdout(io.StringIO()):  # birdnetlib prints a lot
        rec = RecordingBuffer(analyzer, y.astype("float32"), SR, min_conf=0.01)
        rec.analyze()
        rec.extract_embeddings()

    # Stock BirdNET detections, grouped by window start time
    by_window = {}
    for d in rec.detections:
        by_window.setdefault(round(d["start_time"], 2), []).append(d)

    rows = []
    for e in rec.embeddings:
        start = round(e["start_time"], 2)
        dets = by_window.get(start, [])
        top = max(dets, key=lambda d: d["confidence"], default=None)
        row = {
            "start_s": start,
            "end_s": round(e["end_time"], 2),
            "top1_species": top["common_name"] if top else "",
            "top1_conf": top["confidence"] if top else 0.0,
        }
        for sci, short in TARGETS.items():  # 0.0 = below BirdNET's 0.01 floor
            row[f"bn_{short}"] = max(
                (d["confidence"] for d in dets if d["scientific_name"] == sci), default=0.0
            )
        row.update({f"e{i}": v for i, v in enumerate(e["embeddings"])})
        rows.append(row)
    return rows


def source_id(path):
    """Group key for leakage-safe splits: the XC recording ID if present, else the file stem."""
    m = re.search(r"XC\d+", path.name)
    return m.group(0) if m else path.stem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="folder of audio files (searched recursively)")
    ap.add_argument("--out", required=True, help="output .parquet file")
    ap.add_argument("--label-from", choices=["folder", "none"], default="folder")
    ap.add_argument("--limit", type=int, default=0, help="only the first N files (for testing)")
    args = ap.parse_args()

    files = sorted(p for p in Path(args.input).rglob("*") if p.suffix.lower() in AUDIO_EXT)
    if args.limit:
        files = files[: args.limit]
    if not files:
        sys.exit(f"No audio files found under {args.input}")

    analyzer = make_analyzer()
    all_rows, failed = [], []
    for n, path in enumerate(files, 1):
        try:
            y, _ = librosa.load(path, sr=SR, mono=True)
            rows = embed_array(analyzer, y)
        except Exception as exc:  # keep going; report at the end
            failed.append((path, exc))
            continue
        label = path.parent.name if args.label_from == "folder" else ""
        for r in rows:
            r.update(file=str(path), source_id=source_id(path), label=label)
        all_rows.extend(rows)
        print(f"[{n}/{len(files)}] {path.name}: {len(rows)} windows")

    df = pd.DataFrame(all_rows)
    meta = ["file", "source_id", "label", "start_s", "end_s", "top1_species", "top1_conf"]
    meta += [f"bn_{s}" for s in TARGETS.values()]
    df = df[meta + [c for c in df.columns if c not in meta]]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)

    print(f"\nSaved {len(df)} windows from {len(files) - len(failed)} files to {args.out}")
    for path, exc in failed:
        print(f"  FAILED {path}: {exc}")


if __name__ == "__main__":
    main()
