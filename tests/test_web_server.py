from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import web_server


class WebServerSqlBackedTests(unittest.TestCase):
    """web_server.py's Dashboard/Sources/Library endpoints now read SQL
    (links_store.py, query_library_conn) instead of re-parsing sublinks.json
    /manifest.json/progress.jsonl on every request. These tests point the
    module's file-path constants at an isolated temp tree so they never touch
    the real project directory, exercise the public functions the HTTP
    handlers call, and restore the constants afterwards.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._originals = {
            name: getattr(web_server, name)
            for name in (
                "SITES_FILE", "SUBLINKS_FILE", "OUTPUT_DIR", "LIBRARY_DB",
                "SELECTED_LINKS_FILE", "SETTINGS_FILE",
            )
        }
        web_server.SITES_FILE = root / "sites.txt"
        web_server.SUBLINKS_FILE = root / "sublinks.json"
        web_server.OUTPUT_DIR = root / "content_output"
        web_server.LIBRARY_DB = web_server.OUTPUT_DIR / "story_library.sqlite3"
        web_server.SELECTED_LINKS_FILE = web_server.OUTPUT_DIR / "selected_links.json"
        web_server.SETTINGS_FILE = root / "settings.json"
        web_server.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(web_server, name, value)
        self._tmp.cleanup()

    def _write_sublinks(self, groups):
        web_server.SUBLINKS_FILE.write_text(json.dumps(groups), encoding="utf-8")

    def test_build_summary_uses_sql_counts_with_no_links_yet(self):
        summary = web_server.build_summary()
        self.assertEqual(summary["links"], 0)
        self.assertEqual(summary["stories"]["total"], 0)
        self.assertEqual(summary["sources"], [])

    def test_build_summary_reflects_synced_links_and_progress(self):
        self._write_sublinks([
            {"main_site": "SiteA", "sublinks": [
                {"link": "https://a.example/1", "title": "One"},
                {"link": "https://a.example/2", "title": "Two"},
            ]},
        ])
        (web_server.OUTPUT_DIR / "manifest.json").write_text(
            json.dumps({"pages": {"https://a.example/1": {"status": "success"}}}), encoding="utf-8"
        )
        summary = web_server.build_summary()
        self.assertEqual(summary["links"], 2)
        self.assertEqual(summary["raw_success"], 1)
        self.assertEqual(summary["pending_links"], 1)
        self.assertEqual(summary["sources"][0]["source"], "SiteA")

    def test_list_links_filters_by_status_and_search(self):
        self._write_sublinks([
            {"main_site": "SiteA", "sublinks": [
                {"link": "https://a.example/moon-story", "title": "Moon Story"},
                {"link": "https://a.example/sun-story", "title": "Sun Story"},
            ]},
        ])
        result = web_server.list_links({"q": ["moon"]})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Moon Story")

        result = web_server.list_links({"status": ["pending"]})
        self.assertEqual(result["total"], 2)

    def test_query_library_full_text_search_matches_original_text(self):
        conn = web_server.connect_library(web_server.LIBRARY_DB)
        try:
            conn.execute(
                """
                INSERT INTO stories(url, title, status, original_text, romanized_text, updated_at)
                VALUES (?,?,?,?,?,?)
                """,
                ("https://a.example/1", "A Quiet Village", "verified", "a story about a quiet village and a river", "", "2026-01-01T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()
        result = web_server.query_library(q="village")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["url"], "https://a.example/1")

        # a term that appears in neither title nor body must not match
        result = web_server.query_library(q="mountain")
        self.assertEqual(result["total"], 0)


class SiteProfilesSettingsTests(unittest.TestCase):
    """v3.7: settings.site_profiles is a per-host crawl-delay floor, editable
    either as a dict (API) or "host = seconds" lines (Settings textarea)."""

    def test_dict_input_is_normalized(self):
        result = web_server.normalize_site_profiles({"Slow.Example.com": 3, "b.example": "1.5"})
        self.assertEqual(result, {"slow.example.com": 3.0, "b.example": 1.5})

    def test_text_lines_are_parsed(self):
        result = web_server.normalize_site_profiles("slow.example = 3\n# comment\nother.example = 1.5\n")
        self.assertEqual(result, {"slow.example": 3.0, "other.example": 1.5})

    def test_comment_line_containing_an_equals_sign_is_still_skipped(self):
        # A "#" line that happens to look like "host = number" (e.g. an
        # example in a comment) must not be parsed as a real entry.
        result = web_server.normalize_site_profiles("# example: slow.example = 3\nreal.example = 2\n")
        self.assertEqual(result, {"real.example": 2.0})

    def test_negative_and_over_max_values_are_clamped(self):
        result = web_server.normalize_site_profiles({"a.example": -5, "b.example": 999})
        self.assertEqual(result, {"a.example": 0.0, "b.example": 60.0})

    def test_malformed_entries_are_dropped(self):
        result = web_server.normalize_site_profiles({"a.example": "not-a-number", "": 5})
        self.assertEqual(result, {})


class JobHistoryTests(unittest.TestCase):
    """v3.6: JobManager should persist a job_runs row per engine start/finish
    (via job_store), so history survives past the in-memory Job snapshot and
    a server restart.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._originals = {
            name: getattr(web_server, name)
            for name in ("SITES_FILE", "SUBLINKS_FILE", "OUTPUT_DIR", "LIBRARY_DB", "SELECTED_LINKS_FILE", "SETTINGS_FILE")
        }
        web_server.SITES_FILE = root / "sites.txt"
        web_server.SUBLINKS_FILE = root / "sublinks.json"
        web_server.OUTPUT_DIR = root / "content_output"
        web_server.LIBRARY_DB = web_server.OUTPUT_DIR / "story_library.sqlite3"
        web_server.SELECTED_LINKS_FILE = web_server.OUTPUT_DIR / "selected_links.json"
        web_server.SETTINGS_FILE = root / "settings.json"
        web_server.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        web_server.JOBS = web_server.JobManager()

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(web_server, name, value)
        self._tmp.cleanup()

    def test_job_start_waits_for_maintenance_gate(self):
        started = threading.Event()
        errors = []

        def launch():
            try:
                web_server.JOBS.start("content", [sys.executable, "-c", "pass"])
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                started.set()

        web_server.MAINTENANCE_GATE.acquire()
        try:
            thread = threading.Thread(target=launch)
            thread.start()
            time.sleep(0.10)
            self.assertFalse(started.is_set(), "job start bypassed the maintenance gate")
        finally:
            web_server.MAINTENANCE_GATE.release()

        thread.join(timeout=2.0)
        self.assertTrue(started.is_set())
        self.assertEqual(errors, [])

        for _ in range(100):
            if web_server.JOBS.snapshot("content")["state"] in {"complete", "failed", "stopped"}:
                break
            time.sleep(0.02)

    def test_job_run_is_recorded_start_to_finish(self):
        web_server.JOBS.start("content", [sys.executable, "-c", "print('hi')"])
        for _ in range(100):
            if web_server.JOBS.snapshot("content")["state"] in {"complete", "failed", "stopped"}:
                break
            time.sleep(0.05)
        else:
            self.fail("job did not finish in time")

        import job_store
        conn = web_server.connect_library(web_server.LIBRARY_DB)
        try:
            runs = job_store.recent_runs(conn)
        finally:
            conn.close()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["name"], "content")
        self.assertEqual(runs[0]["state"], "complete")
        self.assertIsNotNone(runs[0]["finished_at"])
        self.assertNotEqual(runs[0]["finished_at"], "")


if __name__ == "__main__":
    unittest.main()
