# Story Grabber CLI

This folder is a self-contained command-line edition for Windows and Linux. It
contains the path-limited site crawler, persistent Cloudscraper fetch service,
Defuddle/Readability parser, dependency manifests, and one launcher.

## Requirements

- Python 3.10 or newer
- Node.js 20 or newer
- Git, used by `pip` to install the requested Cloudscraper repository

## Install

Windows PowerShell:

```powershell
cd "D:\docker config\extract\cli"
python extract_cli.py setup
python extract_cli.py doctor
```

Linux:

```bash
cd /path/to/extract/cli
python3 extract_cli.py setup
python3 extract_cli.py doctor
```

## 1. Extract links

Create `sites.txt` in your working directory with one starting URL per line,
then run:

```bash
python3 extract_cli.py links sites.txt -o sublinks.json --depth 1 --max-pages 1000
```

Use `--site-workers N` to crawl several starting sites concurrently. Sitemap
URLs are still fetched and validated as HTML and count toward `--max-pages`.

On Windows, use `python` instead of `python3` if that is how Python is
installed.

## 2. Extract clean HTML

```bash
python3 extract_cli.py content sublinks.json -o content_output
```

The launcher automatically selects Cloudscraper and HTML-only output. Clean
pages are saved under `content_output/pages`. A manifest preserves progress, so
the same command resumes an interrupted run. During a run, small updates are
appended to `progress.jsonl` and periodically compacted into `manifest.json`.

Useful options:

```bash
# Test ten links
python3 extract_cli.py content sublinks.json -o content_output --limit 10

# Reprocess successful links
python3 extract_cli.py content sublinks.json -o content_output --force

# Change worker count and per-host delay
python3 extract_cli.py content sublinks.json -o content_output --concurrency 4 --delay 500

# Also save structured JSON and Markdown
python3 extract_cli.py content sublinks.json -o content_output --no-html-only
```

Press `Ctrl+C` to stop. Completed HTML pages and the resume manifest remain
saved.

Cloudscraper uses its Node.js interpreter. Setup intentionally excludes the
unpatched `js2py` interpreter and installs Playwright Chromium for browser
fallback on Linux and on systems without Chrome or Edge. On Windows, an
installed Edge or Chrome is reused instead of downloading another browser.
