#!/usr/bin/env python3
"""Builds `tyres.json` — the "Tyres: <supplier>" section on a race page.

One entry per series whose tyre supplier Wikipedia documents well enough to
write about: who makes the tyres, since when, and (for Formula 1) the
compounds and their sidewall colours. Like `teams.json` it is generated,
never bundled in either app, and a series without an entry simply shows no
section.

Every word traces back to Wikipedia:

  source    the paragraphs of the configured articles that mention the
            supplier or the series' tyres, fetched fresh each run.
  facts     configured below, but each one is kept only if its numbers and
            key words all appear in that source text — a fact Wikipedia stops
            supporting drops out on the next run instead of going stale.
  summary   two short paragraphs from the same writer as the digest (your own
            server through EGRID_LLM_URL), given only the source text, and
            thrown away for a templated line if it carries a number the
            source does not.
  photo     a Commons file, licence re-checked with drivers/fetch_photos.py's
            rule and mirrored to images/tyres/<channel>.jpg.

Usage:
    python3 tyres/build_tyres.py [--out tyres.json] [--no-llm] [--channel f1]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "digest"))
sys.path.insert(0, str(REPO / "drivers"))
from build_digest import USER_AGENT, log, parse_json_reply  # noqa: E402
import fetch_photos as photos  # noqa: E402  (licence rule + polite Wikimedia client)

DEFAULT_OUT = REPO / "tyres.json"
IMAGES = REPO / "images" / "tyres"
PHOTO_WIDTH = 1200
SEASON = dt.date.today().year

# Official F1 tyre graphics colours (F1 live timing); the apps draw the
# legend and stint bars in the same values.
F1_COMPOUNDS = [
    {"name": "Soft", "colour": "#DA291C"},
    {"name": "Medium", "colour": "#FFD12E"},
    {"name": "Hard", "colour": "#F0F0EC"},
    {"name": "Intermediate", "colour": "#43B02A"},
    {"name": "Wet", "colour": "#0067AD"},
]

# articles: (Wikipedia title, words a paragraph must contain one of)
SUPPLIERS = {
    "f1": {
        "series": "Formula 1", "name": "Pirelli", "product": "P Zero",
        "articles": [("Formula One tyres", ["Pirelli", "2026", "compound"])],
        "photo": "Pirelli Tyre Sets at Formula 1 Exhibition, London 03.jpg",
        "caption": "Pirelli slicks, intermediates and full wets",
        "facts": [
            ("Sole supplier since", "2011"),
            ("Slick range", "C1 to C5, hardest to softest"),
            ("Per race", "Three slick compounds nominated as hard, medium and soft"),
            ("Wet weather", "Intermediate (green) and full wet (blue)"),
            ("2026 slick sizes", "280/705 R18 front, 375/710 R18 rear"),
        ],
        "compounds": F1_COMPOUNDS,
    },
    "motogp": {
        "series": "MotoGP", "name": "Michelin", "product": None,
        "articles": [("Michelin", ["MotoGP"]), ("MotoGP", ["tyre"])],
        "photo": "Suzuki GSX-RR MotoGP-2015.JPG",
        "caption": "A MotoGP bike on the grid with its tyres in warmers",
        "facts": [
            ("Sole supplier since", "2016"),
            ("Before that", "Bridgestone, 2009 to 2015"),
            ("Moto2 and Moto3", "Pirelli since 2024"),
        ],
        "compounds": [],
    },
    "nascar": {
        "series": "the NASCAR Cup Series", "name": "Goodyear", "product": None,
        "articles": [("Goodyear Tire and Rubber Company", ["NASCAR"]),
                     ("NASCAR Cup Series", ["Goodyear", "rain tires"])],
        "photo": "NASCAR Goodyear Eagles (7160044492).jpg",
        "caption": "Goodyear Eagle tyres in the pit lane",
        "facts": [
            ("Supplier since", "1954"),
            ("Sole supplier since", "1975"),
            ("Exclusive across", "Cup, O'Reilly and Truck since 1997"),
        ],
        "compounds": [],
    },
    "indycar": {
        "series": "IndyCar", "name": "Firestone", "product": "Firehawk",
        "articles": [("IndyCar Series", ["Firestone"]),
                     ("Firestone Tire and Rubber Company", ["IndyCar", "Indianapolis 500"])],
        "photo": "Firestone tires 2011 Indy Japan 300.jpg",
        "caption": "Firestone Firehawk tyres, alternates with red sidewalls",
        "facts": [
            ("Supplier since", "1996"),
            ("Sole supplier since", "2000"),
            ("Sizes", "305/45-R15 front, 415/40-R15 rear"),
        ],
        "compounds": [],
    },
}

SYSTEM_PROMPT = """You write short explainers for E-Grid, a sports companion app.
Write about one series' tyre supplier for a fan reading a race page, using ONLY the
source text you are given. Two short paragraphs, 80 to 110 words in total, plain
British English, no headings, no lists, no hype. First paragraph: who supplies the
tyres and since when. Second: something a viewer notices on race day (compounds,
sidewall colours, how teams choose or manage them). Never add a fact, number, year
or name that is not in the source text. Reply with one JSON object only:
{"summary": "first paragraph\\n\\nsecond paragraph"}"""


def plain_text(title: str) -> str:
    data = photos.get("https://en.wikipedia.org/w/api.php", {
        "action": "query", "format": "json", "prop": "extracts", "explaintext": 1,
        "titles": title, "redirects": 1})
    return next(iter(data["query"]["pages"].values())).get("extract", "")


def source_text(spec: dict) -> str:
    """The paragraphs of each article that are about the tyres."""
    out = []
    for title, words in spec["articles"]:
        for para in plain_text(title).split("\n"):
            para = para.strip()
            if len(para) > 80 and any(w.lower() in para.lower() for w in words):
                out.append(para)
        time.sleep(1)
    return "\n\n".join(dict.fromkeys(out))[:12000]


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'"))


def supported(value: str, source: str) -> bool:
    """Every number and every word of four letters or more in `value`
    appears somewhere in the source text."""
    haystack = fold(source)
    numbers = re.findall(r"\d[\d/.\-R]*\d|\d", value)
    words = [w for w in re.findall(r"[A-Za-z']{4,}", value)
             if w.lower() not in {"since", "with", "before", "that", "hardest",
                                  "softest", "front", "rear", "across", "three"}]
    return all(n.lower() in haystack for n in numbers) and all(w.lower() in haystack for w in words)


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,/]\d+)*", text))


def ask_own_server(spec: dict, source: str) -> str | None:
    base = os.environ.get("EGRID_LLM_URL", "").strip().rstrip("/")
    if not base:
        return None
    if "://" not in base:
        base = "https://" + base
    if not base.endswith("/chat/completions"):
        base = (base if base.endswith("/v1") else base + "/v1") + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if os.environ.get("EGRID_LLM_TOKEN", "").strip():
        headers["Authorization"] = "Bearer " + os.environ["EGRID_LLM_TOKEN"].strip()
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "series": spec["series"], "supplier": spec["name"], "season": SEASON,
                "source": source}, ensure_ascii=False)},
        ],
        "temperature": 0.3, "max_tokens": 6000, "response_format": {"type": "json_object"},
    }
    if os.environ.get("EGRID_LLM_MODEL", "").strip():
        payload["model"] = os.environ["EGRID_LLM_MODEL"].strip()
    request = urllib.request.Request(base, data=json.dumps(payload).encode(), headers=headers, method="POST")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                reply = json.load(response)
            content = reply["choices"][0]["message"]["content"] or ""
            data = parse_json_reply(content)
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, TypeError,
                json.JSONDecodeError) as error:
            log(f"  own server: {type(error).__name__}: {error}")
            return None
        if data and isinstance(data.get("summary"), str):
            return data["summary"].strip()
        # Some models name the key differently or split the paragraphs.
        if data:
            texts = [v for v in data.values() if isinstance(v, str) and len(v) > 80]
            lists = [v for v in data.values() if isinstance(v, list) and all(isinstance(x, str) for x in v)]
            if texts:
                return texts[0].strip()
            if lists and lists[0]:
                return "\n\n".join(x.strip() for x in lists[0])
        finish = (reply.get("choices") or [{}])[0].get("finish_reason")
        log(f"  own server returned no summary (attempt {attempt}, finish {finish}): {content[:160]!r}")
    return None


def template(spec: dict, facts: list[dict]) -> str:
    since = next((f["value"] for f in facts if f["label"].endswith("since")), None)
    line = f"{spec['name']} supplies the tyres for {spec['series']}"
    return line + (f", and has done since {since}." if since else ".")


def write_summary(spec: dict, source: str, facts: list[dict], use_llm: bool) -> tuple[str, str]:
    if use_llm:
        text = ask_own_server(spec, source)
        if text:
            extra = numbers_in(text) - numbers_in(source) - {str(SEASON)}
            words = len(text.split())
            if extra:
                log(f"  summary dropped: numbers not in the source {sorted(extra)}")
            elif not 40 <= words <= 160:
                log(f"  summary dropped: {words} words")
            else:
                return text, "llm"
    return template(spec, facts), "template"


def mirror_photo(channel: str, spec: dict) -> dict | None:
    photos.LONG_EDGE = PHOTO_WIDTH
    lic = photos.licence(spec["photo"])
    if not lic:
        log(f"  {channel}: {spec['photo']} fails the licence rule; no photo")
        return None
    IMAGES.mkdir(parents=True, exist_ok=True)
    im = Image.open(BytesIO(photos.get(lic["thumb"], binary=True))).convert("RGB")
    if im.width > PHOTO_WIDTH:
        im = im.resize((PHOTO_WIDTH, round(im.height * PHOTO_WIDTH / im.width)), Image.LANCZOS)
    im.save(IMAGES / f"{channel}.jpg", "JPEG", quality=80, progressive=True, optimize=True)
    return {k: v for k, v in {
        "url": f"images/tyres/{channel}.jpg", "credit": lic["credit"], "licence": lic["licence"],
        "licenceURL": lic["licenceURL"], "sourceURL": lic["sourceURL"], "caption": spec.get("caption"),
    }.items() if v}


def build_entry(channel: str, spec: dict, use_llm: bool) -> dict | None:
    log(f"{channel}: {spec['name']}")
    source = source_text(spec)
    if spec["name"].lower() not in source.lower():
        log(f"  no Wikipedia text names {spec['name']}; skipped")
        return None
    facts = []
    for label, value in spec["facts"]:
        if supported(value, source):
            facts.append({"label": label, "value": value})
        else:
            log(f"  fact dropped, not in the source: {label}: {value}")
    product = spec["product"] if spec["product"] and spec["product"].lower() in source.lower() else None
    summary, writer = write_summary(spec, source, facts, use_llm)
    log(f"  {len(facts)} facts, summary by {writer}")
    title, _ = spec["articles"][0]
    return {k: v for k, v in {
        "channelID": channel, "season": SEASON, "name": spec["name"], "product": product,
        "summary": summary, "facts": facts, "compounds": spec["compounds"],
        "photo": mirror_photo(channel, spec),
        "sourceURL": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
        "writer": writer,
    }.items() if v is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--channel", choices=sorted(SUPPLIERS))
    args = parser.parse_args()

    old = {}
    if args.out.exists():
        old = {s["channelID"]: s for s in json.loads(args.out.read_text()).get("suppliers", [])}
    entries = dict(old)
    for channel, spec in SUPPLIERS.items():
        if args.channel and channel != args.channel:
            continue
        try:
            entry = build_entry(channel, spec, not args.no_llm)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as error:
            log(f"  {channel}: {type(error).__name__}: {error}; keeping the previous entry")
            continue
        if entry is None:
            continue
        # A template never replaces a written summary for the same supplier.
        previous = old.get(channel)
        if entry["writer"] == "template" and previous and previous.get("writer") == "llm" \
                and previous.get("name") == entry["name"]:
            entry["summary"], entry["writer"] = previous["summary"], "llm"
        entries[channel] = entry
    entries = {c: e for c, e in entries.items() if c in SUPPLIERS}
    if not entries:
        log("nothing built")
        return 1
    args.out.write_text(json.dumps({
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_comment": "Tyre supplier per series, for the race page. Written by tyres/build_tyres.py.",
        "suppliers": [entries[c] for c in SUPPLIERS if c in entries],
    }, ensure_ascii=False, indent=1) + "\n")
    rows = ["# Image credits — tyre suppliers", "",
            "Each redistributed under the free licence on its Commons page; the credit is shown over the",
            "photo in both apps. Written by `tyres/build_tyres.py`.", "",
            "| File | Series | Author | Licence | Source |", "|---|---|---|---|---|"]
    for c in SUPPLIERS:
        p = (entries.get(c) or {}).get("photo")
        if p:
            rows.append(f"| `{c}.jpg` | {c} | {p.get('credit', '')} | [{p.get('licence', '')}]({p.get('licenceURL', '')}) "
                        f"| [Commons]({p.get('sourceURL', '')}) |")
    if IMAGES.exists():
        (IMAGES / "CREDITS.md").write_text("\n".join(rows) + "\n")
    log(f"wrote {args.out} ({len(entries)} series)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
