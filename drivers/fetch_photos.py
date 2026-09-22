#!/usr/bin/env python3
"""Find a freely licensed portrait for every driver in drivers.json.

Source: the lead image of the driver's English Wikipedia article, which is
almost always a Wikimedia Commons file with its author and licence stated on
the file page. A photo is kept only when

  - the article is about this person as a racing driver or rider (its short
    description names the sport), found by trying "Name (racing driver)",
    "Name (motorcycle racer)", "Name (rally driver)" before plain "Name";
  - the file lives on Commons (never a local fair-use upload);
  - its licence is CC BY, CC BY-SA, CC0 or public domain, and an author is
    stated for any licence that requires attribution.

Kept photos are mirrored to images/drivers/<id>.jpg (900 px long edge,
progressive JPEG) and written into drivers.json as `image`, the same shape
circuits and teams use, with the credit in images/drivers/CREDITS.md.
Anything that fails a rule is left out; the driver page then shows the
initials badge it always has.

Usage:
    python3 drivers/fetch_photos.py --drivers ../EGrid/Resources/drivers.json [--only NAME] [--refresh]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEST = REPO / "images" / "drivers"
UA = "EGrid/1.0 (alextemplet@yahoo.com) driver-portraits"
LONG_EDGE = 900
QUALITY = 82

SPORT = re.compile(r"racing|racer|driver|rider|motorcycl|nascar|formula|rally|motorsport|indycar|motogp", re.I)
# The UK Open Government Licence is free with attribution, like CC BY — it
# covers Downing Street and government photography on Commons.
FREE = re.compile(r"^(cc[ -]by(-sa)?[ -]?\d(\.\d)?|cc0|public domain|pd|ogl)", re.I)
SUFFIXES = {
    "f1": ["racing driver"], "formulae": ["racing driver"], "indycar": ["racing driver"],
    "nascar": ["racing driver"], "wrc": ["rally driver", "racing driver"],
    "motogp": ["motorcycle racer", "racing driver"],
}


def get(url: str, params: dict | None = None, binary: bool = False):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return data if binary else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(8 * (attempt + 1))
                continue
            raise


# Lead images that show the car rather than the driver. The page heads with a
# face, so these keep the initials badge until a portrait turns up.
NOT_PORTRAIT = {"josh-mcerlean", "jon-armstrong", "christian-rasmussen"}

# Where the feed's name is also a famous relative's, name the article outright.
ARTICLE = {"Carlos Sainz": "Carlos Sainz Jr."}


def article(name: str, channels: list[str]) -> tuple[str, str, str] | None:
    """(title, description, lead image file name) for this person, or None."""
    candidates = ([ARTICLE[name]] if name in ARTICLE else []) + \
        [f"{name} ({s})" for c in channels for s in SUFFIXES.get(c, ["racing driver"])] + [name]
    seen = set()
    for title in candidates:
        if title in seen:
            continue
        seen.add(title)
        data = get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "redirects": 1, "titles": title,
            "prop": "description|pageimages|pageprops", "piprop": "name", "ppprop": "disambiguation",
        })
        for page in (data.get("query", {}).get("pages") or {}).values():
            if "missing" in page or "disambiguation" in (page.get("pageprops") or {}):
                continue
            desc = page.get("description", "")
            if not SPORT.search(desc):
                continue
            return page["title"], desc, page.get("pageimage", "")
    # Accents and suffixes the feeds drop ("Raúl Fernández", "Carlos Sainz
    # Jr."): search, and still insist the hit is about a racer.
    data = get("https://en.wikipedia.org/w/api.php", {
        "action": "query", "format": "json", "list": "search", "srlimit": 5,
        "srsearch": f"{name} {'motorcycle racer' if 'motogp' in channels else 'racing driver'}",
    })
    for hit in data.get("query", {}).get("search", []):
        if hit["title"] in seen:
            continue
        fold = lambda t: re.sub(r"[^a-z ]", "", __import__("unicodedata").normalize("NFKD", t).encode("ascii", "ignore").decode().lower())
        if fold(name.split()[-1]) not in fold(hit["title"]):
            continue
        # A father or son of the same name is a different person.
        if re.search(r"\b(Sr|Jr|Senior|Junior)\b", hit["title"]) and not re.search(r"\b(Sr|Jr)\b", name):
            continue
        page = next(iter(get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "titles": hit["title"],
            "prop": "description|pageimages", "piprop": "name"}).get("query", {}).get("pages", {}).values()), {})
        if SPORT.search(page.get("description", "")):
            return page["title"], page.get("description", ""), page.get("pageimage", "")
    return None


def licence(file_name: str) -> dict | None:
    data = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "titles": f"File:{file_name}",
        "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": LONG_EDGE,
    })
    page = next(iter((data.get("query", {}).get("pages") or {}).values()), {})
    if "missing" in page or not page.get("imageinfo"):
        return None          # not on Commons: a local (usually fair-use) file
    info = page["imageinfo"][0]
    meta = info.get("extmetadata", {})
    short = html.unescape(re.sub("<[^>]+>", "", meta.get("LicenseShortName", {}).get("value", ""))).strip()
    artist = html.unescape(re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", ""))).strip()
    artist = re.sub(r"\s+", " ", artist)
    if not FREE.match(short):
        return None
    needs_credit = not re.match(r"^(cc0|public domain|pd)", short, re.I)
    if needs_credit and not artist:
        return None
    return {
        "thumb": info.get("thumburl") or info["url"],
        "credit": artist or None,
        "licence": short,
        "licenceURL": meta.get("LicenseUrl", {}).get("value") or None,
        "sourceURL": info.get("descriptionurl"),
        "width": info.get("width"), "height": info.get("height"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drivers", required=True, type=Path)
    ap.add_argument("--only")
    ap.add_argument("--refresh", action="store_true", help="re-fetch drivers that already have a photo")
    args = ap.parse_args()

    doc = json.loads(args.drivers.read_text(encoding="utf-8"))
    DEST.mkdir(parents=True, exist_ok=True)
    kept, skipped = 0, []
    for d in doc["drivers"]:
        if args.only and d["name"] != args.only:
            continue
        if d["id"] in NOT_PORTRAIT:
            d.pop("image", None)
            (DEST / f"{d['id']}.jpg").unlink(missing_ok=True)
            skipped.append((d["name"], "lead image shows the car, not the driver")); continue
        if d.get("image") and not args.refresh:
            kept += 1
            continue
        try:
            found = article(d["name"], d.get("channelIDs") or [])
            if not found:
                skipped.append((d["name"], "no racing article")); continue
            title, desc, file_name = found
            if not file_name:
                skipped.append((d["name"], "article has no lead image")); continue
            lic = licence(file_name)
            if not lic:
                skipped.append((d["name"], f"{file_name}: not a free Commons file")); continue
            raw = get(lic["thumb"], binary=True)
            im = Image.open(BytesIO(raw)).convert("RGB")
            w, h = im.size
            scale = LONG_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
            out = DEST / f"{d['id']}.jpg"
            im.save(out, "JPEG", quality=QUALITY, progressive=True, optimize=True)
            d["image"] = {k: v for k, v in {
                "url": f"images/drivers/{d['id']}.jpg", "credit": lic["credit"], "licence": lic["licence"],
                "licenceURL": lic["licenceURL"], "sourceURL": lic["sourceURL"],
            }.items() if v}
            kept += 1
            print(f"  {d['name']:28} {lic['licence']:14} {lic['credit'][:40] if lic['credit'] else ''}  ({title})")
            time.sleep(1.5)       # upload.wikimedia.org rate-limits bursts
        except Exception as e:  # noqa: BLE001 - one bad lookup must not stop the run
            skipped.append((d["name"], f"{type(e).__name__}: {e}"))

    args.drivers.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# Image credits", "",
            "Driver portraits, each redistributed under the free licence stated on its Commons page,",
            "which is linked below. Every one of these licences except CC0 and public domain makes",
            "attribution a condition, and the same credit is shown over the photo in both apps.",
            "Written by `drivers/fetch_photos.py`; if you replace an image, update `drivers.json` too.", "",
            "| File | Driver | Author | Licence | Source |", "|---|---|---|---|---|"]
    for d in doc["drivers"]:
        img = d.get("image")
        if img:
            rows.append(f"| `{d['id']}.jpg` | {d['name']} | {img.get('credit', '')} | "
                        f"[{img.get('licence', '')}]({img.get('licenceURL', '')}) | [Commons]({img.get('sourceURL', '')}) |")
    (DEST / "CREDITS.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"\n{kept} with a photo, {len(skipped)} without")
    for name, why in skipped:
        print(f"  - {name}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
