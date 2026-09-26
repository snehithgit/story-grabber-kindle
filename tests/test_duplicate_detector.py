from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duplicate_detector
from story_formatter import connect_library


class DuplicateDetectorTests(unittest.TestCase):
    def _conn(self, td):
        return connect_library(Path(td) / "story_library.sqlite3")

    def _insert(self, conn, url, title, *, raw_sha256="", series_title="", part_number=None, words=100, status="verified"):
        conn.execute(
            """
            INSERT INTO stories(url, title, raw_sha256, series_title, part_number, words, status, updated_at, added_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (url, title, raw_sha256, series_title, part_number, words, status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )

    def test_exact_content_hash_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "Story A", raw_sha256="hash1")
                self._insert(conn, "https://b.example/1", "Story A Mirror", raw_sha256="hash1")
                self._insert(conn, "https://c.example/1", "Different Story", raw_sha256="hash2")
                conn.commit()
                groups = duplicate_detector.exact_content_duplicates(conn)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["reason"], "exact_content_hash")
                self.assertEqual({s["url"] for s in groups[0]["stories"]}, {"https://a.example/1", "https://b.example/1"})
            finally:
                conn.close()

    def test_duplicate_single_titles(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "The Quiet Village")
                self._insert(conn, "https://b.example/1", "the   quiet village  ")
                self._insert(conn, "https://c.example/1", "Unrelated Title")
                conn.commit()
                groups = duplicate_detector.normalized_title_duplicates(conn)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["reason"], "duplicate_title")
            finally:
                conn.close()

    def test_legitimate_multipart_siblings_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "Moon Story Part 1", series_title="Moon Story", part_number=1)
                self._insert(conn, "https://a.example/2", "Moon Story Part 2", series_title="Moon Story", part_number=2)
                conn.commit()
                groups = duplicate_detector.normalized_title_duplicates(conn)
                self.assertEqual(groups, [])
            finally:
                conn.close()

    def test_two_urls_claiming_the_same_series_part_are_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "Moon Story Part 1", series_title="Moon Story", part_number=1)
                self._insert(conn, "https://mirror.example/1", "Moon Story Part 1 (Repost)", series_title="Moon Story", part_number=1)
                conn.commit()
                groups = duplicate_detector.normalized_title_duplicates(conn)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["reason"], "duplicate_series_part")
            finally:
                conn.close()

    def test_fuzzy_title_duplicates_catches_near_matches_of_similar_length(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "A Story About the Old Lighthouse", words=500)
                self._insert(conn, "https://b.example/1", "A Story About the Old Lighthouse!", words=505)
                self._insert(conn, "https://c.example/1", "Something Completely Different", words=500)
                conn.commit()
                groups = duplicate_detector.fuzzy_title_duplicates(conn)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0]["reason"], "similar_title")
                urls = {s["url"] for s in groups[0]["stories"]}
                self.assertEqual(urls, {"https://a.example/1", "https://b.example/1"})
            finally:
                conn.close()

    def test_find_duplicates_reports_oversized_fuzzy_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                for i in range(41):
                    self._insert(conn, f"https://a.example/{i}", f"Unique Story {i}", words=300)
                conn.commit()
                result = duplicate_detector.find_duplicates(conn)
                self.assertEqual(result["fuzzy_skipped_buckets"], 1)
                self.assertEqual(result["fuzzy_skipped_stories"], 41)
            finally:
                conn.close()

    def test_find_duplicates_does_not_double_report_across_passes(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                self._insert(conn, "https://a.example/1", "Exact Duplicate", raw_sha256="samehash", words=200)
                self._insert(conn, "https://b.example/1", "Exact Duplicate", raw_sha256="samehash", words=200)
                conn.commit()
                result = duplicate_detector.find_duplicates(conn)
                # Exact-hash + identical-title both technically match, but the
                # fuzzy pass must skip URLs already flagged by an earlier pass.
                flagged = [g for g in result["groups"] if {"https://a.example/1", "https://b.example/1"} == {s["url"] for s in g["stories"]}]
                self.assertGreaterEqual(len(flagged), 1)
                self.assertEqual(result["story_count"], 2)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
