#!/usr/bin/env python3
"""Builds `snooker.json`, the one file both E-Grid apps read for snooker.

All data comes from api.snooker.org, by permission of its webmaster (Hermund
Årdalen, October 2026). Two conditions shape this script:

  * Every request carries our approved X-Requested-By value, which lives in
    the EGRID_SNOOKER_KEY secret and never in the apps or this repo.
  * The rate limit is two requests a minute for the whole key. That is why
    the apps never call snooker.org themselves — a few hundred phones would
    exhaust it in seconds — and why this script waits 31 seconds between
    calls and fetches as little as it can.

What it fetches, and how often:

  daily      events of the season (t=5), round names and frame distances
             (t=12), the world rankings (rt=MoneyRankings) and the
             professional players (t=10). Four calls, once every ~20 hours.
  per run    the matches of each event that is under way, starts within
             three days or finished yesterday (t=6). Usually none or one.
  once       the matches of every finished event of the season, fetched the
             first time the event is seen finished and kept from the previous
             file after that.
  as needed  a player missing from the pro list (amateur wildcards, top-ups)
             is looked up on its own (p=), a few per run, and remembered.

So a quiet day costs four calls and a tournament day a few dozen. Nothing
here is prose and nothing is invented: names, scores and dates are copied.

If a call fails the script keeps what the previous run held rather than
publishing less. It exits non-zero only when the output would have no events.

Usage:
    EGRID_SNOOKER_KEY=... python3 snooker/build_snooker.py [--out snooker.json] [--state snooker/state.json] [--refetch]

`--refetch` forgets the stored matches and the daily timestamp.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "snooker.json"
# What the next run needs and the apps do not: raw matches, the round table,
# every player named so far. Committed beside the script, never fetched by
# an app, so the published file stays small.
DEFAULT_STATE = REPO_ROOT / "snooker" / "state.json"
# Player pages, in the drivers.json profile shape so the apps reuse the
# driver page. Facts come from snooker.org; written bios, verified accounts
# and photos are merged in from the two hand-maintained files below.
DEFAULT_PLAYERS_OUT = REPO_ROOT / "snooker-players.json"
EDITORIAL = REPO_ROOT / "snooker" / "players.json"
PHOTOS = REPO_ROOT / "snooker" / "photos.json"
# Header photos per tournament name (past editions or the venue), from
# snooker/fetch_event_photos.py. Keyed by name so they carry across seasons.
EVENT_PHOTOS = REPO_ROOT / "snooker" / "event-photos.json"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

API = "https://api.snooker.org/"
USER_AGENT = "EGrid-snooker/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 30
# Two requests a minute is the agreed limit; one every 31 seconds stays under
# it with a margin for clock drift.
PACE_SECONDS = 31.0
DAILY_EVERY = dt.timedelta(hours=20)
# Player lookups a run may spend beyond the pro list.
MAX_PLAYER_LOOKUPS = 4

# snooker.org round numbers. 1-6, 20 and 21 are qualifying rounds and 17 is
# the "Champion" pseudo-round used for ranking points; none are shown.
MAIN_ROUNDS = set(range(7, 17)) | {18, 19}
STATUS = {0: "scheduled", 1: "live", 2: "onBreak", 3: "finished"}

_last_call = 0.0
_calls = 0


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def api(params: dict) -> object:
    """One paced call. Raises on anything but a 200 with JSON."""
    global _last_call, _calls
    key = os.environ.get("EGRID_SNOOKER_KEY", "").strip()
    if not key:
        raise RuntimeError("EGRID_SNOOKER_KEY is not set")
    wait = _last_call + PACE_SECONDS - time.monotonic()
    if _last_call and wait > 0:
        time.sleep(wait)
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"X-Requested-By": key, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    finally:
        _last_call = time.monotonic()
        _calls += 1
    log(f"  GET {url} ({len(body)} bytes)")
    return json.loads(body)


def safe(label: str, fn, default):
    try:
        return fn()
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as error:
        log(f"  {label} failed: {error}")
        return default


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def day(text: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def player_entry(raw: dict) -> dict | None:
    first = (raw.get("FirstName") or "").strip()
    last = (raw.get("LastName") or "").strip()
    if not first and not last:
        return None
    # Chinese and other surname-first names arrive with SurnameFirst set and
    # are written that way everywhere snooker is reported ("Wu Yize").
    parts = [last, first] if raw.get("SurnameFirst") else [first, last]
    entry = {"name": " ".join(p for p in parts if p)}
    # Kept for the player pages. snooker.org's own Twitter and URL fields
    # stay in the state file only: an account is published only once it is
    # verified on an official page (snooker/players.json).
    for key, field in (("nationality", "Nationality"), ("born", "Born"), ("turnedPro", "FirstSeasonAsPro"),
                       ("rankingTitles", "NumRankingTitles"), ("maximums", "NumMaximums"),
                       ("highestRanking", "HighestRanking"), ("wikipedia", "WikipediaURL"),
                       ("url", "URL"), ("twitter", "Twitter")):
        if raw.get(field):
            entry[key] = raw[field]
    return entry


def is_calendar_event(raw: dict) -> bool:
    """The tournaments a fan follows: main events only, no qualifying
    stages and none of the Championship League's dozens of group events
    (those carry `Main` pointing at their parent)."""
    return (
        raw.get("Type") != "Qualifying"
        and raw.get("ID") == raw.get("Main")
        and "F" in (raw.get("Stage") or "")
    )


def event_status(start: dt.date | None, end: dt.date | None, today: dt.date) -> str:
    if start and today < start:
        return "upcoming"
    if end and today > end:
        return "finished"
    return "inProgress"


def build(previous: dict, refetch: bool) -> dict:
    now = now_utc()
    today = now.date()
    players: dict[str, dict] = {} if refetch else dict(previous.get("players") or {})
    old_events = {e["id"]: e for e in previous.get("events") or []}
    last_daily = None if refetch else parse_iso(previous.get("dailyRefreshedAt"))
    daily_due = last_daily is None or now - last_daily >= DAILY_EVERY

    season = previous.get("season")
    raw_events = previous.get("_rawEvents")
    rounds_by_event = previous.get("_rounds")
    rankings = previous.get("rankings")

    if daily_due:
        log("Daily refresh")
        # t=20 (current season) answers 403 for our key, so the season is
        # taken from the calendar: the latest season whose events have begun.
        # A season runs June to May and is named for the year it starts.
        season = today.year if today.month >= 6 else today.year - 1
        events = safe("events", lambda: api({"t": 5, "s": season, "tr": "main"}), None)
        if isinstance(events, list) and events:
            raw_events = [
                {k: e.get(k) for k in ("ID", "Name", "Sponsor", "Type", "StartDate", "EndDate",
                                        "Venue", "City", "Country", "Url", "DefendingChampion",
                                        "Main", "Stage")}
                for e in events if is_calendar_event(e)
            ]
        rounds = safe("rounds", lambda: api({"t": 12, "s": season}), None)
        if isinstance(rounds, list) and rounds:
            rounds_by_event = {}
            for r in rounds:
                event_id = str(r.get("EventID"))
                if r.get("Round") in MAIN_ROUNDS:
                    rounds_by_event.setdefault(event_id, []).append(
                        {"round": r["Round"], "name": r.get("RoundName") or "", "distance": r.get("Distance") or 0}
                    )
        pros = safe("players", lambda: api({"t": 10, "s": season}), None)
        if isinstance(pros, list):
            for p in pros:
                entry = player_entry(p)
                if entry:
                    players[str(p["ID"])] = entry
        ranking = safe("rankings", lambda: api({"rt": "MoneyRankings", "s": season}), None)
        if isinstance(ranking, list) and ranking:
            rankings = {
                "title": "World Rankings",
                "type": "MoneyRankings",
                "season": season,
                "unit": "GBP",
                "rows": [
                    {"position": r["Position"], "playerId": r["PlayerID"], "points": r.get("Sum") or 0}
                    for r in sorted(ranking, key=lambda r: r.get("Position") or 9999)
                ],
            }
        if raw_events:
            last_daily = now

    if not raw_events:
        raise SystemExit("no events: nothing to publish")

    # Matches, event by event.
    matches_by_event: dict[int, list] = {}
    for raw in raw_events:
        event_id = raw["ID"]
        start, end = day(raw.get("StartDate")), day(raw.get("EndDate"))
        status = event_status(start, end, today)
        stored = None if refetch else (old_events.get(event_id) or {}).get("_rawMatches")
        wanted = (
            status == "inProgress"
            or (status == "upcoming" and start and (start - today).days <= 3)
            or (status == "finished" and end and (today - end).days <= 1)
            or (status == "finished" and stored is None)
        )
        if wanted:
            fetched = safe(f"matches {event_id}", lambda: api({"t": 6, "e": event_id}), None)
            if isinstance(fetched, list):
                stored = [
                    {k: m.get(k) for k in ("Round", "Number", "Player1ID", "Player2ID", "Score1", "Score2",
                                            "Walkover1", "Walkover2", "WinnerID", "Status", "ScheduledDate",
                                            "StartDate", "EndDate", "Estimated", "LiveUrl")}
                    for m in fetched if m.get("Round") in MAIN_ROUNDS
                ]
        matches_by_event[event_id] = stored or []

    # Players we still cannot name: look a few up, remember them.
    wanted_ids = {str(r["playerId"]) for r in (rankings or {}).get("rows", [])}
    for raw in raw_events:
        if raw.get("DefendingChampion"):
            wanted_ids.add(str(raw["DefendingChampion"]))
        for m in matches_by_event[raw["ID"]]:
            wanted_ids.update(str(m[k]) for k in ("Player1ID", "Player2ID") if m.get(k))
    missing = sorted(i for i in wanted_ids - set(players) if i != "0")
    for player_id in missing[:MAX_PLAYER_LOOKUPS]:
        found = safe(f"player {player_id}", lambda: api({"p": player_id}), None)
        if isinstance(found, list) and found:
            found = found[0]
        if isinstance(found, dict):
            entry = player_entry(found)
            if entry:
                players[player_id] = entry
    if len(missing) > MAX_PLAYER_LOOKUPS:
        log(f"  {len(missing) - MAX_PLAYER_LOOKUPS} players left unnamed until the next run")

    def name(player_id) -> str | None:
        return (players.get(str(player_id)) or {}).get("name") if player_id else None

    def nationality(player_id) -> str | None:
        return (players.get(str(player_id)) or {}).get("nationality") if player_id else None

    event_photos = json.loads(EVENT_PHOTOS.read_text()).get("photos", {}) if EVENT_PHOTOS.exists() else {}
    events_out = []
    for raw in sorted(raw_events, key=lambda e: (e.get("StartDate") or "", e["ID"])):
        event_id = raw["ID"]
        start, end = day(raw.get("StartDate")), day(raw.get("EndDate"))
        rounds = sorted((rounds_by_event or {}).get(str(event_id), []), key=lambda r: round_order(r["round"]))
        distance = {r["round"]: r["distance"] for r in rounds}
        matches = []
        for m in sorted(matches_by_event[event_id], key=lambda m: (round_order(m["Round"]), m.get("Number") or 0)):
            p1, p2 = m.get("Player1ID") or 0, m.get("Player2ID") or 0
            if not p1 and not p2:
                continue  # an empty slot further down the draw
            winner = m.get("WinnerID") or 0
            match = {
                "round": m["Round"],
                "number": m.get("Number") or 0,
                "player1": name(p1),
                "player2": name(p2),
                "score1": m.get("Score1") or 0,
                "score2": m.get("Score2") or 0,
                "status": STATUS.get(m.get("Status"), "scheduled"),
            }
            if winner:
                match["winner"] = 1 if winner == p1 else 2 if winner == p2 else None
            if m.get("Walkover1") or m.get("Walkover2"):
                match["walkover"] = True
            if distance.get(m["Round"]):
                match["bestOf"] = distance[m["Round"]] * 2 - 1
            scheduled = parse_iso(m.get("ScheduledDate"))
            if scheduled:
                match["scheduled"] = iso(scheduled)
                if m.get("Estimated"):
                    match["estimated"] = True
            if m.get("LiveUrl"):
                match["url"] = m["LiveUrl"]
            matches.append(match)

        event = {
            "id": event_id,
            "name": raw.get("Name") or "",
            "type": raw.get("Type") or "",
            "start": raw.get("StartDate"),
            "end": raw.get("EndDate"),
            "status": event_status(start, end, today),
        }
        for key, field in (("sponsor", "Sponsor"), ("venue", "Venue"), ("city", "City"),
                           ("country", "Country"), ("url", "Url")):
            if raw.get(field):
                event[key] = raw[field]
        if name(raw.get("DefendingChampion")):
            event["defendingChampion"] = name(raw["DefendingChampion"])
        if event_photos.get(event["name"]):
            event["image"] = event_photos[event["name"]]
        final = next((m for m in matches if m["round"] == 15 and m["status"] == "finished" and m.get("winner")), None)
        if final:
            won = final["winner"] == 1
            event["winner"] = final["player1"] if won else final["player2"]
            event["runnerUp"] = final["player2"] if won else final["player1"]
            high, low = sorted((final["score1"], final["score2"]), reverse=True)
            event["finalScore"] = f"{high}-{low}"
        if rounds:
            event["rounds"] = [
                {"round": r["round"], "name": r["name"], **({"bestOf": r["distance"] * 2 - 1} if r["distance"] else {})}
                for r in rounds
            ]
        event["matches"] = matches
        event["_rawMatches"] = matches_by_event[event_id]
        events_out.append(event)

    rankings_out = None
    if rankings:
        rankings_out = {k: v for k, v in rankings.items() if k != "rows"}
        rankings_out["rows"] = [
            {
                "position": r["position"],
                "name": name(r["playerId"]) or f"Player {r['playerId']}",
                **({"nationality": nationality(r["playerId"])} if nationality(r["playerId"]) else {}),
                "points": r["points"],
                "playerId": r["playerId"],
            }
            for r in rankings["rows"]
        ]

    return {
        "generatedAt": iso(now),
        "source": {"name": "snooker.org", "url": "https://www.snooker.org"},
        "season": season,
        "dailyRefreshedAt": iso(last_daily) if last_daily else None,
        "events": events_out,
        "rankings": rankings_out,
        "players": players,
        "_rawEvents": raw_events,
        "_rounds": rounds_by_event,
    }


def long_date(text: str | None) -> str | None:
    """"1975-03-21" -> "21 March 1975"."""
    d = day(text)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}" if d else None


def player_profiles(state: dict) -> dict:
    """snooker-players.json: one profile per player the channel can show —
    everyone ranked, in a draw, or defending a title."""
    editorial = {}
    if EDITORIAL.exists():
        editorial = {p["name"]: p for p in json.loads(EDITORIAL.read_text()).get("players", [])}
    photos = json.loads(PHOTOS.read_text()).get("photos", {}) if PHOTOS.exists() else {}
    players = state.get("players") or {}
    rank = {str(r["playerId"]): r["position"] for r in (state.get("rankings") or {}).get("rows", [])}

    shown: list[str] = list(rank)
    for event in state.get("events", []):
        for m in event.get("_rawMatches", []):
            shown += [str(m[k]) for k in ("Player1ID", "Player2ID") if m.get(k)]
    for raw in state.get("_rawEvents") or []:
        if raw.get("DefendingChampion"):
            shown.append(str(raw["DefendingChampion"]))

    profiles, seen = [], set()
    for pid in shown:
        info = players.get(pid)
        if pid in seen or not info or not info.get("name"):
            continue
        seen.add(pid)
        extra = editorial.get(info["name"], {})
        facts = []
        if info.get("turnedPro"):
            facts.append({"label": "Turned pro", "value": str(info["turnedPro"])})
        if info.get("rankingTitles"):
            facts.append({"label": "Ranking titles", "value": str(info["rankingTitles"])})
        if info.get("maximums"):
            facts.append({"label": "Maximum breaks", "value": str(info["maximums"])})
        if info.get("highestRanking"):
            facts.append({"label": "Highest ranking", "value": f"No. {info['highestRanking']}"})
        profile = {
            "id": f"snooker-{pid}",
            "name": info["name"],
            "matchKeys": [info["name"].lower()],
            "channelIDs": ["snooker"],
            "nationality": info.get("nationality"),
            "born": long_date(info.get("born")),
            "birthplace": extra.get("birthplace"),
            "bio": extra.get("bio"),
            "highlights": extra.get("highlights") or None,
            "socials": extra.get("socials") or None,
            "youtube": extra.get("youtube"),
            "image": photos.get(pid),
            "facts": facts,
            "links": [{"title": "Wikipedia", "url": info["wikipedia"]}] if info.get("wikipedia") else [],
            "worldRank": rank.get(pid),
        }
        profiles.append({k: v for k, v in profile.items() if v not in (None, [], {})})
    profiles.sort(key=lambda p: (p.get("worldRank") or 9999, p["name"]))
    return {
        "version": 1,
        "generatedAt": state["generatedAt"],
        "source": {"name": "snooker.org", "url": "https://www.snooker.org"},
        "_comment": "Generated by snooker/build_snooker.py. Facts from snooker.org; bio, highlights, "
                    "socials and youtube from snooker/players.json (verified, see snooker/VERIFIED.md); "
                    "photos from snooker/photos.json (Wikimedia Commons, see images/snooker/CREDITS.md).",
        "drivers": profiles,
    }


def round_order(round_number: int) -> int:
    """Draw order: round robin and wildcards first, then 7-12 (rounds 1-6),
    quarter-finals 13, semi-finals 14, bronze 18, final 15."""
    return {19: 0, 16: 1, 18: 14.5, 15: 15}.get(round_number, round_number)


def comparable(data: dict) -> dict:
    """State minus its run timestamp, so an unchanged run writes nothing.
    dailyRefreshedAt stays in: dropping it would leave the daily calls due
    on every run."""
    return {k: v for k, v in data.items() if k != "generatedAt"}


def public(state: dict) -> dict:
    """What the apps read: the state without the generator's working keys."""
    out = {k: v for k, v in state.items() if not k.startswith("_") and k not in ("players", "dailyRefreshedAt")}
    out["events"] = [{k: v for k, v in e.items() if not k.startswith("_")} for e in state["events"]]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--players-out", type=Path, default=DEFAULT_PLAYERS_OUT)
    parser.add_argument("--refetch", action="store_true")
    args = parser.parse_args()

    previous = {}
    if args.state.exists():
        try:
            previous = json.loads(args.state.read_text())
        except ValueError:
            log("previous state unreadable; starting fresh")

    data = build(previous, args.refetch)
    profiles = player_profiles(data)
    old_profiles = json.loads(args.players_out.read_text()) if args.players_out.exists() else None
    profiles_changed = old_profiles is None or \
        {**old_profiles, "generatedAt": None} != {**profiles, "generatedAt": None}
    if profiles_changed:
        args.players_out.write_text(json.dumps(profiles, ensure_ascii=False, indent=1) + "\n")
        log(f"Wrote {args.players_out.name}: {len(profiles['drivers'])} players, "
            f"{sum(1 for p in profiles['drivers'] if p.get('bio'))} with a bio")
    if previous and comparable(previous) == comparable(data) and args.out.exists():
        log(f"No change ({_calls} calls)")
        return 0
    args.state.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    args.out.write_text(json.dumps(public(data), ensure_ascii=False, indent=1) + "\n")
    live = sum(1 for e in data["events"] for m in e["matches"] if m["status"] in ("live", "onBreak"))
    log(f"Wrote {args.out.name}: {len(data['events'])} events, "
        f"{sum(len(e['matches']) for e in data['events'])} matches ({live} live), "
        f"{len((data['rankings'] or {}).get('rows', []))} ranked, {_calls} calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
