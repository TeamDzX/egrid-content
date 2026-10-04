#!/usr/bin/env python3
"""Builds the weekly editorial digest that both E-Grid apps show on Home.

Rebuilt every morning, written into `digest.json` at the repo root:

  recap    (Mon, Tue)  what ran over the last seven days, with podiums where a
                       series publishes them (F1, MotoGP, Formula E). Tuesday
                       re-runs it so a late-posted Sunday-night result lands.
  preview  (Wed-Sun)   every round of every channel racing this weekend,
                       refreshed daily as start times firm up; on Sunday it
                       is simply today's running order.

The facts come from the same sources the apps use — the static calendars in
`channels.json`, Jolpica for F1, the Pulselive feed for MotoGP and
`formula-e.json` (built from the official site by formulae/build_formula_e.py)
for Formula E — plus NASCAR's own public results feed, which carries start
times, broadcasters, podiums and race statistics the static calendar lacks.
WRC's API is dead, so its rounds come from the static calendar like the
other eight channels.

Every round carries a few short `facts` lines (session times, the title
fight, the venue, race statistics) that the apps show under the prose and
the writer is given to work from.

Besides the dated editions the file holds `lastRaces`: one write-up per
series of its most recent completed round, whenever it was. Editions expire
within days; these stay until the next round replaces them, so a race page
and a channel's "Last Race" section always have something to say. A
write-up is only re-written when its round or podium changes.

The prose is optional, and comes from whichever writer is configured:

  EGRID_LLM_URL (+ EGRID_LLM_TOKEN, EGRID_LLM_MODEL)   your own server, any
                 OpenAI-compatible chat endpoint (Ollama, vLLM, llama.cpp,
                 LM Studio, Open WebUI ...) — tried first when set
  ANTHROPIC_API_KEY                                    Claude via the API

Without either, or if the call fails for any reason, a plain templated
sentence is used instead. Either way the edition ships — a missing key
downgrades the writing, never the data. Claude is told to use only the facts
given, and the structured output is checked back against them: an item body
naming a driver who is not in the supplied podium is discarded for the
template. That is what keeps an LLM out of the results business.

Usage:
    python3 digest/build_digest.py [--kind auto|preview|recap] [--date YYYY-MM-DD]
                                   [--out digest.json] [--no-llm]
                                   [--when-due | --check-due]

The scheduled action runs hourly through the day with `--when-due`, which
rebuilds only when something has changed enough to matter: the first build
of the day, the three hours before a round with a known start time, or a
race that has finished since the last build. Nothing runs while the writer's
server hibernates (23:00-07:00 UK time), however late GitHub starts the job.
`--check-due` prints "due" or "skip" and builds nothing.

`auto` picks recap on Monday and Tuesday and preview on every other day. `--date`
pretends it is another day, for testing. Exit code is non-zero only when
nothing at all could be built; a single failed source is logged and skipped.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_PATH = REPO_ROOT / "channels.json"
DEFAULT_OUT = REPO_ROOT / "digest.json"

# Editions kept in the file. The apps only ever show the newest live one; the
# rest are there so a re-run can be compared against what it replaced.
KEEP_EDITIONS = 4

# Monday and Tuesday build the recap; every other day the preview.
RECAP_WEEKDAYS = {0, 1}

# An edition stops showing after this long. A preview from Thursday is stale
# by Tuesday; a Monday recap has been superseded by Thursday's preview.
PREVIEW_TTL_DAYS = 5
RECAP_TTL_DAYS = 4

# The preview is rebuilt this long before each round with a known start, so
# the copy and facts reflect the final running order.
PRE_RACE_LEAD = dt.timedelta(hours=3)
# A round with a start time counts as finished this long after it began, so
# its write-up can land the same evening instead of the next morning.
FINISHED_AFTER = dt.timedelta(hours=4)
# Alex's own server, which writes the prose, hibernates overnight (UK time).
SERVER_ZONE = "Europe/London"
SERVER_SLEEPS_FROM, SERVER_WAKES_AT = 23, 7

USER_AGENT = "EGrid-digest/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 20

JOLPICA = "https://api.jolpi.ca/ergast/f1"
NASCAR = "https://cf.nascar.com/cacher"
NASCAR_CUP_SERIES = 1
CIRCUITS_PATH = REPO_ROOT / "circuits.json"
FORMULA_E_PATH = REPO_ROOT / "formula-e.json"
MOTOGP = "https://api.motogp.pulselive.com/motogp/v1/results"

# Channels whose calendar comes from a live API rather than channels.json.
# Anything not listed here is read from the static `calendar` array.
LIVE_CHANNELS = {"f1", "motogp", "formulae"}


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class PodiumEntry:
    position: int
    name: str
    team: str = ""


@dataclass
class Round:
    channel_id: str
    channel_name: str
    name: str
    location: str
    date: dt.date
    time_utc: str | None = None          # "HH:MM" when the start is confirmed
    round_number: int | None = None
    external_id: str | None = None       # the feed's own ID, for result lookups
    podium: list[PodiumEntry] = field(default_factory=list)
    # Short factual lines shown under the prose: session times, the title
    # fight, the venue, race statistics. Never written by a model.
    facts: list[str] = field(default_factory=list)
    # Qualifying and sprint times; facts only for a round still to run.
    session_facts: list[str] = field(default_factory=list)

    @property
    def sort_key(self):
        return (self.date, self.channel_name)

    @property
    def start(self) -> dt.datetime | None:
        if not self.time_utc:
            return None
        try:
            clock = dt.time.fromisoformat(self.time_utc[:5])
        except ValueError:
            return None
        return dt.datetime.combine(self.date, clock, tzinfo=dt.timezone.utc)

    def has_finished(self, now: dt.datetime) -> bool:
        # A known start wins over the date: a US evening race runs past
        # midnight UTC and is not over just because the date has turned.
        if self.start is not None:
            return now >= self.start + FINISHED_AFTER
        return self.date < now.date()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


_last_request_at = 0.0


def get_json(url: str):
    """One GET, paced to four a second and retried once on a 429. Jolpica
    answers a burst with 429s, and the recap asks it for several results
    tables back to back."""
    global _last_request_at
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"})
    for attempt in (1, 2):
        wait = 0.25 - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                _last_request_at = time.monotonic()
                return json.load(response)
        except urllib.error.HTTPError as error:
            _last_request_at = time.monotonic()
            if error.code == 429 and attempt == 1:
                time.sleep(3)
                continue
            raise


def safe(label: str, fn, default):
    """Runs a source, logging and swallowing any failure. One dead feed must
    not take the whole edition down with it."""
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - deliberately broad
        log(f"  ! {label}: {type(error).__name__}: {error}")
        return default


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def load_channels() -> list[dict]:
    with CHANNELS_PATH.open() as handle:
        return json.load(handle)["channels"]


def static_rounds(channel: dict) -> list[Round]:
    rounds = []
    for entry in channel.get("calendar", []):
        try:
            date = dt.date.fromisoformat(entry["date"])
        except (KeyError, ValueError):
            continue
        rounds.append(Round(
            channel_id=channel["id"],
            channel_name=channel["name"],
            name=entry.get("name", "").strip() or channel["name"],
            location=entry.get("location", "").strip(),
            date=date,
            time_utc=entry.get("time"),
            round_number=entry.get("round"),
        ))
    return rounds


def f1_rounds(channel: dict) -> list[Round]:
    data = get_json(f"{JOLPICA}/current.json?limit=30")
    rounds = []
    for race in data["MRData"]["RaceTable"].get("Races", []):
        circuit = race.get("Circuit", {})
        place = circuit.get("Location", {})
        location = ", ".join(p for p in [circuit.get("circuitName"), place.get("country")] if p)
        rounds.append(Round(
            channel_id="f1",
            channel_name=channel["name"],
            name=race.get("raceName", "Grand Prix"),
            location=location,
            date=dt.date.fromisoformat(race["date"]),
            time_utc=(race.get("time") or "")[:5] or None,
            round_number=int(race["round"]) if race.get("round") else None,
            session_facts=f1_session_facts(race),
        ))
    return rounds


F1_SESSIONS = (("SprintQualifying", "Sprint qualifying"), ("Sprint", "Sprint"),
               ("Qualifying", "Qualifying"))


def f1_session_facts(race: dict) -> list[str]:
    """The weekend's headline sessions, earliest first: "Qualifying Sat 10 Oct,
    13:00 UTC". Practice is left out — the race page lists every session."""
    lines = []
    for key, label in F1_SESSIONS:
        session = race.get(key) or {}
        try:
            day = dt.date.fromisoformat(session["date"])
        except (KeyError, ValueError):
            continue
        time_utc = (session.get("time") or "")[:5]
        lines.append(f"{label} {day_label(day)}" + (f", {time_utc} UTC" if time_utc else ""))
    return lines


def f1_leaders() -> list[tuple[str, float]]:
    data = get_json(f"{JOLPICA}/current/driverStandings.json")
    lists = data["MRData"]["StandingsTable"].get("StandingsLists", [])
    rows = lists[0].get("DriverStandings", []) if lists else []
    return [(f"{r['Driver'].get('givenName', '')} {r['Driver'].get('familyName', '')}".strip(),
             float(r.get("points") or 0)) for r in rows[:2]]


def f1_podium(round_number: int) -> list[PodiumEntry]:
    data = get_json(f"{JOLPICA}/current/{round_number}/results.json")
    races = data["MRData"]["RaceTable"].get("Races", [])
    if not races:
        return []
    podium = []
    for row in races[0].get("Results", [])[:3]:
        driver = row.get("Driver", {})
        name = f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
        podium.append(PodiumEntry(int(row["position"]), name,
                                  row.get("Constructor", {}).get("name", "")))
    return podium


def motogp_rounds(channel: dict) -> list[Round]:
    seasons = get_json(f"{MOTOGP}/seasons")
    current = next((s for s in seasons if s.get("current")), None) or max(seasons, key=lambda s: s.get("year", 0))
    # An event under way can be listed as both finished and unfinished, and
    # the finished list is not in date order (tests come last), so dedupe by
    # ID, sort by date and count only the races.
    by_id = {}
    for finished in ("true", "false"):
        for event in get_json(f"{MOTOGP}/events?seasonUuid={current['id']}&isFinished={finished}"):
            if event.get("id") and event.get("date_end") and not event.get("test"):
                by_id[event["id"]] = event
    events = sorted(by_id.values(), key=lambda e: e["date_end"])
    rounds = []
    for index, event in enumerate(events, start=1):
        name = (event.get("name") or "").strip()
        end = event.get("date_end")
        if not name:
            continue
        # The premier class races on the event's final day.
        date = dt.date.fromisoformat(end[:10])
        title = tidy_title(name)
        country = SHORT_COUNTRY.get((event.get("country") or {}).get("name"),
                                    (event.get("country") or {}).get("name"))
        location = ", ".join(p for p in [(event.get("circuit") or {}).get("name"), country] if p)
        rounds.append(Round("motogp", channel["name"], title, location, date,
                            round_number=index, external_id=event["id"]))
    return rounds


def motogp_podium(event_id: str) -> list[PodiumEntry]:
    categories = get_json(f"{MOTOGP}/categories?eventUuid={event_id}")
    premier = next((c for c in categories if "motogp" in (c.get("name") or "").lower()), None)
    if not premier:
        return []
    sessions = get_json(f"{MOTOGP}/sessions?eventUuid={event_id}&categoryUuid={premier['id']}")
    race = next((s for s in sessions if (s.get("type") or "").upper() == "RAC"), None)
    if not race:
        return []
    rows = get_json(f"{MOTOGP}/session/{race['id']}/classification").get("classification") or []
    podium = []
    for row in rows:
        position = row.get("position")
        if position is None or position > 3:
            continue
        podium.append(PodiumEntry(position, (row.get("rider") or {}).get("full_name", ""),
                                  (row.get("team") or {}).get("name", "")))
    return sorted(podium, key=lambda p: p.position)[:3]


def motogp_leaders() -> list[tuple[str, float]]:
    seasons = get_json(f"{MOTOGP}/seasons")
    current = next((s for s in seasons if s.get("current")), None) or max(seasons, key=lambda s: s.get("year", 0))
    categories = get_json(f"{MOTOGP}/categories?seasonUuid={current['id']}")
    premier = next((c for c in categories if "motogp" in (c.get("name") or "").lower()), None)
    if not premier:
        return []
    rows = get_json(f"{MOTOGP}/standings?seasonUuid={current['id']}&categoryUuid={premier['id']}").get("classification") or []
    return [((r.get("rider") or {}).get("full_name", ""), float(r.get("points") or 0)) for r in rows[:2]]


def formula_e_leaders() -> list[tuple[str, float]]:
    rows = formula_e_file().get("driverStandings", [])
    return [(r.get("name", ""), float(r.get("points") or 0)) for r in rows[:2]]


def formula_e_file() -> dict:
    """`formula-e.json`, written by formulae/build_formula_e.py from the
    official site — Formula E's own API stopped resolving in October 2026."""
    return json.loads(FORMULA_E_PATH.read_text())


def formula_e_rounds(channel: dict) -> list[Round]:
    rounds = []
    for race in formula_e_file().get("races", []):
        location = ", ".join(p for p in [race.get("city"), race.get("country")] if p)
        race_session = next((x for x in race.get("sessions") or [] if x["name"] == "Race"), None)
        rounds.append(Round("formulae", channel["name"], race["name"], location,
                            dt.date.fromisoformat(race["date"]),
                            time_utc=race_session["start"][11:16] if race_session else None,
                            round_number=race["round"], external_id=race["id"]))
    return rounds


def formula_e_podium(race_id: str) -> list[PodiumEntry]:
    race = next((r for r in formula_e_file().get("races", []) if r["id"] == race_id), None)
    rows = ((race or {}).get("results") or {}).get("Race") or []
    return [PodiumEntry(r["position"], r["name"], r.get("team", ""))
            for r in rows if r.get("position") and r["position"] <= 3]


SMALL_WORDS = {"of", "de", "the", "del", "di", "da"}

# The MotoGP feed spells out the long-form state name.
SHORT_COUNTRY = {"United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
                 "United States of America": "USA"}


def tidy_title(shouted: str) -> str:
    """"GRAND PRIX OF ITALY" -> "Grand Prix of Italy"."""
    words = shouted.lower().split()
    return " ".join(w if (i and w in SMALL_WORDS) else w.capitalize() for i, w in enumerate(words))


def nascar_races() -> list[dict]:
    year = dt.date.today().year
    return get_json(f"{NASCAR}/{year}/{NASCAR_CUP_SERIES}/race_list_basic.json")


def nascar_feed_race(r: Round, races: list[dict]) -> dict | None:
    """The feed's entry for a calendar round: same day, give or take one,
    since the static calendar and the feed disagree on time zones."""
    best = None
    for race in races:
        try:
            day = dt.date.fromisoformat((race.get("race_date") or "")[:10])
        except ValueError:
            continue
        gap = abs((day - r.date).days)
        if gap <= 1 and (best is None or gap < best[0]):
            best = (gap, race)
    return best[1] if best else None


def enrich_nascar(r: Round, race: dict, finished: bool, time_only: bool = False) -> None:
    """Start time and broadcasters for a preview; podium and the race's
    shape for a finished round. All of it straight from the feed."""
    r.external_id = str(race.get("race_id"))
    start = next((s.get("start_time_utc") for s in race.get("schedule") or []
                  if (s.get("event_name") or "").strip().lower() == "race"), None)
    if start and len(start) >= 16:
        r.time_utc = start[11:16]
    if time_only:
        return
    if not finished:
        laps = race.get("scheduled_laps")
        distance = race.get("scheduled_distance")
        if laps and distance:
            r.facts.append(f"{laps} laps, {distance:g} miles")
        outlets = [o for o in (race.get("television_broadcaster"), race.get("radio_broadcaster")) if o]
        if outlets:
            r.facts.append("US coverage: " + " (TV), ".join(outlets[:1]) + (f" (TV), {outlets[1]} (radio)" if len(outlets) > 1 else " (TV)"))
        return
    feed = get_json(f"{NASCAR}/{r.date.year}/{NASCAR_CUP_SERIES}/{race['race_id']}/weekend-feed.json")
    weekend = (feed.get("weekend_race") or [{}])[0]
    results = sorted((x for x in weekend.get("results") or [] if x.get("finishing_position")),
                     key=lambda x: x["finishing_position"])
    r.podium = [PodiumEntry(x["finishing_position"], (x.get("driver_fullname") or "").strip(),
                            (x.get("team_name") or "").strip()) for x in results[:3]]
    laps = weekend.get("actual_laps") or race.get("actual_laps")
    changes, leaders = weekend.get("number_of_lead_changes"), weekend.get("number_of_leaders")
    if laps and changes is not None and leaders:
        r.facts.append(f"{laps} laps, {changes} lead changes among {leaders} drivers")
    cautions = weekend.get("number_of_cautions")
    if cautions is not None:
        r.facts.append(f"{cautions} caution{'s' if cautions != 1 else ''} for {weekend.get('number_of_caution_laps') or 0} laps")
    led = max(results, key=lambda x: x.get("laps_led") or 0, default=None)
    if led and led.get("laps_led"):
        r.facts.append(f"Most laps led: {led['driver_fullname'].strip()} ({led['laps_led']})")
    margin = (weekend.get("margin_of_victory") or "").strip()
    if margin and margin[0] in ".0123456789":
        r.facts.append(f"Margin of victory: {('0' + margin) if margin.startswith('.') else margin} s")


def normalise(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def load_circuits() -> list[dict]:
    try:
        return json.loads(CIRCUITS_PATH.read_text()).get("circuits", [])
    except (OSError, json.JSONDecodeError):
        return []


def circuit_for(r: Round, circuits: list[dict]) -> dict | None:
    """Same rule as the apps' `circuit(for:)`: longest matching key wins,
    series-specific entries before shared ones."""
    haystack = normalise(f"{r.location} {r.name}")

    def best(candidates):
        scored = []
        for c in candidates:
            hits = [len(k) for k in (normalise(k) for k in c.get("matchKeys") or [] if k) if k in haystack]
            if hits:
                scored.append((max(hits), c))
        return max(scored, key=lambda t: t[0])[1] if scored else None

    return (best([c for c in circuits if r.channel_id in (c.get("channelIDs") or [])])
            or best([c for c in circuits if not c.get("channelIDs")]))


def circuit_fact(circuit: dict) -> str | None:
    parts = []
    if circuit.get("lengthKm"):
        parts.append(f"{circuit['lengthKm']:g} km lap")
    if circuit.get("corners"):
        parts.append(f"{circuit['corners']} corners")
    if circuit.get("firstHeld"):
        parts.append(f"first raced {circuit['firstHeld']}")
    return ", ".join(parts).capitalize() if parts else None


LEADER_SOURCES = {"f1": f1_leaders, "motogp": motogp_leaders, "formulae": formula_e_leaders}
_leaders_cache: dict[str, list[tuple[str, float]]] = {}


def title_fight(channel_id: str) -> str | None:
    """'Kimi Antonelli leads the championship by 84 points from George
    Russell' — current standings, so only ever attached to the newest
    rounds (this weekend's, or a series' last race)."""
    source = LEADER_SOURCES.get(channel_id)
    if not source:
        return None
    if channel_id not in _leaders_cache:
        _leaders_cache[channel_id] = safe(f"{channel_id} standings", source, [])
    leaders = _leaders_cache[channel_id]
    if len(leaders) < 2 or not leaders[0][0]:
        return None
    (first, a), (second, b) = leaders[0], leaders[1]
    gap = a - b
    if gap <= 0:
        return f"{first} and {second} are level on {a:g} points at the top of the championship"
    return f"{first} leads the championship by {gap:g} point{'s' if gap != 1 else ''} from {second}"


def add_context(rounds: list[Round], circuits: list[dict], with_standings: bool,
                upcoming: bool = False) -> None:
    for r in rounds:
        if upcoming:
            r.facts[:0] = r.session_facts
        if with_standings:
            line = title_fight(r.channel_id)
            if line:
                r.facts.append(line)
        circuit = circuit_for(r, circuits)
        line = circuit_fact(circuit) if circuit else None
        if line:
            r.facts.append(line)


def fetch_podium(r: Round) -> list[PodiumEntry]:
    if r.channel_id == "f1" and r.round_number:
        return safe(f"F1 podium round {r.round_number}", lambda: f1_podium(r.round_number), [])
    if r.channel_id == "motogp" and r.external_id:
        return safe(f"MotoGP podium {r.name}", lambda: motogp_podium(r.external_id), [])
    if r.channel_id == "formulae" and r.external_id:
        return safe(f"Formula E podium {r.name}", lambda: formula_e_podium(r.external_id), [])
    return r.podium


def season_rounds(channels: list[dict]) -> list[Round]:
    """Every round of every live channel this season, undated filtering
    left to the caller."""
    all_rounds: list[Round] = []
    for channel in channels:
        if channel.get("comingSoon"):
            continue
        cid = channel["id"]
        if cid == "f1":
            rounds = safe("F1 calendar", lambda: f1_rounds(channel), [])
        elif cid == "motogp":
            rounds = safe("MotoGP calendar", lambda: motogp_rounds(channel), [])
        elif cid == "formulae":
            rounds = safe("Formula E calendar", lambda: formula_e_rounds(channel), [])
        else:
            rounds = static_rounds(channel)
        if not rounds and cid in LIVE_CHANNELS:
            # A live feed that failed still has the static calendar as a
            # fallback when the file carries one (WRC does).
            rounds = static_rounds(channel)
        log(f"  {channel['name']}: {len(rounds)} rounds")
        all_rounds += rounds
    return all_rounds


_nascar_cache: list[dict] | None = None


def nascar_feed() -> list[dict]:
    global _nascar_cache
    if _nascar_cache is None:
        _nascar_cache = safe("NASCAR feed", nascar_races, [])
    return _nascar_cache


def nascar_start_times(rounds: list[Round]) -> None:
    """Fills in NASCAR start times only, which the static calendar lacks."""
    for r in rounds:
        if r.channel_id == "nascar":
            race = nascar_feed_race(r, nascar_feed())
            if race:
                safe(f"NASCAR {r.name}", lambda: enrich_nascar(r, race, False, time_only=True), None)


def finish_rounds(rounds: list[Round], finished: bool, now: dt.datetime) -> None:
    """Podiums for finished rounds, and NASCAR's feed for both kinds."""
    for r in rounds:
        if r.channel_id == "nascar":
            race = nascar_feed_race(r, nascar_feed())
            if race:
                done = finished and r.has_finished(now)
                safe(f"NASCAR {r.name}", lambda: enrich_nascar(r, race, done), None)
    if finished:
        for r in rounds:
            r.podium = fetch_podium(r)


# --------------------------------------------------------------------------- #
# Editions
# --------------------------------------------------------------------------- #


def window(kind: str, today: dt.date) -> tuple[dt.date, dt.date]:
    if kind == "recap":
        return today - dt.timedelta(days=7), today - dt.timedelta(days=1)
    # Preview: from today to the coming Sunday, plus Monday for the handful of
    # rounds (Le Mans, Bathurst-style events) that spill over.
    days_to_sunday = (6 - today.weekday()) % 7
    return today, today + dt.timedelta(days=days_to_sunday + 1)


def day_label(date: dt.date) -> str:
    return f"{date:%a} {date.day} {date:%b}"


def range_label(start: dt.date, end: dt.date) -> str:
    if start.month == end.month:
        return f"{start.day}–{end.day} {end:%B}"
    return f"{start.day} {start:%B} – {end.day} {end:%B}"


def template_body(r: Round, kind: str) -> str:
    """Fallback copy. The headline and detail line already carry the event,
    venue and day, so this adds what they don't: the round, the start time,
    the podium."""
    which = f"Round {r.round_number} of the {r.channel_name} season" if r.round_number \
        else f"This {r.channel_name} round"
    if kind != "preview":
        if r.podium:
            first = r.podium[0]
            winner = first.name + (f" ({first.team})" if first.team else "")
            rest = [p.name for p in r.podium[1:]]
            chase = f" from {' and '.join(rest)}" if rest else ""
            return f"{which}. Won by {winner}{chase}."
        when = f" on {day_label(r.date)}" if kind == "lastRaces" else ""
        return f"{which} ran at {r.location or r.name}{when}. Results for this series are not in E-Grid's feeds yet."
    if r.time_utc:
        return f"{which}. The race starts at {r.time_utc} UTC."
    return f"{which}."


def template_intro(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> str:
    series = sorted({r.channel_name for r in rounds})
    if not rounds:
        return ("No rounds in the last seven days across the series E-Grid follows."
                if kind == "recap" else
                "A quiet weekend: none of the thirteen series E-Grid follows is racing.")
    listed = ", ".join(series[:-1]) + (f" and {series[-1]}" if len(series) > 1 else series[0])
    if kind == "recap":
        return f"{len(rounds)} round{'s' if len(rounds) != 1 else ''} ran between {range_label(start, end)}, across {listed}."
    return f"{len(rounds)} round{'s' if len(rounds) != 1 else ''} across {listed} between {range_label(start, end)}."


def facts_for_llm(rounds: list[Round], kind: str) -> list[dict]:
    facts = []
    for index, r in enumerate(rounds):
        entry = {
            "index": index,
            "series": r.channel_name,
            "event": r.name,
            "location": r.location or None,
            "date": r.date.isoformat(),
            "day": day_label(r.date),
        }
        if r.round_number:
            entry["round"] = r.round_number
        if r.time_utc and kind == "preview":
            entry["startUTC"] = r.time_utc
        if kind != "preview":
            entry["podium"] = [asdict(p) for p in r.podium] or "not published"
        if r.facts:
            entry["facts"] = r.facts
        facts.append(entry)
    return facts


SYSTEM_PROMPT = """You write the short weekly digest inside E-Grid, a motorsport companion app that follows thirteen championships. British English. Plain, warm, knowledgeable — a well-read fan writing to other fans, not a press release.

Hard rules:
- Use ONLY the facts supplied. Never add results, drivers, teams, weather, injuries, standings, penalties or storylines that are not in the facts. If a podium says "not published", do not name anyone, and do not mention results or their absence at all — write about the event itself.
- Never invent a start time, a round number or a location.
- Do not mention "this app", "E-Grid" or "the digest" in the text.
- No exclamation marks, no emoji, no headings, no bullet points.

Write:
- intro: at most 55 words setting up the week. It may count rounds and name series.
- items: for every fact index, two or three sentences of at most 60 words about that round. For a recap with a podium, state the winner and the other two podium finishers with their teams where given. For a preview, say where and when it runs, in the local-day form given, and add the UTC start only when supplied.
- Where a round carries "facts", use one or two of them to give the reader something more than the date: what is at stake in the championship, a session to watch, the character of the venue, how the race unfolded. Restate them faithfully; do not draw conclusions they do not support.
- An edition called "lastRaces" covers each series' most recent round, whenever it ran: write about it in the past tense."""


COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "body": {"type": "string"}},
                "required": ["index", "body"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intro", "items"],
    "additionalProperties": False,
}


def copy_request(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> str:
    """The facts, as the JSON document every writer is given."""
    return json.dumps({
        "edition": kind,
        "window": {"from": start.isoformat(), "to": end.isoformat(), "label": range_label(start, end)},
        "rounds": facts_for_llm(rounds, kind),
    }, ensure_ascii=False, indent=1)


def apply_copy(data: dict, rounds: list[Round], kind: str, start: dt.date, end: dt.date,
               writer: str) -> tuple[str, list[str]]:
    """Merges a writer's JSON over the templated copy, item by item, keeping
    the template wherever the writer said nothing or said too much."""
    bodies = [template_body(r, kind) for r in rounds]
    by_index = {}
    for item in data.get("items", []) or []:
        try:
            by_index[int(item["index"])] = str(item["body"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
    for index, r in enumerate(rounds):
        body = by_index.get(index)
        if not body:
            continue
        if kind != "preview" and not r.podium and looks_like_a_result(body):
            log(f"  dropped {writer} copy for {r.name}: reads like a result with no podium supplied")
            continue
        if kind == "preview" and looks_like_a_result(body, PREVIEW_RESULT_WORDS):
            log(f"  dropped {writer} copy for {r.name}: a preview that reads like a result")
            continue
        bodies[index] = body
    intro = str(data.get("intro") or "").strip() or template_intro(rounds, kind, start, end)
    return intro, bodies


def parse_json_reply(text: str) -> dict | None:
    """Self-hosted models often wrap JSON in a code fence or a sentence;
    take the outermost object and ignore the rest."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    first, last = text.find("{"), text.rfind("}")
    if first == -1 or last == -1:
        return None
    try:
        data = json.loads(text[first:last + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_with_own_server(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> dict | None:
    """Any OpenAI-compatible chat endpoint: POST {EGRID_LLM_URL}/v1/chat/completions
    with a bearer token. Returns the parsed JSON copy, or None to fall through."""
    base = os.environ.get("EGRID_LLM_URL", "").strip().rstrip("/")
    if not base:
        return None
    if "://" not in base:
        # A bare hostname in the secret is the natural thing to type.
        base = "https://" + base
    if not base.endswith("/chat/completions"):
        base = base if base.endswith("/v1") else base + "/v1"
        base += "/chat/completions"
    model = os.environ.get("EGRID_LLM_MODEL", "").strip()
    token = os.environ.get("EGRID_LLM_TOKEN", "").strip()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nReply with a single JSON object and nothing else, shaped exactly: "
             + json.dumps({"intro": "...", "items": [{"index": 0, "body": "..."}]})},
            {"role": "user", "content": copy_request(rounds, kind, start, end)},
        ],
        "temperature": 0.4,
        "max_tokens": 8000,
        # Honoured by servers that support it (Ollama, vLLM, LM Studio),
        # harmlessly ignored by the rest — hence the instruction above too.
        "response_format": {"type": "json_object"},
    }
    if model:
        payload["model"] = model
    request = urllib.request.Request(base, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
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
    if data is None:
        log("  own server returned non-JSON; falling through")
        return None
    usage = reply.get("usage") or {}
    log(f"  own server ({reply.get('model') or model or 'default model'}) wrote the copy "
        f"({usage.get('prompt_tokens', '?')} in / {usage.get('completion_tokens', '?')} out)")
    return data


def write_with_claude(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> dict | None:
    """Claude through the Anthropic SDK, with a structured-output schema so the
    reply is valid JSON by construction. Returns None on any failure."""
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
            max_tokens=6000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": copy_request(rounds, kind, start, end)}],
            output_config={"format": {"type": "json_schema", "schema": COPY_SCHEMA}},
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
    log(f"  Claude wrote the copy ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
    return data


def write_copy(rounds: list[Round], kind: str, start: dt.date, end: dt.date,
               writer_out: list | None = None) -> tuple[str, list[str]] | None:
    """Tries the writers in order — your own server first, then Claude — and
    returns None when neither produced anything, so the template stands in."""
    for name, writer in (("own server", write_with_own_server), ("Claude", write_with_claude)):
        try:
            data = writer(rounds, kind, start, end)
        except Exception as error:  # noqa: BLE001 - a writer must never sink the edition
            log(f"  {name} writer crashed: {type(error).__name__}: {error}; falling through")
            continue
        if data is not None:
            if writer_out is not None:
                writer_out.append(name)
            return apply_copy(data, rounds, kind, start, end, writer=name)
    log("  no writer available; using templated prose")
    return None


RESULT_WORDS = ("won", "win", "victory", "podium", "finished", "second", "third", "p1", "p2", "p3")


# A preview may say a driver is second in the standings, but never that
# anyone won or finished anything.
PREVIEW_RESULT_WORDS = ("won ", "winner", "victory", "podium", "finished")


def looks_like_a_result(text: str, words=RESULT_WORDS) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def item_json(r: Round, body: str) -> dict:
    detail = " · ".join(p for p in [r.location, day_label(r.date)] if p)
    item = {
        "channelID": r.channel_id,
        "channelName": r.channel_name,
        "headline": r.name,
        "detail": detail,
        "body": body,
        "date": r.date.isoformat(),
    }
    if r.round_number:
        item["round"] = r.round_number
    if r.facts:
        item["facts"] = r.facts
    if r.podium:
        item["podium"] = [asdict(p) for p in r.podium]
    return item


def build_edition(kind: str, today: dt.date, season: list[Round], circuits: list[dict],
                  use_llm: bool) -> dict:
    start, end = window(kind, today)
    log(f"{kind} edition for {start} .. {end}")
    rounds = sorted((r for r in season if start <= r.date <= end), key=lambda r: r.sort_key)
    finish_rounds(rounds, finished=(kind == "recap"),
                  now=dt.datetime.now(dt.timezone.utc))
    # Standings are today's, so they belong with this weekend, not last week.
    add_context(rounds, circuits, with_standings=(kind == "preview"), upcoming=(kind == "preview"))
    log(f"  {len(rounds)} rounds in window")

    written = write_copy(rounds, kind, start, end) if (use_llm and rounds) else None
    if written:
        intro, bodies = written
    else:
        intro = template_intro(rounds, kind, start, end)
        bodies = [template_body(r, kind) for r in rounds]

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ttl = RECAP_TTL_DAYS if kind == "recap" else PREVIEW_TTL_DAYS
    return {
        "id": f"{today.isoformat()}-{kind}",
        "kind": kind,
        "title": "Last week in motorsport" if kind == "recap" else "This weekend in motorsport",
        "dateRange": range_label(start, end),
        "intro": intro,
        "publishedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + dt.timedelta(days=ttl)).isoformat().replace("+00:00", "Z"),
        "items": [item_json(r, body) for r, body in zip(rounds, bodies)],
    }


LAST_RACE_BATCH = 4


def lastrace_key(item: dict) -> tuple:
    """What a write-up is about. When it is unchanged the old prose stands,
    so the writer is asked only about rounds that are new or newly scored."""
    return (item.get("channelID"), item.get("headline"), item.get("date"),
            tuple(p.get("name") for p in item.get("podium") or []))


def latest_finished(season: list[Round], now: dt.datetime) -> list[Round]:
    """Each series' most recent finished round. Today's rounds count once
    their known start is FINISHED_AFTER behind us."""
    nascar_start_times([r for r in season if r.date == now.date()])
    latest: dict[str, Round] = {}
    for r in season:
        if r.has_finished(now) and (r.channel_id not in latest or r.date > latest[r.channel_id].date):
            latest[r.channel_id] = r
    return sorted(latest.values(), key=lambda r: r.sort_key, reverse=True)


def build_last_races(now: dt.datetime, season: list[Round], circuits: list[dict],
                     previous: list[dict], use_llm: bool) -> list[dict]:
    """Each series' most recent completed round, whenever it ran."""
    rounds = latest_finished(season, now)
    today = now.date()
    log(f"last races: {len(rounds)} series")
    finish_rounds(rounds, finished=True, now=now)
    add_context(rounds, circuits, with_standings=True)

    old = {lastrace_key(i): i for i in previous}
    items = [item_json(r, template_body(r, "lastRaces")) for r in rounds]
    stale = [index for index, item in enumerate(items)
             if (old.get(lastrace_key(item)) or {}).get("writer") in (None, "template")]
    for index, item in enumerate(items):
        kept = old.get(lastrace_key(item))
        if kept and index not in stale:
            item["body"], item["writer"] = kept["body"], kept["writer"]
        else:
            item["writer"] = "template"

    if not stale:
        log(f"  all {len(items)} unchanged; nothing to write")
    elif use_llm:
        log(f"  writing {len(stale)} last-race write-up(s)")
        # In small batches: a self-hosted model asked for a dozen at once
        # tends to run out of room and return broken JSON for all of them.
        for first in range(0, len(stale), LAST_RACE_BATCH):
            batch = stale[first:first + LAST_RACE_BATCH]
            todo = [rounds[i] for i in batch]
            writer_out: list[str] = []
            written = write_copy(todo, "lastRaces", today, today, writer_out)
            if not written:
                continue
            _, bodies = written
            for index, body, r in zip(batch, bodies, todo):
                if body != template_body(r, "lastRaces"):
                    items[index]["body"] = body
                    items[index]["writer"] = writer_out[0] if writer_out else "own server"
    return items


def read_existing(out_path: Path) -> dict:
    if out_path.exists():
        try:
            data = json.loads(out_path.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def merge(existing: dict, edition: dict, last_races: list[dict]) -> dict:
    editions = [edition] + [e for e in existing.get("editions", []) if e.get("id") != edition["id"]]
    return {
        "version": 1,
        "generatedAt": edition["publishedAt"],
        "editions": editions[:KEEP_EDITIONS],
        "lastRaces": last_races,
    }


def server_asleep(now: dt.datetime) -> bool:
    from zoneinfo import ZoneInfo
    hour = now.astimezone(ZoneInfo(SERVER_ZONE)).hour
    return hour >= SERVER_SLEEPS_FROM or hour < SERVER_WAKES_AT


def due_reason(existing: dict, kind: str, today: dt.date, now: dt.datetime,
               season: list[Round]) -> str | None:
    """Why a scheduled run should rebuild now, or None to leave the file be."""
    edition = next((e for e in existing.get("editions", [])
                    if e.get("id") == f"{today.isoformat()}-{kind}"), None)
    published = parse_instant(edition.get("publishedAt")) if edition else None
    if not published:
        return "first build of the day"
    if kind == "preview":
        start, end = window(kind, today)
        upcoming = [r for r in season if start <= r.date <= end]
        nascar_start_times(upcoming)
        for r in upcoming:
            if r.start and published < r.start - PRE_RACE_LEAD <= now < r.start:
                return f"{r.channel_name}: {r.name} starts at {r.time_utc} UTC"
    known = {(i.get("channelID"), i.get("headline"), i.get("date")) for i in existing.get("lastRaces", [])}
    for r in latest_finished(season, now):
        if (r.channel_id, r.name, r.date.isoformat()) not in known:
            return f"{r.channel_name}: {r.name} has finished"
    return None


def parse_instant(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=["auto", "preview", "recap"], default="auto")
    parser.add_argument("--date", help="Pretend today is this date (YYYY-MM-DD)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-llm", action="store_true", help="Templated prose only")
    parser.add_argument("--when-due", action="store_true",
                        help="Rebuild only when something is due (scheduled runs)")
    parser.add_argument("--check-due", action="store_true",
                        help='Print "due" or "skip" and build nothing')
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    kind = args.kind if args.kind != "auto" else ("recap" if today.weekday() in RECAP_WEEKDAYS else "preview")

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if args.date:
        now = dt.datetime.combine(today, dt.time(12), tzinfo=dt.timezone.utc)
    out_path = Path(args.out)
    existing = read_existing(out_path)
    if args.when_due or args.check_due:
        if server_asleep(now):
            log("the writer's server is hibernating (23:00-07:00 UK); not building")
            reason = None
        else:
            reason = due_reason(existing, kind, today, now, season_rounds(load_channels()))
            log(f"due: {reason}" if reason else "nothing due; leaving digest.json as it is")
        if args.check_due:
            print("due" if reason else "skip")
            return 0
        if not reason:
            return 0

    channels = load_channels()
    circuits = load_circuits()
    season = season_rounds(channels)
    edition = build_edition(kind, today, copy.deepcopy(season), circuits, use_llm=not args.no_llm)
    last_races = build_last_races(now, copy.deepcopy(season), circuits,
                                  existing.get("lastRaces", []), use_llm=not args.no_llm)
    out_path.write_text(json.dumps(merge(existing, edition, last_races), ensure_ascii=False, indent=2) + "\n")
    log(f"wrote {out_path} ({len(edition['items'])} items, edition {edition['id']}, "
        f"{len(last_races)} last races)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
