#!/usr/bin/env python3
"""Builds `formula-e.json`, the one file both E-Grid apps read for Formula E.

Formula E's old data API (api.formula-e.pulselive.com) stopped resolving in
October 2026 and nothing replaced it: the official site is now rendered on
the server and exposes no JSON API. So this script assembles the season from
three public sources and publishes it next to the other content files:

  calendar   fiaformulae.com/en/calendar[?season=N] — a schema.org ItemList
             in a <script id="calendar-schema"> block: round, dates, venue,
             city, country. Race names there are "TBC" until announced.
  sessions   github.com/sportstimes/f1 `_db/fe/{year}.json` — the volunteer
             dataset behind f1calendar.com, with every session start in UTC.
             Keyed by the year a season ends in (Season 12, 2025-26, is
             2026.json). Absent until they publish it; races then carry a
             date and no sessions, and the apps say so.
  results    fiaformulae.com/en/results-and-standings — HTML rows marked
  standings  with data-testid="results-row-*" / "standings-row-*". Parsed on
             those attributes and the order of the visible text, never on
             class names (theirs carry build hashes).

Results are incremental: a session's classification is fetched once, after
the race day, and kept from the previous file on every later run. A normal
day therefore costs three or four page loads, and a race weekend a dozen.

If anything goes wrong the script keeps what it can from the previous file
rather than publishing less: a failed calendar keeps the old races, a failed
standings page keeps the old table, and a results page that parses to no rows
is simply tried again next run. It exits non-zero only when the output would
have no races at all.

Usage:
    python3 formulae/build_formula_e.py [--out formula-e.json] [--refetch]

`--refetch` ignores stored results and reads every finished session again.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "formula-e.json"

SITE = "https://www.fiaformulae.com/en"
SPORTSTIMES = "https://raw.githubusercontent.com/sportstimes/f1/main/_db/fe"
# The site answers Python's default user agent with a 403 and an honest,
# identifying one with a 200.
USER_AGENT = "EGrid-formula-e/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 30
# One page a second: these are full HTML pages, and we are guests.
PACE_SECONDS = 1.0

# sportstimes key -> the session name the apps show, and the results page's
# `session=` value for it.
SESSIONS = {
    "practice1": ("Practice 1", "free-practice-1"),
    "practice2": ("Practice 2", "free-practice-2"),
    "practice3": ("Practice 3", "free-practice-3"),
    "qualifying": ("Qualifying", "qualifying"),
    "race": ("Race", "race"),
}
SESSION_ORDER = [name for name, _ in SESSIONS.values()]

COUNTRIES = {
    "SA": "Saudi Arabia", "MX": "Mexico", "US": "USA", "BR": "Brazil", "CN": "China",
    "MC": "Monaco", "DE": "Germany", "GB": "United Kingdom", "NL": "Netherlands",
    "ES": "Spain", "JP": "Japan", "IT": "Italy", "IN": "India", "ID": "Indonesia",
    "KR": "South Korea", "CH": "Switzerland", "PT": "Portugal", "ZA": "South Africa",
    "MA": "Morocco", "CL": "Chile", "AR": "Argentina", "FR": "France", "AT": "Austria",
    "AE": "United Arab Emirates", "QA": "Qatar", "BH": "Bahrain", "CA": "Canada",
}

STATUS_WORDS = {"DNF": "retired", "DNS": "didNotStart", "DSQ": "disqualified",
                "DQ": "disqualified", "EXC": "disqualified", "NC": "notClassified",
                "DNQ": "notClassified"}


def log(message: str) -> None:
    print(message, file=sys.stderr)


_last_request = 0.0


def fetch(url: str) -> str:
    global _last_request
    wait = PACE_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    finally:
        _last_request = time.monotonic()


def safe(label: str, fn, default):
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - one bad page must not sink the file
        log(f"  ! {label}: {type(error).__name__}: {error}")
        return default


# --------------------------------------------------------------------------- #
# HTML rows
# --------------------------------------------------------------------------- #

VOID = {"img", "br", "source", "meta", "input", "link", "hr", "path", "rect",
        "circle", "line", "polygon", "use", "stop"}


class TestIdRows(HTMLParser):
    """Collects the visible text, in order, of every element carrying the
    given data-testid, plus the hrefs inside it."""

    def __init__(self, testid: str):
        super().__init__(convert_charrefs=True)
        self.testid = testid
        self.depth = 0
        self.current: dict | None = None
        self.rows: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.current is not None:
            if tag not in VOID:
                self.depth += 1
            if tag == "a" and attrs.get("href"):
                self.current["links"].append(attrs["href"])
        elif attrs.get("data-testid") == self.testid:
            self.current = {"text": [], "links": []}
            self.depth = 1

    def handle_endtag(self, tag):
        if self.current is not None and tag not in VOID:
            self.depth -= 1
            if self.depth == 0:
                self.rows.append(self.current)
                self.current = None

    def handle_data(self, data):
        if self.current is not None and data.strip():
            self.current["text"].append(data.strip())


def rows(page: str, testid: str) -> list[dict]:
    parser = TestIdRows(testid)
    parser.feed(page)
    return parser.rows


CODE = re.compile(r"^[A-Z]{3}$")


def split_driver(tokens: list[str]) -> tuple[str, str, str, list[str]] | None:
    """Finds name, three-letter code and team in a row's text and returns
    them with whatever follows the (duplicated) team name."""
    for i in range(1, len(tokens) - 1):
        if CODE.match(tokens[i]) and not CODE.match(tokens[i - 1]):
            name, code, team = tokens[i - 1], tokens[i], tokens[i + 1]
            rest = tokens[i + 2:]
            if rest and rest[0] == team:
                rest = rest[1:]
            return name, code, team, rest
    return None


def to_int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def to_float(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def seconds(text: str) -> float | None:
    """'42:19.363' -> 2539.363, '1:06.925' -> 66.925, '+0.614' -> 0.614."""
    text = text.strip().lstrip("+")
    if not re.match(r"^[0-9:.]+$", text):
        return None
    total = 0.0
    for part in text.split(":"):
        try:
            total = total * 60 + float(part)
        except ValueError:
            return None
    return total


def team_name(shouted: str) -> str:
    """'JAGUAR TCS RACING' -> 'Jaguar TCS Racing'. Short all-caps words are
    acronyms (TCS, DS, ABT) and stay that way."""
    words = []
    for word in shouted.split():
        if word.isupper() and len(word) <= 3 and word not in {"E", "THE"}:
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def result_row(tokens: list[str], session: str) -> dict | None:
    if not tokens:
        return None
    position = to_int(tokens[0])
    parts = split_driver(tokens)
    if not parts:
        return None
    name, code, team, rest = parts
    row = {"position": position, "name": name, "code": code, "team": team_name(team),
           "grid": None, "duration": None, "gap": None, "gapText": None,
           "points": None, "status": "classified" if position else "retired"}
    if session == "Race":
        row["grid"] = to_int(rest[0]) if rest else None
        shown = rest[1] if len(rest) > 1 else ""
        row["points"] = to_float(rest[2]) if len(rest) > 2 else None
    elif session.startswith("Practice"):
        # best lap, gap to the fastest ("-" for the fastest), best lap again
        shown = rest[0] if rest else ""
        gap = rest[1] if len(rest) > 1 else ""
        if gap.startswith("+"):
            row["gap"] = seconds(gap)
    else:
        shown = rest[0] if rest else ""
    shown = shown.strip()
    word = shown.upper()
    if word in STATUS_WORDS:
        row["status"] = STATUS_WORDS[word]
    elif "LAP" in word:
        row["gapText"] = "+" + word.lstrip("+")
    elif shown.startswith("+"):
        row["gap"] = seconds(shown)
    elif shown and shown not in {"—", "-"}:
        # The leader's total (race) or each car's best lap (qualifying,
        # practice). A retired car's time is how long it lasted.
        row["duration"] = seconds(shown)
    return row


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def calendar(season: int | None) -> tuple[int, list[dict]]:
    """(season number, races) from the calendar page's JSON-LD."""
    page = fetch(f"{SITE}/calendar" + (f"?season={season}" if season else ""))
    match = re.search(r'<script[^>]*id="calendar-schema"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        raise ValueError("no calendar-schema block on the calendar page")
    data = json.loads(match.group(1))
    races, number = [], season
    for element in data.get("itemListElement", []):
        item = element.get("item") or {}
        series = ((item.get("superEvent") or {}).get("name") or "")
        found = re.search(r"(\d+)", series)
        if found:
            number = int(found.group(1))
        place = item.get("location") or {}
        address = place.get("address") or {}
        city = (address.get("addressLocality") or place.get("name") or "").strip()
        code = (address.get("addressCountry") or "").strip().upper()
        name = (item.get("name") or "").strip()
        races.append({
            "round": int(element.get("position")),
            "name": name if name and name.upper() != "TBC" else f"{city} E-Prix",
            "venue": (place.get("name") or "").strip(),
            "city": city,
            "country": COUNTRIES.get(code, code),
            "date": (item.get("endDate") or item.get("startDate") or "")[:10],
        })
    if number is None:
        raise ValueError("calendar page names no season")
    return number, races


def session_times(season: int) -> dict[int, dict]:
    """round -> {"name": ..., "sessions": [...]} from sportstimes, or {} when
    they have not published the season yet."""
    year = season + 2014  # Season 12 ran 2025-26 and is filed as 2026.json
    try:
        data = json.loads(fetch(f"{SPORTSTIMES}/{year}.json"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            log(f"  sportstimes has no {year}.json yet; Season {season} has dates only")
            return {}
        raise
    out = {}
    for race in data.get("races", []):
        sessions = []
        for key, start in (race.get("sessions") or {}).items():
            if key in SESSIONS and start:
                sessions.append({"name": SESSIONS[key][0], "start": start})
        sessions.sort(key=lambda s: s["start"])
        out[int(race["round"])] = {
            "name": (race.get("name") or "").replace("ePrix", "E-Prix").strip(),
            "sessions": sessions,
        }
    return out


def round_slugs(season: int) -> dict[int, str]:
    page = fetch(f"{SITE}/results-and-standings?season={season}")
    return {int(number): f"{number}-{place}"
            for number, place in re.findall(r"round=([0-9]+)-([a-z0-9-]+)", page)}


ROW_KIND = {"Race": "results-row-result", "Qualifying": "results-row-qualifying"}


def results_page(season: int, slug: str, session_key: str) -> str:
    return fetch(f"{SITE}/results-and-standings?season={season}&round={slug}&session={session_key}")


def available_sessions(page: str, practice_names: list[str]) -> dict[str, str]:
    """Session name -> the page's `session=` value, for the sessions this
    round's results page actually links to. Some rounds list a single
    "free-practice" (named after the one practice sportstimes has), others
    numbered ones. The site answers any other value with the race, so only
    these are ever asked for."""
    out = {}
    for key in dict.fromkeys(re.findall(r'data-testid="session-([a-z0-9-]+)"', page)):
        if key == "race":
            out["Race"] = key
        elif key == "qualifying":
            out["Qualifying"] = key
        elif key == "free-practice":
            out[practice_names[0] if len(practice_names) == 1 else "Practice 1"] = key
        elif re.fullmatch(r"free-practice-\d", key):
            out[f"Practice {key[-1]}"] = key
    return out


def parse_results(page: str, session: str) -> list[dict]:
    """Rows of the kind this session should have, and nothing else — a page
    showing the wrong kind of row is the site falling back to the race."""
    testid = ROW_KIND.get(session, "results-row-practice")
    return [r for r in (result_row(row["text"], session) for row in rows(page, testid)) if r]


def round_due(race: dict, results: dict, today: dt.date) -> bool:
    """Whether a round's results page is worth loading. Before race day only
    sessions that have started; once the race result is held, the round is
    done — except for two days after it, so a late practice or qualifying
    table can still arrive. Without that limit a session the site never
    publishes would cost a page load on every run for ever."""
    day = dt.date.fromisoformat(race["date"])
    if day > today and not race["sessions"]:
        return False
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    ran = [x["name"] for x in race["sessions"] if x["start"][:19] <= now]
    if not race["sessions"]:
        return "Race" not in results
    if "Race" in results:
        return (today - day).days <= 2 and any(name not in results for name in ran)
    return bool(ran)


def fetch_round(season: int, slug: str, race: dict, have: dict) -> dict[str, list[dict]]:
    """Every session of one round that has results online and that we do not
    hold yet. The race page comes first: it also says which sessions exist."""
    label = f"S{season} R{race['round']}"
    page = safe(f"{label} race page", lambda: results_page(season, slug, "race"), "")
    if not page:
        return {}
    practice = [x["name"] for x in race["sessions"] if x["name"].startswith("Practice")]
    out = {}
    for name, key in available_sessions(page, practice).items():
        if name in have:
            continue
        body = page if key == "race" else safe(f"{label} {name}", lambda: results_page(season, slug, key), "")
        table = parse_results(body, name) if body else []
        if table:
            out[name] = table
            log(f"  {label} {name}: {len(table)} rows")
    return out


def driver_standings(season: int) -> list[dict]:
    page = fetch(f"{SITE}/results-and-standings?tab=drivers&season={season}")
    table = []
    for row in rows(page, "standings-row-driver"):
        tokens = row["text"]
        if len(tokens) < 4:
            continue
        position, name, nation, team = tokens[0], tokens[1], tokens[2], tokens[3]
        table.append({"position": to_int(position), "name": name, "nationality": nation,
                      "team": team_name(team), "points": to_float(tokens[-1]) or 0.0})
    return table


def team_standings(season: int) -> list[dict]:
    page = fetch(f"{SITE}/results-and-standings?tab=teams&season={season}")
    table = []
    for row in rows(page, "standings-row-team"):
        tokens = row["text"]
        if len(tokens) < 3:
            continue
        table.append({"position": to_int(tokens[0]), "name": team_name(tokens[1]),
                      "points": to_float(tokens[-1]) or 0.0})
    return table


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def build(previous: dict, refetch: bool) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date()
    old_races = {(r["season"], r["round"]): r for r in previous.get("races", [])}

    current, current_races = safe("current calendar", lambda: calendar(None), (None, []))
    if current is None:
        current = previous.get("currentSeason")
        if current is None:
            raise SystemExit("no calendar and no previous file; nothing to build")
    seasons = {current: current_races}
    prior = safe(f"Season {current - 1} calendar", lambda: calendar(current - 1), (None, []))
    if prior[0] == current - 1:
        seasons[current - 1] = prior[1]
    log(f"seasons: " + ", ".join(f"{n} ({len(r)} rounds)" for n, r in sorted(seasons.items())))

    races = []
    for season, entries in sorted(seasons.items()):
        if not entries:
            # Calendar failed: keep whatever the last good file had.
            entries = [r for r in previous.get("races", []) if r["season"] == season]
            log(f"  Season {season}: calendar unavailable, kept {len(entries)} stored rounds")
        times = safe(f"Season {season} session times", lambda: session_times(season), {})
        slugs: dict[int, str] | None = None
        for entry in entries:
            race = {k: entry[k] for k in ("round", "name", "venue", "city", "country", "date")}
            race["season"] = season
            race["id"] = f"s{season}-r{entry['round']}"
            timed = times.get(entry["round"])
            stored = old_races.get((season, entry["round"]), {})
            if timed and timed["name"] and race["name"].endswith(" E-Prix") and timed["name"].endswith("E-Prix"):
                race["name"] = timed["name"]
            race["sessions"] = (timed or {}).get("sessions") or stored.get("sessions") or []
            results = {} if refetch else dict(stored.get("results") or {})

            if round_due(race, results, today):
                if slugs is None:
                    slugs = safe(f"Season {season} round list", lambda: round_slugs(season), {})
                slug = slugs.get(entry["round"])
                if slug:
                    fetched = fetch_round(season, slug, race, results)
                    results.update(fetched)
            race["results"] = {k: results[k] for k in SESSION_ORDER if k in results}
            races.append(race)

    if not races:
        raise SystemExit("no races at all; leaving the previous file in place")

    # Standings belong to the newest season that has a race result in it.
    scored = [r["season"] for r in races if "Race" in r["results"]]
    standings_season = max(scored) if scored else previous.get("standingsSeason")
    drivers = teams = []
    if standings_season:
        drivers = safe("driver standings", lambda: driver_standings(standings_season), [])
        teams = safe("team standings", lambda: team_standings(standings_season), [])
    if previous.get("standingsSeason") == standings_season:
        drivers = drivers or previous.get("driverStandings", [])
        teams = teams or previous.get("teamStandings", [])

    return {
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sources": {
            "calendar": f"{SITE}/calendar",
            "sessions": "https://github.com/sportstimes/f1",
            "results": f"{SITE}/results-and-standings",
        },
        "currentSeason": current,
        "standingsSeason": standings_season,
        "races": races,
        "driverStandings": drivers,
        "teamStandings": teams,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--refetch", action="store_true", help="Read every finished session again")
    args = parser.parse_args()
    out = Path(args.out)
    previous = {}
    if out.exists():
        try:
            previous = json.loads(out.read_text())
        except json.JSONDecodeError:
            previous = {}
    data = build(previous, args.refetch)
    # Unchanged apart from the timestamp: keep the old one, so the action
    # sees no diff and publishes nothing.
    if {**data, "generatedAt": None} == {**previous, "generatedAt": None}:
        data["generatedAt"] = previous["generatedAt"]
    with_results = sum(1 for r in data["races"] if r["results"])
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    log(f"wrote {out}: {len(data['races'])} races ({with_results} with results), "
        f"{len(data['driverStandings'])} drivers / {len(data['teamStandings'])} teams "
        f"for Season {data['standingsSeason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
