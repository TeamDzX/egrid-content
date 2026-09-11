#!/usr/bin/env python3
"""Builds `videos.json` — the per-channel highlight reels both E-Grid apps
show on a channel page.

Rebuilt every morning alongside the digest and pushed straight to `live`.
Like `digest.json` it is generated rather than edited, is never bundled in
either app, and a channel with nothing fetched simply shows no Highlights
section.

Videos are only ever *linked*, never rehosted: the apps play them through
YouTube's IFrame player, which is the only form of playback YouTube's terms
allow. That means this file holds IDs and metadata, nothing else.

A source being listed is not proof it can be embedded. Embedding is refused
per channel, and no API field reports it — the Formula 1 channel marks every
video embeddable and then fails with error 150 on playback. Sources are
therefore verified by playing one in the app before being enabled; see the
notes in `sources.json`.

Sources live in `videos/sources.json`, one entry per app channel:

  playlist   a season-highlights playlist curated by the series itself.
             Preferred wherever one exists — it is the series' own idea of
             what counts as a highlight, so nothing has to be pattern-matched.
  uploads    the channel's uploads feed, narrowed by `titleMatch`. Used where
             a series publishes highlights but files them nowhere.

Under ten quota units per source per run — at most five `playlistItems.list`
pages plus a `videos.list` per 50 matches — against the 10,000 a free key
gets a day. `search.list` is deliberately not used: it costs 100 units a call
and would answer with worse data.

Usage:
    YOUTUBE_API_KEY=... python3 videos/build_videos.py [--out videos.json]
                                                       [--sources videos/sources.json]
                                                       [--limit 12] [--no-key]

`--no-key` swaps the API for YouTube's public RSS and playlist pages. It
exists so the file can be previewed on a machine with no key provisioned;
it is slower, returns less, and CI never passes it. A run without a key and
without the flag fails rather than quietly publishing a thinner file.

Exit code is non-zero only when nothing at all could be built; one dead
source is logged and skipped.
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = REPO_ROOT / "videos" / "sources.json"
DEFAULT_OUT = REPO_ROOT / "videos.json"

USER_AGENT = "EGrid-videos/1.0 (+https://github.com/TeamDzX/egrid-content)"
TIMEOUT = 20
API = "https://www.googleapis.com/youtube/v3"

# Most recent highlights kept per channel. Enough to cover a season's last
# few rounds without turning the section into an archive.
DEFAULT_LIMIT = 12

# Anything shorter is a clip or a short, not a highlight reel. Several series
# file 20-second onboards in the same playlist as the race edit.
MIN_SECONDS = 90

# Pages of 50 uploads to look back through per source. Five covers about 250
# uploads, which on the busiest channel here is roughly a season; beyond that
# the matches are old enough that nobody wants them on a channel page.
MAX_PAGES = 5


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Video:
    id: str
    title: str
    publishedAt: str
    duration: str                        # "8:21", for the badge on the card
    seconds: int
    thumbnail: str
    # ISO country codes YouTube says this video is blocked in, so the apps can
    # hide a video the viewer could never play rather than showing them a
    # player that fails. Empty for the overwhelming majority.
    blockedRegions: list[str] = field(default_factory=list)


@dataclass
class ChannelVideos:
    channelID: str
    source: str                          # the YouTube channel doing the publishing
    sourceURL: str
    videos: list[Video] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


_last_request_at = 0.0


def _get(url: str, *, as_json: bool = True):
    """One GET, paced to four a second and retried once on a 429."""
    global _last_request_at
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json" if as_json else "*/*"})
    for attempt in (1, 2):
        wait = 0.25 - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                _last_request_at = time.monotonic()
                return json.load(response) if as_json else response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            _last_request_at = time.monotonic()
            if error.code == 429 and attempt == 1:
                time.sleep(3)
                continue
            # The API puts the useful part of a 400/403 in the body — a bad key
            # and an exhausted quota are both 403 and need telling apart.
            detail = error.read().decode("utf-8", "replace")[:300] if as_json else ""
            raise RuntimeError(f"HTTP {error.code}: {detail or error.reason}") from None


def api(endpoint: str, key: str, **params) -> dict:
    params["key"] = key
    return _get(f"{API}/{endpoint}?" + urllib.parse.urlencode(params))


def safe(label: str, fn, default):
    """Runs a source, logging and swallowing any failure. One dead channel
    must not take the whole file down with it."""
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - deliberately broad
        log(f"  ! {label}: {type(error).__name__}: {error}")
        return default


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


_ISO_DURATION = re.compile(
    r"P(?:(?P<d>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?")


def parse_duration(iso: str) -> int:
    """`PT1H2M3S` -> seconds. Returns 0 for anything unparseable, which the
    minimum-length filter then drops."""
    match = _ISO_DURATION.fullmatch(iso or "")
    if not match:
        return 0
    part = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return part["d"] * 86400 + part["h"] * 3600 + part["m"] * 60 + part["s"]


def clock(seconds: int) -> str:
    """Seconds -> "8:21" or "1:04:09"."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def uploads_playlist(channel_id: str) -> str:
    """A channel's uploads playlist is its ID with the `UC` prefix swapped for
    `UU`. Saves a `channels.list` call for a value that is pure arithmetic."""
    if not channel_id.startswith("UC"):
        raise ValueError(f"not a channel ID: {channel_id}")
    return "UU" + channel_id[2:]


def thumbnail_for(video_id: str, thumbnails: dict | None = None) -> str:
    """Prefers the widescreen still the API offers, falling back to the
    predictable i.ytimg.com path so `--no-key` runs still get an image."""
    for size in ("maxres", "standard", "high", "medium"):
        url = (thumbnails or {}).get(size, {}).get("url")
        if url:
            return url
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


# --------------------------------------------------------------------------- #
# Sources — YouTube Data API
# --------------------------------------------------------------------------- #


def playlist_candidates(playlist_id: str, key: str, pattern, want: int) -> list[str]:
    """Video IDs from a playlist, newest first, paging until enough of them
    match `pattern` (or the playlist runs out).

    Paging matters because the narrowing happens here rather than after
    `videos.list`. A channel like WRC or NASCAR posts shorts several times a
    day, so a single page of 50 uploads can contain one race edit or none —
    the first run of this script found a single IndyCar highlight. Each page
    costs one quota unit and `snippet` comes free with it, so filtering on the
    title now is both cheaper and deeper than fetching 50 videos' full details
    and throwing most away.
    """
    out: list[str] = []
    token = None
    for _ in range(MAX_PAGES):
        params = dict(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            params["pageToken"] = token
        data = api("playlistItems", key, **params)
        for item in data.get("items", []):
            title = item.get("snippet", {}).get("title", "")
            # A curated playlist is the series' own selection and is taken as
            # read; an uploads feed has to be narrowed to the highlight reels.
            if pattern and not pattern.search(title):
                continue
            out.append(item["contentDetails"]["videoId"])
        token = data.get("nextPageToken")
        # Collect a margin over `want`: the per-video checks below still drop
        # some for length, embeddability or being a premiere.
        if not token or len(out) >= want * 2:
            break
    return out


def video_details(video_ids: list[str], key: str) -> list[dict]:
    """`videos.list` in batches of 50, one quota unit each. Carries the three
    things a playlist entry cannot: how long it is, whether it may be embedded
    at all, and where it is blocked."""
    items: list[dict] = []
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]
        data = api("videos", key, part="snippet,contentDetails,status",
                   id=",".join(batch), maxResults=50)
        items.extend(data.get("items", []))
    return items


def collect_with_key(source: dict, key: str, want: int) -> list[Video]:
    playlist = source.get("playlist") or uploads_playlist(source["channelId"])
    pattern = re.compile(source["titleMatch"], re.I) if source.get("titleMatch") else None
    ids = playlist_candidates(playlist, key, pattern, want)
    if not ids:
        return []

    out: list[Video] = []
    for item in video_details(ids, key):
        snippet, status = item.get("snippet", {}), item.get("status", {})
        details = item.get("contentDetails", {})
        title = snippet.get("title", "")

        # Catches the per-video opt-out only. It does NOT catch a channel that
        # refuses embedding wholesale: the Formula 1 channel reports every
        # video as embeddable and then answers error 150 when one is played.
        # Nothing in the API exposes that, so it is recorded per source in
        # sources.json instead, and the app shows a message if one slips past.
        if status.get("embeddable") is False:
            log(f"    - not embeddable: {title[:60]}")
            continue
        if snippet.get("liveBroadcastContent", "none") != "none":
            continue
        seconds = parse_duration(details.get("duration", ""))
        if seconds < MIN_SECONDS:
            continue

        out.append(Video(
            id=item["id"],
            title=title,
            publishedAt=snippet.get("publishedAt", ""),
            duration=clock(seconds),
            seconds=seconds,
            thumbnail=thumbnail_for(item["id"], snippet.get("thumbnails")),
            blockedRegions=sorted(details.get("regionRestriction", {}).get("blocked", [])),
        ))

    out.sort(key=lambda v: v.publishedAt, reverse=True)
    return out[:want]


# --------------------------------------------------------------------------- #
# Sources — keyless preview
# --------------------------------------------------------------------------- #


_ATOM = {"a": "http://www.w3.org/2005/Atom",
         "yt": "http://www.youtube.com/xml/schemas/2015"}


def _published(video_id: str) -> str:
    """Upload date off the watch page, for the keyless path only. Returns ""
    when the page shape changes, which sorts it oldest and so leaves the
    playlist's own order standing."""
    try:
        html = _get(f"https://www.youtube.com/watch?v={video_id}", as_json=False)
    except Exception:  # noqa: BLE001 - a preview path, never CI
        return ""
    match = re.search(r'"uploadDate":"([^"]+)"', html)
    return match.group(1) if match else ""


def collect_without_key(source: dict, want: int) -> list[Video]:
    """Public RSS for uploads, the playlist page for playlists. No duration,
    no embeddable flag and no region data — enough to eyeball the file, not
    enough to publish. See `--no-key` in the module docstring."""
    pattern = re.compile(source["titleMatch"], re.I) if source.get("titleMatch") else None
    out: list[Video] = []

    if source.get("playlist"):
        html = _get(f"https://www.youtube.com/playlist?list={source['playlist']}",
                    as_json=False)
        # The playlist grid is a list of `lockupViewModel`s, each carrying the
        # video's `contentId` and its title. The two lists stay in step, which
        # is the only reason this works at all — and the reason it is a preview
        # path and not the one CI runs.
        ids = re.findall(r'"contentId":"([\w-]{11})"', html)
        titles = [json.loads(f'"{raw}"') for raw in re.findall(
            r'"lockupMetadataViewModel":\{"title":\{"content":"((?:[^"\\]|\\.)*)"', html)]
        pairs = list(zip(ids, titles))
        # Playlist direction is not consistent even between playlists on the
        # same channel — F1's race highlights run oldest-first while F2's and
        # F3's run newest-first — so it is measured rather than assumed. Two
        # watch pages settle it; the API path gets `publishedAt` for free and
        # needs none of this.
        if len(pairs) > 1 and _published(pairs[0][0]) < _published(pairs[-1][0]):
            pairs.reverse()
        for video_id, title in pairs:
            out.append(Video(id=video_id, title=title, publishedAt="",
                             duration="", seconds=0, thumbnail=thumbnail_for(video_id)))
    else:
        xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={source['channelId']}",
                   as_json=False)
        for entry in ET.fromstring(xml).findall("a:entry", _ATOM):
            title = (entry.findtext("a:title", default="", namespaces=_ATOM) or "")
            if pattern and not pattern.search(title):
                continue
            video_id = entry.findtext("yt:videoId", default="", namespaces=_ATOM)
            out.append(Video(id=video_id, title=title,
                             publishedAt=entry.findtext("a:published", default="",
                                                        namespaces=_ATOM),
                             duration="", seconds=0, thumbnail=thumbnail_for(video_id)))

    return out[:want]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def load_sources(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


def build(sources: list[dict], key: str | None, want: int) -> list[ChannelVideos]:
    out: list[ChannelVideos] = []
    for source in sources:
        channel_id = source["channelID"]
        log(f"  {channel_id}: {source.get('sourceName', source['channelId'])}")
        videos = safe(
            channel_id,
            (lambda s=source: collect_with_key(s, key, want)) if key
            else (lambda s=source: collect_without_key(s, want)),
            [],
        )
        if not videos:
            log(f"    - nothing usable, channel omitted")
            continue
        log(f"    {len(videos)} video(s), newest {videos[0].title[:52]!r}")
        out.append(ChannelVideos(
            channelID=channel_id,
            source=source.get("sourceName", ""),
            sourceURL=f"https://www.youtube.com/channel/{source['channelId']}",
            videos=videos,
        ))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--no-key", action="store_true",
                        help="preview from public feeds instead of the API")
    args = parser.parse_args()

    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key and not args.no_key:
        log("YOUTUBE_API_KEY is not set. Set it, or pass --no-key for a "
            "lower-fidelity preview that must not be published.")
        return 2
    if args.no_key:
        key = None
        log("! --no-key: durations, embeddable flags and region data are absent")

    sources = load_sources(args.sources)
    log(f"Building {args.out.name} from {len(sources)} source(s)")
    channels = build(sources, key, args.limit)
    if not channels:
        log("No channel produced any video; leaving the existing file alone")
        return 1

    payload = {
        "version": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": [asdict(c) for c in channels],
    }
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    total = sum(len(c.videos) for c in channels)
    log(f"Wrote {args.out} — {len(channels)} channel(s), {total} video(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
