from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from story_organizer import (
    bulk_set_category, bulk_unlock_category, list_series, organize_urls, rebuild_library,
)
from story_formatter import connect_library


class BulkCategoryTests(unittest.TestCase):
    def _setup(self, td):
        root = Path(td)
        (root / "romanized_pages").mkdir()
        conn = connect_library(root / "story_library.sqlite3")
        return root, conn

    def _add(self, conn, root, url, title, filename, *, series_title="", part_number=None):
        (root / "romanized_pages" / filename).write_text(f"<p>{title}</p>", encoding="utf-8")
        conn.execute(
            "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at,series_title,part_number,status) "
            "VALUES(?,?,?,?,?,?,?,?,'verified')",
            (url, title, filename, title, title, "2026-01-01T00:00:00Z", series_title, part_number),
        )
        conn.commit()

    def test_bulk_set_category_locks_and_moves_a_single_story(self):
        with tempfile.TemporaryDirectory() as td:
            root, conn = self._setup(td)
            try:
                self._add(conn, root, "https://x/1", "A Story", "s1.html")
                organize_urls(conn, root, ["Family"], ["https://x/1"])
                row = conn.execute("SELECT category FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category"], "Uncategorized")

                bulk_set_category(conn, root, ["https://x/1"], "Romance")
                row = conn.execute("SELECT category, category_source, category_locked FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category"], "Romance")
                self.assertEqual(row["category_source"], "manual")
                self.assertEqual(row["category_locked"], 1)
                self.assertTrue((root / "library" / "Romance" / "A Story.html").is_file())

                # A later automatic pass must not move it back.
                organize_urls(conn, root, ["Family"], ["https://x/1"])
                row = conn.execute("SELECT category FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category"], "Romance")

                # rebuild_library (recategorize-all) must also respect the lock.
                rebuild_library(conn, root, ["Family"])
                row = conn.execute("SELECT category FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category"], "Romance")
            finally:
                conn.close()

    def test_bulk_set_category_pins_the_whole_series(self):
        with tempfile.TemporaryDirectory() as td:
            root, conn = self._setup(td)
            try:
                self._add(conn, root, "https://x/1", "Moon Story Part 1", "p1.html", series_title="Moon Story", part_number=1)
                self._add(conn, root, "https://x/2", "Moon Story Part 2", "p2.html", series_title="Moon Story", part_number=2)
                # Locking just part 1 should pin part 2 as well.
                bulk_set_category(conn, root, ["https://x/1"], "Thriller")
                rows = {r["url"]: r for r in conn.execute("SELECT url, category, category_locked FROM stories")}
                self.assertEqual(rows["https://x/1"]["category"], "Thriller")
                self.assertEqual(rows["https://x/2"]["category"], "Thriller")
                self.assertEqual(rows["https://x/2"]["category_locked"], 1)

                organize_urls(conn, root, ["Family"], ["https://x/2"])
                row = conn.execute("SELECT category FROM stories WHERE url=?", ("https://x/2",)).fetchone()
                self.assertEqual(row["category"], "Thriller")
            finally:
                conn.close()

    def test_bulk_unlock_category_returns_to_automatic_matching(self):
        with tempfile.TemporaryDirectory() as td:
            root, conn = self._setup(td)
            try:
                self._add(conn, root, "https://x/1", "A Family Story", "s1.html")
                bulk_set_category(conn, root, ["https://x/1"], "Manual Category")
                row = conn.execute("SELECT category_locked FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category_locked"], 1)

                bulk_unlock_category(conn, root, ["https://x/1"], ["Family"])
                row = conn.execute("SELECT category, category_locked FROM stories WHERE url=?", ("https://x/1",)).fetchone()
                self.assertEqual(row["category_locked"], 0)
                self.assertEqual(row["category"], "Family")
            finally:
                conn.close()


class SeriesManagerTests(unittest.TestCase):
    def test_list_series_reports_parts_and_missing_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conn = connect_library(root / "story_library.sqlite3")
            try:
                conn.executemany(
                    "INSERT INTO stories(url,title,series_title,part_number,category,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                    [
                        ("https://x/1", "Moon Story Part 1", "Moon Story", 1, "Family", "verified", "t"),
                        ("https://x/3", "Moon Story Part 3", "Moon Story", 3, "Family", "verified", "t"),
                        ("https://x/a", "Sun Story Part 1", "Sun Story", 1, "College", "verified", "t"),
                    ],
                )
                conn.commit()
                series = list_series(conn)
                self.assertEqual(len(series), 2)
                moon = next(s for s in series if s["series_title"] == "Moon Story")
                self.assertEqual(moon["part_count"], 2)
                self.assertEqual(moon["missing_parts"], [2])
                sun = next(s for s in series if s["series_title"] == "Sun Story")
                self.assertEqual(sun["missing_parts"], [])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
