"""train_eval.py: build candidate heads and give every one the same exams.

  python train_eval.py --kernels linear        # fast first look (a few minutes)
  python train_eval.py                         # linear + RBF (RBF is slower; maybe 15-30 min)

CANDIDATES (every combination of):
  recipe     A = field recordings only: Xeno-canto birds + noise (Freesound train + ambient room)
             B = A + your toy-alone recordings (call windows + room tone between presses)
             C = B + Freesound music (training split) as extra noise examples
  threshold  which Xeno-canto windows count as the bird (stock BirdNET own score >= 0.3 / 0.7)
  kernel     linear or RBF SVM
  Classes: the 4 species + "noise". Windows labeled review / partial are never used.

EXAMS (every candidate takes all of them):
  1. Toy calls   : does it name the right bird for toy-alone call windows, and say "noise" for
                   the room tone between presses?  For recipe B this is done fairly: the toy
                   files are split into --toy-folds groups; each group is tested by a head
                   trained WITHOUT those files (like the sweep did with recordings).
                   Caveat: all toy files are from one session, so this is optimistic.
  2. False alarms: how often does it call a bird when it's only noise it has never heard?
                   empty_room (one ambient-room file held out of training),
                   freesound_test (Freesound test split), crowd_playback (your mic recordings of
                   test-split crowd files), real_talking (holdout).  Lower is better.
  2b. Toy in crowd: crowd+toy recordings (auto-labeled by build_dataset.py, never trained on).
                   c_<species> = toy calls named correctly; c_quiet = crowd-only stretches of
                   those files correctly called noise.  Also broken down by toy/crowd distance.
  2c. Hand-labeled: music+toy and talking+toy windows you labeled in label_overrides.csv.
  3. Field check : names the bird on held-out Xeno-canto recordings (own score >= 0.3), using a
                   fixed 20% of recordings that no candidate ever trains on.

Predictions use argmax of decision_function, the same rule Katie's sagemic branch uses.
Writes ../results/train_eval.csv (the table) and ../results/train_eval_predictions.parquet
(every exam window's prediction, for digging into mistakes).
"""
import argparse, time
from pathlib import Path
import numpy as np, pandas as pd
from sklearn.model_selection import GroupShuffleSplit, GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC, LinearSVC

SPECIES = ["house_finch", "western_meadowlark", "stellers_jay", "california_quail"]
SHORT = {"house_finch": "finch", "western_meadowlark": "meadow", "stellers_jay": "jay",
         "california_quail": "quail"}
CLASSES = SPECIES + ["noise"]

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="../embeddings/dataset.parquet")
ap.add_argument("--recipes", nargs="+", default=["A", "B", "C"])
ap.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.7])
ap.add_argument("--kernels", nargs="+", default=["linear", "rbf"])
ap.add_argument("--cap", type=int, default=3000, help="max Xeno-canto training windows per species (speed)")
ap.add_argument("--toy-folds", type=int, default=4)
ap.add_argument("--C", type=float, default=1.0)
ap.add_argument("--out", default="../results/train_eval.csv")
args = ap.parse_args()

df = pd.read_parquet(args.data)
E = [c for c in df.columns if c.startswith("e") and c[1:].isdigit()]
X = df[E].to_numpy(np.float32)
lab, role, kind, set_ = df.label.to_numpy(), df.role.to_numpy(), df.kind.to_numpy(), df.set.to_numpy()
own = df.own.to_numpy()

# --- fixed pools ---------------------------------------------------------------------------
xc_idx = np.where(set_ == "xc")[0]
tr, te = next(GroupShuffleSplit(1, test_size=0.2, random_state=0).split(xc_idx, groups=df.source_id.to_numpy()[xc_idx]))
xc_train, xc_test = xc_idx[tr], xc_idx[te]
xc_exam = xc_test[own[xc_test] >= 0.3]

# Ambient room: hold out the last ambient file as an exam ("does it stay quiet in the empty lab?"),
# train on the others. With only one ambient file, it is used for training and the exam is skipped.
amb_files = sorted(df.file[set_ == "ambient"].unique())
amb_exam_file = amb_files[-1] if len(amb_files) >= 2 else None
amb_exam = np.where((set_ == "ambient") & (df.file.to_numpy() == amb_exam_file))[0]
noise_train = np.where(np.isin(set_, ["noise_train", "ambient"]) & (lab == "noise") & (role == "trainable")
                       & (df.file.to_numpy() != amb_exam_file))[0]
toy = np.where((kind == "toy_alone") & np.isin(lab, CLASSES) & (role == "trainable"))[0]
music_train = np.where((set_ == "music_train") & (lab == "noise"))[0]
noise_exams = {"empty_room": amb_exam,
               "freesound_test": np.where(set_ == "noise_test")[0],
               "crowd_playback": np.where((kind == "crowd_only") & (lab == "noise"))[0],
               "real_talking": np.where((set_ == "holdout") & (df.label_src.to_numpy() == "holdout_no_toy"))[0],
               "music": np.where(set_ == "music_test")[0]}

crowd_toy = np.where((kind == "crowd_toy") & np.isin(lab, CLASSES))[0]
# Hand-labeled exams (label_overrides.csv): music + toy, and the talking + toy holdout
hand = np.where(((kind == "music_toy") | ((set_ == "holdout") & (df.label_src.to_numpy() == "manual")))
                & np.isin(lab, CLASSES))[0]

def xc_pool(thr):
    rng = np.random.default_rng(0)
    idx = xc_train[own[xc_train] >= thr]
    parts = [rng.permutation(idx[lab[idx] == sp])[: args.cap or None] for sp in SPECIES]
    return np.concatenate(parts)

def make_model(kernel):
    clf = (LinearSVC(C=args.C, class_weight="balanced", max_iter=20000) if kernel == "linear"
           else SVC(kernel="rbf", C=args.C, class_weight="balanced"))
    return make_pipeline(StandardScaler(), clf)

def fit(idx, kernel):
    return make_model(kernel).fit(X[idx], lab[idx])

def predict(m, idx):
    return m.classes_[m.decision_function(X[idx]).argmax(axis=1)]

def predict_s(m, idx):
    """(winning class, its decision_function score): the same two things Katie's sagemic code uses."""
    if len(idx) == 0:
        return np.array([], dtype=object), np.array([])
    d = m.decision_function(X[idx])
    k = d.argmax(axis=1)
    return m.classes_[k], d[np.arange(len(k)), k]

print(f"music training windows (recipe C only): {len(music_train)}")
print(f"pools: XC train recordings={len(np.unique(df.source_id.to_numpy()[xc_train]))}, "
      f"XC exam windows={len(xc_exam)}, noise train={len(noise_train)}, toy windows={len(toy)} "
      f"({', '.join(f'{SHORT.get(c, c)}={int((lab[toy] == c).sum())}' for c in CLASSES)})")
print(f"  crowd+toy exam: {len(crowd_toy)} windows "
      f"({', '.join(f'{SHORT.get(c, c)}={int((lab[crowd_toy] == c).sum())}' for c in CLASSES)})")
print(f"  hand-labeled exam (music/talking + toy): {len(hand)} windows "
      f"({', '.join(f'{SHORT.get(c, c)}={int((lab[hand] == c).sum())}' for c in CLASSES)})")
print(f"  ambient room: training on {len(amb_files) - (amb_exam_file is not None)} file(s), "
      f"exam file = {amb_exam_file}")
for k, v in noise_exams.items():
    print(f"  false-alarm exam {k}: {len(v)} windows")
print()

rows, preds = [], []
for thr in args.thresholds:
    for recipe in args.recipes:
        for kernel in args.kernels:
            name = f"{recipe} thr{thr} {kernel}"
            t0 = time.time()
            base = np.concatenate([xc_pool(thr), noise_train])
            if recipe == "C":
                base = np.concatenate([base, music_train])
            full_idx = np.concatenate([base, toy]) if recipe in ("B", "C") else base
            full = fit(full_idx, kernel)

            # exam 1: toys
            toy_pred = np.empty(len(toy), dtype=object)
            toy_score = np.full(len(toy), np.nan)
            if recipe == "A":
                toy_pred[:], toy_score[:] = predict_s(full, toy)
            else:
                gkf = GroupKFold(n_splits=args.toy_folds)
                for k_tr, k_te in gkf.split(toy, groups=df.file.to_numpy()[toy]):
                    m = fit(np.concatenate([base, toy[k_tr]]), kernel)
                    toy_pred[k_te], toy_score[k_te] = predict_s(m, toy[k_te])
            row = {"candidate": name, "recipe": recipe, "threshold": thr, "kernel": kernel,
                   "train_windows": len(full_idx)}
            for sp in SPECIES:
                m_ = lab[toy] == sp
                row[f"toy_{SHORT[sp]}"] = (toy_pred[m_] == sp).mean() if m_.any() else np.nan
            m_ = lab[toy] == "noise"
            row["toy_room_ok"] = (toy_pred[m_] == "noise").mean() if m_.any() else np.nan
            preds.append(pd.DataFrame({"candidate": name, "exam": "toy", "row": toy,
                                       "truth": lab[toy], "pred": toy_pred, "score": toy_score}))
            # exam 2: false alarms
            for ex, idx in noise_exams.items():
                p, sc = predict_s(full, idx)
                row[f"FA_{ex}"] = (p != "noise").mean() if len(idx) else np.nan
                preds.append(pd.DataFrame({"candidate": name, "exam": ex, "row": idx, "truth": "noise",
                                           "pred": p, "score": sc}))
            # exam 2b: toy in crowd (never trained on, so the full model is fair for A and B)
            if len(crowd_toy):
                p, sc = predict_s(full, crowd_toy)
                for sp in SPECIES:
                    m_ = lab[crowd_toy] == sp
                    row[f"c_{SHORT[sp]}"] = (p[m_] == sp).mean() if m_.any() else np.nan
                m_ = lab[crowd_toy] == "noise"
                row["c_quiet"] = (p[m_] == "noise").mean() if m_.any() else np.nan
                preds.append(pd.DataFrame({"candidate": name, "exam": "crowd_toy", "row": crowd_toy,
                                           "truth": lab[crowd_toy], "pred": p, "score": sc}))
            # exam 2c: hand-labeled music/talking + toy
            if len(hand):
                p, sc = predict_s(full, hand)
                row["hand_acc"] = (p == lab[hand]).mean()
                preds.append(pd.DataFrame({"candidate": name, "exam": "hand", "row": hand,
                                           "truth": lab[hand], "pred": p, "score": sc}))
            # exam 3: field check
            p = predict(full, xc_exam)
            row["xc_acc"] = (p == lab[xc_exam]).mean()
            row["secs"] = round(time.time() - t0)
            rows.append(row)
            print(f"done {name:22s} ({row['secs']} s)", flush=True)

res = pd.DataFrame(rows)
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
res.to_csv(args.out, index=False)
pp = pd.concat(preds, ignore_index=True)
pp = pp.join(df[["file", "start_s", "kind", "species", "toy_dist"]], on="row")
pp.to_parquet(Path(args.out).with_name("train_eval_predictions.parquet"), index=False)

show = res.set_index("candidate")[[f"toy_{SHORT[s]}" for s in SPECIES] + ["toy_room_ok", "FA_empty_room", "FA_freesound_test",
                                    "FA_crowd_playback", "FA_real_talking", "FA_music", "xc_acc"]]
show.columns = ["finch", "meadow", "jay", "quail", "room_ok", "FA_room", "FA_fs", "FA_crowd", "FA_talk", "FA_music", "xc_acc"]
print("\nToy columns + room_ok + xc_acc: fraction right (higher is better).")
print("FA_* columns: fraction of pure-noise windows wrongly called a bird (lower is better).\n")
print(show.round(2).to_string())
if len(crowd_toy):
    cshow = res.set_index("candidate")[[f"c_{SHORT[s]}" for s in SPECIES] + ["c_quiet"]]
    cshow.columns = ["finch", "meadow", "jay", "quail", "quiet_ok"]
    print("\nToy IN CROWD (fraction right; quiet_ok = crowd-only stretches called noise):\n")
    print(cshow.round(2).to_string())
    ct = pp[pp.exam == "crowd_toy"].copy()
    ct = ct[ct.truth != "noise"]
    ct["condition"] = "toy " + ct.toy_dist.fillna("") + " / crowd " + df.other_dist.to_numpy()[ct.row]
    ct["right"] = ct.truth == ct.pred
    print("\nToy in crowd, call windows right, by distance (toy closer = easier):\n")
    print(ct.pivot_table(index="candidate", columns="condition", values="right", aggfunc="mean").round(2).to_string())
    print("\nWhat the toy-in-crowd call windows were called (all candidates):\n")
    print(pd.crosstab([ct.candidate, ct.truth], ct.pred).to_string())
if len(hand):
    h = pp[pp.exam == "hand"].copy()
    h["source"] = np.where(kind[h.row] == "music_toy", "music+toy", "talking+toy")
    print("\nHand-labeled music / talking + toy windows: what each was called\n")
    print(pd.crosstab([h.candidate, h.source, h.truth], h.pred).to_string())
mu = pp[(pp.exam == "music") & (pp.pred != "noise")]
if len(mu):
    print("\nMusic windows wrongly called a bird, by candidate and Freesound file (worst first):\n")
    mu = mu.assign(sound=mu.file.str.split("/").str[-1])
    print(mu.groupby(["candidate", "sound", "pred"]).size().sort_values(ascending=False).head(25).to_string())
# ---- choosing CUSTOM_CONF ---------------------------------------------------------------
# Mirrors Katie's sagemic rule for each 3 s block: the head's top class wins; if it is "noise"
# nothing is reported; otherwise the bird is reported only if its score > CUSTOM_CONF.
# Uses exam windows only (never training windows).
CONFS = [-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
GROUPS = {"quiet_room": "toy alone, quiet room", "crowd_easy": "toy 1 ft / crowd 1 m",
          "crowd_hard": "toy 1 m / crowd 1 ft", "music_talk": "toy with music or talking"}
ex = pp[pp.exam.isin(["toy", "crowd_toy", "hand"] + list(noise_exams))].copy()
ex["group"] = np.select([ex.exam == "toy",
                         (ex.exam == "crowd_toy") & (ex.toy_dist == "1ft"),
                         (ex.exam == "crowd_toy") & (ex.toy_dist == "1m"),
                         ex.exam == "hand"], list(GROUPS), "noise_only")
calls, quiet = ex[ex.truth != "noise"], ex[ex.truth == "noise"]
print("\n" + "=" * 90)
print("CHOOSING CUSTOM_CONF: what sagemic would REPORT at each setting (exam windows only)")
print("  call columns: correct bird reported / toy-call windows.  wrong_bird: a call reported as")
print("  the wrong species.  false_alarms: no-toy windows reported as any bird / all no-toy windows.")
print("  Groups: " + "; ".join(f"{k} = {v}" for k, v in GROUPS.items()))
for cand in res.candidate:
    c, q = calls[calls.candidate == cand], quiet[quiet.candidate == cand]
    out = []
    for t in CONFS:
        rc = (c.pred != "noise") & (c.score > t)
        rq = (q.pred != "noise") & (q.score > t)
        r = {"CUSTOM_CONF": t}
        for g in GROUPS:
            m = (c.group == g).to_numpy()
            r[g] = f"{int((rc & (c.pred == c.truth))[m].sum())}/{int(m.sum())}"
        r["wrong_bird"] = int((rc & (c.pred != c.truth)).sum())
        r["false_alarms"] = f"{int(rq.sum())}/{len(q)}"
        out.append(r)
    print(f"\n{cand}:")
    print(pd.DataFrame(out).set_index("CUSTOM_CONF").to_string())
    ok = c[c.pred == c.truth]
    fa = q[q.pred != "noise"]
    if len(ok):
        print("  scores of correctly named calls: min {:.2f}, 10th pct {:.2f}, median {:.2f}".format(
            ok.score.min(), ok.score.quantile(0.1), ok.score.median()))
        print("  lowest correct score per species: " + ", ".join(
            f"{SHORT[sp]} {ok[ok.truth == sp].score.min():.2f}" for sp in SPECIES if (ok.truth == sp).any()))
    print("  false-alarm scores (no-toy windows where a bird won): "
          + (", ".join(f"{s:.2f} ({p})" for s, p in sorted(zip(fa.score, fa.pred), reverse=True)[:8]) if len(fa) else "none"))
print(f"\nwrote {args.out} and train_eval_predictions.parquet")
