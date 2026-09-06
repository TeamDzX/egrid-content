#!/usr/bin/env python3
"""Builds the weekly editorial digest that both E-Grid apps show on Home.

Two editions a week, written into `digest.json` at the repo root:

  preview  (Thursday)  every round of every channel racing this weekend
  recap    (Monday)    what ran over the last seven days, with podiums where a
                       series publishes them (F1, MotoGP, Formula E)

The facts come from the same sources the apps use — the static calendars in
`channels.json`, Jolpica for F1, and the Pulselive feeds for MotoGP and
Formula E. WRC's API is dead, so its rounds come from the static calendar
like the other nine channels.

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

`auto` picks recap on a Monday and preview on every other day. `--date`
pretends it is another day, for testing. Exit code is non-zero only when
nothing at all could be built; a single failed source is logged and skipped.
"""

from __future__ import annotations

import argparse
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

# An edition stops showing after this long. A preview from Thursday is stale
# by Tuesday; a Monday recap has been superseded by Thursday's preview.
PREVIEW_TTL_DAYS = 5
RECAP_TTL_DAYS = 4

USER_AGENT = "EGrid-digest/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 20

JOLPICA = "https://api.jolpi.ca/ergast/f1"
MOTOGP = "https://api.motogp.pulselive.com/motogp/v1/results"
FORMULA_E = "https://api.formula-e.pulselive.com/formula-e/v1"

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

    @property
    def sort_key(self):
        return (self.date, self.channel_name)


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
        ))
    return rounds


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
    events = []
    for finished in ("true", "false"):
        events += get_json(f"{MOTOGP}/events?seasonUuid={current['id']}&isFinished={finished}")
    rounds = []
    for index, event in enumerate(events, start=1):
        name = (event.get("name") or "").strip()
        end = event.get("date_end")
        if not name or not end or event.get("test"):
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


def formula_e_rounds(channel: dict) -> list[Round]:
    championships = get_json(f"{FORMULA_E}/championships")["championships"]
    current = next((c for c in championships if c.get("status") == "Present"), championships[-1])
    races = get_json(f"{FORMULA_E}/races?championshipId={current['id']}")["races"]
    rounds = []
    for index, race in enumerate(races, start=1):
        name, date = race.get("name"), race.get("date")
        if not name or not date:
            continue
        location = ", ".join(p for p in [race.get("city"), race.get("country")] if p)
        rounds.append(Round("formulae", channel["name"], name, location,
                            dt.date.fromisoformat(date[:10]),
                            round_number=race.get("sequence") or index, external_id=race["id"]))
    return rounds


def formula_e_podium(race_id: str) -> list[PodiumEntry]:
    sessions = get_json(f"{FORMULA_E}/races/{race_id}/sessions").get("sessions") or []
    race = next((s for s in sessions
                 if s.get("sessionDate") and (s.get("sessionName") or "").strip().lower() == "race"), None)
    if not race:
        return []
    payload = get_json(f"{FORMULA_E}/races/{race_id}/sessions/{race['id']}/results")
    rows = payload if isinstance(payload, list) else next(
        (v for v in payload.values() if isinstance(v, list)), [])
    podium = []
    for row in rows:
        position = row.get("driverPosition") or 0
        if not 1 <= position <= 3:
            continue
        name = f"{row.get('driverFirstName', '')} {row.get('driverLastName', '')}".strip() or row.get("driverTLA", "")
        team = (row.get("team") or {}).get("name", "")
        podium.append(PodiumEntry(position, name, tidy_title(team) if team.isupper() else team))
    return sorted(podium, key=lambda p: p.position)[:3]


SMALL_WORDS = {"of", "de", "the", "del", "di", "da"}

# The MotoGP feed spells out the long-form state name.
SHORT_COUNTRY = {"United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
                 "United States of America": "USA"}


def tidy_title(shouted: str) -> str:
    """"GRAND PRIX OF ITALY" -> "Grand Prix of Italy"."""
    words = shouted.lower().split()
    return " ".join(w if (i and w in SMALL_WORDS) else w.capitalize() for i, w in enumerate(words))


def gather_rounds(channels: list[dict], want_podiums: bool,
                  start: dt.date, end: dt.date) -> list[Round]:
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
    all_rounds = [r for r in all_rounds if start <= r.date <= end]
    if want_podiums:
        for r in all_rounds:
            if r.channel_id == "f1" and r.round_number:
                r.podium = safe(f"F1 podium round {r.round_number}", lambda: f1_podium(r.round_number), [])
            elif r.channel_id == "motogp" and r.external_id:
                r.podium = safe(f"MotoGP podium {r.name}", lambda: motogp_podium(r.external_id), [])
            elif r.channel_id == "formulae" and r.external_id:
                r.podium = safe(f"Formula E podium {r.name}", lambda: formula_e_podium(r.external_id), [])
    return all_rounds


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
    if kind == "recap":
        if r.podium:
            first = r.podium[0]
            winner = first.name + (f" ({first.team})" if first.team else "")
            rest = [p.name for p in r.podium[1:]]
            chase = f" from {' and '.join(rest)}" if rest else ""
            return f"{which}. Won by {winner}{chase}."
        return f"{which} is complete. This series does not publish results to E-Grid's data sources."
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
        if kind == "recap":
            entry["podium"] = [asdict(p) for p in r.podium] or "not published"
        facts.append(entry)
    return facts


SYSTEM_PROMPT = """You write the short weekly digest inside E-Grid, a motorsport companion app that follows thirteen championships. British English. Plain, warm, knowledgeable — a well-read fan writing to other fans, not a press release.

Hard rules:
- Use ONLY the facts supplied. Never add results, drivers, teams, weather, injuries, standings, penalties or storylines that are not in the facts. If a podium says "not published", do not name anyone.
- Never invent a start time, a round number or a location.
- Do not mention "this app", "E-Grid" or "the digest" in the text.
- No exclamation marks, no emoji, no headings, no bullet points.

Write:
- intro: at most 55 words setting up the week. It may count rounds and name series.
- items: for every fact index, one or two sentences of at most 40 words about that round. For a recap with a podium, state the winner and the other two podium finishers with their teams where given. For a preview, say where and when it runs, in the local-day form given, and add the UTC start only when supplied."""


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
        if kind == "recap" and not r.podium and looks_like_a_result(body):
            log(f"  dropped {writer} copy for {r.name}: reads like a result with no podium supplied")
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
        "max_tokens": 4000,
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


def write_copy(rounds: list[Round], kind: str, start: dt.date, end: dt.date) -> tuple[str, list[str]] | None:
    """Tries the writers in order — your own server first, then Claude — and
    returns None when neither produced anything, so the template stands in."""
    for name, writer in (("own server", write_with_own_server), ("Claude", write_with_claude)):
        try:
            data = writer(rounds, kind, start, end)
        except Exception as error:  # noqa: BLE001 - a writer must never sink the edition
            log(f"  {name} writer crashed: {type(error).__name__}: {error}; falling through")
            continue
        if data is not None:
            return apply_copy(data, rounds, kind, start, end, writer=name)
    log("  no writer available; using templated prose")
    return None


RESULT_WORDS = ("won", "win", "victory", "podium", "finished", "second", "third", "p1", "p2", "p3")


def looks_like_a_result(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in RESULT_WORDS)


def build_edition(kind: str, today: dt.date, channels: list[dict], use_llm: bool) -> dict:
    start, end = window(kind, today)
    log(f"{kind} edition for {start} .. {end}")
    rounds = gather_rounds(channels, want_podiums=(kind == "recap"), start=start, end=end)
    rounds.sort(key=lambda r: r.sort_key)
    log(f"  {len(rounds)} rounds in window")

    written = write_copy(rounds, kind, start, end) if (use_llm and rounds) else None
    if written:
        intro, bodies = written
    else:
        intro = template_intro(rounds, kind, start, end)
        bodies = [template_body(r, kind) for r in rounds]

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ttl = RECAP_TTL_DAYS if kind == "recap" else PREVIEW_TTL_DAYS
    items = []
    for r, body in zip(rounds, bodies):
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
        if r.podium:
            item["podium"] = [asdict(p) for p in r.podium]
        items.append(item)

    return {
        "id": f"{today.isoformat()}-{kind}",
        "kind": kind,
        "title": "Last week in motorsport" if kind == "recap" else "This weekend in motorsport",
        "dateRange": range_label(start, end),
        "intro": intro,
        "publishedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + dt.timedelta(days=ttl)).isoformat().replace("+00:00", "Z"),
        "items": items,
    }


def merge(out_path: Path, edition: dict) -> dict:
    existing = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text()).get("editions", [])
        except (json.JSONDecodeError, AttributeError):
            existing = []
    editions = [edition] + [e for e in existing if e.get("id") != edition["id"]]
    return {
        "version": 1,
        "generatedAt": edition["publishedAt"],
        "editions": editions[:KEEP_EDITIONS],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=["auto", "preview", "recap"], default="auto")
    parser.add_argument("--date", help="Pretend today is this date (YYYY-MM-DD)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-llm", action="store_true", help="Templated prose only")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    kind = args.kind if args.kind != "auto" else ("recap" if today.weekday() == 0 else "preview")

    channels = load_channels()
    edition = build_edition(kind, today, channels, use_llm=not args.no_llm)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(merge(out_path, edition), ensure_ascii=False, indent=2) + "\n")
    log(f"wrote {out_path} ({len(edition['items'])} items, edition {edition['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
