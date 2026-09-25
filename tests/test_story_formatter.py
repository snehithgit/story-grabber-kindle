from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from story_formatter import (
    StoryFormatter, extract_story, format_blocks, formatting_quality, infer_speaker_labels,
    normalize_fidelity, plain_from_blocks, render_story_html, resolve_raw_page,
)


class StoryFormatterTests(unittest.TestCase):
    def test_dialogue_split_preserves_text(self):
        text = "After coming home brother: where did you go sister: I went to market brother: why sister: I forgot"
        paragraphs, breaks = format_blocks([text])
        self.assertEqual(breaks, 4)
        self.assertEqual(paragraphs[0], "After coming home")
        self.assertEqual(paragraphs[1], "brother: where did you go")
        self.assertEqual(paragraphs[2], "sister: I went to market")
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))


    def test_nested_html_entities_are_decoded_before_save(self):
        raw = """<!doctype html><html><head><title>Test</title></head><body><article><h1>Test</h1><p>Nenu:&amp;#x61;nte vadina, ratri 11:&amp;#x33;0 ayyindi</p></article></body></html>"""
        _, blocks = extract_story(raw)
        self.assertEqual(blocks, ["Nenu:ante vadina, ratri 11:30 ayyindi"])

    def test_time_colon_is_not_dialogue_but_common_speaker_variants_are(self):
        text = "intiki vachesariki ratri 11:30 ayyindi vadhina:- vachchaavaa neenu:- vachchaanu"
        paragraphs, breaks = format_blocks([text])
        self.assertEqual(breaks, 2)
        self.assertEqual(paragraphs[0], "intiki vachesariki ratri 11:30 ayyindi")
        self.assertEqual(paragraphs[1], "vadhina:- vachchaavaa")
        self.assertEqual(paragraphs[2], "neenu:- vachchaanu")
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))

    def test_speaker_colon_without_space_is_supported(self):
        text = "Narration first Nenu:ante okay Vadina:mari cheppu"
        paragraphs, breaks = format_blocks([text])
        self.assertEqual(breaks, 2)
        self.assertEqual(paragraphs, ["Narration first", "Nenu:ante okay", "Vadina:mari cheppu"])
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))


    def test_single_letter_and_repeated_story_labels_split_inline(self):
        text = "Conversation starts V:hello N:hi V:-come here N:okay"
        paragraphs, breaks = format_blocks([text])
        self.assertEqual(paragraphs, ["Conversation starts", "V:hello", "N:hi", "V:-come here", "N:okay"])
        self.assertEqual(breaks, 4)
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))

    def test_time_url_and_metadata_colons_are_not_speakers(self):
        text = "Time 11:30 Source: https://example.com Note: saved V:hello N:hi"
        paragraphs, _ = format_blocks([text])
        self.assertEqual(paragraphs[0], "Time 11:30 Source: https://example.com Note: saved")
        self.assertEqual(paragraphs[1:], ["V:hello", "N:hi"])
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))

    def test_long_unpunctuated_story_gets_safe_whitespace_paragraphs(self):
        text = " ".join(f"word{i}" for i in range(700))
        paragraphs, _ = format_blocks([text], long_threshold=500)
        self.assertGreater(len(paragraphs), 5)
        self.assertLessEqual(max(map(len, paragraphs)), 600)
        self.assertEqual(normalize_fidelity(text), normalize_fidelity(plain_from_blocks(paragraphs)))

    def test_quality_audit_detects_joined_dialogue(self):
        paragraphs = ["V:hello N:hi"]
        inferred = infer_speaker_labels(paragraphs)
        quality = formatting_quality(paragraphs, review_threshold=1800, inferred_labels=inferred)
        self.assertFalse(quality["quality_pass"])
        self.assertEqual(quality["unsplit_dialogue"], 1)

    def test_raw_file_resolves_by_url_hash_when_manifest_name_differs(self):
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            raw_dir = Path(td)
            url = "https://example.com/telugu-story"
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            actual = raw_dir / f"#U0c35-story-{digest}.html"
            actual.write_text("<html></html>", encoding="utf-8")
            name, path = resolve_raw_page(raw_dir, url, {"html_file": f"pages/తెలుగు-{digest}.html"})
            self.assertEqual(name, actual.name)
            self.assertEqual(path, actual)

    def test_nested_title_entity_is_decoded(self):
        raw = """<!doctype html><html><head><title>Story &amp;#8211; 2</title></head><body><article><h1>Story &amp;#8211; 2</h1><p>Body text.</p></article></body></html>"""
        title, blocks = extract_story(raw)
        self.assertEqual(title, "Story – 2")
        self.assertEqual(blocks, ["Body text."])

    def test_serialized_html_roundtrip_is_exact(self):
        paragraphs = ["Narration here.", "Brother: hello", "Sister: hi"]
        saved = render_story_html("Title", paragraphs, "https://example.com/story")
        title, blocks = extract_story(saved, formatted_mode=True)
        self.assertEqual(title, "Title")
        self.assertEqual(blocks, paragraphs)

    def test_manual_editor_rejects_word_changes(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "pages").mkdir()
            raw = """<!doctype html><html><head><title>Test</title></head><body><article><h1>Test</h1><div>Brother: hello Sister: hi</div></article></body></html>"""
            (out / "pages" / "test.html").write_text(raw, encoding="utf-8")
            manifest = {"pages": {"https://example.com/test": {"status": "success", "title": "Test", "words": 4, "html_file": "pages/test.html"}}}
            (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            formatter = StoryFormatter(out)
            result = formatter.run()
            self.assertEqual(result["verified"], 1)
            con = sqlite3.connect(out / "story_library.sqlite3")
            row = con.execute("SELECT original_text,formatted_text,integrity_exact FROM stories").fetchone()
            con.close()
            self.assertEqual(row[2], 1)
            with self.assertRaises(ValueError):
                formatter.save_manual_format("https://example.com/test", row[1].replace("hello", "goodbye"))


if __name__ == "__main__":
    unittest.main()
