#!/usr/bin/env python3
"""Central schema/version manager for ``story_library.sqlite3``.

Before v3.4 every module (story_formatter.py, story_organizer.py,
kindle_export.py) added its own columns/tables ad hoc with
``ALTER TABLE ... ADD COLUMN`` guarded by ``PRAGMA table_info`` checks. That
worked, but every new feature had to remember which file owned the schema.

This module is now the single place new tables/columns are registered,
tracked with SQLite's built-in ``PRAGMA user_version`` counter so a brand new
database and one migrated in place from v3.3.x always end up with identical
schema. Migrations are strictly additive and idempotent: they never drop or
rewrite existing data, and running ``migrate()`` on an already-current
database costs one cheap PRAGMA read.

Adding a new migration later: append a new ``_migration_N_...`` function to
``MIGRATIONS`` and bump nothing else — the runner infers the version from the
list position.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

FTS_AVAILABLE = True


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migration_1_stories_base(conn: sqlite3.Connection) -> None:
    """Baseline ``stories`` table plus every column later releases bolted on."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stories (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source_host TEXT NOT NULL DEFAULT '',
            words INTEGER NOT NULL DEFAULT 0,
            raw_file TEXT NOT NULL DEFAULT '',
            formatted_file TEXT NOT NULL DEFAULT '',
            romanized_file TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            integrity_exact INTEGER NOT NULL DEFAULT 0,
            telugu INTEGER NOT NULL DEFAULT 0,
            romanized INTEGER NOT NULL DEFAULT 0,
            paragraphs INTEGER NOT NULL DEFAULT 0,
            dialogue_breaks INTEGER NOT NULL DEFAULT 0,
            dialogue_turns INTEGER NOT NULL DEFAULT 0,
            unsplit_dialogue INTEGER NOT NULL DEFAULT 0,
            max_paragraph_chars INTEGER NOT NULL DEFAULT 0,
            quality_pass INTEGER NOT NULL DEFAULT 0,
            formatter_version INTEGER NOT NULL DEFAULT 0,
            review_reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            raw_sha256 TEXT NOT NULL DEFAULT '',
            original_text TEXT NOT NULL DEFAULT '',
            formatted_text TEXT NOT NULL DEFAULT '',
            romanized_text TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            manual_accept INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _add_columns(conn, "stories", {
        "dialogue_turns": "INTEGER NOT NULL DEFAULT 0",
        "unsplit_dialogue": "INTEGER NOT NULL DEFAULT 0",
        "max_paragraph_chars": "INTEGER NOT NULL DEFAULT 0",
        "quality_pass": "INTEGER NOT NULL DEFAULT 0",
        "formatter_version": "INTEGER NOT NULL DEFAULT 0",
        "series_title": "TEXT NOT NULL DEFAULT ''",
        "part_number": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
        "organized_file": "TEXT NOT NULL DEFAULT ''",
        "added_at": "TEXT NOT NULL DEFAULT ''",
    })
    conn.execute("UPDATE stories SET added_at=updated_at WHERE added_at='' AND updated_at<>''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_status ON stories(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_source ON stories(source_host)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_updated ON stories(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_series ON stories(series_title, part_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_category ON stories(category)")


def _migration_2_links(conn: sqlite3.Connection) -> None:
    """v3.4 P0: crawler links move out of sublinks.json/manifest.json/progress.jsonl.

    ``links`` is kept in sync incrementally (see links_store.py) instead of
    being rebuilt from scratch on every request. ``import_state`` remembers a
    byte offset into progress.jsonl so re-syncing only tails new lines.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            url TEXT PRIMARY KEY,
            normalized_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL DEFAULT '',
            discovered_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _add_columns(conn, "links", {
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "TEXT NOT NULL DEFAULT ''",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_source ON links(source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_status ON links(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_updated ON links(updated_at)")
    # UNIQUE(normalized_url) enforces crawl dedup at the database level. Rows
    # are inserted with INSERT OR IGNORE (see links_store.py) so a second URL
    # that canonicalizes to an already-known link is silently dropped instead
    # of raising, while the constraint still guarantees no duplicates exist.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_links_normalized ON links(normalized_url)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS import_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )


def _create_fts_schema(conn: sqlite3.Connection) -> bool:
    """Create ``stories_fts`` and sync triggers when FTS5 is available.

    This is intentionally safe to call on every connection.  Migration 3 may
    have run previously on an SQLite build without FTS5; retrying here lets a
    later upgraded runtime self-heal instead of remaining on LIKE fallback.
    """
    global FTS_AVAILABLE
    if _table_exists(conn, "stories_fts"):
        FTS_AVAILABLE = True
        return True
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE stories_fts USING fts5(
                title, url, original_text, romanized_text,
                content='stories', content_rowid='rowid'
            )
            """
        )
    except sqlite3.OperationalError:
        FTS_AVAILABLE = False
        return False
    conn.execute(
        """
        CREATE TRIGGER stories_ai AFTER INSERT ON stories BEGIN
            INSERT INTO stories_fts(rowid, title, url, original_text, romanized_text)
            VALUES (new.rowid, new.title, new.url, new.original_text, new.romanized_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER stories_ad AFTER DELETE ON stories BEGIN
            INSERT INTO stories_fts(stories_fts, rowid, title, url, original_text, romanized_text)
            VALUES ('delete', old.rowid, old.title, old.url, old.original_text, old.romanized_text);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER stories_au AFTER UPDATE ON stories BEGIN
            INSERT INTO stories_fts(stories_fts, rowid, title, url, original_text, romanized_text)
            VALUES ('delete', old.rowid, old.title, old.url, old.original_text, old.romanized_text);
            INSERT INTO stories_fts(rowid, title, url, original_text, romanized_text)
            VALUES (new.rowid, new.title, new.url, new.original_text, new.romanized_text);
        END
        """
    )
    # A freshly created virtual table starts empty when migrating an existing
    # library, so backfill the current story rows once.
    total = int(conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0])
    if total:
        conn.execute(
            """
            INSERT INTO stories_fts(rowid, title, url, original_text, romanized_text)
            SELECT rowid, title, url, original_text, romanized_text FROM stories
            """
        )
    FTS_AVAILABLE = True
    return True


def _migration_3_fts(conn: sqlite3.Connection) -> None:
    """v3.4 P0: create the FTS5 full-text index when supported."""
    _create_fts_schema(conn)

def _migration_4_ops(conn: sqlite3.Connection) -> None:
    """v3.4/v3.6: job history, Kindle export change-tracking, backup log."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            command TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            finished_at TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kindle_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _migration_5_category_source(conn: sqlite3.Connection) -> None:
    """v3.5: persist *why* a story landed in a category (title/content/none),
    and let a bulk edit lock a category so automatic recategorization leaves
    it alone (see story_organizer.bulk_set_category)."""
    _add_columns(conn, "stories", {
        "category_source": "TEXT NOT NULL DEFAULT 'none'",
        "category_locked": "INTEGER NOT NULL DEFAULT 0",
    })


def _migration_6_source_change(conn: sqlite3.Connection) -> None:
    """v3.6: don't silently overwrite a manually-accepted story if the source changed."""
    _add_columns(conn, "stories", {
        "source_changed": "INTEGER NOT NULL DEFAULT 0",
        "previous_raw_sha256": "TEXT NOT NULL DEFAULT ''",
    })


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_1_stories_base,
    _migration_2_links,
    _migration_3_fts,
    _migration_4_ops,
    _migration_5_category_source,
    _migration_6_source_change,
]


def migrate(conn: sqlite3.Connection) -> int:
    """Bring ``conn`` up to ``len(MIGRATIONS)``. Returns how many steps ran."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    applied = 0
    for index, step in enumerate(MIGRATIONS, start=1):
        if index <= current:
            continue
        step(conn)
        conn.execute(f"PRAGMA user_version = {index}")
        applied += 1
    # Retry FTS5 schema creation on every connection.  This is a cheap no-op
    # once present and allows a database first opened under an SQLite build
    # without FTS5 to recover automatically after the runtime is upgraded.
    _create_fts_schema(conn)
    conn.commit()
    return applied


def fts_available(conn: sqlite3.Connection) -> bool:
    """Return whether this connection can actually execute an FTS5 MATCH."""
    if not _table_exists(conn, "stories_fts"):
        return False
    try:
        conn.execute(
            "SELECT rowid FROM stories_fts WHERE stories_fts MATCH ? LIMIT 1",
            ("__fts_probe__",),
        )
        return True
    except sqlite3.OperationalError:
        return False
