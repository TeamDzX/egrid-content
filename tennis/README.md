# Tennis inputs (public)

Hand-maintained inputs for the tennis channel. None of this is data from BSD
(sports.bzzoiro.com), the licensed source of draws, results and rankings: that is fetched and
served privately (TeamDzX/egrid-snooker, `tennis/build_tennis.py`, endpoint
`https://optidns.uk/egrid/tennis/`), never committed here. See `TENNIS-PLAN.md` in the iOS
repo for the whole design.

| File | What | Written by |
|---|---|---|
| `calendar.json` | Grand Slams, ATP Masters 1000, WTA 1000, ATP Finals and WTA Finals from the current week to the 2027 Finals: first main-draw day, last day, venue, city, surface, the venue's UTC offset during the event, Wikipedia link, and one `draws` entry per singles draw (`men` / `women`) with its BSD tournament id (`bsdId`, BSD has no dates or venues) and defending champion. | Hand, from Wikipedia "2026 ATP Tour", "2026 WTA Tour", each event and singles-draw article, the ATP 2027 calendar (PDF, 14 Jan 2026) and the WTA 2027 calendar (PDF, 1 Oct 2026); sources in `_comment`. |
| `players.json` | Bios and highlights for the ATP and WTA singles top 20, keyed by name as the data feed spells it (some names lack diacritics; that is the join key). | Hand, from each player's Wikipedia article (in `sources`). |
| `photos.json` -> `images/tennis/` | Player portraits, Commons only (BSD's photos are not licensed to us). | `fetch_photos.py` |
| `event-photos.json` -> `images/tennis/events/` | Event header photos (venue court, or a past edition's court where the current venue has none on Commons), keyed by calendar event name. | `fetch_event_photos.py CURATED.json` |
| `VERIFIED.md` | Where each social account was confirmed. None yet. | Hand |

## How to update

- **After each event:** the next edition's `defendingChampion` becomes this edition's winner
  (BSD-style spelling where the player is in `players.json`). Until the 2026 Shanghai, Wuhan,
  Paris, WTA Finals and ATP Finals are played, their 2027 entries carry the 2025 winners.
- **New dates:** replace provisional 2027 dates (below) when the tournaments publish their
  order of play, and add the 2027-28 season once the tours publish their calendars.
- **Bios:** once a season, or when the top 20 changes; keep names exactly as the feed spells them.
- **Photos:** `python3 tennis/fetch_photos.py` picks up new players; `--refresh` re-fetches all.
- Combined ATP+WTA 1000s have type `ATP Masters 1000 / WTA 1000`. The 2027 Canadian Open is split
  by city (men Toronto, women Montreal), so it has two entries; the cities swap every year and BSD
  keeps a separate tournament per city and tour (Toronto ATP 125 / WTA 694, Montreal ATP 693 / WTA 126).

## BSD ids

Every draw in the calendar has a BSD singles tournament id; none is null. Qualifying, doubles,
juniors, wheelchair and mixed tournaments are never used.

## Provisional dates

The 2026 events and the 2027 Australian Open, Indian Wells, Miami, Monte-Carlo, Rome (men),
Madrid (men), Roland Garros, Wimbledon, Toronto, Cincinnati (men), US Open, Shanghai, Paris and
ATP Finals dates come from published calendars (the ATP PDF gives start weekdays). These are
provisional, inferred from the WTA calendar's week plus the 2026 edition's pattern, or
otherwise uncertain:

| Event | In file | Why |
|---|---|---|
| Qatar Open 2027 | 7-13 Feb | WTA calendar gives week of 8 Feb; 2026 main draw began on the Sunday |
| Dubai Tennis Championships 2027 | 14-20 Feb | WTA week of 15 Feb; 2026 pattern (Sunday to Saturday) |
| Miami Open 2027 | 16-28 Mar | ATP men start Wed 17 Mar; women's start (Tuesday) from the 2026 pattern |
| Madrid Open 2027 | 20 Apr - 2 May | ATP men start Wed 21 Apr; women's start (Tuesday) from the 2026 pattern |
| Italian Open 2027 | 4-16 May | ATP men start Wed 5 May; women's start (Tuesday) from the 2026 pattern |
| Canadian Open (Montreal) 2027 | 1-12 Aug | WTA weeks of 2 and 9 Aug; assumed the same days as the men's Toronto event |
| Cincinnati Open 2027 | 12-22 Aug | ATP gives Thursday 12 Aug start; final day from the 2026 pattern |
| China Open 2027 | 29 Sep - 10 Oct | WTA weeks of 27 Sep and 4 Oct; 2026 pattern (Wednesday start) |
| Wuhan Open 2027 | 11-17 Oct | WTA week of 11 Oct |
| WTA Finals 2027 | 10-14 Nov, Spectrum Center, Charlotte | WBTV (16 Sep 2026) quotes 10-14 Nov; the WTA calendar only gives the week of 8 Nov |
| ATP Finals 2027 | 14-21 Nov, Inalpi Arena, Turin | ATP calendar says Sunday 14 Nov in "Italy"; Turin assumed (host since 2021) |

## Photos

40 player portraits; 19 of the 20 event names have a header photo. Wuhan Open has none: Commons
has no freely licensed photo of the Optics Valley International Tennis Center. Some headers
show the event's court at a previous venue, as the caption says: ATP Finals (The O2, London, 2019),
WTA Finals (Istanbul, 2011), Paris Masters (Bercy, 2024; La Défense Arena since 2025). Miami Open
shows the Hard Rock Stadium exterior (no free photo of the tennis court).
