from __future__ import annotations

import sqlite3
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

import maintenance
from story_formatter import connect_library


def _seed(conn, output_dir: Path, *, with_files: bool = True):
    (output_dir / "pages").mkdir(parents=True, exist_ok=True)
    (output_dir / "formatted_pages").mkdir(parents=True, exist_ok=True)
    (output_dir / "romanized_pages").mkdir(parents=True, exist_ok=True)
    (output_dir / "library").mkdir(parents=True, exist_ok=True)
    if with_files:
        (output_dir / "pages" / "a.html").write_text("raw", encoding="utf-8")
        (output_dir / "formatted_pages" / "a.html").write_text("fmt", encoding="utf-8")
        (output_dir / "romanized_pages" / "a.html").write_text("rom", encoding="utf-8")
        (output_dir / "library" / "a.html").write_text("lib", encoding="utf-8")
    conn.execute(
        "INSERT INTO stories(url,title,status,raw_file,formatted_file,romanized_file,organized_file,"
        "original_text,romanized_text,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("https://x/1", "A Story", "verified", "a.html", "a.html", "a.html", "a.html", "x", "x", "2026-01-01T00:00:00Z"),
    )
    conn.commit()


class IntegrityCheckTests(unittest.TestCase):
    def test_clean_library_has_no_issues(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir)
            result = maintenance.integrity_check(conn, output_dir)
            self.assertEqual(result["issue_count"], 0)
            conn.close()

    def test_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir)
            (output_dir / "formatted_pages" / "a.html").unlink()
            result = maintenance.integrity_check(conn, output_dir)
            self.assertEqual(result["issue_count"], 1)
            self.assertEqual(result["issues"][0]["type"], "missing_formatted_file")
            conn.close()

    def test_orphan_file_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir)
            (output_dir / "pages" / "orphan.html").write_text("stray", encoding="utf-8")
            result = maintenance.integrity_check(conn, output_dir)
            self.assertEqual(result["by_type"].get("orphan_pages_file"), 1)
            conn.close()

    def test_duplicate_part_number_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir, with_files=False)
            conn.execute(
                "INSERT INTO stories(url,title,status,series_title,part_number,original_text,romanized_text,updated_at) "
                "VALUES('https://x/2','B','verified','A Story',NULL,'x','x','2026-01-01T00:00:00Z')"
            )
            conn.execute("UPDATE stories SET series_title='Moon', part_number=1 WHERE url='https://x/1'")
            conn.execute(
                "INSERT INTO stories(url,title,status,series_title,part_number,original_text,romanized_text,updated_at) "
                "VALUES('https://x/3','C','verified','Moon',1,'x','x','2026-01-01T00:00:00Z')"
            )
            conn.commit()
            result = maintenance.integrity_check(conn, output_dir)
            self.assertEqual(result["by_type"].get("duplicate_part_number"), 1)
            conn.close()


class RepairTests(unittest.TestCase):
    def test_repair_rebuilds_fts_index_without_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir)
            result = maintenance.repair(conn, output_dir, [], {})
            self.assertIn("fts_reindexed_rows", result)
            self.assertIn("organization", result)
            conn.close()


class BackupRestoreTests(unittest.TestCase):
    def test_backup_creates_zip_and_records_row_then_restores(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            backups_dir = root / "backups"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)

            result = maintenance.create_backup(conn, db_path, root, output_dir, backups_dir, keep=5, note="test")
            zip_path = backups_dir / result["filename"]
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path) as zf:
                self.assertIn("story_library.sqlite3", zf.namelist())

            backups = maintenance.list_backups(conn)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0]["filename"], result["filename"])
            conn.close()

            # Mutate the live DB, then restore the backup and confirm the
            # mutation is gone.
            conn = connect_library(db_path)
            conn.execute("UPDATE stories SET title='Changed' WHERE url='https://x/1'")
            conn.commit()
            conn.close()

            maintenance.restore_backup(root, output_dir, db_path, backups_dir, result["filename"])
            self.assertTrue(db_path.with_name(db_path.name + ".before-restore").is_file())
            conn = connect_library(db_path)
            row = conn.execute("SELECT title FROM stories WHERE url='https://x/1'").fetchone()
            self.assertEqual(row["title"], "A Story")
            conn.close()

    def test_before_restore_copy_includes_committed_wal_pages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            backups_dir = root / "backups"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)
            backup = maintenance.create_backup(conn, db_path, root, output_dir, backups_dir)

            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("UPDATE stories SET title='Changed in WAL' WHERE url='https://x/1'")
            conn.commit()
            self.assertTrue(db_path.with_name(db_path.name + "-wal").exists())

            maintenance.restore_backup(root, output_dir, db_path, backups_dir, backup["filename"])
            aside = db_path.with_name(db_path.name + ".before-restore")
            check = connect_library(aside)
            try:
                title = check.execute("SELECT title FROM stories WHERE url='https://x/1'").fetchone()[0]
                self.assertEqual(title, "Changed in WAL")
            finally:
                check.close()
                conn.close()

    def test_failed_backup_does_not_leave_a_stray_snapshot_file(self):
        # If the zip step blows up partway, the intermediate
        # .snapshot-*.sqlite3 file must still be cleaned up rather than left
        # sitting in backups_dir forever.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            backups_dir = root / "backups"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)

            with unittest.mock.patch("maintenance.zipfile.ZipFile", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    maintenance.create_backup(conn, db_path, root, output_dir, backups_dir)

            leftover = list(backups_dir.glob(".snapshot-*.sqlite3"))
            self.assertEqual(leftover, [])
            self.assertEqual(list(backups_dir.glob("backup-*.zip")), [])
            conn.close()


    def test_create_backup_rejects_corrupt_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            backups_dir = root / "backups"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)

            class _FakeCorruptCursor:
                def fetchone(self):
                    return ("*** possible corruption ***",)

            class _FakeIntegrityConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if isinstance(sql, str) and "integrity_check" in sql:
                        return _FakeCorruptCursor()
                    return super().execute(sql, *args, **kwargs)

            real_connect = sqlite3.connect

            def patched_connect(*args, **kwargs):
                kwargs["factory"] = _FakeIntegrityConnection
                return real_connect(*args, **kwargs)

            with unittest.mock.patch("maintenance.sqlite3.connect", side_effect=patched_connect):
                with self.assertRaises(RuntimeError):
                    maintenance.create_backup(conn, db_path, root, output_dir, backups_dir)

            self.assertEqual(list(backups_dir.glob(".snapshot-*.sqlite3")), [])
            self.assertEqual(list(backups_dir.glob("backup-*.zip")), [])
            self.assertEqual(maintenance.list_backups(conn), [])
            conn.close()

    def test_prune_backups_keeps_only_the_newest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            backups_dir = root / "backups"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)
            for _ in range(3):
                maintenance.create_backup(conn, db_path, root, output_dir, backups_dir, keep=2)
            remaining = maintenance.list_backups(conn)
            self.assertEqual(len(remaining), 2)
            self.assertEqual(len(list(backups_dir.glob("backup-*.zip"))), 2)
            conn.close()


class DbMaintenanceTests(unittest.TestCase):
    def test_analyze_and_vacuum_do_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            conn = connect_library(output_dir / "story_library.sqlite3")
            _seed(conn, output_dir)
            maintenance.analyze(conn)
            maintenance.vacuum(conn)
            conn.close()

    def test_storage_health_reports_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            db_path = output_dir / "story_library.sqlite3"
            conn = connect_library(db_path)
            _seed(conn, output_dir)
            health = maintenance.storage_health(conn, db_path, output_dir)
            self.assertEqual(health["story_count"], 1)
            self.assertGreater(health["database_bytes"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
