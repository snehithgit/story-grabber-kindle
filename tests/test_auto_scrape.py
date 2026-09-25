from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from auto_scrape import discovered_links, terminal_urls


class AutoScrapeTests(unittest.TestCase):
    def test_discovers_partial_crawler_output_without_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sublinks.json"
            path.write_text(json.dumps([
                {"main_site": "https://x/", "sublinks": [
                    {"title": "A", "link": "https://x/a"},
                    {"title": "A again", "link": "https://x/a"},
                    {"title": "B", "link": "https://x/b"},
                ]}
            ]), encoding="utf-8")
            self.assertEqual([x["link"] for x in discovered_links(path)], ["https://x/a", "https://x/b"])

    def test_terminal_urls_reads_progress_journal(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "manifest.json").write_text(json.dumps({"pages": {
                "https://x/a": {"status": "success"}
            }}), encoding="utf-8")
            (out / "progress.jsonl").write_text(
                json.dumps({"url": "https://x/b", "state": {"status": "failed"}}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(terminal_urls(out), {"https://x/a", "https://x/b"})


if __name__ == "__main__":
    unittest.main()
