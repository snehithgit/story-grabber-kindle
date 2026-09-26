from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kindle_export import write_kindle_manifest
from story_organizer import ensure_story_columns


BASE_SCHEMA = """
CREATE TABLE stories (
    url TEXT PRIMARY KEY,title TEXT NOT NULL,source_host TEXT NOT NULL DEFAULT '',words INTEGER NOT NULL DEFAULT 0,
    raw_file TEXT NOT NULL DEFAULT '',formatted_file TEXT NOT NULL DEFAULT '',romanized_file TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'verified',integrity_exact INTEGER NOT NULL DEFAULT 1,telugu INTEGER NOT NULL DEFAULT 0,
    romanized INTEGER NOT NULL DEFAULT 1,paragraphs INTEGER NOT NULL DEFAULT 0,dialogue_breaks INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',raw_sha256 TEXT NOT NULL DEFAULT '',
    original_text TEXT NOT NULL DEFAULT '',formatted_text TEXT NOT NULL DEFAULT '',romanized_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',manual_accept INTEGER NOT NULL DEFAULT 0
)
"""


class KindleDebounceAndChangesTests(unittest.TestCase):
    def _db(self, root: Path):
        conn = sqlite3.connect(root / "story_library.sqlite3")
        conn.row_factory = sqlite3.Row
        conn.execute(BASE_SCHEMA)
        ensure_story_columns(conn)
        return conn

    def _add_story(self, conn, url, title, filename, romanized_dir):
        (romanized_dir / filename).write_text(f"<p>{title}</p>", encoding="utf-8")
        conn.execute(
            "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at,organized_file) "
            "VALUES(?,?,?,?,?,?,?)",
            (url, title, filename, title, title, "2026-01-01T00:00:00Z", filename),
        )
        conn.commit()

    def test_min_interval_skips_rewrite_until_elapsed_then_catches_up(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            romanized_dir = root / "romanized_pages"
            romanized_dir.mkdir()
            conn = self._db(root)
            self._add_story(conn, "https://x/1", "Story One", "s1.html", romanized_dir)
            first = write_kindle_manifest(conn, root, min_interval=60)
            self.assertEqual(first["kindle_stories"], 1)

            # A second story appears, but we call again inside the debounce
            # window: the on-disk manifest must NOT be rewritten (it should
            # still describe only the first story), and the cached counts
            # from the *first* write are returned as-is.
            self._add_story(conn, "https://x/2", "Story Two", "s2.html", romanized_dir)
            second = write_kindle_manifest(conn, root, min_interval=60)
            self.assertEqual(second, first)
            on_disk = json.loads((root / "library" / "library.json").read_text("utf-8"))
            self.assertEqual(on_disk["storyCount"], 1)

            # force=True (used by explicit rebuilds) always bypasses the guard.
            third = write_kindle_manifest(conn, root, min_interval=60, force=True)
            self.assertEqual(third["kindle_stories"], 2)
            on_disk = json.loads((root / "library" / "library.json").read_text("utf-8"))
            self.assertEqual(on_disk["storyCount"], 2)
            conn.close()

    def test_changes_json_tracks_added_updated_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            romanized_dir = root / "romanized_pages"
            romanized_dir.mkdir()
            conn = self._db(root)
            self._add_story(conn, "https://x/1", "Story One", "s1.html", romanized_dir)
            write_kindle_manifest(conn, root)
            changes = json.loads((root / "library" / "changes.json").read_text("utf-8"))
            self.assertEqual(changes["added"], 1)
            self.assertEqual(changes["updated"], 0)
            self.assertEqual(changes["removed"], 0)

            self._add_story(conn, "https://x/2", "Story Two", "s2.html", romanized_dir)
            conn.execute("DELETE FROM stories WHERE url=?", ("https://x/1",))
            conn.commit()
            write_kindle_manifest(conn, root)
            changes = json.loads((root / "library" / "changes.json").read_text("utf-8"))
            self.assertEqual(changes["added"], 1)
            self.assertEqual(changes["removed"], 1)
            self.assertEqual(changes["updated"], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
