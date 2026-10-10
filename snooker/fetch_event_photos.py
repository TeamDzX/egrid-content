#!/usr/bin/env python3
"""Mirror the snooker tournament header photos into the content repo.

Input: a JSON file of curated Commons photos, one per tournament name
({"photos": {"World Championship": {"file": "File:...", "caption": ...}}}),
chosen from past editions of each event, its arena, or its venue.

Every file's licence is re-checked here with drivers/fetch_photos.py's rule
(CC BY, CC BY-SA, CC0, public domain or OGL, with an author wherever credit
is required) before anything is kept. Kept photos go to
images/snooker/events/<slug>.jpg (1600 px wide, for a full-width banner)
and to snooker/event-photos.json, keyed by tournament name, which
build_snooker.py merges into every edition of that tournament in
snooker.json — so next season's World Championship keeps its photo.

Usage:
    python3 snooker/fetch_event_photos.py CURATED.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "drivers"))
import fetch_photos as shared  # noqa: E402  (drivers/fetch_photos.py)

DEST = REPO / "images" / "snooker" / "events"
OUT = HERE / "event-photos.json"
WIDTH = 1600


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main(path: str) -> int:
    curated = json.loads(Path(path).read_text())["photos"]
    out = json.loads(OUT.read_text())["photos"] if OUT.exists() else {}
    DEST.mkdir(parents=True, exist_ok=True)
    shared.LONG_EDGE = WIDTH  # thumbnail width the licence lookup asks for
    for name, pick in curated.items():
        file_name = pick["file"].removeprefix("File:")
        lic = shared.licence(file_name)
        if not lic:
            print(f"  ! {name}: {file_name} fails the licence rule, skipped")
            continue
        im = Image.open(BytesIO(shared.get(lic["thumb"], binary=True))).convert("RGB")
        if im.width > WIDTH:
            im = im.resize((WIDTH, round(im.height * WIDTH / im.width)), Image.LANCZOS)
        target = DEST / f"{slug(name)}.jpg"
        im.save(target, "JPEG", quality=80, progressive=True, optimize=True)
        out[name] = {k: v for k, v in {
            "url": f"images/snooker/events/{slug(name)}.jpg", "credit": lic["credit"], "licence": lic["licence"],
            "licenceURL": lic["licenceURL"], "sourceURL": lic["sourceURL"], "caption": pick.get("caption"),
        }.items() if v}
        print(f"  {name:36} {lic['licence']:14} {im.width}x{im.height}  {pick.get('caption', '')}")
        time.sleep(1.5)

    OUT.write_text(json.dumps({"_comment": "Tournament header photos, keyed by event name. "
                                           "Written by snooker/fetch_event_photos.py.",
                               "photos": dict(sorted(out.items()))}, ensure_ascii=False, indent=1) + "\n")
    rows = ["# Image credits — snooker tournament headers", "",
            "Each redistributed under the free licence on its Commons page, linked below; the credit",
            "is shown over the photo in both apps. Written by `snooker/fetch_event_photos.py`.", "",
            "| File | Tournament | Caption | Author | Licence | Source |", "|---|---|---|---|---|---|"]
    for name, img in sorted(out.items()):
        rows.append(f"| `{Path(img['url']).name}` | {name} | {img.get('caption', '')} | {img.get('credit', '')} | "
                    f"[{img.get('licence', '')}]({img.get('licenceURL', '')}) | [Commons]({img.get('sourceURL', '')}) |")
    (DEST / "CREDITS.md").write_text("\n".join(rows) + "\n")
    print(f"{len(out)} tournaments with a photo")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
