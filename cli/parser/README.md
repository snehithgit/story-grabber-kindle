# Lightweight Sublink Content Parser

Parser stack:

- Defuddle — https://github.com/kepano/defuddle
- Mozilla Readability — https://github.com/mozilla/readability
- LinkeDOM — https://github.com/WebReflection/linkedom

This is deliberately a parser engine, not a browser.

## Install

```powershell
npm ci
```

## Parse one already-fetched HTML page

```powershell
node parser_engine.mjs --html page.html --url-context "https://example.com/page/" -o parsed.json
```

## Parse HTML through stdin

PowerShell:

```powershell
Get-Content page.html -Raw | node parser_engine.mjs --stdin --url-context "https://example.com/page/" -o parsed.json
```

## Lightweight direct URL test

```powershell
node parser_engine.mjs --url "https://example.com/page/" -o parsed.json
```

The URL mode uses ordinary HTTP fetch only. It does not bypass interactive verification.

## Output

The JSON contains:

- metadata
- clean main text
- Markdown
- cleaned HTML
- ordered blocks
  - heading
  - paragraph
  - list
  - table
  - quote
  - code
  - image
- links
- images
- statistics
- parser diagnostics

## Integration API

```javascript
import { parseContent } from "./parser_engine.mjs";

const result = await parseContent(html, pageUrl);
```

## Batch link content engine

`content_engine.mjs` turns the crawler's TXT, JSON, or JSONL link output into a
resumable content dataset. It uses bounded parallelism, per-host pacing,
transient-error retries, request timeouts, and a maximum response-size limit.
Its default `auto` fetch mode starts with lightweight HTTP and switches a host
to persistent Cloudscraper sessions when the server returns 401, 403, or 429.

Use Cloudscraper directly for protected sites:

```powershell
node content_engine.mjs ..\sublinks.json -o ..\content_output --fetch-mode cloudscraper --html-only
```

From the `parser` directory:

```powershell
node content_engine.mjs ..\sublinks.json -o ..\content_output
```

Test a small batch first:

```powershell
node content_engine.mjs ..\sublinks.json -o ..\content_output --limit 10
```

Interrupted runs resume automatically. Successful URLs in `manifest.json` are
skipped. Use `--retry-failed` to retry only failed pages, or `--force` to parse
everything again.

```powershell
node content_engine.mjs ..\sublinks.json -o ..\content_output --retry-failed
```

If a site rejects ordinary requests, fetch failed pages directly through the
browser:

```powershell
node content_engine.mjs ..\sublinks.json -o ..\content_output --fetch-mode browser --retry-failed
```

On Windows the default browser is a headed Edge/Chrome window positioned
off-screen; Linux defaults to bundled headless Chromium. If a
verification page requires user interaction, rerun a small batch with
`--browser-mode visible`, complete the verification once, then reuse the saved
`content_browser_profile` in background mode.

The output directory contains:

- `manifest.json`: atomic resume state and summary
- `progress.jsonl`: live crash-safe updates, compacted into the manifest
- `results.jsonl`: compact one-record-per-link index
- `pages/*.json`: metadata, text, Markdown, HTML, blocks, links, and images
- `pages/*.md`: clean Markdown content
- `pages/*.html`: minimal standalone pages containing only the title and
  semantic article content; scripts, navigation, ads, images, and page chrome
  are omitted

Pass `--html-only` to omit the per-page JSON and Markdown files. The web server
uses this mode so its `content_output/pages` directory contains clean HTML only.

Run `node content_engine.mjs --help` for tuning options including concurrency,
delay, timeout, retries, page-size limit, User-Agent, and dry-run mode.
