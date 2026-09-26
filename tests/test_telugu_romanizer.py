from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROMANIZER_DIR = ROOT / "telugu_romanizer"
sys.path.insert(0, str(ROMANIZER_DIR))

from telugu_romanize import EnglishMatcher, convert  # noqa: E402
from story_formatter import StoryFormatter  # noqa: E402


SAMPLE_TELUGU = (
    "నా తెలుగు సెక్స్ స్టోరీస్ లో ఈ రోజు నేను మీకు ఒక కథ చెప్తాను. "
    "మీరు ఖచ్చితం గ చదివి ఎంజాయ్ చేస్తారు అని ఆశిస్తున్నాను. "
    "నా పేరు మనిష, నా ఎత్తు 5 అడుగుల 10 అంగుళాలు, నా బాడీ డీసెంట్ గా ఉంటది, "
    "నా వయస్సు 20 yrs. నేను మీకు ఒక అద్భుతమైన సెక్స్ స్టొరీ ని చెప్పబోతున్నాను."
)


class TeluguRomanizerTests(unittest.TestCase):
    def matcher(self) -> EnglishMatcher:
        return EnglishMatcher(
            ROMANIZER_DIR / "english_words.txt",
            ROMANIZER_DIR / "my_words.tsv",
            auto=True,
            dataset=ROMANIZER_DIR / "tenglish_words.tsv",
            common=ROMANIZER_DIR / "common_words.tsv",
        )

    def test_noisy_corpus_cannot_override_core_telugu_words(self):
        matcher = self.matcher()
        expected = {
            "నా": "naa",
            "రోజు": "roju",
            "ఒక": "oka",
            "తెలుగు": "telugu",
            "ఈ": "ee",
            "నేను": "nenu",
            "పేరు": "peru",
        }
        for source, wanted in expected.items():
            with self.subTest(source=source):
                self.assertEqual(convert(source, "casual", False, matcher, " "), wanted)

    def test_english_loanwords_are_natural_not_literal(self):
        matcher = self.matcher()
        expected = {
            "సెక్స్": "sex",
            "స్టోరీస్": "stories",
            "ఎంజాయ్": "enjoy",
            "బాడీ": "body",
            "డీసెంట్": "decent",
            "స్టొరీ": "story",
        }
        for source, wanted in expected.items():
            with self.subTest(source=source):
                self.assertEqual(convert(source, "casual", False, matcher, " "), wanted)

    def test_reported_sentence_regression(self):
        output = convert(SAMPLE_TELUGU, "casual", False, self.matcher(), " ")
        self.assertTrue(output.startswith("naa telugu sex stories lo ee roju nenu meeku oka katha"))
        for bad in ("mon", "roeju", "ooka", "telegu", "yea", "peruu", "enjoys", "baadii"):
            self.assertNotRegex(output, rf"(?<![A-Za-z]){bad}(?![A-Za-z])")
        for good in ("naa", "roju", "oka", "telugu", "nenu", "peru", "enjoy", "body", "decent", "story"):
            self.assertRegex(output, rf"(?<![A-Za-z]){good}(?![A-Za-z])")

    def test_reromanize_updates_existing_library_without_touching_formatted_source(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "pages").mkdir()
            raw = (
                "<!doctype html><html><head><title>Romanizer Test</title></head><body>"
                f"<article><h1>Romanizer Test</h1><p>{SAMPLE_TELUGU}</p></article>"
                "</body></html>"
            )
            (out / "pages" / "story.html").write_text(raw, encoding="utf-8")
            url = "https://example.com/romanizer-test"
            (out / "manifest.json").write_text(
                json.dumps({"pages": {url: {
                    "status": "success", "title": "Romanizer Test", "words": 40,
                    "html_file": "pages/story.html",
                }}}),
                encoding="utf-8",
            )

            formatter = StoryFormatter(out)
            first = formatter.run()
            self.assertEqual(first["failed"], 0)

            conn = sqlite3.connect(out / "story_library.sqlite3")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM stories WHERE url=?", (url,)).fetchone()
            formatted_path = out / "formatted_pages" / row["formatted_file"]
            romanized_path = out / "romanized_pages" / row["romanized_file"]
            formatted_hash = hashlib.sha256(formatted_path.read_bytes()).hexdigest()

            # Simulate an old v3.7.2 artifact and database value.
            romanized_path.write_text("<p>mon telegu roeju ooka</p>", encoding="utf-8")
            conn.execute("UPDATE stories SET romanized_text='mon telegu roeju ooka' WHERE url=?", (url,))
            conn.commit()
            conn.close()

            result = formatter.reromanize()
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(hashlib.sha256(formatted_path.read_bytes()).hexdigest(), formatted_hash)

            conn = sqlite3.connect(out / "story_library.sqlite3")
            conn.row_factory = sqlite3.Row
            updated = conn.execute("SELECT romanized_text FROM stories WHERE url=?", (url,)).fetchone()[0]
            conn.close()
            self.assertIn("naa telugu sex stories", updated)
            self.assertIn("ee roju nenu meeku oka katha", updated)
            self.assertNotIn("mon", updated)
            self.assertNotIn("roeju", updated)
            self.assertNotIn("ooka", updated)
            self.assertNotIn("telegu", updated)

            # User overrides are now stored under the persistent output volume.
            self.assertTrue((out / "romanizer_words.tsv").is_file())


if __name__ == "__main__":
    unittest.main()
