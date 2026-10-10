#!/usr/bin/env python3
"""Merge researched player profiles into snooker/players.json.

Input: one or more JSON arrays of researched players, each
  {name, birthplace?, bio, highlights[], socials{}, verifiedVia{}, youtube?, sources[]}
The rule that every account must be linked from the player's own site, their
WST profile or a management/sponsor page is applied by the research; this
script refuses any account that arrives without a verifiedVia URL, keeps a
YouTube channel only with its own verifiedVia, and writes snooker/VERIFIED.md
so each account can be traced to the page that proved it.

A later file wins for a name it repeats. Names must match snooker.org's
spelling (the name in snooker-players.json); build_snooker.py joins on it.

Usage:
    python3 snooker/merge_research.py research-1.json [research-2.json ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "players.json"
VERIFIED = HERE / "VERIFIED.md"
HANDLES = ("instagram", "x", "tiktok", "threads", "bluesky", "facebook", "website")


def main(paths: list[str]) -> int:
    existing = {p["name"]: p for p in json.loads(OUT.read_text()).get("players", [])} if OUT.exists() else {}
    known = {p["name"] for p in json.loads((HERE.parent / "snooker-players.json").read_text())["drivers"]}
    for path in paths:
        for r in json.loads(Path(path).read_text()):
            name = r["name"]
            if name not in known:
                print(f"  ! {name}: not a name in snooker-players.json, skipped")
                continue
            via = r.get("verifiedVia") or {}
            socials = {k: v for k, v in (r.get("socials") or {}).items() if k in HANDLES and v and via.get(k)}
            dropped = sorted(set(r.get("socials") or {}) - set(socials))
            if dropped:
                print(f"  ! {name}: dropped unverified {', '.join(dropped)}")
            entry = {
                "name": name,
                "birthplace": r.get("birthplace"),
                "bio": r.get("bio"),
                "highlights": r.get("highlights") or [],
                "socials": socials,
                "verifiedVia": {k: via[k] for k in socials},
                "sources": r.get("sources") or [],
            }
            yt = r.get("youtube")
            if yt and yt.get("channelId") and yt.get("verifiedVia"):
                # build_videos.py only reads a channel marked embeddable: set
                # it in the research once a video has been embed-tested.
                entry["youtube"] = {k: v for k, v in {"channelId": yt["channelId"], "handle": yt.get("handle"),
                                                      "embeddable": yt.get("embeddable")}.items() if v is not None}
                entry["verifiedVia"]["youtube"] = yt["verifiedVia"]
            existing[name] = {k: v for k, v in entry.items() if v not in (None, "", [], {})}

    players = sorted(existing.values(), key=lambda p: p["name"])
    OUT.write_text(json.dumps({
        "_comment": "Hand-researched snooker player profiles, merged by merge_research.py. "
                    "Accounts only with a verifiedVia page (see VERIFIED.md). Edit the research, then re-merge.",
        "players": players,
    }, ensure_ascii=False, indent=1) + "\n")

    rows = ["# Verified snooker player accounts", "",
            "Where each account in `snooker/players.json` was confirmed as official: the player's own",
            "site, their WST profile, or a management/sponsor page. Written by `snooker/merge_research.py`.", "",
            "| Player | Account | Verified at |", "|---|---|---|"]
    for p in players:
        for key, url in sorted((p.get("verifiedVia") or {}).items()):
            rows.append(f"| {p['name']} | {key} | {url} |")
    if len(rows) == 6:
        rows.append("| — | — | No account has been verified yet. |")
    VERIFIED.write_text("\n".join(rows) + "\n")
    print(f"{len(players)} profiles, {sum(1 for p in players if p.get('socials'))} with verified accounts")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
