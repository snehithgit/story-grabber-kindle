from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from story_organizer import ensure_story_columns, match_category, organize_urls, parse_story_part


class StoryOrganizerTests(unittest.TestCase):
    def test_part_title_detection_is_conservative(self):
        self.assertEqual(parse_story_part("My Story Part 1"), ("My Story", 1))
        self.assertEqual(parse_story_part("My Story - Pt. 12"), ("My Story", 12))
        self.assertEqual(parse_story_part("My Story (Part 3)"), ("My Story", 3))
        self.assertEqual(parse_story_part("My Story 1"), ("My Story", 1))
        self.assertEqual(parse_story_part("My Story - 2"), ("My Story", 2))
        self.assertEqual(parse_story_part("My Story Chapter 3"), ("My Story Chapter 3", None))
        self.assertEqual(parse_story_part("My Story Episode 4"), ("My Story Episode 4", None))
        self.assertEqual(parse_story_part("My Story 2026"), ("My Story 2026", None))

    def test_category_uses_title_first_then_content(self):
        # Title phase wins even if an earlier configured category is present only in body.
        self.assertEqual(
            match_category(["College", "Family"], "A Family Story", "College appears in the body"),
            "Family",
        )
        # If title has no configured match, scan body using configured category order.
        self.assertEqual(
            match_category(["College", "Family"], "A Story", "Family and College are both in body"),
            "College",
        )
        self.assertEqual(match_category(["Romance"], "Unrelated title", "Unrelated text"), "Uncategorized")

    def test_multipart_parts_share_category_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "p1.html").write_text("part one", encoding="utf-8")
            (root / "romanized_pages" / "p2.html").write_text("part two", encoding="utf-8")
            conn = sqlite3.connect(root / "story_library.sqlite3")
            conn.row_factory = sqlite3.Row
            conn.execute("""
                CREATE TABLE stories (
                    url TEXT PRIMARY KEY,title TEXT NOT NULL,source_host TEXT NOT NULL DEFAULT '',words INTEGER NOT NULL DEFAULT 0,
                    raw_file TEXT NOT NULL DEFAULT '',formatted_file TEXT NOT NULL DEFAULT '',romanized_file TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'verified',integrity_exact INTEGER NOT NULL DEFAULT 1,telugu INTEGER NOT NULL DEFAULT 0,
                    romanized INTEGER NOT NULL DEFAULT 1,paragraphs INTEGER NOT NULL DEFAULT 0,dialogue_breaks INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',raw_sha256 TEXT NOT NULL DEFAULT '',
                    original_text TEXT NOT NULL DEFAULT '',formatted_text TEXT NOT NULL DEFAULT '',romanized_text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',manual_accept INTEGER NOT NULL DEFAULT 0
                )
            """)
            ensure_story_columns(conn)
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                [
                    ("https://x/1", "Moon Story Part 1", "p1.html", "Nothing special", "Nothing special"),
                    ("https://x/2", "Moon Story Part 2", "p2.html", "This is a College story", "This is a College story"),
                ],
            )
            conn.commit()
            result = organize_urls(conn, root, ["College"], ["https://x/1", "https://x/2"])
            self.assertEqual(result["multipart_groups"], 1)
            rows = conn.execute("SELECT part_number,category,organized_file FROM stories ORDER BY part_number").fetchall()
            self.assertEqual([row["category"] for row in rows], ["College", "College"])
            self.assertTrue((root / "library" / "College" / "Moon Story" / "Part 001.html").is_file())
            self.assertTrue((root / "library" / "College" / "Moon Story" / "Part 002.html").is_file())
            conn.close()

    def test_multipart_title_match_beats_body_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "p1.html").write_text("part one", encoding="utf-8")
            (root / "romanized_pages" / "p2.html").write_text("part two", encoding="utf-8")
            conn = sqlite3.connect(root / "story_library.sqlite3")
            conn.row_factory = sqlite3.Row
            conn.execute("""
                CREATE TABLE stories (
                    url TEXT PRIMARY KEY,title TEXT NOT NULL,source_host TEXT NOT NULL DEFAULT '',words INTEGER NOT NULL DEFAULT 0,
                    raw_file TEXT NOT NULL DEFAULT '',formatted_file TEXT NOT NULL DEFAULT '',romanized_file TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'verified',integrity_exact INTEGER NOT NULL DEFAULT 1,telugu INTEGER NOT NULL DEFAULT 0,
                    romanized INTEGER NOT NULL DEFAULT 1,paragraphs INTEGER NOT NULL DEFAULT 0,dialogue_breaks INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',raw_sha256 TEXT NOT NULL DEFAULT '',
                    original_text TEXT NOT NULL DEFAULT '',formatted_text TEXT NOT NULL DEFAULT '',romanized_text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',manual_accept INTEGER NOT NULL DEFAULT 0
                )
            """)
            ensure_story_columns(conn)
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                [
                    ("https://x/1", "Family Moon Part 1", "p1.html", "College is in body", "College is in body"),
                    ("https://x/2", "Family Moon Part 2", "p2.html", "College is also in body", "College is also in body"),
                ],
            )
            conn.commit()
            organize_urls(conn, root, ["College", "Family"], ["https://x/1", "https://x/2"])
            rows = conn.execute("SELECT category FROM stories ORDER BY part_number").fetchall()
            self.assertEqual([row["category"] for row in rows], ["Family", "Family"])
            self.assertTrue((root / "library" / "Family" / "Family Moon" / "Part 001.html").is_file())
            self.assertTrue((root / "library" / "Family" / "Family Moon" / "Part 002.html").is_file())
            conn.close()

    def test_bare_number_parts_share_series_folder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "p1.html").write_text("part one", encoding="utf-8")
            (root / "romanized_pages" / "p2.html").write_text("part two", encoding="utf-8")
            conn = sqlite3.connect(root / "story_library.sqlite3")
            conn.row_factory = sqlite3.Row
            conn.execute("""
                CREATE TABLE stories (
                    url TEXT PRIMARY KEY,title TEXT NOT NULL,source_host TEXT NOT NULL DEFAULT '',words INTEGER NOT NULL DEFAULT 0,
                    raw_file TEXT NOT NULL DEFAULT '',formatted_file TEXT NOT NULL DEFAULT '',romanized_file TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'verified',integrity_exact INTEGER NOT NULL DEFAULT 1,telugu INTEGER NOT NULL DEFAULT 0,
                    romanized INTEGER NOT NULL DEFAULT 1,paragraphs INTEGER NOT NULL DEFAULT 0,dialogue_breaks INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',raw_sha256 TEXT NOT NULL DEFAULT '',
                    original_text TEXT NOT NULL DEFAULT '',formatted_text TEXT NOT NULL DEFAULT '',romanized_text TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',manual_accept INTEGER NOT NULL DEFAULT 0
                )
            """)
            ensure_story_columns(conn)
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text) VALUES(?,?,?,?,?)",
                [
                    ("https://x/1", "Moon Story 1", "p1.html", "Nothing special", "Nothing special"),
                    ("https://x/2", "Moon Story 2", "p2.html", "This is a College story", "This is a College story"),
                ],
            )
            conn.commit()
            result = organize_urls(conn, root, ["College"], ["https://x/1", "https://x/2"])
            self.assertEqual(result["multipart_groups"], 1)
            rows = conn.execute("SELECT series_title,part_number,category FROM stories ORDER BY part_number").fetchall()
            self.assertEqual([row["series_title"] for row in rows], ["Moon Story", "Moon Story"])
            self.assertEqual([row["part_number"] for row in rows], [1, 2])
            self.assertEqual([row["category"] for row in rows], ["College", "College"])
            self.assertTrue((root / "library" / "College" / "Moon Story" / "Part 001.html").is_file())
            self.assertTrue((root / "library" / "College" / "Moon Story" / "Part 002.html").is_file())
            conn.close()


if __name__ == "__main__":
    unittest.main()
