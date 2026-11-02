"""package_head.py: train the chosen head once and save it in the format Katie's sagemic expects.

  python package_head.py --data ../embeddings/dataset.parquet --out sagemic_head_C.joblib \
                         --check ../results/train_eval_predictions.parquet

What it builds (recipe C, threshold 0.7, linear SVM: the model the exams scored):
  * Same training data as train_eval.py's "full" C model: Xeno-canto windows with stock BirdNET
    own score >= 0.7 from the 80% training recordings, Freesound train noise, 2 of 3 ambient
    files, training-split music, and all toy-alone call/room-tone windows.
  * Labels are the integers 0-4 in CUSTOM_LABELS order (Katie's code does labels[int(classes_[i])]).
  * Saved as ONE joblib file: a scikit-learn Pipeline (StandardScaler -> LinearSVC).

Self-check (--check): reloads the saved file from disk and predicts every exam window that
train_eval.py scored with the full C model. Every prediction should match.

Prints the config lines and versions to paste into the hand-off note.
"""
import argparse, json, platform, sys
from pathlib import Path
import numpy as np, pandas as pd, joblib, sklearn
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC

# Order = CUSTOM_LABELS order. Scientific names match what stock BirdNET mode writes
# (sagemic lowercases and replaces spaces with "_"), so downstream sees the same names.
CLASSES = [("house_finch", "haemorhous_mexicanus"),
           ("western_meadowlark", "sturnella_neglecta"),
           ("stellers_jay", "cyanocitta_stelleri"),
           ("california_quail", "callipepla_californica"),
           ("noise", "noise")]
TO_INT = {common: i for i, (common, _) in enumerate(CLASSES)}
SPECIES = [c for c, _ in CLASSES[:4]]

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="../embeddings/dataset.parquet")
ap.add_argument("--out", default="sagemic_head_C.joblib")
ap.add_argument("--threshold", type=float, default=0.7)
ap.add_argument("--cap", type=int, default=3000, help="must match train_eval.py (default 3000)")
ap.add_argument("--C", type=float, default=1.0)
ap.add_argument("--custom-conf", type=float, default=0.0)
ap.add_argument("--check", default="", help="train_eval_predictions.parquet to compare against")
args = ap.parse_args()

df = pd.read_parquet(args.data)
E = [c for c in df.columns if c.startswith("e") and c[1:].isdigit()]
X = df[E].to_numpy(np.float32)
lab, role, kind, set_ = df.label.to_numpy(), df.role.to_numpy(), df.kind.to_numpy(), df.set.to_numpy()
own, files = df.own.to_numpy(), df.file.to_numpy()

# ---- the same pools, in the same order, as train_eval.py --------------------------------------
xc_idx = np.where(set_ == "xc")[0]
tr, _ = next(GroupShuffleSplit(1, test_size=0.2, random_state=0).split(xc_idx, groups=df.source_id.to_numpy()[xc_idx]))
xc_train = xc_idx[tr]
amb_files = sorted(df.file[set_ == "ambient"].unique())
amb_exam_file = amb_files[-1] if len(amb_files) >= 2 else None
noise_train = np.where(np.isin(set_, ["noise_train", "ambient"]) & (lab == "noise") & (role == "trainable")
                       & (files != amb_exam_file))[0]
toy = np.where((kind == "toy_alone") & np.isin(lab, list(TO_INT)) & (role == "trainable"))[0]
music_train = np.where((set_ == "music_train") & (lab == "noise"))[0]
rng = np.random.default_rng(0)
idx = xc_train[own[xc_train] >= args.threshold]
xc_pool = np.concatenate([rng.permutation(idx[lab[idx] == sp])[: args.cap or None] for sp in SPECIES])
train_idx = np.concatenate([xc_pool, noise_train, music_train, toy])

y = np.array([TO_INT[l] for l in lab[train_idx]])
model = make_pipeline(StandardScaler(),
                      LinearSVC(C=args.C, class_weight="balanced", max_iter=20000))
model.fit(X[train_idx], y)
joblib.dump(model, args.out)
size_kb = Path(args.out).stat().st_size / 1024

counts = pd.Series([CLASSES[i][0] for i in y]).value_counts()
print(f"trained on {len(train_idx)} windows: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
print(f"saved {args.out} ({size_kb:.0f} KB)")

# ---- self-check: reload from disk, compare with the exams ----------------------------------
m = joblib.load(args.out)
assert list(m.classes_) == [0, 1, 2, 3, 4], m.classes_
if args.check:
    pp = pd.read_parquet(args.check)
    cand = f"C thr{args.threshold} linear"
    ref = pp[(pp.candidate == cand) & (pp.exam != "toy")]  # toy exam used per-fold models, not the full one
    if ref.empty:
        sys.exit(f"no '{cand}' rows in {args.check}; re-run train_eval.py with recipe C at this threshold")
    d = m.decision_function(X[ref.row.to_numpy()])
    best = d.argmax(axis=1)
    pred = np.array([CLASSES[m.classes_[b]][0] for b in best])
    score = d[np.arange(len(best)), best]
    same = (pred == ref.pred.to_numpy())
    print(f"self-check vs {cand}: {same.sum()}/{len(same)} exam predictions identical"
          + ("" if same.all() else "  <-- MISMATCH: the packaged model is not the one the exams scored"))
    if "score" in ref and ref.score.notna().any():
        print(f"  largest score difference: {np.nanmax(np.abs(score - ref.score.to_numpy())):.2e}")

# ---- what to paste into the note / config ----------------------------------------------------
labels = [sci for _, sci in CLASSES]
print("\n--- config lines (SETTINGS / PATHS) ---")
print('MODEL_TYPE: "custom"')
print(f'CUSTOM_MODEL_PATH: "/path/to/{Path(args.out).name}"')
print(f"CUSTOM_CONF: {args.custom_conf}")
print("CUSTOM_LABELS: [" + ", ".join(labels) + "]")
print("\n--- versions this file was saved with (install the same on the device) ---")
print(json.dumps({"python": platform.python_version(), "scikit-learn": sklearn.__version__,
                  "joblib": joblib.__version__, "numpy": np.__version__}, indent=2))
