from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from story_formatter import connect_library


class FtsTriggerSyncTests(unittest.TestCase):
    """The stories_fts external-content index (db_migrations.py migration 3)
    is only useful if it never drifts from the stories table it mirrors.
    These exercise the three paths that touch it: a plain INSERT, an
    INSERT ... ON CONFLICT DO UPDATE upsert (what story_formatter.py uses on
    every re-scrape), and a plain UPDATE (what /api/story/accept uses), plus
    DELETE.
    """

    def _match(self, conn, term):
        rows = conn.execute("SELECT url FROM stories_fts WHERE stories_fts MATCH ?", (term,)).fetchall()
        return sorted(r["url"] for r in rows)

    def test_insert_upsert_update_delete_all_stay_in_sync(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "story_library.sqlite3"
            conn = connect_library(db_path)
            try:
                conn.execute(
                    "INSERT INTO stories(url, title, original_text, updated_at) VALUES (?,?,?,?)",
                    ("https://a.example/1", "Moonlight", "a story about moonlight over water", "t1"),
                )
                conn.commit()
                self.assertEqual(self._match(conn, "moonlight"), ["https://a.example/1"])

                # upsert path (same shape story_formatter.process_one uses)
                conn.execute(
                    """
                    INSERT INTO stories(url, title, original_text, updated_at) VALUES (?,?,?,?)
                    ON CONFLICT(url) DO UPDATE SET title=excluded.title, original_text=excluded.original_text,
                        updated_at=excluded.updated_at
                    """,
                    ("https://a.example/1", "Sunrise", "a story about sunrise over mountains", "t2"),
                )
                conn.commit()
                self.assertEqual(self._match(conn, "moonlight"), [])
                self.assertEqual(self._match(conn, "sunrise"), ["https://a.example/1"])

                # plain UPDATE path (/api/story/accept style)
                conn.execute("UPDATE stories SET title=? WHERE url=?", ("Sunrise Over the Hills", "https://a.example/1"))
                conn.commit()
                self.assertEqual(self._match(conn, "hills"), ["https://a.example/1"])

                conn.execute("DELETE FROM stories WHERE url=?", ("https://a.example/1",))
                conn.commit()
                self.assertEqual(self._match(conn, "sunrise"), [])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
