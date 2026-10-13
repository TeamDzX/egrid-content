#!/usr/bin/env python3
"""Find a freely licensed portrait for every tennis player in tennis/players.json.

The same rules as snooker/fetch_photos.py and drivers/fetch_photos.py, whose
Commons licence check this reuses: the lead image of the player's English
Wikipedia article (the first `sources` entry in players.json), kept only
when the file lives on Commons under CC BY, CC BY-SA, CC0, public domain or
the UK OGL, with an author stated wherever the licence requires credit. The
article must describe a tennis player. BSD's player photos are NOT used —
its licence (s.6) does not cover them.

Kept photos are mirrored to images/tennis/<slug>.jpg (900 px long edge) and
recorded in tennis/photos.json, keyed by player name, which build_tennis.py
merges into tennis-players.json. Credits go to images/tennis/CREDITS.md.

Usage:
    python3 tennis/fetch_photos.py [--only "Player Name"] [--refresh]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "drivers"))
import fetch_photos as shared  # noqa: E402  (drivers/fetch_photos.py)

DEST = REPO / "images" / "tennis"
PLAYERS = HERE / "players.json"
PHOTOS = HERE / "photos.json"
TENNIS = re.compile(r"tennis", re.I)


def slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def lead_image(wikipedia_url: str) -> tuple[str, str] | None:
    """(description, lead image file name) for the linked article."""
    title = urllib.parse.unquote(wikipedia_url.rsplit("/wiki/", 1)[-1]).replace("_", " ")
    data = shared.get("https://en.wikipedia.org/w/api.php", {
        "action": "query", "format": "json", "redirects": 1, "titles": title,
        "prop": "description|pageimages", "piprop": "name",
    })
    for page in (data.get("query", {}).get("pages") or {}).values():
        if "missing" in page:
            return None
        return page.get("description", ""), page.get("pageimage", "")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only")
    ap.add_argument("--refresh", action="store_true", help="re-fetch players who already have a photo")
    args = ap.parse_args()

    players = json.loads(PLAYERS.read_text(encoding="utf-8"))["players"]
    photos = json.loads(PHOTOS.read_text(encoding="utf-8")).get("photos", {}) if PHOTOS.exists() else {}
    DEST.mkdir(parents=True, exist_ok=True)

    kept, skipped = 0, []
    for player in players:
        name = player["name"]
        if args.only and name != args.only:
            continue
        if name in photos and not args.refresh:
            kept += 1
            continue
        wiki = next((s for s in player.get("sources", []) if "wikipedia.org/wiki/" in s), None)
        if not wiki:
            skipped.append((name, "no Wikipedia article in sources")); continue
        try:
            found = lead_image(wiki)
            if not found:
                skipped.append((name, "linked article missing")); continue
            desc, file_name = found
            if not TENNIS.search(desc):
                skipped.append((name, f"article is not about a tennis player ({desc!r})")); continue
            if not file_name:
                skipped.append((name, "article has no lead image")); continue
            lic = shared.licence(file_name)
            if not lic:
                skipped.append((name, f"{file_name}: not a free Commons file")); continue
            im = Image.open(BytesIO(shared.get(lic["thumb"], binary=True))).convert("RGB")
            w, h = im.size
            scale = shared.LONG_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
            file = f"{slug(name)}.jpg"
            im.save(DEST / file, "JPEG", quality=shared.QUALITY, progressive=True, optimize=True)
            photos[name] = {k: v for k, v in {
                "url": f"images/tennis/{file}", "credit": lic["credit"], "licence": lic["licence"],
                "licenceURL": lic["licenceURL"], "sourceURL": lic["sourceURL"],
            }.items() if v}
            kept += 1
            print(f"  {name:26} {lic['licence']:14} {(lic['credit'] or '')[:40]}")
            time.sleep(1.5)       # upload.wikimedia.org rate-limits bursts
        except Exception as e:  # noqa: BLE001 - one bad lookup must not stop the run
            skipped.append((name, f"{type(e).__name__}: {e}"))

    PHOTOS.write_text(json.dumps({"_comment": "Written by tennis/fetch_photos.py; keyed by player name.",
                                  "photos": dict(sorted(photos.items()))},
                                 ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    rows = ["# Image credits", "",
            "Tennis player portraits, each redistributed under the free licence stated on its Commons page,",
            "which is linked below. Every licence except CC0 and public domain makes attribution a",
            "condition, and the same credit is shown over the photo in both apps.",
            "Written by `tennis/fetch_photos.py`.", "",
            "| File | Player | Author | Licence | Source |", "|---|---|---|---|---|"]
    for name, img in sorted(photos.items()):
        rows.append(f"| `{Path(img['url']).name}` | {name} | {img.get('credit', '')} | "
                    f"[{img.get('licence', '')}]({img.get('licenceURL', '')}) | [Commons]({img.get('sourceURL', '')}) |")
    (DEST / "CREDITS.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n{kept} with a photo, {len(skipped)} without")
    for name, why in skipped:
        print(f"  - {name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
