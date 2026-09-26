from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import links_store
from story_formatter import connect_library


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class NormalizeUrlTests(unittest.TestCase):
    def test_strips_tracking_params_and_keeps_real_ones(self):
        a = links_store.normalize_url("https://example.com/story?id=123&utm_source=x")
        b = links_store.normalize_url("https://example.com/story?id=123")
        self.assertEqual(a, b)

    def test_trailing_slash_normalized_but_root_kept(self):
        self.assertEqual(
            links_store.normalize_url("https://example.com/story/"),
            links_store.normalize_url("https://example.com/story"),
        )
        self.assertEqual(links_store.normalize_url("https://example.com/"), "https://example.com/")

    def test_host_case_insensitive(self):
        self.assertEqual(
            links_store.normalize_url("https://Example.COM/story"),
            links_store.normalize_url("https://example.com/story"),
        )

    def test_different_pages_stay_different(self):
        self.assertNotEqual(
            links_store.normalize_url("https://example.com/story?id=1"),
            links_store.normalize_url("https://example.com/story?id=2"),
        )


class LinksStoreSyncTests(unittest.TestCase):
    def test_sync_sublinks_inserts_only_new_links_and_is_token_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "content_output" / "story_library.sqlite3"
            sublinks_path = root / "sublinks.json"
            write_json(sublinks_path, [
                {"main_site": "https://a.example", "sublinks": [
                    {"link": "https://a.example/1", "title": "One"},
                    {"link": "https://a.example/2?utm_source=x", "title": "Two"},
                ]},
            ])
            conn = connect_library(db_path)
            try:
                inserted = links_store.sync_sublinks(conn, sublinks_path)
                self.assertEqual(inserted, 2)
                # unchanged file -> no-op (token guard)
                inserted_again = links_store.sync_sublinks(conn, sublinks_path)
                self.assertEqual(inserted_again, 0)

                rows = {row["url"]: row for row in conn.execute("SELECT * FROM links")}
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows["https://a.example/1"]["status"], "pending")

                # add a genuinely new link + a duplicate that only differs by tracking param
                write_json(sublinks_path, [
                    {"main_site": "https://a.example", "sublinks": [
                        {"link": "https://a.example/1", "title": "One"},
                        {"link": "https://a.example/2?utm_source=x", "title": "Two"},
                        {"link": "https://a.example/3", "title": "Three"},
                    ]},
                ])
                inserted = links_store.sync_sublinks(conn, sublinks_path)
                self.assertEqual(inserted, 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 3)
            finally:
                conn.close()

    def test_normalized_url_unique_index_prevents_duplicate_canonical_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "content_output" / "story_library.sqlite3"
            sublinks_path = root / "sublinks.json"
            write_json(sublinks_path, [
                {"main_site": "https://a.example", "sublinks": [
                    {"link": "https://a.example/story?id=1", "title": "A"},
                    {"link": "https://a.example/story?id=1&utm_source=newsletter", "title": "A dup"},
                ]},
            ])
            conn = connect_library(db_path)
            try:
                links_store.sync_sublinks(conn, sublinks_path)
                # Both raw URLs were distinct strings, but only one canonical link exists.
                count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                conn.close()

    def test_sync_progress_applies_manifest_then_tails_progress_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "story_library.sqlite3"
            sublinks_path = root / "sublinks.json"
            write_json(sublinks_path, [
                {"main_site": "https://a.example", "sublinks": [
                    {"link": "https://a.example/1", "title": "One"},
                    {"link": "https://a.example/2", "title": "Two"},
                ]},
            ])
            write_json(output_dir / "manifest.json", {"pages": {
                "https://a.example/1": {"status": "success"},
            }})
            conn = connect_library(db_path)
            try:
                links_store.sync_sublinks(conn, sublinks_path)
                links_store.sync_progress(conn, output_dir)
                status_by_url = {r["url"]: r["status"] for r in conn.execute("SELECT url, status FROM links")}
                self.assertEqual(status_by_url["https://a.example/1"], "scraped")
                self.assertEqual(status_by_url["https://a.example/2"], "pending")

                # progress.jsonl appends a failure for link 2
                (output_dir / "progress.jsonl").write_text(
                    json.dumps({"url": "https://a.example/2", "state": {"status": "failed", "error": "HTTP 404 Not Found"}}) + "\n",
                    encoding="utf-8",
                )
                links_store.sync_progress(conn, output_dir)
                row = conn.execute("SELECT status, error, retry_count FROM links WHERE url=?", ("https://a.example/2",)).fetchone()
                self.assertEqual(row["status"], "failed")
                self.assertIn("404", row["error"])
                self.assertEqual(row["retry_count"], 1)

                # calling again without new lines must not double-count retries
                links_store.sync_progress(conn, output_dir)
                row = conn.execute("SELECT retry_count FROM links WHERE url=?", ("https://a.example/2",)).fetchone()
                self.assertEqual(row["retry_count"], 1)
            finally:
                conn.close()

    def test_progress_offset_resets_when_journal_is_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "story_library.sqlite3"
            progress_path = output_dir / "progress.jsonl"
            progress_path.write_text(
                json.dumps({"url": "https://a.example/1", "state": {"status": "success"}}) + "\n"
                + json.dumps({"url": "https://a.example/2", "state": {"status": "success"}}) + "\n",
                encoding="utf-8",
            )
            conn = connect_library(db_path)
            try:
                links_store.sync_progress(conn, output_dir)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 2)
                # engine checkpoints: manifest rewritten, journal truncated to empty
                write_json(output_dir / "manifest.json", {"pages": {
                    "https://a.example/1": {"status": "success"},
                    "https://a.example/2": {"status": "success"},
                    "https://a.example/3": {"status": "failed", "error": "timeout"},
                }})
                progress_path.write_text("", encoding="utf-8")
                links_store.sync_progress(conn, output_dir)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 3)
                progress_path.write_text(
                    json.dumps({"url": "https://a.example/4", "state": {"status": "success"}}) + "\n",
                    encoding="utf-8",
                )
                links_store.sync_progress(conn, output_dir)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 4)
            finally:
                conn.close()

    def test_partial_progress_line_is_not_consumed_or_crash_sync(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "story_library.sqlite3"
            progress = output_dir / "progress.jsonl"
            first = json.dumps({"url": "https://a.example/1", "state": {"status": "failed", "error": "timeout"}}) + "\n"
            second = json.dumps({"url": "https://a.example/2", "state": {"status": "success"}})
            progress.write_text(first + second, encoding="utf-8")
            conn = connect_library(db_path)
            try:
                links_store.sync_progress(conn, output_dir)
                rows = {r["url"]: r for r in conn.execute("SELECT url,status,retry_count FROM links")}
                self.assertEqual(set(rows), {"https://a.example/1"})
                self.assertEqual(rows["https://a.example/1"]["retry_count"], 1)
                offset = int(conn.execute("SELECT value FROM import_state WHERE key='progress_offset'").fetchone()[0])
                self.assertEqual(offset, len(first.encode("utf-8")))

                # Finish the in-flight record; the next sync must pick it up.
                with open(progress, "a", encoding="utf-8") as handle:
                    handle.write("\n")
                links_store.sync_progress(conn, output_dir)
                rows = {r["url"]: r for r in conn.execute("SELECT url,status FROM links")}
                self.assertEqual(rows["https://a.example/2"]["status"], "scraped")
            finally:
                conn.close()

    def test_manifest_checkpoint_does_not_replay_old_journal_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "content_output"
            output_dir.mkdir(parents=True)
            db_path = output_dir / "story_library.sqlite3"
            url = "https://a.example/1"
            progress = output_dir / "progress.jsonl"
            progress.write_text(
                json.dumps({"url": url, "state": {"status": "failed", "error": "timeout"}}) + "\n",
                encoding="utf-8",
            )
            conn = connect_library(db_path)
            try:
                links_store.sync_progress(conn, output_dir)
                self.assertEqual(conn.execute("SELECT retry_count FROM links WHERE url=?", (url,)).fetchone()[0], 1)

                # Writer order is manifest first, then journal truncation.  At
                # this instant the same old line can still be present.
                import os, time
                old = time.time_ns() - 2_000_000_000
                os.utime(progress, ns=(old, old))
                write_json(output_dir / "manifest.json", {"pages": {url: {"status": "failed", "error": "timeout"}}})
                links_store.sync_progress(conn, output_dir)
                self.assertEqual(conn.execute("SELECT retry_count FROM links WHERE url=?", (url,)).fetchone()[0], 1)
            finally:
                conn.close()

    def test_sync_sublinks_reports_exact_insert_count_after_normalized_collision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "content_output" / "story_library.sqlite3"
            sublinks_path = root / "sublinks.json"
            write_json(sublinks_path, [{"main_site": "https://a.example", "sublinks": [
                {"link": "https://a.example/story?id=1", "title": "A"},
            ]}])
            conn = connect_library(db_path)
            try:
                self.assertEqual(links_store.sync_sublinks(conn, sublinks_path), 1)
                write_json(sublinks_path, [{"main_site": "https://a.example", "sublinks": [
                    {"link": "https://a.example/story?id=1&utm_source=x", "title": "A duplicate"},
                ]}])
                self.assertEqual(links_store.sync_sublinks(conn, sublinks_path), 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM links").fetchone()[0], 1)
            finally:
                conn.close()

    def test_dashboard_counts_and_list_links_db(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "content_output" / "story_library.sqlite3"
            sublinks_path = root / "sublinks.json"
            write_json(sublinks_path, [
                {"main_site": "SiteA", "sublinks": [
                    {"link": "https://a.example/1", "title": "One"},
                    {"link": "https://a.example/2", "title": "Two"},
                ]},
            ])
            conn = connect_library(db_path)
            try:
                links_store.sync_sublinks(conn, sublinks_path)
                conn.execute("UPDATE links SET status='scraped' WHERE url='https://a.example/1'")
                conn.commit()
                counts = links_store.dashboard_counts(conn)
                self.assertEqual(counts["links"], 2)
                self.assertEqual(counts["scraped"], 1)
                self.assertEqual(counts["pending"], 1)
                self.assertEqual(counts["sources"][0]["source"], "SiteA")

                result = links_store.list_links_db(conn, status="pending", page=1, per_page=10)
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["url"], "https://a.example/2")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
