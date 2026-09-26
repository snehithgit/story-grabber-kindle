# Changelog

## v3.7.2 — Merged hardening release

- Keeps all v3.7.1 correctness/recovery fixes, including serialized maintenance/job startup, byte-safe journal sync, retry/checkpoint de-duplication, multipart category aliases, partial-backup cleanup, WAL-safe rollback copies, duplicate-scan coverage reporting, and accurate bulk/link counts.
- FTS5 is now self-healing: if an older runtime could not create the FTS index, later connections retry creation automatically after SQLite gains FTS5 support. `fts_available()` now performs a functional MATCH probe instead of trusting schema presence alone.
- Backup creation now runs `PRAGMA integrity_check` on the transactionally consistent SQLite snapshot before zipping/recording it; a corrupt snapshot is rejected and both the temporary snapshot and partial ZIP are removed.
- Added regression coverage for both merged hardening changes and the maintenance/job startup serialization gate.
- Test suite: 102 tests passing.

## v3.7.1 — Correctness and recovery fixes

- Human acceptance now clears stale `source_changed` / `previous_raw_sha256` state in both direct accept and manual-format paths.
- `progress.jsonl` is tailed with byte-safe `readline()` offsets, so a partially written final JSONL record is left for the next sync instead of crashing on `tell()`.
- Manifest checkpoints no longer replay the pre-truncate journal or inflate `retry_count`; snapshot imports do not count the same historical failure as a new attempt.
- Incremental multipart categorization now passes category aliases through the series-wide ranking pass, so aliases such as `tammudu` / `thamudu` remain in the canonical `Thammudu` category without requiring a full rebuild.
- Backup failure cleanup removes both the temporary SQLite snapshot and any partial ZIP. Pre-restore rollback copies now use SQLite's backup API so committed WAL pages are preserved.
- Destructive maintenance (restore, repair, VACUUM, re-categorization/category-rule rebuild) is serialized against engine startup to close the old check-then-start race.
- Job history marks runs left `running` by a previous server process as `interrupted` at startup.
- Site-profile text parsing ignores `#` comment lines even when they contain `=`.
- Duplicate scans now report oversized fuzzy buckets that were intentionally skipped instead of presenting an unqualified "0 duplicates" result.
- `sync_sublinks()` reports the exact number of rows inserted after canonical-URL collisions, bulk category messages distinguish affected records from files actually copied, and tests assert the crawler/app tracking-parameter sets stay aligned.
- Fixed an accidental double `connect_library()` in the Analyze maintenance route and a test-only unclosed SQLite connection.
- Test suite: 98 tests passing.

## v3.7.0 — Site intelligence

- **URL normalization at crawl time.** `cli/site_crawler.py`'s own `normalize_url()` now strips the same tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, ...) as `links_store.normalize_url()` and sorts the remaining query string, so two links that differ only by a tracking parameter or query-param order collapse to one crawl-queue entry instead of being fetched and stored twice.
- **Per-site crawl-delay profiles.** A new `settings.site_profiles` map (`{"slow-site.example": 3.0}`) lets you set a slower minimum delay for specific hosts, on top of the global crawl delay and any `robots.txt` `Crawl-delay`. Configurable in **Settings → Advanced crawler settings → Per-site crawl delay overrides**, and consumed by both `cli/site_crawler.py --site-profiles` and `auto_scrape.py --site-profiles`.
- Canonical URL dedup at the database layer (`links.normalized_url` UNIQUE index) already existed from v3.4 and is unchanged.
- **Out of scope, by design:** per-site CSS-selector extraction and cross-page pagination stitching, which live in the Node.js `cli/parser/content_engine.mjs` extraction engine, were intentionally not touched in this pass — see "Deferred" below.

## v3.6.0 — Reliability

- **Integrity checker** (`maintenance.integrity_check`): cross-checks every `stories` row against the files it references (raw/formatted/romanized/organized), flags orphaned files in those directories, and flags duplicate `(series_title, part_number)` combinations. Surfaced in Settings → Maintenance and via `GET /api/maintenance/integrity`.
- **Repair** (`maintenance.repair`): rebuilds only what can be safely re-derived from the database — the FTS5 search index, the crawler-link sync, and the organized `content_output/library/` tree. It never deletes a raw/formatted/romanized page; those are treated as source-of-truth evidence, and a re-scrape is the only correct way to replace one that's genuinely missing.
- **Backups** (`maintenance.create_backup` / `restore_backup` / `list_backups`): a consistent snapshot of the SQLite database (via SQLite's own online backup API, so it's never a torn copy even while the server is writing) plus `settings.json`, `sites.txt`, and the generated Kindle catalog, zipped and rotated (default: keep the newest 5). Raw HTML is intentionally excluded from backups — it's what a re-scrape recreates, and can be many GB. Restoring moves the current database aside as `story_library.sqlite3.before-restore` rather than deleting it.
- **Job run history** (`job_store.py`): every engine start/finish (crawler, content pipeline, formatter, auto mode) is now persisted to a `job_runs` table, not just held in memory, so history survives a server restart. Surfaced via `GET /api/jobs/history` and the Settings → Maintenance panel.
- **Transient vs. permanent failure classification** (`job_store.classify_error`): a failed link's error message is pattern-matched into transient (timeouts, 429/5xx, connection resets) or permanent (404/410, captcha) so **Retry failed links** (`/api/links/retry-failed`) only requeues the failures actually worth retrying.
- **Source-change detection** (`story_formatter.py`): if a story that was already `verified` or `review` gets re-scraped and its raw HTML content has genuinely changed (not just a formatter-version bump), the story is no longer silently overwritten. It's flipped back to `review` with an explanatory `review_reason`, and `source_changed` / `previous_raw_sha256` record what happened for the UI.
- **Maintenance panel** in Settings: integrity check, repair, analyze/vacuum, backup/restore, retry-failed-links, storage health, and job history, all in one place.
- `database maintenance`: `ANALYZE`/`VACUUM` wrappers and a storage-health summary (row counts, database size, per-directory disk usage).

## v3.5.0 — Library quality

- **Duplicate detection** (`duplicate_detector.py`): exact content-hash duplicates, normalized-title duplicates (single stories and multipart series claiming the same part number), and fuzzy near-duplicate titles (difflib, bucketed to keep it fast on a large library). Surfaced via `GET /api/library/duplicates` and the Library page's "Possible duplicates" panel.
- **Category confidence + explanation**: every story now records *why* it landed in its category (`category_source`: `title`, `content`, `manual`, or `none`), shown as a tooltip in the Library table and in the story reader.
- **Category locking**: a bulk "move to category" action (`bulk_set_category`) sets `category_locked=1`, which makes that category immune to future automatic recategorization passes (`rebuild_library`, `organize_urls`) — both for that story and, for multipart series, for every part. `bulk_unlock_category` returns a story (and its series) to automatic matching.
- **Series manager**: `story_organizer.list_series` groups multipart stories by series, reporting part count and any missing part numbers, surfaced in a new Library page panel.
- **Bulk category operations**: the Library page gained multi-select checkboxes and a bulk-action bar (move to category / return to automatic), backed by `POST /api/library/bulk`.

## v3.4.0 — Performance

- **Links moved from JSON to SQLite.** `links_store.py` keeps a `links` table in sync with `sublinks.json` (diff-imported, file-token guarded) and `manifest.json`/`progress.jsonl` (checkpoint + byte-offset-tailed journal, with truncation detection), replacing a full JSON re-parse and re-merge on every Dashboard/Sources request.
- **FTS5 full-text search** (`db_migrations.py` migration 3): an external-content FTS5 index over story title/URL/text, kept in sync by triggers on every insert/upsert/update/delete, with automatic fallback to `LIKE` search if the SQLite build lacks FTS5.
- **Indexed dashboard counters**: `links_store.dashboard_counts` and `web_server.story_counts_conn` replace the old O(n) Python merge of three files with indexed SQL aggregates.
- **Adaptive frontend polling** (`web/app.js`): job status polls at 1.5s while an engine is running and backs off to 6s while idle; polling pauses entirely while the tab is hidden and resumes immediately on return.
- **Incremental, debounced Kindle export** (`kindle_export.py`): `write_kindle_manifest` can skip a rewrite within `--kindle-min-interval` seconds of the last one (state persisted in a `kindle_state` table so it survives the fact that `auto_scrape.py` spawns a fresh subprocess per batch), and now emits a `changes.json` fingerprint diff (added/updated/removed story IDs) alongside the full manifest.
- **Central schema ownership** (`db_migrations.py`): every table/column the app has ever needed is applied through one idempotent, `PRAGMA user_version`-tracked migration list, so a v3.3.x database upgrades in place with no recrawl or rescrape required.

## Deferred (explicitly out of scope)

Two roadmap items live inside the Node.js extraction engine (`cli/parser/content_engine.mjs`), which has no test harness in this codebase:

- **Per-site CSS-selector profiles** for content extraction.
- **Cross-page pagination stitching** (combining a story split across multiple URLs into one).

Both were intentionally left untouched rather than edited blind. `settings.site_profiles` (v3.7) only covers crawl *rate*, not extraction *selectors* — a different, still-open concern for a future pass with proper test coverage for `content_engine.mjs` first.
