#!/usr/bin/env python3
"""Which snooker bios need (re-)research.

Bios, highlights and verified accounts for the world's top 32 are written
by hand (snooker/players.json) and do not refresh themselves; the facts
around them do, daily. This lists what is due:

  missing  a player in the current top 32 with no bio (a newcomer)
  stale    a bio researched more than 365 days ago (`researchedAt`)

It writes a Markdown report to stdout and exits 1 when anything is due, so
.github/workflows/snooker-bios.yml can open or update an issue. Research
the listed players under the rules in the README (accounts only when an
official page links them), then `python3 snooker/merge_research.py FILE`.

Usage:
    python3 snooker/bios_due.py [--top 32] [--max-age-days 365]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=32)
    ap.add_argument("--max-age-days", type=int, default=365)
    args = ap.parse_args()

    ranked = json.loads((REPO / "snooker.json").read_text())["rankings"]["rows"][: args.top]
    editorial = {p["name"]: p for p in json.loads((REPO / "snooker" / "players.json").read_text())["players"]}
    today = dt.date.today()

    missing = [r for r in ranked if not editorial.get(r["name"], {}).get("bio")]
    stale = []
    for name, p in sorted(editorial.items()):
        when = dt.date.fromisoformat(p.get("researchedAt", "2000-01-01"))
        if (today - when).days > args.max_age_days:
            stale.append((name, when))

    if not missing and not stale:
        print(f"Nothing due: all of the top {args.top} have a bio researched within {args.max_age_days} days.")
        return 0
    lines = [f"Checked {today.isoformat()} against the world rankings in snooker.json.", ""]
    if missing:
        lines += [f"### New to the top {args.top} — no bio yet", ""]
        lines += [f"- No. {r['position']} {r['name']}" for r in missing] + [""]
    if stale:
        lines += [f"### Researched more than {args.max_age_days} days ago", ""]
        lines += [f"- {n} (researched {w.isoformat()})" for n, w in stale] + [""]
    lines += ["Research these under the account rule in the README (only accounts an official page links),",
              "write a JSON array in the merge_research.py input shape, then run",
              "`python3 snooker/merge_research.py FILE` and commit. The next Snooker run publishes it."]
    print("\n".join(lines))
    return 1


if __name__ == "__main__":
    sys.exit(main())
