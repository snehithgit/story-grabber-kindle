from __future__ import annotations

import json
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


class KindleExportTests(unittest.TestCase):
    def _db(self, root: Path):
        conn = sqlite3.connect(root / "story_library.sqlite3")
        conn.row_factory = sqlite3.Row
        conn.execute(BASE_SCHEMA)
        ensure_story_columns(conn)
        return conn

    def test_manifest_groups_parts_and_uses_relative_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            for name in ("p1.html", "p2.html", "single.html"):
                (root / "romanized_pages" / name).write_text(f"<p>{name}</p>", encoding="utf-8")
            conn = self._db(root)
            rows = [
                ("https://x/moon-1", "Moon Story Part 1", "p1.html", "College", "2026-09-20T10:00:00+00:00"),
                ("https://x/moon-2", "Moon Story Part 2", "p2.html", "College", "2026-09-25T10:00:00+00:00"),
                ("https://x/one", "One Story", "single.html", "Family", "2026-09-22T10:00:00+00:00"),
            ]
            conn.executemany(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at) VALUES(?,?,?,?,?,?)",
                [(u, t, f, c, c, dt) for u, t, f, c, dt in rows],
            )
            conn.commit()
            rebuild_library(conn, root, ["College", "Family"])
            manifest = json.loads((root / "library" / "library.json").read_text("utf-8"))
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["storyCount"], 2)
            self.assertEqual(manifest["partCount"], 3)
            moon = next(x for x in manifest["stories"] if x["title"] == "Moon Story")
            self.assertEqual(moon["category"], "College")
            self.assertTrue(moon["id"].startswith("series-"))
            self.assertEqual([p["number"] for p in moon["parts"]], [1, 2])
            self.assertEqual(moon["parts"][0]["path"], "College/Moon Story/Part 001.html")
            self.assertGreater(moon["addedAt"], 0)
            single = next(x for x in manifest["stories"] if x["title"] == "One Story")
            self.assertTrue(single["id"].startswith("story-"))
            self.assertEqual(single["parts"][0]["path"], "Family/One Story.html")
            conn.close()

    def test_incremental_update_refreshes_manifest_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "p1.html").write_text("part one", encoding="utf-8")
            (root / "romanized_pages" / "p2.html").write_text("part two", encoding="utf-8")
            conn = self._db(root)
            conn.execute(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at) VALUES(?,?,?,?,?,?)",
                ("https://x/moon-1", "Moon Story Part 1", "p1.html", "College", "College", "2026-09-20T10:00:00+00:00"),
            )
            conn.commit()
            organize_urls(conn, root, ["College"], ["https://x/moon-1"])
            first = json.loads((root / "library" / "library.json").read_text("utf-8"))["stories"][0]

            conn.execute(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at) VALUES(?,?,?,?,?,?)",
                ("https://x/moon-2", "Moon Story Part 2", "p2.html", "College", "College", "2026-09-25T10:00:00+00:00"),
            )
            conn.commit()
            organize_urls(conn, root, ["College"], ["https://x/moon-2"])
            second = json.loads((root / "library" / "library.json").read_text("utf-8"))["stories"][0]
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(second["parts"]), 2)
            self.assertGreaterEqual(second["addedAt"], first["addedAt"])
            conn.close()

    def test_existing_added_at_is_not_reset_by_reorganization(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "romanized_pages").mkdir()
            (root / "romanized_pages" / "s.html").write_text("story", encoding="utf-8")
            conn = self._db(root)
            conn.execute(
                "INSERT INTO stories(url,title,romanized_file,original_text,romanized_text,updated_at,added_at) VALUES(?,?,?,?,?,?,?)",
                ("https://x/s", "Story", "s.html", "Family", "Family", "2026-09-25T10:00:00+00:00", "2026-09-10T10:00:00+00:00"),
            )
            conn.commit()
            organize_urls(conn, root, ["Family"], ["https://x/s"])
            before = conn.execute("SELECT added_at FROM stories WHERE url=?", ("https://x/s",)).fetchone()[0]
            conn.execute("UPDATE stories SET updated_at=? WHERE url=?", ("2026-09-30T10:00:00+00:00", "https://x/s"))
            conn.commit()
            organize_urls(conn, root, ["Family"], ["https://x/s"])
            after = conn.execute("SELECT added_at FROM stories WHERE url=?", ("https://x/s",)).fetchone()[0]
            self.assertEqual(before, "2026-09-10T10:00:00+00:00")
            self.assertEqual(after, before)
            conn.close()


if __name__ == "__main__":
    unittest.main()
