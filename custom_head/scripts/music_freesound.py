"""music_freesound.py: download party-style music and sounds that overlap the bird band.

  export FREESOUND_TOKEN="your-api-key"
  python music_freesound.py                 # ~10 sounds per search
  python music_freesound.py --per-query 20  # more

Why these searches: the toys' calls sit between ~1.2 and 9.6 kHz and are clean, pitched
notes. The riskiest party sounds are the same: tonal, whistle-like instruments (flute,
piccolo, violin, clarinet, soprano sax, trumpet, upper piano), singing, whistling, and
glasses clinking. Broadband chatter is less risky than these.

Writes ../data/music/{train,test}/fs<ID>.mp3 and ../data/music/sources.csv.
Train/test split is by UPLOADER, using the same rule as noise_freesound.py, so an uploader
is on the same side in both the crowd and music sets.

Anything tagged or described with birds is excluded: a music clip with birdsong in it,
labeled "noise", would teach the head that birdsong is noise.
"""
import argparse, csv, hashlib, os, time
from pathlib import Path
import requests

QUERIES = [
    "flute solo", "piccolo", "violin solo", "string quartet", "clarinet",
    "soprano saxophone", "trumpet solo", "cocktail jazz", "jazz trio", "piano bar",
    "solo piano", "jazz vocals", "whistling", "glass clink", "wine glass toast",
]
EXCLUDE = " -bird -birds -birdsong -birdcall -chirp -nature -forest -dawn"
BIRD_WORDS = ("bird", "chirp", "tweet", "warbler", "finch", "robin", "sparrow", "jay", "quail")

API = "https://freesound.org/apiv2/search/text/"
OUT = Path("../data/music")

def split_for(uploader):
    """Same rule as noise_freesound.py: same uploader always lands in the same split (~25% test)."""
    return "test" if int(hashlib.md5(uploader.encode()).hexdigest(), 16) % 4 == 0 else "train"

ap = argparse.ArgumentParser()
ap.add_argument("--per-query", type=int, default=10)
args = ap.parse_args()
token = os.environ["FREESOUND_TOKEN"]

seen, rows, skipped = set(), [], 0
for q in QUERIES:
    r = requests.get(API, params={
        "query": q + EXCLUDE, "token": token,
        "filter": "duration:[20 TO 600]",   # 20 s to 10 min
        "fields": "id,name,username,license,duration,tags,description,previews",
        "page_size": args.per_query,
    }, timeout=60)
    r.raise_for_status()
    results = r.json()["results"]
    print(f"{q!r}: {len(results)} results", flush=True)
    for s in results:
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        text = " ".join([s.get("name", ""), s.get("description", "")] + s.get("tags", [])).lower()
        if any(w in text for w in BIRD_WORDS):   # second guard in case the search let one through
            skipped += 1
            continue
        split = split_for(s["username"])
        dest = OUT / split / f"fs{s['id']}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            audio = requests.get(s["previews"]["preview-hq-mp3"], timeout=120)
            if not audio.ok:
                continue
            dest.write_bytes(audio.content)
            time.sleep(1)  # be polite
        rows.append({"file": dest.name, "source": "freesound", "id": s["id"], "name": s["name"],
                     "uploader": s["username"], "license": s["license"],
                     "duration_s": s["duration"], "split": split, "query": q})

if not rows:
    raise SystemExit("Nothing downloaded; print r.json() for one query to see what came back.")
manifest = OUT / "sources.csv"
with open(manifest, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    if f.tell() == 0:
        w.writeheader()
    w.writerows(rows)
n_test = sum(r["split"] == "test" for r in rows)
print(f"\n{len(rows)} sounds from {len({r['uploader'] for r in rows})} uploaders "
      f"({len(rows) - n_test} train, {n_test} test); skipped {skipped} mentioning birds -> {manifest}")
