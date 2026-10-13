# Darts inputs (public)

Hand-maintained inputs for the darts channel. None of this is data from BSD
(sports.bzzoiro.com), the licensed source of results, draws and the Order of Merit:
that is fetched and served privately (TeamDzX/egrid-snooker, `darts/build_darts.py`,
endpoint `https://optidns.uk/egrid/darts/`), never committed here. See
`DARTS-PLAN.md` in the iOS repo for the whole design.

| File | What | Written by |
|---|---|---|
| `calendar.json` | Televised PDC events of the season: dates, venue, city, format (sets/legs), defending champion, Wikipedia link, and `bsdName` to join on BSD's tournament (BSD has no dates or venues). Premier League: one entry per night. | Hand, from Wikipedia "2026 in PDC" + event articles and pdc.tv; sources in `_comment`. Never guessed: unknown fields are null, unannounced events left out. |
| `players.json` | Bios and highlights for the top 32 of the PDC Order of Merit, keyed by name. | Hand, from each player's Wikipedia article (in `sources`). |
| `photos.json` → `images/darts/` | Player portraits, Commons only (BSD's photos are not licensed to us). | `fetch_photos.py` |
| `event-photos.json` → `images/darts/events/` | Event header photos (venue or past edition), keyed by calendar event name. | `fetch_event_photos.py CURATED.json` |
| `VERIFIED.md` | Where each social account was confirmed. None yet. | Hand |

Update the calendar when the PDC announces dates (the 2027 World Masters and UK Open
venues are not yet in the file) and the bios once a season.
