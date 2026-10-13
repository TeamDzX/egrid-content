# E-Grid — content repo

These files drive the E-Grid app's catalogue and editorial content. **Editing
them does not require an App Store submission.** The app fetches them at launch;
the copies bundled inside the app are the offline/first-launch fallback.

## Two branches: edit on `main`, serve from `live`

Both apps read the **`live`** branch, never `main`:

```
https://raw.githubusercontent.com/TeamDzX/egrid-content/live/<file>
```

`main` is where edits land. `live` is what iOS and Android actually fetch, and
it only moves when you promote it. There is no staged rollout for a file served
off a CDN — without this gate a single bad commit reaches every installed client
on both platforms within minutes, and the only way back is another commit.

**To publish an edit:**

```bash
git checkout main            # edit, commit, push as normal
git push origin main

# then, once you have actually looked at it:
git checkout live
git merge --ff-only main
git push origin live
git checkout main
```

`--ff-only` is deliberate: it refuses if `live` has drifted, rather than making
a merge commit that serves a state `main` was never in.

To roll back, point `live` at the last good commit and force-push it — the apps
pick it up on their next launch, no store submission involved:

```bash
git checkout live && git reset --hard <good-sha> && git push --force origin live
```

Both apps ship bundled copies of every JSON file, so if `live` is missing or a
fetch 404s they fall back to the bundled content rather than showing nothing.
That makes a typo in the branch name a silent staleness bug, not a crash —
worth knowing when content changes appear not to land.

The repo must stay **public** — the apps fetch these unauthenticated. The base
URL lives in exactly two constants, and they must agree:

| App | Constant |
|---|---|
| iOS | `ContentService.remoteBase` in `EGrid/Services/ContentService.swift` |
| Android | `ContentRepository.REMOTE_BASE` in `data/ContentRepository.kt` |

## Licensing

Not uniformly licensed, so please read before reusing:

- **The JSON** is public factual data — calendars, circuit dimensions,
  biographical facts, technical regulations. Facts are not copyrightable.
- **The photographs** in `images/circuits/` are each individually licensed
  (Creative Commons or public domain) and are **not** ours to relicense. Every
  file's author, licence and source are listed in
  [`images/circuits/CREDITS.md`](images/circuits/CREDITS.md). Reusing one means
  honouring its own licence, which for every CC licence here except CC0 means
  crediting the photographer.

E-Grid is not affiliated with, endorsed by, or connected to any championship,
team or governing body named in these files.

## Golden rule

**Leave a field out rather than guessing it.** Every optional field is hidden
when absent, so a partial entry looks deliberate. A wrong lap record or an
invented circuit length does not.

---

## `digest.json` — the weekly digest (generated, do not hand-edit)

**Multi-sport (October 2026).** Snooker is **off in the public digest**: snooker.org asked (11 Oct 2026) that its data not be publicly readable, so the generator includes snooker only when `EGRID_DIGEST_SNOOKER=1`, for a digest served from the private endpoint. When on, snooker rounds come from `snooker.json` (not
`channels.json`) and span their days: a preview includes anything on during
its window, a recap anything that finished in it. Non-motorsport rounds go in
each edition's **`moreItems`**, never `items`, because app versions before
2.7 / 1.7.0 read only `items` and cannot show an unknown channel; for the
same reason `intro` describes `items` only and **`introAll`** covers both.
Titles are "This weekend on E-Grid" / "Last week on E-Grid". From 2.7 /
1.7.0 the Home card lists only the reader's own channels (hidden when none)
and writes its own one-line summary unless they follow every channel in the
edition. `lastRaces` stays motorsport-only.

The card at the top of Home. **This file is written by a robot**, straight
to `live`. The action checks hourly, around the clock (the writer's server
runs 24/7 since October 2026), and rebuilds only when something is due: the
first build of the day, the three hours before a round with a known start
time (so the preview carries the final running order), or a race that has
just finished (its "Race Report" and the weekend edition update as soon as
the official result is published; series without a results feed four hours
after a known start, otherwise when the date turns). A manual run always
builds.

| Day | Edition | What it says |
|---|---|---|
| Monday, Tuesday | `recap` | What ran over the last seven days, with podiums for F1, MotoGP and Formula E. Tuesday re-runs it so a late Sunday-night result lands. |
| Wednesday to Sunday | `preview` | Every round of every channel racing between now and Monday, refreshed daily. On Sunday it is today's running order. |
| Saturday, Sunday | `preview` titled "Results from the weekend" once a result is in | The preview reaches back to Friday. As each official result arrives (F1 from Jolpica about an hour after the flag, MotoGP when its race session is marked finished, NASCAR when it names a winner, snooker when the final is won) that round gets its podium, race facts, the standings and a past-tense write-up, and the edition rebuilds; rounds still to run stay previews. Series without a results feed count as run four hours after a known start, otherwise when the date turns. It stays kind `preview`, so every app version labels it THIS WEEKEND. Monday's recap supersedes it. |

The generator is `digest/build_digest.py`, run by the
`.github/workflows/weekly-digest.yml` action. It reads the same sources the
apps do — the static calendars in this repo's `channels.json`, Jolpica for F1,
Pulselive for MotoGP and Formula E — so the digest can never name a round the
app doesn't know about.

NASCAR's own public results feed (`cf.nascar.com/cacher`) is matched to the
static calendar by date and adds start times, US broadcasters, the podium and
the race's story: where the winner started, stage winners, the margin, how the
Chase lead moved (2026's title format: the top 16 race the last ten rounds on
points, so the feed's playoff points are always zero) from the race's own
`live_points.json`, lead changes, cautions, retirements and pre-race penalties.
Every round also carries
a few `facts` lines — qualifying and sprint times, the title fight for F1,
MotoGP and Formula E, the venue from `circuits.json` — which the apps show
under the prose and the writer is given to work from. They are always read
from a feed, never written by a model.

Besides the dated editions the file holds **`lastRaces`**: one write-up per
series of its most recent completed round, however long ago. The apps show it
as the "Race Report" on that race's page and under "Last Race" on the channel
page, wherever `teams.json` has no fuller review. A write-up is only re-written
when its round or podium changes, and the writer is asked about four at a time
(a self-hosted model asked for a dozen at once returns broken JSON).

**It is the one file that skips the `main` → `live` gate.** A Thursday preview
that waits for a human on Friday is worthless, it carries no images or
licensing, and every fact in it is machine-read from a feed. After pushing
`live` the action merges `live` back into `main`, so the normal `--ff-only`
promotion keeps working. If it ever refuses, `main` and `live` have diverged
on something other than the digest — resolve that on `main` as usual.

### Prose: your own server, Claude, or a template

The writer is chosen by which **repository secrets** exist (Settings →
Secrets and variables → Actions), in this order:

| Secrets | Writer |
|---|---|
| `EGRID_LLM_URL`, plus `EGRID_LLM_TOKEN` and `EGRID_LLM_MODEL` as needed | Your own server. Any OpenAI-compatible chat endpoint — Ollama, vLLM, llama.cpp, LM Studio, Open WebUI. The URL may be the host (`https://llm.optidns.uk`), the `/v1` base, or the full `/v1/chat/completions` path. The token goes out as `Authorization: Bearer`. |
| `ANTHROPIC_API_KEY` | Claude through the Anthropic API. |
| neither | A plain templated sentence per round. |

If the first choice fails — server down, bad token, a reply that isn't JSON —
the script falls through to the next and says so in the log. A missing or
broken writer downgrades the prose, never the data, and the edition always
publishes.

Claude is instructed to use only the facts supplied and nothing else. The
recap additionally checks its copy back: an item that reads like a result
for a series whose podium was not published is thrown away for the template.
The model never sees a results feed directly and cannot introduce a name that
isn't in the JSON it was given.

### Running it by hand

```bash
python3 digest/build_digest.py                 # auto: recap on Monday, else preview
python3 digest/build_digest.py --kind recap --date 2026-07-28   # rehearse a past week
python3 digest/build_digest.py --no-llm        # templated prose only
```

The file keeps the last four editions; both apps show only the newest one
whose `publishedAt` has passed and `expiresAt` has not. Previews expire after
five days, recaps after four, so a missed run leaves the card blank rather
than stale. Both apps hide the card when nothing is live.

The **Actions → Weekly digest → Run workflow** button builds an edition on
demand, with the same `kind` and `date` options.

---

## `formula-e.json` — Formula E (generated, do not hand-edit)

Formula E's data API (`api.formula-e.pulselive.com`) stopped resolving in
October 2026 and the official site no longer exposes one, so this file is
both apps' Formula E source from 2.6 / 1.6.0 on: calendar, session times,
every session's classification, and the drivers' and teams' standings. The
digest and the team reviews read it too.

`formulae/build_formula_e.py` assembles it, run every three hours from 06:13
to 21:13 UTC by `.github/workflows/formula-e.yml`, which pushes straight to
`live` like the digest:

| What | From |
|---|---|
| Calendar (round, date, venue, city, country) | The schema.org JSON-LD on fiaformulae.com/en/calendar (`?season=N` for past seasons). Race names there are "TBC" until announced, so a race is "<City> E-Prix" until then. |
| Session start times (UTC) | sportstimes/f1 `_db/fe/{year}.json`, filed under the year a season ends (Season 13 = 2027.json). Until it exists, races carry a date and no sessions. |
| Results and standings | The HTML of fiaformulae.com/en/results-and-standings, read on its `data-testid` row markers — never on class names, which carry build hashes. |

It holds the current season and the one before, so the channel has a last
race and a table between seasons. Standings are for the newest season with
a race result. Results are fetched once per session and kept, so a quiet run
costs about four page loads; the site is asked only for the sessions a
round's results page links to (it answers anything else with the race).
A failed source keeps the previous file's data rather than publishing less,
and a run that changes nothing publishes nothing. The site refuses Python's
default user agent and accepts the script's identifying one.

The `channels.json` Formula E calendar stays as the fallback for 2.5 / 1.5.0
and earlier, which still call the dead API and fall back to it.

## `snooker.json` — Snooker (generated, do not hand-edit)

> **Private since 11 Oct 2026.** snooker.org asked that its data reach only
> holders of a key, not a public repository. The generator, the server job
> and the endpoint config live in the private repo **TeamDzX/egrid-snooker**;
> the data lives only on Alex's server and is served at
> `https://optidns.uk/egrid/snooker/` to requests carrying an app's
> `X-EGrid-Key`. `snooker.json`, `snooker-players.json` and
> `snooker/state.json` stay gitignored here, and the old Snooker workflow is
> gone. See that repo's README for the fetch schedule, rate limit and
> licence terms (credit "Snooker data from snooker.org" on the channel page,
> every event page and in Settings).
>
> What stays here, because none of it is snooker.org data: the hand-written
> inputs the generator merges (`snooker/players.json`, `photos.json`,
> `event-photos.json`), the Wikimedia photos in `images/snooker/`, and the
> scripts that maintain them.

### `snooker-players.json` — player pages (generated)

Written by the same run, in the `drivers.json` profile shape so both apps
reuse the driver page (and favourites, news and alerts). Every player who is
ranked, in a draw or defending a title gets a profile:

| Field | From |
|---|---|
| Nationality, born, `facts` (turned pro, ranking titles, maximums, highest ranking), Wikipedia link | snooker.org (`t=10`, `p=`) |
| `bio`, `highlights`, `birthplace`, `socials`, `youtube` | `snooker/players.json` — hand-researched for the top 32, merged by `snooker/merge_research.py` |
| `image` | `snooker/photos.json` — Wikimedia Commons only, by `snooker/fetch_photos.py`; credits in `images/snooker/CREDITS.md` |

Account rule, as for drivers: an account ships only when it is linked from
the player's own site, their WST profile or a management/sponsor page —
`snooker/VERIFIED.md` records where. snooker.org's own Twitter/URL fields are
kept in the state file and never published. WST's player feed is not a safe
shortcut: in October 2026 it carried test data and other players' accounts
(Yuan Sijun's record linked a WST developer, Zhou Yuelong's linked
@ronnie). snooker.org's player photos are not used — their rights are not
ours.

Re-run `python3 snooker/fetch_photos.py` when new players appear, and
re-research the top 32 each season; the facts refresh themselves daily.

The snooker channel's video feed is WST's own YouTube channel
(`videos/sources.json`), and Shaun Murphy's own channel feeds his page.

The snooker channel is in the apps' **bundled** `channels.json` only, not in
this repo's: older versions read this repo's directory and have no snooker
code. Apps from 2.7 / 1.7.0 append bundled channels the remote directory
lacks.

## `darts/` — Darts inputs (public; the data is private)

The darts channel (iOS 2.7 / Android 1.7.0) gets results, draws, live scores and
the PDC Order of Merit from **BSD** (sports.bzzoiro.com, Bzzoiro LLC), whose
licence allows showing the data in our own apps but not republishing it as a
feed. So, like snooker, the generator lives in the private repo
**TeamDzX/egrid-snooker** (`darts/build_darts.py`), runs on Alex's server and is
served only to the apps at `https://optidns.uk/egrid/darts/` with an app key.
`darts.json` and `darts-players.json` are never committed here.

What lives here is ours and public: `darts/calendar.json` (the season's televised
events with dates and venues from Wikipedia and pdc.tv, which BSD lacks),
`darts/players.json` (bios for the top 32), Commons photos in `images/darts/`
and `images/darts/events/`, and the scripts that maintain them. Details in
`darts/README.md`.

## `channels.json` — the series catalogue

Adds or changes the channels users can subscribe to.

| Field | Notes |
|---|---|
| `id` | Stable identifier. Changing it unsubscribes existing users — don't. |
| `name`, `tagline` | Shown in the channel browser and page header. |
| `accentHex` | Channel accent colour, no `#`. |
| `heroImageAsset` | Must match an image set already in the app bundle. New artwork is the one thing that *does* need an app update. |
| `hasStandings` | `true` only where the app has a data service for that series (F1, MotoGP, Formula E; snooker in the bundled directory). |
| `comingSoon` | Greys the row out and disables subscribing. |
| `feeds` | RSS/Atom sources. **Check a feed returns HTTP 200 before adding it.** |
| `regulationLinks` | Official rulebook/series links. |
| `calendar` | Hand-maintained season for series without a live API — see below. |

### Static calendars

Series without a data service (F2, F3, WEC, IMSA, NASCAR, IndyCar, NHRA,
Porsche Supercup, Clio Cup) get their rounds listed here:

```json
{ "round": 4, "name": "24 Hours of Le Mans", "location": "Circuit de la Sarthe, France", "date": "2026-06-13" }
```

- `date` is the **race day**, `yyyy-MM-dd`.
- `time` (`"HH:mm"`, UTC) is optional. Omit it unless the start time is
  confirmed — the app then shows the date only instead of inventing a time.
- `round` is optional. Omit it where official round numbering isn't verified;
  the app shows a checkered-flag badge instead of a made-up number.

**These need refreshing each December** when next season's calendars are
published. That is the main recurring maintenance job.

---

## `circuits.json` — track guides

Shown on the race page. Matched to a race by `matchKeys`: lowercase substrings
tested against the race's circuit, name and locality (accent- and
case-insensitive). `["silverstone", "british grand prix"]` catches both the F1
and MotoGP events.

### Writing matchKeys that don't collide

74 circuits across 12 championships share a lot of names, and a substring match
is blunt. Two rules, both learned from entries that were wrong:

- **Never use a bare country or city name.** `"spa"` is a substring of
  **Spa**in, which handed four MotoGP rounds the Francorchamps entry.
  `"italy"` on Monza swallowed both Imola and Mugello. Key on the circuit's
  name, not the country it sits in.
- **Where two series race at different venues in one country, key on the
  venue.** `"netherlands"` would give MotoGP's Assen round Zandvoort's photo.

When several entries match, **the longest matching key wins** — the longer key
is the more specific claim. This is what lets the WEC's Austin round, which is
confusingly named "Lone Star Le Mans", resolve to `cota` rather than `lemans`,
and IMSA's "Motul Petit Le Mans" to `road-atlanta`. File order is irrelevant.

`channelIDs` restricts an entry to particular channels, and a channel-scoped
entry beats an unscoped one. Only needed where one name really is two tracks:
MotoGP's French Grand Prix runs on the 4.2 km Bugatti circuit inside Le Mans,
not the 13.6 km Sarthe layout, so `le-mans-bugatti` is scoped to `motogp` while
`lemans` stays open to everyone.

Most entries carry only `id`, `name`, `matchKeys` and `image` — no length, no
lap record. That is deliberate: the key-figures card is hidden entirely unless
at least one fact is present, so a photo-only entry looks finished rather than
broken. Add facts only where you can verify them.

### Rally rounds (`wrc-*`)

The WRC's fourteen rounds are in here too, because a rally needs the same thing
a circuit does — a photo and a location. They differ in two ways:

- **All of them are scoped `"channelIDs": ["wrc"]`,** without exception. A
  rally's keys are necessarily loose (`"sweden"`, `"japan"`, `"chile"`), and
  scoping makes that safe: those keys can only ever be tested against a WRC
  round, so they cannot reach another series' race.
- **They carry no `lengthKm`, `corners` or `lapRecord`.** A rally has no lap,
  and the app labels that field "Length", which for 339 km of special stages
  across four days would read as nonsense. Route distance and stage count go in
  `facts`, where they can be stated in full: *"17 special stages covering
  339.15 km of competitive running in 2026."*

Because the WRC calendar comes from a live API, the exact event names the app
sees are whatever that API publishes — usually sponsor-prefixed
("Secto Rally Finland", "Vodafone Rally de Portugal"). Substring keys handle
those, but if a rally stops matching, check the name the API actually returns
before rewriting anything.

Three rounds — Japan, Paraguay and Saudi Arabia — have entries but no photo.
Nothing on Commons could be confirmed as those events (Japan's category is
mostly auto-show display cars; the other two are too new to have one). They
still match, so they still get their location and facts.

Optional fields: `lengthKm`, `corners`, `firstHeld`, `lapRecord`, `overview`,
`facts`, `trackPath`/`viewBox`, `image`. A race with no matching entry simply
shows "No track guide for this circuit yet."

### Circuit photography (`image`)

The race header and the Home screen's "Next Race" card use the circuit's own
photo where one exists, falling back to the app's artwork where it doesn't. The
series hero was dropped from both because it made every F1 weekend — and every
MotoGP weekend — look identical.

```json
"image": {
  "url": "images/circuits/monza.jpg",
  "credit": "Ank Kumar",
  "licence": "CC BY-SA 4.0",
  "licenceURL": "https://creativecommons.org/licenses/by-sa/4.0",
  "sourceURL": "https://commons.wikimedia.org/wiki/File:…"
}
```

**All 74 photos are mirrored into this repo** under `images/circuits/`, and
`url` is a path relative to the repo's raw base. Absolute `https://` URLs still
work, but don't ship one: hotlinking `upload.wikimedia.org` rate-limited the
original sourcing run with HTTP 429s, and a URL we don't control can move or
disappear.

**The app also ships copies of these files** in `EGrid/Resources/CircuitPhotos`
(~18 MB), so a race page is complete on first launch and offline instead of
waiting on a fetch. `sync-from-app.sh` copies them across, so the two stay
identical — run it after changing any image.

The app prefers its bundled copy and falls back to this repo, which is what
makes a circuit added *after* a release still work: it arrives in the updated
`circuits.json` with no bundled file, and loads from here instead. That is the
only case where these hosted copies are read at runtime, but it is the case
that keeps new circuits shipping without an App Store submission.

### Adding a photo

1. Put the absolute source URL in `url` (with credit, licence and source).
2. Run `python3 content/mirror-images.py`. It downloads anything still
   pointing at `http`, resizes to a 1280 px long edge, saves progressive JPEG
   at quality 82 (~240 KB each), and leaves already-local entries alone.
3. Change `url` to `images/circuits/<id>.jpg`.
4. Regenerate `images/circuits/CREDITS.md` so the redistributed copies carry
   their own attribution — we redistribute these files now, and every licence
   here makes that conditional on crediting the author.
5. Commit the JSON and the image together.

**"Publicly available" is not a licence.** A photo being reachable on the open
web says nothing about whether we may reproduce it. Agency and press
photography — Getty, LAT, Motorsport Images, a team's own media site, anything
lifted from a news article — is all rights reserved however easily it
downloads, and a credit line does not fix that. Rights holders bill for this.

What is safe:

| Source | Notes |
|---|---|
| **Wikimedia Commons** | Best source by far. Everything is CC or public domain, and each file page states the exact author and licence. |
| **Flickr**, filtered to Creative Commons | Check the licence on the photo, not the photostream. |
| **Public domain / CC0** | No attribution condition, though crediting anyway is polite. |
| **Our own images** | Commit them here and use a bare relative path — it resolves against this repo's raw base. |

`credit` is **a condition of the licence, not a courtesy**. Every CC licence
except CC0 makes attribution mandatory; an uncredited copy is an infringing
one. So the app refuses to display any image whose `credit` is empty unless
`licence` says public domain or CC0 — a half-filled entry vanishes instead of
shipping unattributed. The credit renders as a small capsule over the top-right
of the photo, tappable on the race page to open `sourceURL`.

Getting the fields right from Commons, without transcribing by hand:

```bash
curl -s -A "EGrid/1.0 (you@example.com)" \
  "https://commons.wikimedia.org/w/api.php?action=query&format=json&prop=imageinfo&iiprop=url|extmetadata&iiurlwidth=1280&titles=File:YOUR_FILE.jpg" \
  | python3 -m json.tool
```

`thumburl` is the `url` to use, `extmetadata.Artist` is `credit`,
`LicenseShortName` is `licence`, `LicenseUrl` is `licenceURL` and
`descriptionurl` is `sourceURL`.

One practical note: **use `iiurlwidth` to get a thumbnail URL, never the
original.** Commons originals run to 6000 px and 15 MB. `mirror-images.py`
resizes whatever it is given, but starting from a 1280 px render keeps the
download sane.

### Circuit layouts

`trackPath` is SVG path data in `viewBox` units, rendered as a vector — so a new
layout is one line of JSON, not an image asset.

The seeded paths for Monza and Le Mans are **stylised** and labelled as such in
the app. To use accurate outlines, take a circuit SVG from Wikimedia Commons
(most are CC-licensed — check the licence and attribute if required), copy the
`d=` attribute of the track path and the `viewBox`, and paste them in. Supported
commands: `M L H V C S Q T Z`, absolute and relative.

---

## `drivers.json` — driver and rider bios

Reached by tapping a name in the standings. Matched by `matchKeys` (surname is
usually enough) against the name the series API returns.

**Only stable biographical facts belong here** — date of birth, birthplace,
titles won, career highlights. Current points, position and wins come live from
the series APIs and are displayed above the bio, so this file never goes stale
mid-season and never contradicts the live table.

A driver with no entry shows a plain standings row with no tap target, rather
than a dead end.

---

## `machines.json` — teams, constructors and manufacturers

**Public factual data only.** No logos, no marketing copy. Facts are not
copyrightable; trade marks are, and shipping them would contradict the "not
affiliated" disclaimer in Settings. Since 2.4 a team page may carry three
things that are not marks:

- `colour` — the team's colour as `#RRGGBB`, for the monogram badge the apps
  draw in place of a logo.
- `image` — a Creative Commons photograph of the current car or bike, on the
  same terms as a circuit photo (author, licence, source page; mirrored into
  `images/teams/` with its credit in `images/teams/CREDITS.md`). A photo of a
  car can be licensed; a logo cannot.
- `videoSources` — the `source` names in `videos.json` that are the team's own
  YouTube channel, so its page can show its own videos out of the channel feed.

Two arrays:

- **`series`** — governing-body technical regulations. Identical for every
  competitor, so stated once per channel rather than repeated per team.
  Each has a `channelID`, `title`, optional `season`, a list of
  `{label, value}` entries and an optional `footnote`.
- **`teams`** — per-entrant profiles matched by `matchKeys` against the name
  the series API returns. Optional fields: `country`, `base`, `firstSeason`,
  `titles`, `chassis`, `powerUnit`, `notes`, `facts`.

Live data (current entries, championship position, points) comes from the
series APIs and is merged in at runtime, so this file holds only what does
not change during a season.

### A note on points

Where a series publishes an official team championship (F1, Formula E) the
app shows real positions and points from the API. MotoGP does **not** expose
constructor standings, and its constructor championship counts only each
manufacturer's best-placed rider per race — so the app lists manufacturers
without a points total rather than summing rider scores into a number that
would look official and be wrong. Don't add invented totals here.

---

## Workflow

1. Edit the JSON in `EGrid/Resources/` (keeps the app's fallback current).
2. Run `./content/sync-from-app.sh` to copy them here.
3. Commit and push. Users pick the change up on their next launch.

Validate before pushing — a malformed file is ignored and the app silently
keeps the bundled copy:

```bash
python3 -m json.tool content/circuits.json > /dev/null && echo OK
```

---

## `teams.json` — team performance reviews (generated)

Written every morning by `teams/build_teams.py` (the "Team reviews" Action,
07:40 UTC) for the three series with full results: Formula 1 constructors,
MotoGP manufacturers and Formula E teams. Each entry carries the season facts
(position, points, rounds, wins, podiums, the drivers or riders and their
standings), the team's lines in the most recent race, and two short pieces of
prose — a season summary and a last-race review — from the same writer as the
digest (`EGRID_LLM_*` first, Claude second, a templated sentence otherwise).
Prose that names anyone not in the team's own facts is dropped for the template.
The file also carries `races`: one entry per series for its most recent race —
podium, how many were classified, a summary and two to four highlight lines
written from the full classification — which the channel page shows under
"Last Race" when the round matches.
Never edit it by hand; never bundle it. The apps match entries the way they
match `machines.json`: `channelID` plus `matchKeys` against the live name.


---

## `tyres.json` — tyre suppliers (generated)

Written on the 1st of every month by `tyres/build_tyres.py` (the "Tyre
suppliers" Action; run it by hand after editing the script). One entry per
series in `SUPPLIERS` (F1 Pirelli, MotoGP Michelin, NASCAR Goodyear, IndyCar
Firestone): name, product, a two-paragraph summary, fact rows, F1's compounds
with their official colours, a Commons photo mirrored to `images/tyres/` and
the Wikipedia article it came from. The apps show it as "Tyres: <name>" on
every race page of that series.

All of it is grounded in the Wikipedia paragraphs about the series' tyres,
re-read every run: a configured fact is kept only if its numbers and words are
in that text, and the summary (`EGRID_LLM_*`, else a templated line) is
dropped if it carries a number the text does not. Formula E is left out on
purpose: Hankook's contract ran to 2025–26 and the Gen4 supplier for 2026–27
should be confirmed before an entry is added.

## `videos.json` — `externalVideos`

A source in `videos/sources.json` may name an `external` channel: the series'
own race edits from a channel that refuses embedding (Formula 1). They are
written as `externalVideos` beside `videos`; a finished race's page lists the
ones about that race under "Highlights" and opens them in YouTube.
