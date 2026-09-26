#!/usr/bin/env python3
"""v3.4 P0 / v3.7: crawler links live in SQLite instead of being re-parsed.

Before this module, every Dashboard/Sources request re-read sublinks.json in
full, re-read manifest.json in full, replayed the entirety of progress.jsonl,
and merged all three in Python -- for every request, every 8 seconds, no
matter how many of the (potentially 100k+) links were actually new. That is
fine at 1,000 links and painfully slow at 100,000.

This module keeps a ``links`` table (schema in db_migrations.py) in sync with
those same three files using two independent, resumable diff-imports:

* :func:`sync_sublinks` -- sublinks.json is rewritten wholesale by the
  crawler on every progress tick, but we only care about the URLs, which are
  cheap to diff against what's already stored. Only genuinely new links are
  inserted; nothing already in the table is re-written. Guarded by a
  (mtime, size) file token so an unchanged file costs one stat() call.
* :func:`sync_progress` -- manifest.json is a periodic full checkpoint (also
  token-guarded); progress.jsonl is an append-only journal between
  checkpoints, so it is *tailed* from a saved byte offset rather than
  re-read from the start. The engine truncates progress.jsonl right after
  each manifest checkpoint, which we detect (current size < saved offset)
  and treat as "start tailing from zero again".

Everything downstream (the Dashboard summary, the Sources/Links screen, the
retry-failed action) then reads only ``links`` with indexed SQL instead of
walking Python lists.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "gclid", "fbclid", "msclkid",
    "mc_cid", "mc_eid", "ref", "ref_src", "refsrc", "igshid", "spm", "_ga",
    "yclid", "vero_id",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """Canonicalize a URL for crawl dedup.

    ``/story?id=123&utm_source=x`` and ``/story?id=123`` normalize to the
    same value: tracking parameters and the fragment are dropped, remaining
    query parameters are sorted for a stable ordering, the host is
    lower-cased, and a trailing slash on a non-root path is removed. This is
    intentionally conservative -- it never reorders path segments or guesses
    at site-specific ID parameters -- so it only catches the common tracking
    and formatting noise, never two genuinely different pages.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)
    return urlunsplit((scheme, netloc, path, query, ""))


def _file_token(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return ""


def _get_state(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM import_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO import_state(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _iter_sublink_entries(sublinks_path: Path):
    payload = load_json(sublinks_path, [])
    groups = payload if isinstance(payload, list) else [payload]
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        source = str(group.get("main_site") or group.get("site") or group.get("url") or f"Source {index + 1}")
        for entry in group.get("sublinks", []) or []:
            if isinstance(entry, str):
                url, title = entry, entry
            elif isinstance(entry, dict):
                url = str(entry.get("link") or entry.get("url") or "")
                title = str(entry.get("title") or entry.get("name") or url)
            else:
                continue
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            yield url, title, source, (parsed.hostname or "").lower()


def sync_sublinks(conn: sqlite3.Connection, sublinks_path: Path, *, force: bool = False) -> int:
    """Insert newly discovered links from sublinks.json. Returns rows inserted."""
    token = _file_token(sublinks_path)
    state_key = "sublinks_token"
    if not force and token and token == _get_state(conn, state_key):
        return 0

    existing = {row[0] for row in conn.execute("SELECT url FROM links")}
    now = utc_now()
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    seen: set[str] = set()
    for url, title, source, host in _iter_sublink_entries(sublinks_path):
        if url in existing or url in seen:
            continue
        seen.add(url)
        rows.append((url, normalize_url(url), title, source, host, now, now))

    inserted = 0
    if rows:
        # ``cursor.rowcount`` is not a reliable inserted-row count for every
        # sqlite3/executemany + INSERT OR IGNORE combination.  total_changes
        # gives the exact number of rows that actually made it past the
        # normalized_url UNIQUE constraint.
        before_changes = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO links
                (url, normalized_url, title, source, host, discovered_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            rows,
        )
        inserted = conn.total_changes - before_changes
    if token:
        _set_state(conn, state_key, token)
    conn.commit()
    return inserted


def _apply_state(
    conn: sqlite3.Connection, url: str, state: dict[str, Any], when: str, *, count_failure: bool = True,
) -> None:
    """Apply one crawler state record to ``links``.

    Journal records represent individual attempts, so a failed journal event
    increments ``retry_count``.  ``manifest.json`` is only a full snapshot;
    replaying a newer checkpoint must not count the same historical failure
    again.  A first-ever failed snapshot is still recorded as one failure.
    """
    raw_status = state.get("status") if isinstance(state, dict) else None
    status = "scraped" if raw_status == "success" else "failed" if raw_status == "failed" else "pending"
    error = str(state.get("error") or "") if isinstance(state, dict) else ""
    row = conn.execute("SELECT retry_count FROM links WHERE url=?", (url,)).fetchone()
    if row is None:
        retry_count = 1 if status == "failed" else 0
        conn.execute(
            """
            INSERT OR IGNORE INTO links(
                url, normalized_url, title, source, host, status, error, retry_count, discovered_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                url, normalize_url(url), str(state.get("title") or url), "", "", status, error,
                retry_count, when, when,
            ),
        )
        return
    retry_count = int(row["retry_count"] or 0)
    if status == "failed" and count_failure:
        retry_count += 1
    conn.execute(
        "UPDATE links SET status=?, error=?, retry_count=?, updated_at=? WHERE url=?",
        (status, error, retry_count, when, url),
    )


def sync_progress(conn: sqlite3.Connection, output_dir: Path, *, force: bool = False) -> dict[str, int]:
    """Apply manifest checkpoint then tail complete journal records by byte offset.

    The Node writer deliberately writes the new manifest *before* truncating
    ``progress.jsonl`` for crash safety.  A reader can therefore briefly see
    the new checkpoint together with the old journal.  We distinguish that
    pre-truncate journal by mtime and skip it because its records are already
    represented by the manifest.  If the journal was truncated/rewritten
    after the manifest, it is tailed from byte zero.

    Binary ``readline`` is intentional: saved offsets are real byte offsets,
    and an incomplete final line is never consumed until its terminating
    newline arrives.
    """
    manifest_path = output_dir / "manifest.json"
    progress_path = output_dir / "progress.jsonl"
    now = utc_now()
    applied = 0

    stored_offset = int(_get_state(conn, "progress_offset") or "0")
    offset = stored_offset
    manifest_token = _file_token(manifest_path)
    manifest_changed = bool(manifest_token and (force or manifest_token != _get_state(conn, "manifest_token")))

    if manifest_changed:
        manifest = load_json(manifest_path, {"pages": {}})
        pages = manifest.get("pages", {}) if isinstance(manifest, dict) else {}
        for url, state in pages.items():
            if isinstance(state, dict):
                _apply_state(conn, url, state, now, count_failure=False)
                applied += 1
        _set_state(conn, "manifest_token", manifest_token)

        try:
            manifest_mtime = manifest_path.stat().st_mtime_ns
        except OSError:
            manifest_mtime = 0
        try:
            progress_stat = progress_path.stat()
        except OSError:
            progress_stat = None

        if progress_stat is None or progress_stat.st_size == 0:
            offset = 0
        elif progress_stat.st_mtime_ns <= manifest_mtime:
            # Writer has published the checkpoint but has not truncated the
            # old journal yet.  Every existing line is already in manifest.
            offset = progress_stat.st_size
        else:
            # Writer's serialized checkpoint does truncate only after the
            # manifest is durable.  A newer journal mtime therefore means the
            # truncate happened and any current records are post-checkpoint.
            offset = 0
        _set_state(conn, "progress_offset", str(offset))

    try:
        size = progress_path.stat().st_size
    except OSError:
        size = 0
    if size < offset:
        offset = 0  # journal was truncated by a checkpoint since our last read

    if size > offset:
        new_offset = offset
        with open(progress_path, "rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    # Partial record still being written.  Do not advance the
                    # checkpoint past it; the next sync will read it again.
                    new_offset = line_start
                    break
                new_offset = handle.tell()
                try:
                    entry = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("url") and isinstance(entry.get("state"), dict):
                    _apply_state(conn, entry["url"], entry["state"], now, count_failure=True)
                    applied += 1
        _set_state(conn, "progress_offset", str(new_offset))

    conn.commit()
    return {"applied": applied}


def sync_all(conn: sqlite3.Connection, sublinks_path: Path, output_dir: Path) -> dict[str, int]:
    inserted = sync_sublinks(conn, sublinks_path)
    progress = sync_progress(conn, output_dir)
    return {"inserted": inserted, **progress}


def dashboard_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """Indexed aggregate counts -- replaces looping every discovered link."""
    totals = {"links": 0, "pending": 0, "scraped": 0, "failed": 0}
    for status, count in conn.execute("SELECT status, COUNT(*) FROM links GROUP BY status"):
        totals["links"] += int(count)
        if status in totals:
            totals[status] = int(count)
    sources: dict[str, dict[str, int]] = {}
    for source, status, count in conn.execute(
        "SELECT source, status, COUNT(*) FROM links GROUP BY source, status"
    ):
        bucket = sources.setdefault(source or "", {"source": source or "", "links": 0, "scraped": 0, "failed": 0, "pending": 0})
        bucket["links"] += int(count)
        if status in bucket:
            bucket[status] = int(count)
    return {**totals, "sources": list(sources.values())}


def list_links_db(
    conn: sqlite3.Connection, *, q: str = "", status: str = "all", source: str = "",
    page: int = 1, per_page: int = 50,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status in {"scraped", "failed", "pending"}:
        where.append("status=?")
        params.append(status)
    if source:
        where.append("source=?")
        params.append(source)
    q = q.strip()
    if q:
        like = f"%{q}%"
        where.append("(title LIKE ? OR url LIKE ?)")
        params.extend([like, like])
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = int(conn.execute(f"SELECT COUNT(*) FROM links{clause}", params).fetchone()[0])
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT url, title, source, host, status, error, retry_count FROM links{clause} "
        f"ORDER BY discovered_at DESC LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total else 0,
    }


def retryable_failed(conn: sqlite3.Connection) -> list[str]:
    """URLs whose most recent failure looks transient (see job_store.classify_error)."""
    from job_store import is_transient_error  # local import avoids a cycle at module load

    urls: list[str] = []
    for row in conn.execute("SELECT url, error FROM links WHERE status='failed'"):
        if is_transient_error(row["error"]):
            urls.append(row["url"])
    return urls
