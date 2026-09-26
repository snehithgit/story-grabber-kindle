from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from story_organizer import ensure_story_columns, organize_urls, rebuild_library

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


class CategorySourceTests(unittest.TestCase):
    """v3.5 'category confidence/explanation': every categorized story should
    record *why* it landed where it did (title match, body match, or no
    match), not just the category name.
    """

    def _db(self, root: Path):
        conn = sqlite3.connect(root / "story_library.sqlite3")
        conn.row_factory = sqlite3.Row
        conn.execute(BASE_SCHEMA)
        ensure_story_columns(conn)
        return conn

    def test_organize_urls_persists_category_source_title_vs_content_vs_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            for name in ("a.html", "b.html", "c.html"):
                (root / "romanized_pages" / name).write_text("x", encoding="utf-8")
            conn = self._db(root)
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                [
                    ("https://x/1", "A College Story", "a.html", "no category words here", "no category words here"),
                    ("https://x/2", "An Ordinary Title", "b.html", "College appears only in the body", "College appears only in the body"),
                    ("https://x/3", "Nothing Related", "c.html", "still nothing related", "still nothing related"),
                ],
            )
            conn.commit()
            organize_urls(conn, root, ["College"], ["https://x/1", "https://x/2", "https://x/3"])
            rows = {r["url"]: r for r in conn.execute("SELECT url, category, category_source FROM stories")}
            self.assertEqual(rows["https://x/1"]["category"], "College")
            self.assertEqual(rows["https://x/1"]["category_source"], "title")
            self.assertEqual(rows["https://x/2"]["category"], "College")
            self.assertEqual(rows["https://x/2"]["category_source"], "content")
            self.assertEqual(rows["https://x/3"]["category"], "Uncategorized")
            self.assertEqual(rows["https://x/3"]["category_source"], "none")
            conn.close()

    def test_multipart_series_shares_one_category_source_across_all_parts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "p1.html").write_text("x", encoding="utf-8")
            (root / "romanized_pages" / "p2.html").write_text("x", encoding="utf-8")
            conn = self._db(root)
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                [
                    # Neither part's title matches; only part 1's body does.
                    # The whole series shares one category and one explanation.
                    ("https://x/1", "Moon Story Part 1", "p1.html", "College mentioned here", "College mentioned here"),
                    ("https://x/2", "Moon Story Part 2", "p2.html", "nothing relevant here", "nothing relevant here"),
                ],
            )
            conn.commit()
            organize_urls(conn, root, ["College"], ["https://x/1", "https://x/2"])
            rows = {r["url"]: r for r in conn.execute("SELECT url, category, category_source FROM stories")}
            self.assertEqual(rows["https://x/1"]["category"], "College")
            self.assertEqual(rows["https://x/2"]["category"], "College")
            self.assertEqual(rows["https://x/1"]["category_source"], "content")
            self.assertEqual(rows["https://x/2"]["category_source"], "content")
            conn.close()

    def test_rebuild_library_also_persists_category_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "a.html").write_text("x", encoding="utf-8")
            conn = self._db(root)
            conn.execute(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                ("https://x/1", "A Family Story", "a.html", "no match", "no match"),
            )
            conn.commit()
            rebuild_library(conn, root, ["Family"])
            row = conn.execute("SELECT category, category_source FROM stories WHERE url=?", ("https://x/1",)).fetchone()
            self.assertEqual(row["category"], "Family")
            self.assertEqual(row["category_source"], "title")
            conn.close()


if __name__ == "__main__":
    unittest.main()
