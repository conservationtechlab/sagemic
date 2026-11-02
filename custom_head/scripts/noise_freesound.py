# noise_freesound.py: download crowd/room ambience previews from Freesound
import os, csv, time, hashlib, requests
from pathlib import Path

TOKEN = os.environ["FREESOUND_TOKEN"]
API = "https://freesound.org/apiv2/search/text/"
QUERIES = ["crowd chatter", "restaurant ambience", "cocktail party", "walla",
           "museum crowd", "reception crowd", "party ambience", "indoor crowd"]
PER_QUERY = 15                       # results per query; raise once it works
OUT = Path("../data/noise")
MANIFEST = OUT / "sources.csv"

def split_for(uploader):
    """Same uploader always lands in the same split (~25% test)."""
    return "test" if int(hashlib.md5(uploader.encode()).hexdigest(), 16) % 4 == 0 else "train"

seen, rows = set(), []
for q in QUERIES:
    r = requests.get(API, params={
        "query": q, "token": TOKEN,
        "filter": "duration:[60 TO 600]",                 # 1-10 minutes
        "fields": "id,name,username,license,duration,previews",
        "page_size": PER_QUERY,
    }, timeout=60)
    r.raise_for_status()
    results = r.json()["results"]
    print(f"{q!r}: {len(results)} results")
    for s in results:
        if s["id"] in seen:
            continue                                    # same sound from another query
        seen.add(s["id"])
        split = split_for(s["username"])
        dest = OUT / split / f"fs{s['id']}.mp3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            audio = requests.get(s["previews"]["preview-hq-mp3"], timeout=120)
            if not audio.ok:
                continue
            dest.write_bytes(audio.content)
            time.sleep(1)                               # be polite
        rows.append({"file": dest.name, "source": "freesound", "id": s["id"],
                     "name": s["name"], "uploader": s["username"],
                     "license": s["license"], "duration_s": s["duration"],
                     "split": split, "query": q})

with open(MANIFEST, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    if f.tell() == 0:
        w.writeheader()
    w.writerows(rows)
print(f"{len(rows)} sounds from {len({r['uploader'] for r in rows})} uploaders -> {MANIFEST}")