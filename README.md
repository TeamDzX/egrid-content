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

The card at the top of Home. **This file is written by a robot**, every
morning at 07:00 UTC, straight to `live`:

| Day | Edition | What it says |
|---|---|---|
| Monday, Tuesday | `recap` | What ran over the last seven days, with podiums for F1, MotoGP and Formula E. Tuesday re-runs it so a late Sunday-night result lands. |
| Wednesday to Sunday | `preview` | Every round of every channel racing between now and Monday, refreshed daily. On Sunday it is today's running order. |

The generator is `digest/build_digest.py`, run by the
`.github/workflows/weekly-digest.yml` action. It reads the same sources the
apps do — the static calendars in this repo's `channels.json`, Jolpica for F1,
Pulselive for MotoGP and Formula E — so the digest can never name a round the
app doesn't know about.

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

## `channels.json` — the series catalogue

Adds or changes the channels users can subscribe to.

| Field | Notes |
|---|---|
| `id` | Stable identifier. Changing it unsubscribes existing users — don't. |
| `name`, `tagline` | Shown in the channel browser and page header. |
| `accentHex` | Channel accent colour, no `#`. |
| `heroImageAsset` | Must match an image set already in the app bundle. New artwork is the one thing that *does* need an app update. |
| `hasStandings` | `true` only where the app has a data service for that series (F1, MotoGP, Formula E). |
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

**Public factual data only.** No logos, no photography, no marketing copy.
Facts are not copyrightable; trade marks and images are, and shipping them
would contradict the "not affiliated" disclaimer in Settings.

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
