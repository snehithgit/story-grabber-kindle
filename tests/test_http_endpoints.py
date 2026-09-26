from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import web_server


class HttpEndpointTests(unittest.TestCase):
    """Starts the real HTTP server (web_server.AppHandler) against an
    isolated temp tree and exercises the v3.4/v3.5 endpoints as actual HTTP
    requests -- nothing in the existing test suite drove the server itself,
    only its helper functions, so routing mistakes (a typo'd path, a missing
    return) would not have been caught otherwise.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls._originals = {
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

        conn = web_server.connect_library(web_server.LIBRARY_DB)
        conn.execute(
            "INSERT INTO stories(url,title,status,original_text,romanized_text,updated_at,series_title,part_number) "
            "VALUES('https://a.example/1','Moon Story Part 1','verified','x','x','2026-01-01T00:00:00Z','Moon Story',1)"
        )
        conn.execute(
            "INSERT INTO stories(url,title,status,integrity_exact,original_text,romanized_text,updated_at,"
            "source_changed,previous_raw_sha256) "
            "VALUES('https://a.example/2','Flagged Story','review',1,'x','x','2026-01-01T00:00:00Z',1,'deadbeef')"
        )
        conn.commit()
        conn.close()

        cls.server = web_server.LocalThreadingHTTPServer(("127.0.0.1", 0), web_server.AppHandler)
        cls.server.allow_lan = False
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for name, value in cls._originals.items():
            setattr(web_server, name, value)
        cls._tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return json.loads(response.read())

    def _post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def test_summary_endpoint(self):
        data = self._get("/api/summary")
        self.assertEqual(data["stories"]["total"], 2)

    def test_series_endpoint(self):
        data = self._get("/api/library/series")
        self.assertEqual(len(data["series"]), 1)
        self.assertEqual(data["series"][0]["series_title"], "Moon Story")

    def test_duplicates_endpoint(self):
        data = self._get("/api/library/duplicates")
        self.assertIn("groups", data)
        self.assertEqual(data["group_count"], 0)

    def test_bulk_endpoint_moves_a_story(self):
        result = self._post("/api/library/bulk", {"urls": ["https://a.example/1"], "action": "set_category", "category": "Family"})
        self.assertTrue(result["ok"])
        row = self._get("/api/story?url=https://a.example/1")
        self.assertEqual(row["story"]["category"], "Family")
        self.assertEqual(row["story"]["category_locked"], 1)

    def test_accept_clears_stale_source_changed_flag(self):
        # v3.6: accepting a story that was flagged for a source-content
        # change must resolve the flag, not leave it stuck forever.
        result = self._post("/api/story/accept", {"url": "https://a.example/2"})
        self.assertTrue(result["ok"])
        row = self._get("/api/story?url=https://a.example/2")
        self.assertEqual(row["story"]["status"], "verified")
        self.assertEqual(row["story"]["source_changed"], 0)
        self.assertEqual(row["story"]["previous_raw_sha256"], "")


if __name__ == "__main__":
    unittest.main()
