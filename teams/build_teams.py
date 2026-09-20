#!/usr/bin/env python3
"""Builds the team performance reviews both E-Grid apps show on a team page.

Rebuilt every morning into `teams.json` at the repo root, one entry per
team, constructor or manufacturer in the three series that publish full
results:

  f1        constructors, from Jolpica — standings, every round's results
  motogp    manufacturers, from the Pulselive feed — standings and each
            finished event's race classification
  formulae  teams, from the Formula E Pulselive feed — team standings and
            each race's results

Each entry carries the season facts (position, points, wins, podiums, the
riders or drivers and where they stand), the team's result in the most
recent race, and two short pieces of prose: a season summary and a review of
that last race. The prose comes from the same writer as the weekly digest —
your own server through EGRID_LLM_URL first, Claude second — and falls back
to a templated sentence when neither is available or either says anything
the facts do not support. A missing key downgrades the writing, never the
data.

The apps match an entry to a team by `channelID` and `matchKeys` against the
name their live standings give, exactly as they match `machines.json`, so
names here are the series feed's own.

Usage:
    python3 teams/build_teams.py [--out teams.json] [--no-llm] [--channel f1|motogp|formulae]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "teams.json"

# The digest generator already knows how to talk to the feeds and to the
# writers; borrow the pieces that are not digest-specific.
sys.path.insert(0, str(REPO_ROOT / "digest"))
from build_digest import (  # noqa: E402
    FORMULA_E, JOLPICA, MOTOGP, USER_AGENT, log, parse_json_reply, tidy_title,
)
from build_digest import get_json as _get_json  # noqa: E402


def get_json(url: str):
    """Jolpica in particular answers slowly under its rate limit; one retry
    after a pause turns most of those timeouts into a result."""
    import time
    for attempt in (1, 2, 3):
        try:
            return _get_json(url)
        except Exception as error:  # noqa: BLE001 - re-raised on the last try
            if attempt == 3:
                raise
            log(f"  {type(error).__name__} on {url.split('?')[0]}; retrying in {4 * attempt}s")
            time.sleep(4 * attempt)

# Champion-ship names the apps show, so the prose can name the series.
SERIES = {"f1": "Formula 1", "motogp": "MotoGP", "formulae": "Formula E"}
ENTRANT = {"f1": "constructor", "motogp": "manufacturer", "formulae": "team"}
PEOPLE = {"f1": "drivers", "motogp": "riders", "formulae": "drivers"}


# ---------------------------------------------------------------------------
# Data model


@dataclass
class Member:
    name: str
    position: int | None = None
    points: float | None = None


@dataclass
class RaceLine:
    """One of the team's cars or bikes in the last race."""
    name: str
    position: int | None
    grid: int | None = None
    points: float | None = None
    status: str | None = None


@dataclass
class Classified:
    """One row of the last race's classification, series-neutral."""
    position: int | None
    name: str
    team: str
    grid: int | None = None
    points: float | None = None
    status: str | None = None
    gap: str | None = None
    fastest_lap: bool = False


@dataclass
class RaceSummary:
    channel_id: str
    name: str
    round_number: int | None
    date: str | None
    rows: list[Classified] = field(default_factory=list)

    @property
    def everyone(self) -> set[str]:
        return {r.name for r in self.rows if r.name}


@dataclass
class Team:
    channel_id: str
    name: str
    position: int | None
    points: float | None
    wins: int = 0
    podiums: int = 0
    rounds: int = 0
    members: list[Member] = field(default_factory=list)
    last_race: str | None = None
    last_race_round: int | None = None
    last_race_date: str | None = None
    lines: list[RaceLine] = field(default_factory=list)

    @property
    def match_keys(self) -> list[str]:
        return [self.name.lower()]


# ---------------------------------------------------------------------------
# Formula 1


def season_results() -> list[dict]:
    """Every race of the season with its full Results list. Jolpica pages at
    100 result rows, not races, so a race straddles pages and its Results
    arrive in pieces that have to be stitched back together by round."""
    by_round: dict[int, dict] = {}
    offset = 0
    while True:
        data = get_json(f"{JOLPICA}/current/results.json?limit=100&offset={offset}")["MRData"]
        page = data["RaceTable"].get("Races", [])
        for race in page:
            number = int(race.get("round") or 0)
            if number in by_round:
                by_round[number]["Results"] += race.get("Results", [])
            else:
                by_round[number] = dict(race, Results=list(race.get("Results", [])))
        offset += 100
        if offset >= int(data.get("total", 0)) or not page:
            break
    return [by_round[n] for n in sorted(by_round)]


def classified_position(result: dict) -> int | None:
    """Jolpica numbers every car, retirements included; only `positionText`
    says whether the car was classified. "R", "D", "W" and friends are a
    did-not-finish, which the status field then explains."""
    text = str(result.get("positionText", ""))
    return int(text) if text.isdigit() else None


def f1_teams() -> list[Team]:
    standings = get_json(f"{JOLPICA}/current/constructorStandings.json")["MRData"]["StandingsTable"]
    lists = standings.get("StandingsLists", [])
    if not lists:
        return []
    teams: dict[str, Team] = {}
    for row in lists[0].get("ConstructorStandings", []):
        name = row["Constructor"]["name"]
        teams[name] = Team("f1", name, int(row["position"]), float(row["points"]),
                           wins=int(row.get("wins") or 0))

    drivers = get_json(f"{JOLPICA}/current/driverStandings.json")["MRData"]["StandingsTable"]
    for row in drivers.get("StandingsLists", [{}])[0].get("DriverStandings", []):
        driver = row["Driver"]
        full = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
        for constructor in row.get("Constructors", [])[-1:]:   # the current team
            team = teams.get(constructor["name"])
            if team:
                team.members.append(Member(full, int(row["position"]), float(row["points"])))

    races = season_results()
    last = races[-1] if races else None
    for race in races:
        for result in race.get("Results", []):
            team = teams.get(result.get("Constructor", {}).get("name"))
            if not team:
                continue
            position = classified_position(result)
            if position and position <= 3:
                team.podiums += 1
    for team in teams.values():
        team.rounds = len(races)
    if last:
        for team in teams.values():
            team.last_race = last.get("raceName")
            team.last_race_round = int(last["round"]) if last.get("round") else None
            team.last_race_date = last.get("date")
        for result in last.get("Results", []):
            team = teams.get(result.get("Constructor", {}).get("name"))
            if not team:
                continue
            driver = result.get("Driver", {})
            name = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
            grid = int(result["grid"]) if str(result.get("grid", "")).isdigit() and int(result["grid"]) > 0 else None
            team.lines.append(RaceLine(name, classified_position(result), grid, float(result.get("points") or 0),
                                       result.get("status")))
    race = None
    if last:
        race = RaceSummary("f1", last.get("raceName", "Grand Prix"),
                           int(last["round"]) if last.get("round") else None, last.get("date"))
        for result in last.get("Results", []):
            driver = result.get("Driver", {})
            name = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
            grid = int(result["grid"]) if str(result.get("grid", "")).isdigit() and int(result["grid"]) > 0 else None
            race.rows.append(Classified(
                classified_position(result), name, result.get("Constructor", {}).get("name", ""), grid,
                float(result.get("points") or 0), result.get("status"),
                (result.get("Time") or {}).get("time"),
                (result.get("FastestLap") or {}).get("rank") == "1"))
    return sorted(teams.values(), key=lambda t: t.position or 99), race


# ---------------------------------------------------------------------------
# MotoGP


def motogp_teams() -> list[Team]:
    seasons = get_json(f"{MOTOGP}/seasons")
    current = next((s for s in seasons if s.get("current")), None) or max(seasons, key=lambda s: s.get("year", 0))
    categories = get_json(f"{MOTOGP}/categories?seasonUuid={current['id']}")
    premier = next((c for c in categories if "motogp" in (c.get("name") or "").lower()), None)
    if not premier:
        return []
    standings = get_json(f"{MOTOGP}/standings?seasonUuid={current['id']}&categoryUuid={premier['id']}")

    teams: dict[str, Team] = {}
    order: list[str] = []
    for row in standings.get("classification") or []:
        make = (row.get("constructor") or {}).get("name")
        if not make:
            continue
        if make not in teams:
            order.append(make)
            teams[make] = Team("motogp", make, None, None)
        rider = (row.get("rider") or {}).get("full_name")
        if rider:
            teams[make].members.append(Member(rider, row.get("position"), row.get("points")))

    # The feed has no manufacturers' table; rank makes by their best rider,
    # which is how the apps list them, and leave points out rather than sum
    # rider scores that would not match MotoGP's own constructor scoring.
    for index, make in enumerate(order, start=1):
        teams[make].position = index

    events = get_json(f"{MOTOGP}/events?seasonUuid={current['id']}&isFinished=true")
    finished = [e for e in events if not e.get("test") and e.get("date_end")]
    finished.sort(key=lambda e: e["date_end"])
    last_rows: list[dict] = []
    for event in finished:
        rows = motogp_race_rows(event["id"], premier["id"])
        if not rows:
            continue
        for team in teams.values():
            team.rounds += 1
        for row in rows:
            make = (row.get("constructor") or {}).get("name")
            team = teams.get(make)
            position = row.get("position")
            if team and position:
                if position == 1:
                    team.wins += 1
                if position <= 3:
                    team.podiums += 1
        last_rows, last_event = rows, event
    if last_rows:
        for team in teams.values():
            team.last_race = tidy_title((last_event.get("name") or "").strip())
            team.last_race_round = team.rounds
            team.last_race_date = (last_event.get("date_end") or "")[:10] or None
        for row in last_rows:
            team = teams.get((row.get("constructor") or {}).get("name"))
            if not team:
                continue
            points = row.get("points")
            team.lines.append(RaceLine((row.get("rider") or {}).get("full_name", ""), row.get("position"),
                                       None, float(points) if points is not None else None,
                                       (row.get("status") or None)))
    race = None
    if last_rows:
        race = RaceSummary("motogp", tidy_title((last_event.get("name") or "").strip()),
                           next(iter(teams.values())).rounds if teams else None,
                           (last_event.get("date_end") or "")[:10] or None)
        for row in last_rows:
            points = row.get("points")
            race.rows.append(Classified(
                row.get("position") or None, (row.get("rider") or {}).get("full_name", ""),
                (row.get("team") or {}).get("name", ""), None,
                float(points) if points is not None else None, row.get("status") or None,
                row.get("gap") if isinstance(row.get("gap"), str) else None))
    return [teams[m] for m in order], race


def motogp_race_rows(event_id: str, category_id: str) -> list[dict]:
    sessions = get_json(f"{MOTOGP}/sessions?eventUuid={event_id}&categoryUuid={category_id}")
    race = next((s for s in sessions if (s.get("type") or "").upper() == "RAC"), None)
    if not race:
        return []
    return get_json(f"{MOTOGP}/session/{race['id']}/classification").get("classification") or []


# ---------------------------------------------------------------------------
# Formula E


def formula_e_teams() -> list[Team]:
    championships = get_json(f"{FORMULA_E}/championships")["championships"]
    current = next((c for c in championships if c.get("status") == "Present"), championships[-1])
    rows = get_json(f"{FORMULA_E}/standings/teams?championshipId={current['id']}")
    rows = rows if isinstance(rows, list) else next((v for v in rows.values() if isinstance(v, list)), [])

    teams: dict[str, Team] = {}
    for index, row in enumerate(rows, start=1):
        # The apps capitalise the feed's shouted names the same way.
        name = title_case(row.get("teamName") or "")
        if not name:
            continue
        teams[name.lower()] = Team("formulae", name, row.get("teamPosition") or index, row.get("teamPoints"))

    drivers = get_json(f"{FORMULA_E}/standings/drivers?championshipId={current['id']}")
    drivers = drivers if isinstance(drivers, list) else next((v for v in drivers.values() if isinstance(v, list)), [])
    for row in drivers:
        team = teams.get(title_case(row.get("driverTeamName") or row.get("teamName") or "").lower())
        if team:
            name = f"{row.get('driverFirstName', '')} {row.get('driverLastName', '')}".strip()
            team.members.append(Member(name, row.get("driverPosition"), row.get("driverPoints")))

    races = get_json(f"{FORMULA_E}/races?championshipId={current['id']}")["races"]
    today = dt.date.today().isoformat()
    done = [r for r in races if (r.get("date") or "")[:10] <= today]
    done.sort(key=lambda r: r.get("date") or "")
    last_rows, last_race = [], None
    for race in done:
        rows = formula_e_race_rows(race["id"])
        if not rows:
            continue
        for team in teams.values():
            team.rounds += 1
        for row in rows:
            team = teams.get(title_case((row.get("team") or {}).get("name") or "").lower())
            position = row.get("driverPosition") or 0
            if team and position:
                if position == 1:
                    team.wins += 1
                if position <= 3:
                    team.podiums += 1
        last_rows, last_race = rows, race
    if last_rows:
        for team in teams.values():
            team.last_race = last_race.get("name")
            team.last_race_round = last_race.get("sequence") or len(done)
            team.last_race_date = (last_race.get("date") or "")[:10] or None
        for row in last_rows:
            team = teams.get(title_case((row.get("team") or {}).get("name") or "").lower())
            if not team:
                continue
            name = f"{row.get('driverFirstName', '')} {row.get('driverLastName', '')}".strip() or row.get("driverTLA", "")
            points = row.get("driverPoints")
            team.lines.append(RaceLine(name, row.get("driverPosition") or None, row.get("driverGridPosition"),
                                       float(points) if points is not None else None, None))
    race = None
    if last_rows:
        race = RaceSummary("formulae", last_race.get("name", "E-Prix"), last_race.get("sequence") or len(done),
                           (last_race.get("date") or "")[:10] or None)
        for row in last_rows:
            name = f"{row.get('driverFirstName', '')} {row.get('driverLastName', '')}".strip() or row.get("driverTLA", "")
            points = row.get("driverPoints")
            race.rows.append(Classified(
                row.get("driverPosition") or None, name, title_case((row.get("team") or {}).get("name") or ""),
                row.get("driverGridPosition"), float(points) if points is not None else None, None))
    return sorted(teams.values(), key=lambda t: t.position or 99), race


def formula_e_race_rows(race_id: str) -> list[dict]:
    sessions = get_json(f"{FORMULA_E}/races/{race_id}/sessions").get("sessions") or []
    race = next((s for s in sessions
                 if s.get("sessionDate") and (s.get("sessionName") or "").strip().lower() == "race"), None)
    if not race:
        return []
    payload = get_json(f"{FORMULA_E}/races/{race_id}/sessions/{race['id']}/results")
    return payload if isinstance(payload, list) else next((v for v in payload.values() if isinstance(v, list)), [])


def title_case(shouted: str) -> str:
    return tidy_title(shouted) if shouted.isupper() else shouted


# ---------------------------------------------------------------------------
# Prose


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_points(points: float | None) -> str:
    if points is None:
        return ""
    return str(int(points)) if float(points).is_integer() else f"{points:g}"


def template_summary(t: Team) -> str:
    series = SERIES[t.channel_id]
    parts = []
    if t.position and t.points is not None:
        parts.append(f"{t.name} are {ordinal(t.position)} in the {series} {ENTRANT[t.channel_id]}s' standings on {fmt_points(t.points)} points after {t.rounds} rounds")
    elif t.position:
        parts.append(f"{t.name} are the {ordinal(t.position)}-ranked {ENTRANT[t.channel_id]} in {series} after {t.rounds} rounds")
    else:
        parts.append(f"{t.name} have completed {t.rounds} rounds of the {series} season")
    if t.wins or t.podiums:
        parts.append(f"with {t.wins} win{'s' if t.wins != 1 else ''} and {t.podiums} podium{'s' if t.podiums != 1 else ''}")
    text = ", ".join(parts) + "."
    lead = [m for m in t.members if m.position]
    if lead:
        best = min(lead, key=lambda m: m.position)
        text += f" {best.name} is their best-placed {PEOPLE[t.channel_id][:-1]}, {ordinal(best.position)} overall."
    return text


def template_last_race(t: Team) -> str:
    if not t.last_race or not t.lines:
        return ""
    bits = []
    for line in sorted(t.lines, key=lambda l: l.position or 99):
        if line.position:
            place = ordinal(line.position)
            if line.grid:
                place += f" from {ordinal(line.grid)} on the grid"
            bits.append(f"{line.name} finished {place}")
        else:
            bits.append(f"{line.name} did not finish" + (f" ({line.status})" if line.status else ""))
    return f"At the {t.last_race}, " + "; ".join(bits) + "."


def template_race(race: RaceSummary) -> tuple[str, list[str]]:
    rows = sorted([r for r in race.rows if r.position], key=lambda r: r.position)
    if not rows:
        return "", []
    podium = rows[:3]
    summary = f"{podium[0].name} won the {race.name} for {podium[0].team}"
    if len(podium) > 1:
        summary += ", ahead of " + " and ".join(f"{r.name} ({r.team})" for r in podium[1:])
    summary += "."
    highlights = []
    climbers = [r for r in rows if r.grid and r.grid - r.position >= 5]
    if climbers:
        best = max(climbers, key=lambda r: r.grid - r.position)
        highlights.append(f"{best.name} climbed from {ordinal(best.grid)} on the grid to {ordinal(best.position)}.")
    fastest = next((r for r in race.rows if r.fastest_lap), None)
    if fastest:
        highlights.append(f"Fastest lap went to {fastest.name}.")
    out = [r for r in race.rows if r.position is None and r.name]
    if out:
        highlights.append("Did not finish: " + ", ".join(r.name for r in out) + ".")
    return summary, highlights


SYSTEM_PROMPT = """You write short team performance notes inside E-Grid, a motorsport companion app. British English. Plain, knowledgeable and even-handed — a well-read fan writing to other fans, not a press release and not a team's own marketing.

Hard rules:
- Use ONLY the facts supplied for that team. Never add results, incidents, penalties, weather, injuries, upgrades, quotes, rumours, history or other teams' results that are not in the facts.
- Never name a person who is not listed in that team's facts.
- Numbers must match the facts exactly: positions, points, wins, podiums, rounds, grid slots.
- Do not mention "this app", "E-Grid" or "the review" in the text.
- No exclamation marks, no emoji, no headings, no bullet points.

For every team index write:
- summary: at most 70 words on the season so far — where they stand, how the points have come, how their drivers or riders compare with each other. If position or points are absent, describe the season without ranking them.
- lastRace: at most 60 words on the most recent race, from the lines given (finishing position, grid position where given, points, a did-not-finish). If no race lines are given, return an empty string.

If a "race" object is supplied — the full classification of that most recent race — also write, under the same rules:
- race.summary: at most 90 words on the race as a whole: the winner and podium with their teams, and what the classification shows (a drive through the field, a front-row car that fell away). Use only the rows given; nothing about incidents, weather or strategy unless a status or gap in the rows states it.
- race.highlights: two to four short sentences, each one fact from the classification: the biggest climb from grid to finish, the fastest lap where marked, a retirement and its stated reason, a first points finish only if the rows show it. Each at most 25 words. No bullets or numbering inside the strings."""


def race_facts(race: RaceSummary | None) -> dict | None:
    if not race or not race.rows:
        return None
    return {
        "name": race.name, "round": race.round_number, "date": race.date,
        "classification": [asdict(r) for r in sorted(race.rows, key=lambda r: r.position or 99)],
    }


def facts_for_llm(teams: list[Team]) -> list[dict]:
    facts = []
    for index, t in enumerate(teams):
        entry = {
            "index": index,
            "series": SERIES[t.channel_id],
            "team": t.name,
            "standing": {"position": t.position, "points": t.points, "rounds": t.rounds,
                         "wins": t.wins, "podiums": t.podiums},
            PEOPLE[t.channel_id]: [asdict(m) for m in t.members],
        }
        if t.last_race and t.lines:
            entry["lastRace"] = {"name": t.last_race, "round": t.last_race_round, "date": t.last_race_date,
                                 "lines": [asdict(l) for l in t.lines]}
        facts.append(entry)
    return facts


def ask_own_server(facts: list[dict], race: dict | None) -> dict | None:
    """Any OpenAI-compatible chat endpoint: POST {EGRID_LLM_URL}/v1/chat/completions."""
    base = os.environ.get("EGRID_LLM_URL", "").strip().rstrip("/")
    if not base:
        return None
    if "://" not in base:
        base = "https://" + base
    if not base.endswith("/chat/completions"):
        base = base if base.endswith("/v1") else base + "/v1"
        base += "/chat/completions"
    model = os.environ.get("EGRID_LLM_MODEL", "").strip()
    token = os.environ.get("EGRID_LLM_TOKEN", "").strip()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    shape = json.dumps({"teams": [{"index": 0, "summary": "...", "lastRace": "..."}],
                        "race": {"summary": "...", "highlights": ["...", "..."]}})
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nReply with a single JSON object and nothing else, shaped exactly: " + shape},
            {"role": "user", "content": json.dumps({"teams": facts, "race": race}, ensure_ascii=False, indent=1)},
        ],
        "temperature": 0.4,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    if model:
        payload["model"] = model
    request = urllib.request.Request(base, data=json.dumps(payload).encode(), headers=headers, method="POST")
    # A self-hosted model occasionally answers with prose around a broken
    # object; one more try at the same temperature usually lands clean JSON,
    # and is far cheaper than losing the whole series to the template.
    data = None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                reply = json.load(response)
        except urllib.error.HTTPError as error:
            log(f"  own server {base}: HTTP {error.code}; falling through")
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            log(f"  own server {base}: {type(error).__name__}: {error}; falling through")
            return None
        try:
            text = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log("  own server: reply had no choices[0].message.content; falling through")
            return None
        data = parse_json_reply(text or "")
        if data is not None:
            break
        log(f"  own server returned non-JSON (attempt {attempt})")
    if data is None:
        log("  own server returned non-JSON twice; falling through")
        return None
    usage = reply.get("usage") or {}
    log(f"  own server ({reply.get('model') or model or 'default model'}) wrote the notes "
        f"({usage.get('prompt_tokens', '?')} in / {usage.get('completion_tokens', '?')} out)")
    return data


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "teams": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "summary": {"type": "string"},
                               "lastRace": {"type": "string"}},
                "required": ["index", "summary", "lastRace"],
                "additionalProperties": False,
            },
        },
        "race": {
            "type": "object",
            "properties": {"summary": {"type": "string"},
                           "highlights": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary", "highlights"],
            "additionalProperties": False,
        },
    },
    "required": ["teams", "race"],
    "additionalProperties": False,
}


def ask_claude(facts: list[dict], race: dict | None) -> dict | None:
    try:
        import anthropic  # noqa: WPS433 - optional dependency
    except ImportError:
        log("  anthropic SDK not installed")
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log("  no Anthropic credentials in the environment")
        return None
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps({"teams": facts, "race": race}, ensure_ascii=False, indent=1)}],
            output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        )
    except anthropic.APIError as error:
        log(f"  Claude call failed: {error}")
        return None
    if response.stop_reason != "end_turn":
        log(f"  Claude stopped with {response.stop_reason}")
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log("  Claude returned non-JSON")
        return None
    log(f"  Claude wrote the notes ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
    return data


def names_outside(text: str, team: Team, everyone: set[str]) -> list[str]:
    """Surnames of people in this series who are NOT on this team but are
    named in the text — the check that keeps a writer inside its facts."""
    own = {m.name for m in team.members} | {l.name for l in team.lines}
    own_surnames = {n.split()[-1].lower() for n in own if n}
    words = {w.strip(".,;:()'\"").lower() for w in text.split()}
    strays = []
    for full in everyone:
        surname = full.split()[-1].lower() if full else ""
        if surname and surname not in own_surnames and surname in words and len(surname) > 3:
            strays.append(full)
    return strays


def stray_names(text: str, allowed: set[str], everyone: set[str]) -> list[str]:
    """People from this series named in the text who are not in `allowed`."""
    ok = {n.split()[-1].lower() for n in allowed if n}
    words = {w.strip(".,;:()'\"").lower() for w in text.split()}
    return [full for full in everyone
            if (sur := full.split()[-1].lower() if full else "") and sur not in ok and sur in words and len(sur) > 3]


def write_reviews(teams: list[Team], race: RaceSummary | None,
                  use_llm: bool) -> tuple[list[tuple[str, str, str]], tuple[str, list[str], str]]:
    """Per team (summary, lastRace, writer), and for the race (summary,
    highlights, writer) — templated wherever the writer said nothing, or
    named someone it was not given."""
    out = [(template_summary(t), template_last_race(t), "template") for t in teams]
    race_out = (*template_race(race), "template") if race else ("", [], "template")
    if not use_llm or not teams:
        return out, race_out
    facts = facts_for_llm(teams)
    data, writer = None, ""
    for name, ask in (("own server", ask_own_server), ("Claude", ask_claude)):
        try:
            data = ask(facts, race_facts(race))
        except Exception as error:  # noqa: BLE001 - a writer must never sink the file
            log(f"  {name} writer crashed: {type(error).__name__}: {error}; falling through")
            continue
        if data is not None:
            writer = name
            break
    if data is None:
        log("  no writer available; using templated prose")
        return out, race_out
    everyone = {m.name for t in teams for m in t.members} | {l.name for t in teams for l in t.lines}
    if race:
        everyone |= race.everyone
        written = data.get("race") or {}
        summary = str(written.get("summary") or "").strip()
        highlights = [str(h).strip() for h in (written.get("highlights") or []) if str(h).strip()][:4]
        strays = stray_names(summary + " " + " ".join(highlights), race.everyone, everyone)
        if strays:
            log(f"  dropped {writer} race copy: names {', '.join(strays)} who were not classified")
        elif summary:
            race_out = (summary, highlights or race_out[1], writer)
    by_index = {}
    for item in data.get("teams", []) or []:
        try:
            by_index[int(item["index"])] = (str(item.get("summary") or "").strip(),
                                            str(item.get("lastRace") or "").strip())
        except (KeyError, TypeError, ValueError):
            continue
    for index, team in enumerate(teams):
        summary, last = by_index.get(index, ("", ""))
        tmpl_summary, tmpl_last, _ = out[index]
        strays = names_outside(summary + " " + last, team, everyone)
        if strays:
            log(f"  dropped {writer} notes for {team.name}: names {', '.join(strays)} who are not on the team")
            continue
        if not team.lines:
            last = ""
        out[index] = (summary or tmpl_summary, last or tmpl_last, writer if (summary or last) else "template")
    return out, race_out


# ---------------------------------------------------------------------------
# Main


FETCHERS = {"f1": f1_teams, "motogp": motogp_teams, "formulae": formula_e_teams}


def build(channels: list[str], use_llm: bool) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    entries, races = [], []
    for channel_id in channels:
        log(f"{channel_id}")
        try:
            teams, race = FETCHERS[channel_id]()
        except Exception as error:  # noqa: BLE001 - one dead feed must not sink the others
            log(f"  {channel_id} skipped: {type(error).__name__}: {error}")
            continue
        log(f"  {len(teams)} {ENTRANT[channel_id]}s, {teams[0].rounds if teams else 0} rounds run")
        reviews, (race_summary, race_highlights, race_writer) = write_reviews(teams, race, use_llm)
        if race and race.rows:
            ordered = sorted([r for r in race.rows if r.position], key=lambda r: r.position)
            races.append({
                "channelID": channel_id,
                "name": race.name, "round": race.round_number, "date": race.date,
                "podium": [{"position": r.position, "name": r.name, "team": r.team} for r in ordered[:3]],
                "classified": len(ordered),
                "review": {"summary": race_summary, "highlights": race_highlights, "writer": race_writer},
                "publishedAt": now.isoformat().replace("+00:00", "Z"),
            })
        for team, (summary, last, writer) in zip(teams, reviews):
            entry = {
                "id": f"{channel_id}-{team.name.lower().replace(' ', '-')}",
                "channelID": channel_id,
                "name": team.name,
                "matchKeys": team.match_keys,
                "season": {
                    "position": team.position, "points": team.points, "rounds": team.rounds,
                    "wins": team.wins, "podiums": team.podiums,
                    "members": [asdict(m) for m in team.members],
                },
                "review": {"summary": summary, "lastRace": last, "writer": writer},
                "publishedAt": now.isoformat().replace("+00:00", "Z"),
            }
            if team.last_race and team.lines:
                entry["lastRace"] = {
                    "name": team.last_race, "round": team.last_race_round, "date": team.last_race_date,
                    "lines": [asdict(l) for l in sorted(team.lines, key=lambda l: l.position or 99)],
                }
            entries.append(entry)
    return {"version": 1, "generatedAt": now.isoformat().replace("+00:00", "Z"), "teams": entries, "races": races}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-llm", action="store_true", help="templated prose only")
    parser.add_argument("--channel", action="append", choices=sorted(FETCHERS), help="limit to one series (repeatable)")
    args = parser.parse_args()

    payload = build(args.channel or list(FETCHERS), use_llm=not args.no_llm)
    if not payload["teams"]:
        log("nothing could be built")
        return 1
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"wrote {args.out}: {len(payload['teams'])} teams")
    return 0


if __name__ == "__main__":
    sys.exit(main())
