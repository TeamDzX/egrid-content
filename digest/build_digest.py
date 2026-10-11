#!/usr/bin/env python3
"""Builds the weekly editorial digest that both E-Grid apps show on Home.

Rebuilt every morning, written into `digest.json` at the repo root:

  recap    (Mon, Tue)  what ran over the last seven days, with podiums where a
                       series publishes them (F1, MotoGP, Formula E). Tuesday
                       re-runs it so a late-posted Sunday-night result lands.
  preview  (Wed-Sun)   every round of every channel racing this weekend,
                       refreshed daily as start times firm up; on Sunday it
                       is simply today's running order.
                       On Saturday and Sunday it covers Friday to Monday and
                       becomes "Results from the weekend" the moment the
                       first official result is in: each finished round
                       carries its podium and a past-tense write-up, the
                       rest stay previews, and every new result rebuilds it.

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
that names anyone from the results who is in neither the supplied podium nor
the facts, or reads like a result where none was supplied, is discarded for
the template. That is what keeps an LLM out of the results business.

For a finished F1, MotoGP or snooker round the facts include its story,
computed from the feeds: winning margin and grid slot, biggest climbers,
retirements, safety cars, weather and tyres (OpenF1), lap records, the
sprint, how the championship lead moved; for snooker the winner's route,
the other semi-final and the fate of the defending champion and world No. 1.

Usage:
    python3 digest/build_digest.py [--kind auto|preview|recap] [--date YYYY-MM-DD]
                                   [--out digest.json] [--no-llm]
                                   [--when-due | --check-due]

The scheduled action runs hourly through the day with `--when-due`, which
rebuilds only when something has changed enough to matter: the first build
of the day, the three hours before a round with a known start time, or a
race that has finished since the last build. The writer's server runs around
the clock, so the job does too: a Sunday race in America finishes after
midnight UK time, and the weekend's results should not wait for morning.
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
import re
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
# A round with a start time but no results feed counts as finished this long
# after it began, so its write-up can land the same evening.
FINISHED_AFTER = dt.timedelta(hours=4)
# On these days the preview reaches back to Friday and fills in results as
# they arrive ("Results from the weekend").
WEEKEND_DAYS = {5, 6}
# Series whose official result E-Grid can read. A round of any other series
# is only "over" once its date has passed (or FINISHED_AFTER its known
# start): nothing says it has not been red-flagged into the evening.
RESULT_FEEDS = {"f1", "motogp", "formulae", "nascar", "snooker"}
# A timed round with a results feed that still has no result this long after
# its start is written up without one.
RESULT_GIVE_UP = dt.timedelta(hours=12)

USER_AGENT = "EGrid-digest/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 20

JOLPICA = "https://api.jolpi.ca/ergast/f1"
NASCAR = "https://cf.nascar.com/cacher"
NASCAR_CUP_SERIES = 1
CIRCUITS_PATH = REPO_ROOT / "circuits.json"
FORMULA_E_PATH = REPO_ROOT / "formula-e.json"
# Snooker, built by snooker/build_snooker.py. Not in channels.json (older app
# versions read that and have no snooker code), so read on its own.
SNOOKER_PATH = REPO_ROOT / "snooker.json"
MOTOGP = "https://api.motogp.pulselive.com/motogp/v1/results"
OPENF1 = "https://api.openf1.org/v1"
# The apps list an item's facts under the prose; the writer sees them all.
FACTS_SHOWN = 6
# Bump when the facts given to the writer change enough that every
# last-race write-up deserves rewriting from them.
FACTS_VERSION = 4

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
    # Last day of a multi-day event (a snooker tournament); None for one day.
    end: dt.date | None = None
    # "motorsport" items go in an edition's `items`; any other sport goes in
    # `moreItems`, which only app versions that know that sport read.
    sport: str = "motorsport"
    # Set in a weekend preview: the round's official result is in.
    over: bool = False
    # Everyone in the results (drivers, riders, players), so a write-up that
    # names anyone outside the podium and the facts can be caught.
    field_names: list[str] = field(default_factory=list)
    # A story fact already says how the championship moved, so the plain
    # "X leads by N" line is not added as well.
    has_title_line: bool = False

    @property
    def last_day(self) -> dt.date:
        return self.end or self.date

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


_json_cache: dict[str, object] = {}


def get_cached(url: str):
    """get_json, remembered for the run: the podium and the race story read
    the same results table."""
    if url not in _json_cache:
        _json_cache[url] = get_json(url)
    return _json_cache[url]


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
    data = get_cached(f"{JOLPICA}/current/{round_number}/results.json")
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


def motogp_sessions(event_id: str) -> dict[str, dict]:
    """The premier class's sessions of an event, by type (RAC, SPR, Q ...)."""
    categories = get_cached(f"{MOTOGP}/categories?eventUuid={event_id}")
    premier = next((c for c in categories if "motogp" in (c.get("name") or "").lower()), None)
    if not premier:
        return {}
    sessions = get_cached(f"{MOTOGP}/sessions?eventUuid={event_id}&categoryUuid={premier['id']}")
    return {(s.get("type") or "").upper(): s for s in sessions}


def motogp_classification(session: dict | None) -> dict:
    """A session's classification, only once the session is finished: a race
    still running has a live one, which is not a result."""
    if not session or (session.get("status") or "").upper() != "FINISHED":
        return {}
    return get_cached(f"{MOTOGP}/session/{session['id']}/classification")


def motogp_podium(event_id: str) -> list[PodiumEntry]:
    rows = motogp_classification(motogp_sessions(event_id).get("RAC")).get("classification") or []
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
    # Until NASCAR names a winner the feed's running order is not a result.
    if not finished or not race.get("winner_driver_id"):
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
    r.field_names = [(x.get("driver_fullname") or "").strip() for x in results if x.get("driver_fullname")]
    nascar_story(r, race, weekend, results)


def nascar_story(r: Round, race: dict, weekend: dict, results: list[dict]) -> None:
    """The race's shape from NASCAR's weekend feed, most telling first: how
    the winner got there, the stages, the margin, the Chase, then the race's
    texture (lead changes, cautions, retirements, penalties)."""
    def name(x: dict) -> str:
        return (x.get("driver_fullname") or "").strip()

    laps = weekend.get("actual_laps") or race.get("actual_laps")
    winner = results[0] if results else {}
    led = max(results, key=lambda x: x.get("laps_led") or 0, default=None)
    start = winner.get("starting_position")
    if winner and start:
        line = f"{name(winner)} won from {ordinal(start)} on the grid"
        if led is winner and winner.get("laps_led") and laps:
            line += f" and led {winner['laps_led']} of {laps} laps"
        r.facts.append(line)
    if led and led.get("laps_led") and led is not winner:
        r.facts.append(f"Most laps led: {name(led)} ({led['laps_led']})")

    stages = [(s.get("stage_number"), name((s.get("results") or [{}])[0]))
              for s in sorted(weekend.get("stage_results") or [], key=lambda s: s.get("stage_number") or 0)
              if s.get("results")]
    stages = [(n, who) for n, who in stages if n and who]
    if stages:
        winners = {who for _, who in stages}
        if len(stages) > 1 and len(winners) == 1:
            r.facts.append(f"{stages[0][1]} swept stages {name_list_and([str(n) for n, _ in stages])}")
        else:
            r.facts.append("Stage winners: " + ", ".join(f"Stage {n} {who}" for n, who in stages))

    margin = (weekend.get("margin_of_victory") or "").strip()
    if margin and margin[0] in ".0123456789":
        r.facts.append(f"Margin of victory: {('0' + margin) if margin.startswith('.') else margin} s")

    safe(f"NASCAR Chase {r.name}", lambda: nascar_chase(r, race, winner), None)

    changes, leaders = weekend.get("number_of_lead_changes"), weekend.get("number_of_leaders")
    if laps and changes is not None and leaders:
        r.facts.append(f"{laps} laps, {changes} lead changes among {leaders} drivers")
    cautions = weekend.get("number_of_cautions")
    if cautions is not None:
        r.facts.append(f"{cautions} caution{'s' if cautions != 1 else ''} for {weekend.get('number_of_caution_laps') or 0} laps")

    out = [f"{name(x)} ({x['finishing_status'].lower()})" for x in results
           if (x.get("finishing_status") or "Running") != "Running" and name(x)]
    if out:
        r.facts.append(f"Did not finish: {name_list_and(out[:4])}" + (f" and {len(out) - 4} more" if len(out) > 4 else ""))

    # "dropped to the rear of the field under penalty: No. 21 (Backup car) and No. 4 (...)"
    comments = weekend.get("race_comments") or ""
    if "to the rear" in comments:
        by_car = {str(x.get("car_number")): name(x) for x in results}
        dropped = [f"{by_car[car]} ({why.strip().lower()})"
                   for car, why in re.findall(r"No\. (\d+) \(([^)]+)\)", comments.split("to the rear", 1)[1])
                   if by_car.get(car)]
        if dropped:
            r.facts.append(f"Started from the rear under penalty: {name_list_and(dropped)}")


def nascar_chase(r: Round, race: dict, winner: dict) -> None:
    """The Chase (NASCAR's 2026 title format: the top 16 race the last ten
    rounds on points, no eliminations, no playoff points): how its lead moved
    and how many races are left. Standings after this race come from the
    race's own points feed; before = after minus this race's points."""
    if not race.get("playoff_round"):
        return
    rows = get_json(f"https://cf.nascar.com/live/feeds/series_1/{race['race_id']}/live_points.json")
    chase = [x for x in rows or [] if x.get("is_in_chase")]
    if len(chase) < 2:
        return

    def full(x: dict) -> str:
        return re.sub(r"\s*\([A-Z]\)$", "", f"{x.get('first_name', '')} {x.get('last_name', '')}".strip())

    # The feed is keyed by race; make sure it really is this race's table.
    if winner and not any(x.get("driver_id") == winner.get("driver_id")
                          and x.get("points_earned_this_race") == winner.get("points_earned") for x in rows):
        log(f"  NASCAR Chase {r.name}: points feed does not match the result, skipped")
        return
    after = sorted(((full(x), float(x.get("points") or 0)) for x in chase), key=lambda t: -t[1])
    before = sorted(((full(x), float((x.get("points") or 0) - (x.get("points_earned_this_race") or 0)))
                     for x in chase), key=lambda t: -t[1])
    races = [x for x in nascar_feed() if x.get("playoff_round") and x.get("race_type_id") == 1]
    left = sum(1 for x in races if (x.get("race_date") or "") > (race.get("race_date") or ""))
    line = championship_move(before[:2], after[:2], "this weekend", title="Chase")
    (leader, a), (second, b) = after[0], after[1]
    to_go = f"{left} race{'s' if left != 1 else ''} to go" if left else "the title decided"
    if line:
        r.facts.append(f"{line}, with {to_go}")
    else:
        r.facts.append(f"{leader} leads the Chase by {a - b:g} points from {second}, with {to_go}")
    r.has_title_line = True
    who = (winner.get("driver_fullname") or "").strip()
    was = next((i for i, (n, _) in enumerate(before, 1) if n == who), None)
    now_at = next((i for i, (n, _) in enumerate(after, 1) if n == who), None)
    if was and now_at and now_at < was:
        r.facts.append(f"The win lifted {who} from {ordinal(was)} to {ordinal(now_at)} in the Chase")


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
        if with_standings and not r.has_title_line:
            line = title_fight(r.channel_id)
            if line:
                r.facts.append(line)
        # Circuit guides are motorsport's; a snooker venue's city would match
        # a racetrack.
        circuit = circuit_for(r, circuits) if r.sport == "motorsport" else None
        line = circuit_fact(circuit) if circuit else None
        if line:
            r.facts.append(line)


_podium_cache: dict[tuple, list[PodiumEntry]] = {}


def fetch_podium(r: Round) -> list[PodiumEntry]:
    """The official podium, asked of each feed at most once per run (the
    due check and the build both want it)."""
    key = (r.channel_id, r.round_number, r.external_id, r.name)
    if r.channel_id in {"f1", "motogp", "formulae"}:
        if key not in _podium_cache:
            _podium_cache[key] = _fetch_podium(r)
        return list(_podium_cache[key])
    return _fetch_podium(r)


def _fetch_podium(r: Round) -> list[PodiumEntry]:
    if r.channel_id == "f1" and r.round_number:
        return safe(f"F1 podium round {r.round_number}", lambda: f1_podium(r.round_number), [])
    if r.channel_id == "motogp" and r.external_id:
        return safe(f"MotoGP podium {r.name}", lambda: motogp_podium(r.external_id), [])
    if r.channel_id == "formulae" and r.external_id:
        return safe(f"Formula E podium {r.name}", lambda: formula_e_podium(r.external_id), [])
    return r.podium


# --------------------------------------------------------------------------- #
# Race stories: how a finished round unfolded, worked out from the feeds
# --------------------------------------------------------------------------- #
#
# The writer is given these as facts and told to tell the race from two or
# three of them. Every line is computed here from published results; a
# model never decides who climbed, retired or took the lead.


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def name_list_and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def gap_text(gap: str) -> str:
    """"+7.335" -> "7.3 seconds"; "+1:21.666" -> "1 minute 21.7 seconds"."""
    gap = gap.lstrip("+").strip()
    try:
        if ":" in gap:
            minutes, seconds = gap.split(":")
            return f"{int(minutes)} minute{'s' if int(minutes) != 1 else ''} {float(seconds):.1f} seconds"
        return f"{float(gap):.1f} seconds"
    except ValueError:
        return gap


def lap_time(text: str) -> str:
    """"01:29.960" -> "1:29.960"."""
    return text.lstrip("0") if text.startswith("0") and ":" in text else text


def championship_move(before: list[tuple[str, float]], after: list[tuple[str, float]],
                      when: str, title: str = "championship") -> str | None:
    """How the top of the table moved: a new leader, or the lead growing or
    shrinking. `before` and `after` are (name, points), leader first."""
    if len(before) < 2 or len(after) < 2:
        return None
    (old_leader, a0), (_, b0) = before[0], before[1]
    (leader, a1), (second, b1) = after[0], after[1]
    if leader != old_leader:
        return f"{leader} took the {title} lead from {old_leader}, and now leads by {a1 - b1:g} points"
    old_gap, gap = a0 - b0, a1 - b1
    if old_gap == gap:
        return None
    return (f"{leader}'s {title} lead over {second} went from {old_gap:g} to {gap:g} points "
            f"{when}")


def jolpica_standings(round_number: int) -> list[tuple[str, float]]:
    data = get_cached(f"{JOLPICA}/current/{round_number}/driverStandings.json")
    lists = data["MRData"]["StandingsTable"].get("StandingsLists", [])
    rows = lists[0].get("DriverStandings", []) if lists else []
    return [(f"{r['Driver'].get('givenName', '')} {r['Driver'].get('familyName', '')}".strip(),
             float(r.get("points") or 0)) for r in rows[:2]]


def f1_story(r: Round) -> None:
    data = get_cached(f"{JOLPICA}/current/{r.round_number}/results.json")
    races = data["MRData"]["RaceTable"].get("Races", [])
    rows = races[0].get("Results", []) if races else []
    if len(rows) < 2:
        return

    def full(row):
        d = row.get("Driver", {})
        return f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()

    r.field_names = [full(row) for row in rows]
    winner, second = rows[0], rows[1]
    grid = int(winner.get("grid") or 0)
    start = "from pole" if grid == 1 else "from the pit lane" if grid == 0 else f"from {ordinal(grid)} on the grid"
    margin = (second.get("Time") or {}).get("time")
    r.facts.append(f"{full(winner)} won {start}"
                   + (f", {gap_text(margin)} ahead of {full(second)}" if margin else ""))

    climbers = []
    for row in rows:
        g, pos = int(row.get("grid") or 0), int(row.get("position") or 0)
        if g > 0 and pos and g - pos >= 5 and row.get("positionText", "").isdigit():
            climbers.append((g - pos, f"{full(row)} from {ordinal(g)} to {ordinal(pos)}"))
    if climbers:
        best = [text for _, text in sorted(climbers, key=lambda c: -c[0])[:2]]
        r.facts.append(f"Biggest climber{'s' if len(best) > 1 else ''}: {name_list_and(best)}")

    fastest = next((row for row in rows if (row.get("FastestLap") or {}).get("rank") == "1"), None)
    if fastest:
        time_text = ((fastest["FastestLap"].get("Time") or {}).get("time"))
        r.facts.append(f"Fastest lap: {full(fastest)}" + (f", {time_text}" if time_text else ""))

    out = []
    for row in rows:
        status = row.get("status") or ""
        if status in ("Finished", "Lapped") or status.startswith("+"):
            continue
        out.append(full(row) + ("" if status in ("Retired", "") else f" ({status.lower()})"))
    if out:
        r.facts.append(f"Did not finish: {name_list_and(out)}")

    if r.round_number and r.round_number > 1:
        line = championship_move(jolpica_standings(r.round_number - 1), jolpica_standings(r.round_number),
                                 "over the weekend")
        if line:
            r.facts.append(line)
            r.has_title_line = True

    safe(f"OpenF1 {r.name}", lambda: openf1_story(r, winner), None)


def openf1_story(r: Round, winner: dict) -> None:
    """Safety cars, red flags, rain and the winner's tyres. OpenF1 refuses
    every request while any F1 session is live; a refusal just means fewer
    facts this run."""
    day = r.date.isoformat()
    nxt = (r.date + dt.timedelta(days=1)).isoformat()
    sessions = get_cached(f"{OPENF1}/sessions?session_name=Race&date_start>={day}&date_start<{nxt}")
    if not sessions:
        return
    key = sessions[0]["session_key"]
    neutralised: dict[str, list[int]] = {}
    for message in get_cached(f"{OPENF1}/race_control?session_key={key}"):
        text = (message.get("message") or "").upper()
        lap = message.get("lap_number")
        # By word: "CHEQUERED FLAG" contains "RED FLAG".
        label = ("Red flag" if message.get("flag") == "RED" or re.search(r"\bRED FLAG\b", text) else
                 "Virtual safety car" if text.startswith("VSC DEPLOYED") else
                 "Safety car" if text.startswith("SAFETY CAR DEPLOYED") else None)
        if label and lap and lap not in neutralised.setdefault(label, []):
            neutralised[label].append(lap)
    for label, laps in neutralised.items():
        r.facts.append(f"{label} on lap{'s' if len(laps) > 1 else ''} {name_list_and([str(n) for n in laps])}")

    weather = get_cached(f"{OPENF1}/weather?session_key={key}")
    if weather:
        if any(w.get("rainfall") for w in weather):
            r.facts.append("Rain fell during the race")
        else:
            mid = weather[len(weather) // 2]
            r.facts.append(f"Dry, {mid['air_temperature']:.0f}°C air and {mid['track_temperature']:.0f}°C track")

    number = winner.get("number")
    stints = [s for s in get_cached(f"{OPENF1}/stints?session_key={key}")
              if str(s.get("driver_number")) == str(number)]
    tyres = [s.get("compound", "").lower() for s in sorted(stints, key=lambda s: s.get("stint_number") or 0)
             if s.get("compound")]
    if tyres:
        stops = len(tyres) - 1
        family = (winner.get("Driver") or {}).get("familyName", "The winner")
        r.facts.append(f"{family}'s tyres: {' then '.join(tyres)}"
                       + (f", {stops} stop{'s' if stops != 1 else ''}" if stops else ", no stop"))


def motogp_story(r: Round, today: dt.date) -> None:
    sessions = motogp_sessions(r.external_id)
    race = motogp_classification(sessions.get("RAC"))
    rows = race.get("classification") or []
    if len(rows) < 2:
        return

    def full(row):
        return (row.get("rider") or {}).get("full_name", "")

    r.field_names = [full(row) for row in rows]
    finishers = sorted((row for row in rows if row.get("position")), key=lambda row: row["position"])
    if len(finishers) >= 2:
        gap = (finishers[1].get("gap") or {}).get("first")
        r.facts.append(f"{full(finishers[0])} won by {gap_text(gap)} from {full(finishers[1])}" if gap
                       else f"{full(finishers[0])} won from {full(finishers[1])}")

    out = [full(row) for row in rows if not row.get("position")]
    if out:
        r.facts.append(f"Did not finish: {name_list_and(out)}")

    for record in race.get("records") or []:
        if record.get("type") == "fastestLap":
            time_text = lap_time((record.get("bestLap") or {}).get("time") or "")
            who = (record.get("rider") or {}).get("full_name", "")
            if record.get("isNewRecord"):
                r.facts.append(f"{who} set a new lap record, {time_text}")
            elif who:
                r.facts.append(f"Fastest lap: {who}, {time_text}")

    condition = (sessions.get("RAC") or {}).get("condition") or {}
    track = (condition.get("track") or "").lower()
    air, ground = (condition.get("air") or "").strip("º"), (condition.get("ground") or "").strip("º")
    if track and track != "dry":
        r.facts.append(f"{track.capitalize()} track")
    elif track and air and ground:
        r.facts.append(f"Dry, {air}°C air and {ground}°C track")

    sprint_rows = motogp_classification(sessions.get("SPR")).get("classification") or []
    sprint_winner = next((row for row in sprint_rows if row.get("position") == 1), None)
    if sprint_winner:
        r.facts.append(f"Saturday's sprint was won by {full(sprint_winner)}")

    # The standings feed is today's, so the before-and-after only holds
    # while this is the last round scored: today's table minus this
    # weekend's points. A back-to-back weekend's sprint can come six days
    # later, so only up to the Thursday after.
    if 0 <= (today - r.date).days <= 4:
        after = motogp_table()
        scored: dict[str, float] = {}
        for row in rows + sprint_rows:
            scored[full(row)] = scored.get(full(row), 0) + float(row.get("points") or 0)
        before = sorted(((name, pts - scored.get(name, 0)) for name, pts in after), key=lambda x: -x[1])
        line = championship_move(before[:2], after[:2], "this weekend")
        if line:
            r.facts.append(line)
            r.has_title_line = True


def motogp_table() -> list[tuple[str, float]]:
    seasons = get_cached(f"{MOTOGP}/seasons")
    current = next((s for s in seasons if s.get("current")), None) or max(seasons, key=lambda s: s.get("year", 0))
    categories = get_cached(f"{MOTOGP}/categories?seasonUuid={current['id']}")
    premier = next((c for c in categories if "motogp" in (c.get("name") or "").lower()), None)
    if not premier:
        return []
    rows = get_cached(f"{MOTOGP}/standings?seasonUuid={current['id']}&categoryUuid={premier['id']}").get("classification") or []
    return [((row.get("rider") or {}).get("full_name", ""), float(row.get("points") or 0)) for row in rows]


def snooker_round_name(name: str) -> str:
    """snooker.org's "Quarterfinals" -> "quarter-finals", "Round 2" -> "round 2"."""
    return {"quarterfinals": "quarter-finals", "semifinals": "semi-finals",
            "final": "final"}.get(name.lower(), name.lower())


def snooker_story(r: Round, e: dict, rankings: list[dict]) -> None:
    """A finished tournament: the winner's route, the other semi-final, what
    became of the defending champion and the world No. 1, and how many
    matches went the distance."""
    matches = [m for m in e.get("matches") or [] if m.get("status") == "finished" and m.get("winner") in (1, 2)]
    if not matches:
        return
    names = {x["round"]: snooker_round_name(x.get("name", "")) for x in e.get("rounds") or []}

    def won_by(m):
        return m["player1"] if m["winner"] == 1 else m["player2"]

    def lost_by(m):
        return m["player2"] if m["winner"] == 1 else m["player1"]

    def score(m):
        a, b = m.get("score1"), m.get("score2")
        return f"{max(a, b)}-{min(a, b)}" if isinstance(a, int) and isinstance(b, int) else ""

    r.field_names = sorted({m.get("player1") for m in matches} | {m.get("player2") for m in matches} - {None})
    winner = e.get("winner")
    route = sorted((m for m in matches if won_by(m) == winner and m.get("round") != 15), key=lambda m: m["round"])
    if route:
        steps = [f"{lost_by(m)} {score(m)}".strip() for m in route]
        r.facts.append(f"{winner}'s route to the final: beat {name_list_and(steps)}")
    other_semi = next((m for m in matches if m.get("round") == 14 and won_by(m) != winner), None)
    if other_semi:
        r.facts.append(f"Other semi-final: {won_by(other_semi)} beat {lost_by(other_semi)} {score(other_semi)}")

    def fate(player: str, label: str) -> str | None:
        if not player:
            return None
        if player == winner:
            return f"{label} {player} won the title"
        beaten = next((m for m in matches if lost_by(m) == player), None)
        if beaten and beaten.get("round") != 15:
            where = names.get(beaten["round"])
            return f"{label} {player} lost to {won_by(beaten)} {score(beaten)}" + (f" in the {where}" if where else "")
        return None

    champion = e.get("defendingChampion")
    line = fate(champion, "Defending champion")
    if line:
        r.facts.append(line.replace("won the title", "retained the title"))
    top = (rankings or [{}])[0].get("name")
    if top and top != champion:
        line = fate(top, "World No. 1")
        if line:
            r.facts.append(line)

    deciders = 0
    for m in matches:
        a, b, best_of = m.get("score1"), m.get("score2"), m.get("bestOf")
        if isinstance(a, int) and isinstance(b, int) and best_of and max(a, b) == best_of // 2 + 1 and abs(a - b) == 1:
            deciders += 1
    if deciders:
        r.facts.append(f"{deciders} of {len(matches)} matches went to a deciding frame")


def add_story(r: Round, today: dt.date) -> None:
    """Story facts for a finished round with a results feed."""
    if r.channel_id == "f1" and r.round_number:
        safe(f"F1 story {r.name}", lambda: f1_story(r), None)
    elif r.channel_id == "motogp" and r.external_id:
        safe(f"MotoGP story {r.name}", lambda: motogp_story(r, today), None)


def snooker_rounds() -> list[Round]:
    """The season's main snooker tournaments from snooker.json, each spanning
    its days. A finished one carries winner and runner-up as its podium."""
    try:
        data = json.loads(SNOOKER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("snooker.json unreadable; no snooker rounds")
        return []
    rounds = []
    for e in data.get("events", []):
        try:
            start = dt.date.fromisoformat(e["start"])
            end = dt.date.fromisoformat(e.get("end") or e["start"])
        except (KeyError, ValueError):
            continue
        location = ", ".join(p for p in (e.get("venue"), e.get("city")) if p)
        r = Round("snooker", "Snooker", e.get("name", ""), location, start,
                  external_id=str(e.get("id")), end=end, sport="snooker")
        final = next((x for x in e.get("rounds", []) if x.get("round") == 15 and x.get("bestOf")), None)
        if e.get("defendingChampion"):
            r.session_facts.append(f"Defending champion: {e['defendingChampion']}")
        if final:
            r.session_facts.append(f"The final is best of {final['bestOf']} frames")
        if e.get("winner"):
            r.podium = [PodiumEntry(1, e["winner"])]
            if e.get("runnerUp"):
                r.podium.append(PodiumEntry(2, e["runnerUp"]))
            if e.get("finalScore"):
                r.facts.append(f"Final score {e['finalScore']}")
            safe(f"snooker story {r.name}",
                 lambda: snooker_story(r, e, (data.get("rankings") or {}).get("rows") or []), None)
        rounds.append(r)
    return rounds


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
    # snooker.org's data may not appear in a public file (Hermund Årdalen,
    # 11 Oct 2026), and digest.json is public. Snooker joins only a digest
    # built for the private endpoint, which sets EGRID_DIGEST_SNOOKER=1.
    if os.environ.get("EGRID_DIGEST_SNOOKER") != "1":
        return all_rounds
    snooker = snooker_rounds()
    log(f"  Snooker: {len(snooker)} events")
    return all_rounds + snooker


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
                done = finished and round_over(r, now)
                safe(f"NASCAR {r.name}", lambda: enrich_nascar(r, race, done), None)
    if finished:
        for r in rounds:
            r.podium = fetch_podium(r)
            if r.podium:
                add_story(r, now.date())


# --------------------------------------------------------------------------- #
# Editions
# --------------------------------------------------------------------------- #


def window(kind: str, today: dt.date) -> tuple[dt.date, dt.date]:
    if kind == "recap":
        return today - dt.timedelta(days=7), today - dt.timedelta(days=1)
    # Preview: from today to the coming Sunday, plus Monday for the handful of
    # rounds (Le Mans, Bathurst-style events) that spill over. At the
    # weekend it reaches back to Friday, so Saturday's and Sunday's editions
    # keep the races already run, with their results.
    days_to_sunday = (6 - today.weekday()) % 7
    first = today - dt.timedelta(days=today.weekday() - 4) if today.weekday() in WEEKEND_DAYS else today
    return first, today + dt.timedelta(days=days_to_sunday + 1)


def day_label(date: dt.date) -> str:
    return f"{date:%a} {date.day} {date:%b}"


def span_label(r: Round) -> str:
    """"Sun 11 Oct" for a one-day round, "18–25 Oct" for a tournament."""
    if not r.end or r.end == r.date:
        return day_label(r.date)
    if r.date.month == r.end.month:
        return f"{r.date.day}–{r.end.day} {r.end:%b}"
    return f"{r.date.day} {r.date:%b} – {r.end.day} {r.end:%b}"


def range_label(start: dt.date, end: dt.date) -> str:
    if start == end:
        return f"{end.day} {end:%B}"
    if start.month == end.month:
        return f"{start.day}–{end.day} {end:%B}"
    return f"{start.day} {start:%B} – {end.day} {end:%B}"


def template_body(r: Round, kind: str) -> str:
    """Fallback copy. The headline and detail line already carry the event,
    venue and day, so this adds what they don't: the round, the start time,
    the podium."""
    if kind == "results":
        kind = "recap" if r.over else "preview"
    if r.sport != "motorsport":
        if kind != "preview":
            if r.podium:
                runner = f", beating {r.podium[1].name}" if len(r.podium) > 1 else ""
                score = next((f[len("Final score "):] for f in r.facts if f.startswith("Final score ")), None)
                return f"{r.podium[0].name} won the {r.name}{runner}" + (f" {score}" if score else "") + "."
            return f"The {r.name} ran at {r.location or r.name}, {span_label(r)}."
        return f"The {r.name} runs {span_label(r)}" + (f" at {r.location}" if r.location else "") + "."
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


def name_list(names: list[str]) -> str:
    return ", ".join(names[:-1]) + (f" and {names[-1]}" if len(names) > 1 else names[0])


def template_intro(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> str:
    if kind == "results" and rounds:
        # Only a podium is a result; a round that has run without one (no
        # results feed) is neither "in" nor "to come".
        scored = list(dict.fromkeys(r.channel_name for r in rounds if r.over and r.podium))
        ran = list(dict.fromkeys(r.channel_name for r in rounds if r.over and not r.podium))
        to_come = list(dict.fromkeys(r.channel_name for r in rounds if not r.over))
        parts = [f"Results are in from {name_list(scored)}." if scored else "",
                 f"{name_list(ran)} {'has' if len(ran) == 1 else 'have'} run." if ran else "",
                 f"Still to come: {name_list(to_come)}." if to_come else ""]
        return " ".join(part for part in parts if part)
    series = sorted({r.channel_name for r in rounds})
    if not rounds:
        return ("Nothing finished in the last seven days across E-Grid's channels."
                if kind == "recap" else
                "A quiet week: nothing on across E-Grid's channels.")
    listed = ", ".join(series[:-1]) + (f" and {series[-1]}" if len(series) > 1 else series[0])
    noun = "round" if all(r.sport == "motorsport" for r in rounds) else "event"
    plural = "s" if len(rounds) != 1 else ""
    span = f"on {range_label(start, end)}" if start == end else f"between {range_label(start, end)}"
    if kind == "recap":
        return f"{len(rounds)} {noun}{plural} ran {span}, across {listed}."
    return f"{len(rounds)} {noun}{plural} across {listed} {span}."


def facts_for_llm(rounds: list[Round], kind: str) -> list[dict]:
    facts = []
    edition = kind
    for index, r in enumerate(rounds):
        kind = ("recap" if r.over else "preview") if edition == "results" else edition
        entry = {
            "index": index,
            "series": r.channel_name,
            "event": r.name,
            "location": r.location or None,
            "date": r.date.isoformat(),
            "day": span_label(r),
        }
        if r.sport != "motorsport":
            entry["sport"] = r.sport
            if r.end and r.end != r.date:
                entry["lastDay"] = r.end.isoformat()
        if r.round_number:
            entry["round"] = r.round_number
        if r.time_utc and kind == "preview":
            entry["startUTC"] = r.time_utc
        if kind != "preview":
            entry["podium"] = [asdict(p) for p in r.podium] or "not published"
        if edition == "results":
            entry["status"] = "finished" if r.over else "upcoming"
        if r.facts:
            entry["facts"] = r.facts
        facts.append(entry)
    return facts


SYSTEM_PROMPT = """You write the short weekly digest inside E-Grid, a sports companion app that follows motorsport championships and professional snooker. British English. Plain, warm, knowledgeable — a well-read fan writing to other fans, not a press release.

Hard rules:
- Use ONLY the facts supplied. Never add results, drivers, teams, weather, injuries, standings, penalties or storylines that are not in the facts. If a podium says "not published", do not name anyone, and do not mention results or their absence at all — write about the event itself.
- Never invent a start time, a round number or a location.
- Do not mention "this app", "E-Grid" or "the digest" in the text.
- No exclamation marks, no emoji, no headings, no bullet points.

Write:
- intro: at most 55 words setting up the week. It may count rounds and name series.
- items: for every fact index, two or three sentences of at most 60 words about that round. For a recap with a podium, state the winner and the other two podium finishers with their teams where given. For a preview, say where and when it runs, in the local-day form given, and add the UTC start only when supplied.
- Where a round carries "facts", use one or two of them to give the reader something more than the date: what is at stake in the championship, a session to watch, the character of the venue. Restate them faithfully; do not draw conclusions they do not support.
- A finished round may carry several facts about how it unfolded: the winning margin, where the winner started, the biggest climbers, who did not finish, safety cars, the weather, tyres, a lap record, how the championship lead moved (for snooker: the winner's route, the other semi-final, what became of the defending champion). For such a round write three or four sentences, at most 90 words: lead with the result, then tell the race through the two or three most telling facts. Never list them all, and never name anyone who is not in the podium or the facts.
- An edition called "lastRaces" covers each series' most recent round, whenever it ran: write about it in the past tense.
- An edition called "results" is this weekend, part run: a round with "status": "finished" is a recap (past tense, the result from its podium); a round with "status": "upcoming" is still a preview and must not read like a result. The intro leads with what has been decided so far and says what is still to come.
- A round with "sport": "snooker" is a tournament played over several days (from "date" to "lastDay"), not a race: no laps, grids, drivers or teams. Its podium is the winner (position 1) and the runner-up (position 2); in a recap say who won the final and whom they beat, with the final score when a fact gives it."""


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
    edition = kind
    for index, r in enumerate(rounds):
        kind = ("recap" if r.over else "preview") if edition == "results" else edition
        body = by_index.get(index)
        if not body:
            continue
        if kind != "preview" and not r.podium and looks_like_a_result(body):
            log(f"  dropped {writer} copy for {r.name}: reads like a result with no podium supplied")
            continue
        if kind == "preview" and looks_like_a_result(body, PREVIEW_RESULT_WORDS):
            log(f"  dropped {writer} copy for {r.name}: a preview that reads like a result")
            continue
        stranger = unsupported_name(body, r)
        if stranger:
            log(f"  dropped {writer} copy for {r.name}: names {stranger}, who is in neither the podium nor the facts")
            continue
        claim = unsupported_claim(body, r, kind)
        if claim:
            log(f"  dropped {writer} copy for {r.name}: {claim} is not in its facts")
            continue
        bodies[index] = body
    intro = str(data.get("intro") or "").strip() or template_intro(rounds, edition, start, end)
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


def unsupported_name(text: str, r: Round) -> str | None:
    """Someone from the round's results whom the copy names although the
    podium and facts it was given do not: the writer reaching past its
    facts. Motorsport names are matched by surname too ("Russell"); a
    surname shared with someone supported (the Marquez brothers) is let
    through. Snooker names, family name first, only in full."""
    supported = [p.name for p in r.podium] + [n for n in r.field_names if any(n in f for f in r.facts)]
    surnames = {n.split()[-1] for n in supported if n}
    for name in r.field_names:
        if not name or name in supported:
            continue
        patterns = [name]
        if r.sport == "motorsport" and name.split()[-1] not in surnames and len(name.split()[-1]) > 2:
            patterns.append(name.split()[-1])
        if any(re.search(rf"\b{re.escape(pattern)}\b", text) for pattern in patterns):
            return name
    return None


# Things that happen in a race which copy may only mention when a fact does.
INCIDENT_WORDS = ("red flag", "safety car", "rain", "wet", "crash", "collision", "penalt",
                  "lap record", "puncture", "disqualif")


def unsupported_claim(text: str, r: Round, kind: str) -> str | None:
    """A number or an incident the copy has from nowhere: a self-hosted
    model given several rounds at once will lend one round's "62°C track"
    to another as "a red flag on lap 62"."""
    source = " ".join([r.name, r.location, r.channel_name, span_label(r), day_label(r.date),
                       r.date.isoformat(), str(r.round_number or ""), r.time_utc or "",
                       *r.facts, *(f"{p.position} {p.name} {p.team}" for p in r.podium)])
    if r.end:
        source += f" {r.end.isoformat()} {day_label(r.end)}"
    known = set(re.findall(r"\d+(?:\.\d+)?", source))
    for number in re.findall(r"\d+(?:\.\d+)?", text):
        if number not in known and number.lstrip("0") not in known:
            return f'the number "{number}"'
    lowered, facts = text.lower(), " ".join(r.facts).lower()
    for word in INCIDENT_WORDS:
        if re.search(rf"\b{word}", lowered) and not re.search(rf"\b{word}", facts):
            return f'"{word}"'
    return None


def looks_like_a_result(text: str, words=RESULT_WORDS) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def item_json(r: Round, body: str) -> dict:
    detail = " · ".join(p for p in [r.location, span_label(r)] if p)
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
        item["facts"] = r.facts[:FACTS_SHOWN]
    if r.podium:
        item["podium"] = [asdict(p) for p in r.podium]
    return item


def build_edition(kind: str, today: dt.date, now: dt.datetime, season: list[Round],
                  circuits: list[dict], use_llm: bool) -> dict:
    """`kind` is preview or recap. A weekend preview with any official
    result in becomes "Results from the weekend": still kind preview, so
    every app version labels it THIS WEEKEND, with finished rounds written up
    as results."""
    start, end = window(kind, today)
    log(f"{kind} edition for {start} .. {end}")
    # A preview takes anything on during the window, so a tournament already
    # under way stays in; a recap takes what finished in it.
    if kind == "preview":
        rounds = sorted((r for r in season if r.date <= end and r.last_day >= start), key=lambda r: r.sort_key)
    else:
        rounds = sorted((r for r in season if start <= r.last_day <= end), key=lambda r: r.sort_key)
    if kind == "preview" and today.weekday() in WEEKEND_DAYS:
        for r in rounds:
            r.over = round_over(r, now)
    results = any(r.over for r in rounds)
    done = [r for r in rounds if r.over]
    to_come = [r for r in rounds if not r.over]
    if kind == "recap":
        finish_rounds(rounds, finished=True, now=now)
        add_context(rounds, circuits, with_standings=False)
    else:
        finish_rounds(done, finished=True, now=now)
        finish_rounds(to_come, finished=False, now=now)
        # Standings are today's, so they belong with this weekend (to come,
        # or just run), not last week.
        add_context(done, circuits, with_standings=True)
        add_context(to_come, circuits, with_standings=True, upcoming=True)
    log(f"  {len(rounds)} rounds in window" + (f", {len(done)} with results" if results else ""))

    copy_kind = "results" if results else kind
    written = write_copy(rounds, copy_kind, start, end) if (use_llm and rounds) else None
    if written:
        intro, bodies = written
    else:
        intro = template_intro(rounds, copy_kind, start, end)
        bodies = [template_body(r, copy_kind) for r in rounds]
    now = now.replace(microsecond=0)

    ttl = RECAP_TTL_DAYS if kind == "recap" else PREVIEW_TTL_DAYS
    pairs = list(zip(rounds, bodies))
    motorsport = [(r, b) for r, b in pairs if r.sport == "motorsport"]
    others = [(r, b) for r, b in pairs if r.sport != "motorsport"]
    edition = {
        "id": f"{today.isoformat()}-{kind}",
        "kind": kind,
        "title": ("Results from the weekend" if results else
                  "Last week on E-Grid" if kind == "recap" else "This weekend on E-Grid"),
        "dateRange": range_label(start, end),
        # Older apps read only `items` and `intro`, so `intro` must not talk
        # about events they cannot show; `introAll` covers everything.
        "intro": intro if not others else template_intro([r for r, _ in motorsport], copy_kind, start, end),
        "publishedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + dt.timedelta(days=ttl)).isoformat().replace("+00:00", "Z"),
        "items": [item_json(r, body) for r, body in motorsport],
    }
    if others:
        edition["introAll"] = intro
        edition["moreItems"] = [item_json(r, body) for r, body in others]
    return edition


LAST_RACE_BATCH = 4


def lastrace_key(item: dict) -> tuple:
    """What a write-up is about. When it is unchanged the old prose stands,
    so the writer is asked only about rounds that are new or newly scored."""
    return (item.get("channelID"), item.get("headline"), item.get("date"),
            tuple(p.get("name") for p in item.get("podium") or []))


def latest_finished(season: list[Round], now: dt.datetime) -> list[Round]:
    """Each series' most recent finished round. Today's rounds count once
    their official result is in (see round_over)."""
    nascar_start_times([r for r in season if r.date == now.date()])
    latest: dict[str, Round] = {}
    for r in season:
        # Last-race write-ups feed motorsport race pages only.
        if r.sport != "motorsport":
            continue
        if (r.channel_id not in latest or r.date > latest[r.channel_id].date) and round_over(r, now):
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
    for item in items:
        item["factsVersion"] = FACTS_VERSION
    # Re-written when new, when only the template had it, or when it was
    # written from an older set of facts.
    stale = [index for index, item in enumerate(items)
             if (old.get(lastrace_key(item)) or {}).get("writer") in (None, "template")
             or (old.get(lastrace_key(item)) or {}).get("factsVersion") != FACTS_VERSION]
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


def official_result(r: Round) -> bool:
    if r.channel_id == "nascar":
        race = nascar_feed_race(r, nascar_feed())
        return bool(race and race.get("winner_driver_id"))
    return bool(fetch_podium(r))


def round_over(r: Round, now: dt.datetime) -> bool:
    """Whether a round can be written up as a result. For a series with a
    results feed, as soon as the official result is published (F1 about an
    hour after the flag, MotoGP when its race session is marked finished,
    NASCAR when it names the winner). Without one, FINISHED_AFTER its known
    start, or once its date has passed."""
    today = now.date()
    if r.start is None:
        if r.last_day < today:
            return True
        return r.last_day == today and r.channel_id in RESULT_FEEDS and official_result(r)
    if now < r.start:
        return False
    if r.channel_id not in RESULT_FEEDS:
        return now >= r.start + FINISHED_AFTER
    return now >= r.start + RESULT_GIVE_UP or official_result(r)


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
    season = season_rounds(load_channels())
    if args.when_due or args.check_due:
        reason = due_reason(existing, kind, today, now, season)
        log(f"due: {reason}" if reason else "nothing due; leaving digest.json as it is")
        if args.check_due:
            print("due" if reason else "skip")
            return 0
        if not reason:
            return 0

    circuits = load_circuits()
    edition = build_edition(kind, today, now, copy.deepcopy(season), circuits, use_llm=not args.no_llm)
    last_races = build_last_races(now, copy.deepcopy(season), circuits,
                                  existing.get("lastRaces", []), use_llm=not args.no_llm)
    out_path.write_text(json.dumps(merge(existing, edition, last_races), ensure_ascii=False, indent=2) + "\n")
    log(f"wrote {out_path} ({len(edition['items'])} items + {len(edition.get('moreItems', []))} more, edition {edition['id']}, "
        f"{len(last_races)} last races)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
