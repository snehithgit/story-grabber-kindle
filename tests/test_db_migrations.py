from __future__ import annotations

import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import db_migrations
from story_formatter import connect_library


class DbMigrationsTests(unittest.TestCase):
    def test_fresh_database_reaches_current_version(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"
            conn = connect_library(db_path)
            try:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(version, len(db_migrations.MIGRATIONS))
                for table in ("stories", "links", "import_state", "job_runs", "kindle_state", "backups"):
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    self.assertIsNotNone(exists, f"expected table {table} to exist")
            finally:
                conn.close()

    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"
            conn = connect_library(db_path)
            try:
                applied_again = db_migrations.migrate(conn)
                self.assertEqual(applied_again, 0)
            finally:
                conn.close()

    def test_old_database_without_new_tables_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"
            # Simulate a v3.3.x database: only the original stories columns,
            # no user_version bookkeeping, some real data already in it.
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE stories (
                    url TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                    source_host TEXT NOT NULL DEFAULT '', words INTEGER NOT NULL DEFAULT 0,
                    raw_file TEXT NOT NULL DEFAULT '', formatted_file TEXT NOT NULL DEFAULT '',
                    romanized_file TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                    integrity_exact INTEGER NOT NULL DEFAULT 0, telugu INTEGER NOT NULL DEFAULT 0,
                    romanized INTEGER NOT NULL DEFAULT 0, paragraphs INTEGER NOT NULL DEFAULT 0,
                    dialogue_breaks INTEGER NOT NULL DEFAULT 0, review_reason TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', raw_sha256 TEXT NOT NULL DEFAULT '',
                    original_text TEXT NOT NULL DEFAULT '', formatted_text TEXT NOT NULL DEFAULT '',
                    romanized_text TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                    manual_accept INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT INTO stories(url,title,original_text,romanized_text,updated_at) "
                "VALUES('https://example.com/a','A Story','hello','hello','2026-01-01T00:00:00Z')"
            )
            conn.commit()
            conn.close()

            conn = connect_library(db_path)
            try:
                row = conn.execute("SELECT * FROM stories WHERE url=?", ("https://example.com/a",)).fetchone()
                self.assertEqual(row["title"], "A Story")
                self.assertEqual(row["category"], "Uncategorized")  # column added by migration
                self.assertTrue(db_migrations.fts_available(conn))
                hits = conn.execute("SELECT url FROM stories_fts WHERE stories_fts MATCH 'hello'").fetchall()
                self.assertEqual([h["url"] for h in hits], ["https://example.com/a"])
            finally:
                conn.close()


class FtsSelfHealingTests(unittest.TestCase):
    def test_fts_available_checks_functionality_not_only_schema_presence(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"
            conn = connect_library(db_path)
            try:
                self.assertTrue(db_migrations.fts_available(conn))
                conn.execute("DROP TABLE stories_fts")
                conn.execute("CREATE TABLE stories_fts(rowid INTEGER, title TEXT)")
                conn.commit()
                self.assertFalse(db_migrations.fts_available(conn))
            finally:
                conn.close()

    def test_fts_creation_retries_after_earlier_runtime_lacked_fts5(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"

            class _FailingFTSConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if isinstance(sql, str) and "CREATE VIRTUAL TABLE stories_fts" in sql:
                        raise sqlite3.OperationalError("no such module: fts5")
                    return super().execute(sql, *args, **kwargs)

            real_connect = sqlite3.connect

            def failing_connect(*args, **kwargs):
                kwargs["factory"] = _FailingFTSConnection
                return real_connect(*args, **kwargs)

            with unittest.mock.patch("sqlite3.connect", side_effect=failing_connect):
                conn = connect_library(db_path)
                try:
                    self.assertFalse(db_migrations.fts_available(conn))
                    self.assertGreaterEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 3)
                finally:
                    conn.close()

            conn = connect_library(db_path)
            try:
                self.assertTrue(db_migrations.fts_available(conn))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
