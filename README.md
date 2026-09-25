# Story Grabber v3.3.2

# Story Grabber v3.3.1

A local-first web application for crawling story sites, extracting story text, fixing readability without changing the wording, romanizing Telugu, grouping multipart stories, and organizing the result into a simple local library.

Native launches bind only to `127.0.0.1` by default. Docker starts Story Grabber with explicit private-LAN mode so the same web app can be opened from phones/tablets on your LAN while public-host and cross-origin requests remain blocked.


## v3.3.1: Docker/LAN + mobile view

- Native `python web_server.py` remains localhost-only.
- Docker launches with `--host 0.0.0.0 --allow-lan`. Only loopback/private/link-local clients and loopback/private-IP Host headers are accepted.
- Browser POST requests must remain same-origin; cross-site POSTs are rejected.
- `docker-compose.yml` publishes `8000:8000`, allowing the UI to be opened from a phone with `http://<PC-LAN-IP>:8000`.
- Below 720 px, Sources/Library/Recent tables become touch-friendly cards instead of horizontally scrolling tables.
- Mobile controls use larger touch targets; manual scrape actions become sticky; reader spacing is optimized for a phone.

## Workflow

```text
Website
  ↓
Crawler
  ↓
Discovered links
  ├─ Manual mode → wait in link queue
  └─ Auto mode   → scrape new links while crawling continues
                     ↓
                 Raw HTML
                     ↓
              Story Formatter
                     ↓
            Exact text verifier
                     ↓
          Telugu romanization
                     ↓
       Category + multipart organizer
                     ↓
                Story Library
                     ↓
          Kindle library.json export
```

## KISS interface

The primary UI intentionally has only four pages:

- **Dashboard** — counts, source progress, recent stories, and expandable engine logs.
- **Sources** — choose Auto or Manual mode, start/stop crawling, and inspect links.
- **Library** — search/filter/read all stories, including category and multipart-folder information.
- **Settings** — category names and common defaults; low-level formatter tuning stays internal so the normal UI remains simple.

Formatting review is shown inside the affected story instead of having a separate administration page.

## Auto and manual scraping

### Auto mode

The crawler refreshes `sublinks.json` with partial results while it runs. `auto_scrape.py` watches those partial results and sends newly discovered links to the normal story pipeline in small batches.

This means the scraper does **not** have to wait for a large crawl to finish.

Default auto batch size: `20` stories. It is configurable in Settings.

### Manual mode

The crawler only discovers links. They remain in the queue until you:

- select individual links and click **Scrape selected**, or
- click **Scrape pending**.

No second scraper implementation exists: Auto and Manual modes both use the same `story_pipeline.py` and the same extractor/formatter path.

## Multipart story folders

Multipart titles are detected conservatively from endings such as:

```text
Moon Story Part 1
Moon Story - Part 2
Moon Story Pt. 3
Moon Story (Part 4)
Moon Story 5
Moon Story - 6
```

They are normalized to one series:

```text
Moon Story
```

and the readable files are organized as:

```text
content_output/library/
└─ Uncategorized/
   └─ Moon Story/
      ├─ Part 001.html
      ├─ Part 002.html
      ├─ Part 003.html
      └─ Part 004.html
```

A bare trailing sequence number is also accepted for sites that publish parts as `Story Name 1`, `Story Name 2`, and so on. Four-digit suffixes (for example years) and labelled `Chapter`/`Episode` endings are not treated as parts.

Raw, formatted, and romanized processing files remain separate and untouched by this folder view.

## Categories

Open **Settings → Categories** and enter one category name per line, for example:

```text
College
Family
Romance
Office
```

For each story the organizer searches the title plus original/formatted/romanized story text. The **first configured category name that appears in the story wins**.

Example:

```text
Categories:
College
Family

Story text:
"She met her brother at College ..."
```

Result:

```text
content_output/library/College/...
```

If no configured category is found, the story goes to:

```text
content_output/library/Uncategorized/
```

If one part of a multipart story matches a category, all parts of that series stay together in the same category folder. If several parts match different categories, the first category in your Settings list has priority.

Changing the category list rebuilds only the generated `content_output/library/` view. The source/raw evidence is never modified.

## Kindle Story Reader export

Version 3.3 automatically writes a Kindle-compatible catalog into the same generated folder you already copy:

```text
content_output/library/
├─ library.json
├─ College/
├─ Family/
└─ Uncategorized/
```

No separate export workflow is required. Copy the **contents of `content_output/library/`** to:

```text
/mnt/us/stories/
```

The standalone KUAL Story Reader then receives the canonical Story Grabber metadata instead of guessing from filenames. `library.json` contains:

- a stable ID for each single story or multipart series,
- canonical story/series title,
- category,
- ordered part numbers and relative HTML paths,
- stable added timestamps used by Kindle library views.

For multipart series, Part 1's source URL is the stable identity anchor, so adding Part 2/3/4 or changing the category does not reset Kindle reading progress. Adding a new part updates the series `addedAt` value so the series can surface under **Recently Added**.

The manifest is refreshed automatically after:

- Auto scraping,
- Manual scraping,
- Reformatting,
- Manual paragraph correction,
- Category changes,
- Full library rebuild/migration,
- Application startup when upgrading an existing v3.2 library.

You can also regenerate only the Kindle manifest from the command line:

```bash
python kindle_export.py --output content_output
```

The manifest is written atomically. Story HTML files remain unchanged.

## Readability formatter

The formatter is deliberately non-destructive. It may add paragraph/line structure, but it may not rewrite the story. Version 3.2 adds story-local dialogue detection, one-letter speaker labels such as `V:` / `N:` / `P:`, `Speaker:-` and `Speaker:text` forms, nested HTML-entity cleanup, conservative metadata/time exclusions, and a whitespace-only fallback for very long punctuation-free prose.

A raw block such as:

```text
After coming home brother: where did you go sister: I went to market brother: why sister: I forgot
```

can become:

```text
After coming home

brother: where did you go

sister: I went to market

brother: why

sister: I forgot
```

Before a formatted file is accepted, it is written to disk, opened again, parsed independently, and checked with:

```text
normalize(raw text) == normalize(formatted text)
```

Normalization decodes HTML character references and then normalizes whitespace. Removing, inserting, changing, or reordering story characters fails verification.

A second readability audit runs after the exact-text check. A story is automatically marked verified only when recognized dialogue turns are separated and no paragraph remains above the review threshold. Very long punctuation-free blocks are split only at existing whitespace boundaries.

The inline manual formatting editor uses the same guards: only paragraph/line breaks can be changed.

### Windows / Telugu filename repair

Every extracted filename ends with a stable 12-character hash of its source URL. If Windows or a ZIP tool escapes Telugu filename characters as `#Uxxxx` while the manifest still contains the Unicode spelling, the formatter resolves the real file by that URL hash. Reader, reprocess, romanization and library organization therefore continue to use the correct story file.

## Runtime layout

```text
content_output/
├─ pages/                    # immutable raw clean HTML
├─ formatted_pages/          # readability-formatted + integrity-verified
├─ romanized_pages/          # formatted version after Telugu romanization
├─ library/                  # generated human-readable category/series folders
│  ├─ library.json           # Kindle Story Reader catalog
│  ├─ College/
│  │  └─ Moon Story/
│  │     ├─ Part 001.html
│  │     └─ Part 002.html
│  └─ Uncategorized/
├─ manifest.json             # scraper state
├─ progress.jsonl            # crash-safe scraper journal
├─ processing_manifest.json
└─ story_library.sqlite3     # searchable metadata/text library
```

`library/` is a generated view and can be rebuilt. The authoritative processing artifacts remain in `pages/`, `formatted_pages/`, and `romanized_pages/`.

## Requirements

- Python 3.10+
- Node.js 20+
- npm
- Git

## One-time setup

### Windows PowerShell

```powershell
cd "D:\path\to\story-grabber\cli"
python extract_cli.py setup
python extract_cli.py doctor
```

### Linux

```bash
cd /path/to/story-grabber/cli
python3 extract_cli.py setup
python3 extract_cli.py doctor
```

## Start

Windows:

```powershell
python web_server.py
```

or double-click:

```text
start_story_grabber.bat
```

Linux:

```bash
./start_story_grabber.sh
```

Open:

```text
http://127.0.0.1:8000
```

Different port:

```bash
python web_server.py --port 8080
```

## Recheck existing stories

From **Library**, click **Recheck formatting**, or run:

```bash
python story_formatter.py
```

Force formatting again:

```bash
python story_formatter.py --force
```

When upgrading an existing library from an older formatter version, run **Recheck formatting** once. The formatter version is stored per story, so later normal runs skip stories that are already current unless `--force` is used.

An older v2 SQLite library is migrated automatically on first v3 server start. New organization columns are added in place and existing verified stories are placed in the generated library tree.

## Important source files

```text
web_server.py                  local HTTP API, job control, library queries
auto_scrape.py                 incremental Auto crawl + scrape coordinator
story_pipeline.py              one shared raw extraction → formatting pipeline
story_formatter.py             readability formatter + exact verifier + romanizer
story_organizer.py             Part N grouping, category matching, folder organization
web/index.html                 four-page KISS frontend
web/app.js                     frontend routing/API behavior
web/app.css                    compact responsive UI
cli/site_crawler.py            resumable crawler with partial result saves
cli/parser/content_engine.mjs  existing story extraction engine
telugu_romanizer/              offline Telugu → Tenglish engine
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers:

- run-on dialogue separation,
- exact serialized-text fidelity,
- rejection of wording changes in manual formatting,
- `Part N` / `Pt. N` and bare `Story Name N` detection,
- conservative rejection of unrelated `Chapter N` grouping,
- category precedence,
- multipart parts staying in one category folder,
- partial crawler-output detection for Auto mode,
- scraper progress-state detection.

## Design constraints

The v3 changes intentionally follow KISS/DRY/SOLID/YAGNI principles:

- one crawler and one scraper pipeline are reused by both Auto and Manual modes;
- category logic and file organization live in one module (`story_organizer.py`);
- raw evidence is never mixed with the generated library view;
- multipart grouping supports `Part N`, `Pt. N`, and bare trailing sequence numbers such as `Story Name 1`;
- review and logs are contextual instead of adding more permanent navigation pages;
- auto organization is incremental, so a large library is not recopied after every scrape batch.


## v3.3.2 category priority

Category assignment is deterministic and two-stage:

1. Search configured category names in the story title. The first configured title match wins.
2. Only when the title has no match, search original/formatted/romanized story content.
3. If nothing matches, use `Uncategorized`.

The Library page includes **Re-categorize Library**. It reapplies this rule to all existing verified/review stories, rebuilds only `content_output/library/`, moves multipart groups together, and regenerates Kindle `library.json`. Raw, formatted and romanized source artifacts are not modified.
