#!/usr/bin/env python3
"""Find a freely licensed portrait for every snooker player we show.

The same rules as drivers/fetch_photos.py, whose Commons licence check this
reuses: the lead image of the player's English Wikipedia article, kept only
when the file lives on Commons under CC BY, CC BY-SA, CC0, public domain or
the UK OGL, with an author stated wherever the licence requires credit.

The article is the one snooker.org links for the player (WikipediaURL in
snooker/state.json), so there is no guessing between namesakes; the article
must still describe a snooker player. snooker.org's own player photos are
NOT used — their rights are not ours to redistribute.

Kept photos are mirrored to images/snooker/<playerId>.jpg (900 px long
edge) and recorded in snooker/photos.json, which build_snooker.py merges
into snooker-players.json. Credits go to images/snooker/CREDITS.md.

Usage:
    python3 snooker/fetch_photos.py [--only "Player Name"] [--refresh]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "drivers"))
import fetch_photos as shared  # noqa: E402  (drivers/fetch_photos.py)

DEST = REPO / "images" / "snooker"
STATE = HERE / "state.json"
PHOTOS = HERE / "photos.json"
SNOOKER = re.compile(r"snooker", re.I)
# Lead images that are wide table shots with the player a speck in them: a
# page header would show cloth, not a face. Keyed by snooker.org player ID.
NOT_PORTRAIT = {'1257', '1108', '1982'}  # Si Jiahui, Yuan Sijun, Pang Junxu


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

    state = json.loads(STATE.read_text())
    players = state.get("players", {})
    shown = json.loads((REPO / "snooker-players.json").read_text())["drivers"]
    photos = json.loads(PHOTOS.read_text()).get("photos", {}) if PHOTOS.exists() else {}
    DEST.mkdir(parents=True, exist_ok=True)

    kept, skipped = 0, []
    for profile in shown:
        pid = profile["id"].removeprefix("snooker-")
        name = profile["name"]
        if args.only and name != args.only:
            continue
        if pid in NOT_PORTRAIT:
            photos.pop(pid, None)
            (DEST / f"{pid}.jpg").unlink(missing_ok=True)
            skipped.append((name, "lead image is a wide table shot, not a portrait")); continue
        if pid in photos and not args.refresh:
            kept += 1
            continue
        wiki = players.get(pid, {}).get("wikipedia")
        if not wiki:
            skipped.append((name, "no Wikipedia article linked")); continue
        try:
            found = lead_image(wiki)
            if not found:
                skipped.append((name, "linked article missing")); continue
            desc, file_name = found
            if not SNOOKER.search(desc):
                skipped.append((name, f"article is not about a snooker player ({desc!r})")); continue
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
            im.save(DEST / f"{pid}.jpg", "JPEG", quality=shared.QUALITY, progressive=True, optimize=True)
            photos[pid] = {k: v for k, v in {
                "url": f"images/snooker/{pid}.jpg", "credit": lic["credit"], "licence": lic["licence"],
                "licenceURL": lic["licenceURL"], "sourceURL": lic["sourceURL"],
            }.items() if v}
            kept += 1
            print(f"  {name:26} {lic['licence']:14} {(lic['credit'] or '')[:40]}")
            time.sleep(1.5)       # upload.wikimedia.org rate-limits bursts
        except Exception as e:  # noqa: BLE001 - one bad lookup must not stop the run
            skipped.append((name, f"{type(e).__name__}: {e}"))

    PHOTOS.write_text(json.dumps({"_comment": "Written by snooker/fetch_photos.py; keyed by snooker.org player ID.",
                                  "photos": dict(sorted(photos.items(), key=lambda kv: int(kv[0])))},
                                 ensure_ascii=False, indent=1) + "\n")
    names = {p["id"].removeprefix("snooker-"): p["name"] for p in shown}
    rows = ["# Image credits", "",
            "Snooker player portraits, each redistributed under the free licence stated on its Commons page,",
            "which is linked below. Every licence except CC0 and public domain makes attribution a",
            "condition, and the same credit is shown over the photo in both apps.",
            "Written by `snooker/fetch_photos.py`.", "",
            "| File | Player | Author | Licence | Source |", "|---|---|---|---|---|"]
    for pid, img in sorted(photos.items(), key=lambda kv: int(kv[0])):
        rows.append(f"| `{pid}.jpg` | {names.get(pid, pid)} | {img.get('credit', '')} | "
                    f"[{img.get('licence', '')}]({img.get('licenceURL', '')}) | [Commons]({img.get('sourceURL', '')}) |")
    (DEST / "CREDITS.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n{kept} with a photo, {len(skipped)} without")
    for name, why in skipped:
        print(f"  - {name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
