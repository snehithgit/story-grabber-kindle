#!/usr/bin/env python3
"""Deterministic story formatter + integrity verifier + Telugu romanizer.

Raw extractor output in content_output/pages is treated as immutable evidence.
This engine writes separate formatted and romanized copies and maintains a
SQLite-backed story library for fast browsing/search.

Formatting is deliberately non-destructive: only whitespace/paragraph
structure is added. A formatted story is accepted only when its normalized text
is exactly identical to the normalized raw story text.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "content_output"
RAW_DIR = OUTPUT_DIR / "pages"
FORMATTED_DIR = OUTPUT_DIR / "formatted_pages"
ROMANIZED_DIR = OUTPUT_DIR / "romanized_pages"
PROCESSING_MANIFEST = OUTPUT_DIR / "processing_manifest.json"
LIBRARY_DB = OUTPUT_DIR / "story_library.sqlite3"
SETTINGS_FILE = ROOT / "settings.json"
ROMANIZER_DIR = ROOT / "telugu_romanizer"

sys.path.insert(0, str(ROMANIZER_DIR))
from telugu_romanize import EnglishMatcher, convert_html  # noqa: E402
from story_organizer import ensure_story_columns, organize_urls, rebuild_library  # noqa: E402
import db_migrations  # noqa: E402

TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")
SPACE_RE = re.compile(r"\s+")
SENTENCE_END_RE = re.compile(r"(?<=[.!?…।])\s+")
FORMATTER_VERSION = 5

# Conservative dialogue labels. Generic one-word labels are also detected when
# they look like names/roles and appear directly before a colon. Multi-word
# labels are limited to common relationship/role phrases to avoid false splits.
KNOWN_SINGLE_SPEAKERS = {
    "brother", "sister", "mother", "father", "mom", "mum", "mummy", "dad", "daddy",
    "friend", "teacher", "doctor", "sir", "madam", "ma'am", "wife", "husband",
    "boy", "girl", "uncle", "aunt", "aunty", "grandma", "grandmother", "grandpa",
    "grandfather", "son", "daughter", "boss", "manager", "watchman", "driver",
    "police", "officer", "nurse", "master", "madam", "akka", "anna", "amma",
    "nanna", "chelli", "thammudu", "vadina", "vadhina", "vadinaa", "bava", "mama", "atta",
    "nenu", "neenu", "me", "i", "he", "she", "her", "him", "thanu", "gf",
    "annaya", "pinni", "bhabhi", "peddavadina", "bro",
    "అన్న", "అక్క", "అమ్మ", "నాన్న", "చెల్లి", "తమ్ముడు", "వదిన", "బావ", "మామ",
    "అత్త", "తాత", "అమ్మమ్మ", "నానమ్మ", "డాక్టర్", "టీచర్", "సార్", "మేడం",
}
KNOWN_MULTI_SPEAKERS = {
    "elder brother", "younger brother", "elder sister", "younger sister",
    "big brother", "little brother", "big sister", "little sister",
    "my brother", "my sister", "my mother", "my father", "my friend",
    "police officer", "school teacher", "class teacher", "elder son", "younger son",
    "elder daughter", "younger daughter",
}

# Labels that look syntactically like ``Name:`` but are normally metadata, not
# a character speaking. Keeping these out prevents bad splits around URLs, mail
# labels and chapter metadata while still allowing one-letter dialogue labels.
NON_SPEAKER_LABELS = {
    "http", "https", "url", "link", "source", "email", "e-mail", "mail",
    "id", "note", "time", "date", "chapter", "episode", "part", "title",
    "author", "website", "site", "phone", "mobile",
}

# A candidate label may begin at the start of a block, after whitespace, or
# immediately after sentence punctuation. This also catches ``...done.Nenu:``
# without treating the numeric left side of ``11:30`` as a speaker.
DIALOGUE_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9_\u0C00-\u0C7F])([^\s:]{1,32})\s*:(?=(?:\s+[-–—]?\s*|[-–—]\s*|[A-Za-z\u0C00-\u0C7F]))"
)
MULTI_DIALOGUE_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9_\u0C00-\u0C7F])([A-Za-z][A-Za-z'’-]*(?:\s+[A-Za-z][A-Za-z'’-]*){1,2})\s*:(?=(?:\s+[-–—]?\s*|[-–—]\s*|[A-Za-z\u0C00-\u0C7F]))"
)

BLOCK_TAGS = {
    "p", "div", "section", "article", "blockquote", "li", "pre", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "dd", "dt",
}
SKIP_TAGS = {"script", "style", "template", "noscript", "svg", "canvas"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def resolve_raw_page(raw_dir: Path, url: str, page: dict) -> tuple[str, Path]:
    """Resolve the actual raw HTML file even when Windows escaped Unicode.

    The extractor appends the first 12 SHA-256 hex characters of the source URL
    to every page filename. Some Windows/ZIP paths encode Telugu filename
    characters as ``#Uxxxx`` while the manifest retains the Unicode spelling.
    Matching by the stable URL hash repairs that mismatch without guessing titles.
    """
    expected = Path(str(page.get("html_file") or "")).name
    if expected:
        path = raw_dir / expected
        if path.is_file():
            return expected, path

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    matches = sorted(raw_dir.glob(f"*-{digest}.html"))
    if len(matches) == 1:
        return matches[0].name, matches[0]
    if len(matches) > 1:
        raise FileNotFoundError(f"Multiple raw HTML files match URL hash {digest}")
    raise FileNotFoundError(f"Raw HTML missing for {url}: {expected or '(no filename)'}")


def decode_text_entities(value: str, max_rounds: int = 3) -> str:
    """Decode nested HTML character references left behind by some source sites.

    Raw HTML stays untouched on disk. This only canonicalizes extracted story text
    so sequences such as ``&amp;#x61;`` become the intended character ``a``.
    """
    current = value
    for _ in range(max_rounds):
        decoded = html.unescape(current)
        if decoded == current:
            break
        current = decoded
    return current


def normalize_fidelity(value: str) -> str:
    """Normalize entities + whitespace for exact semantic-text verification."""
    return SPACE_RE.sub(" ", decode_text_entities(value)).strip()


class StoryHTMLParser(HTMLParser):
    """Extract title and readable body blocks without third-party libraries."""

    def __init__(self, *, formatted_mode: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.formatted_mode = formatted_mode
        self.title_parts: list[str] = []
        self.blocks: list[str] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_story_article = False
        self._story_depth = 0
        self._heading_depth = 0
        self._first_h1: str | None = None
        self._capture_heading: list[str] = []

    def _flush(self) -> None:
        text = SPACE_RE.sub(" ", decode_text_entities("".join(self._buffer))).strip()
        self._buffer.clear()
        if text:
            self.blocks.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag == "article" and not self._in_story_article:
            if (self.formatted_mode and attrs_dict.get("id") == "story-body") or not self.formatted_mode:
                self._in_story_article = True
                self._story_depth = 1
                self._flush()
                return
        if self._in_story_article and tag == "article":
            self._story_depth += 1
        if not self._in_story_article:
            return
        if tag in BLOCK_TAGS or tag == "br":
            self._flush()
        if tag == "h1":
            self._heading_depth += 1
            self._capture_heading = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if not self._in_story_article:
            return
        if tag in BLOCK_TAGS:
            self._flush()
        if tag == "h1" and self._heading_depth:
            self._heading_depth -= 1
            heading = SPACE_RE.sub(" ", "".join(self._capture_heading)).strip()
            if self._first_h1 is None and heading:
                self._first_h1 = heading
            self._capture_heading = []
        if tag == "article":
            self._story_depth -= 1
            if self._story_depth <= 0:
                self._flush()
                self._in_story_article = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if not self._in_story_article:
            return
        self._buffer.append(data)
        if self._heading_depth:
            self._capture_heading.append(data)

    def finish(self) -> tuple[str, list[str]]:
        self._flush()
        title = SPACE_RE.sub(" ", decode_text_entities("".join(self.title_parts))).strip()
        blocks = [b for b in self.blocks if b]
        # Extractor pages place the document title as the first h1 inside article.
        # Remove only that duplicate; actual story headings remain.
        if not self.formatted_mode and blocks:
            first = normalize_fidelity(blocks[0]).casefold()
            if title and first == normalize_fidelity(title).casefold():
                blocks = blocks[1:]
        return title, blocks


def extract_story(html_text: str, *, formatted_mode: bool = False) -> tuple[str, list[str]]:
    parser = StoryHTMLParser(formatted_mode=formatted_mode)
    parser.feed(html_text)
    parser.close()
    return parser.finish()


def plain_from_blocks(blocks: Iterable[str]) -> str:
    return "\n\n".join(block.strip() for block in blocks if block.strip())


def _clean_speaker_label(value: str) -> str:
    return value.strip("\"'“”‘’()[]{}<>*-–—").strip()


def _candidate_labels(text: str) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    for match in DIALOGUE_LABEL_RE.finditer(text):
        label = _clean_speaker_label(match.group(1))
        if label:
            candidates.append((match.start(1), label))
    return candidates


def infer_speaker_labels(blocks: Iterable[str]) -> set[str]:
    """Infer story-local dialogue labels without needing a fixed name list.

    Known relationships/roles and capitalized names are accepted immediately.
    Unknown lowercase labels are accepted only when they recur across the story,
    which catches author-specific names/abbreviations while avoiding metadata.
    One-letter uppercase labels (V:, N:, P:, etc.) are common in these stories and
    are accepted directly.
    """
    frequency: dict[str, int] = {}
    observed: dict[str, str] = {}
    for block in blocks:
        for _, label in _candidate_labels(block):
            key = label.casefold()
            if key in NON_SPEAKER_LABELS or label.isdigit():
                continue
            frequency[key] = frequency.get(key, 0) + 1
            observed.setdefault(key, label)

    accepted: set[str] = set()
    for key, count in frequency.items():
        label = observed[key]
        is_known = key in KNOWN_SINGLE_SPEAKERS
        is_single_upper = bool(re.fullmatch(r"[A-Z]", label))
        is_repeated_single = count >= 2 and bool(re.fullmatch(r"[A-Za-z]", label))
        is_name = bool(re.fullmatch(r"[A-Z][A-Za-z.'’-]{1,30}", label))
        is_repeated_word = count >= 2 and bool(re.fullmatch(r"[A-Za-z][A-Za-z.'’-]{1,30}", label))
        is_telugu = bool(TELUGU_RE.search(label)) and len(label) <= 18
        if is_known or is_single_upper or is_repeated_single or is_name or is_repeated_word or is_telugu:
            accepted.add(key)

    return accepted


def _speaker_positions(text: str, inferred_labels: set[str] | None = None) -> list[int]:
    """Return positions where a confirmed new dialogue turn begins."""
    inferred = inferred_labels or infer_speaker_labels([text])
    positions: set[int] = set()

    for position, label in _candidate_labels(text):
        key = label.casefold()
        if key in NON_SPEAKER_LABELS or label.isdigit():
            continue
        if key in inferred or key in KNOWN_SINGLE_SPEAKERS:
            positions.add(position)

    # Known multi-word roles are intentionally narrow; generic prose before a
    # colon must never be treated as a speaker just because it is capitalized.
    for match in MULTI_DIALOGUE_LABEL_RE.finditer(text):
        label = SPACE_RE.sub(" ", match.group(1)).casefold()
        if label in KNOWN_MULTI_SPEAKERS:
            positions.add(match.start(1))

    return sorted(positions)


def split_dialogue(block: str, inferred_labels: set[str] | None = None) -> tuple[list[str], int]:
    positions = _speaker_positions(block, inferred_labels)
    if not positions:
        return [block.strip()], 0

    pieces: list[str] = []
    cursor = 0
    for position in positions:
        if position > cursor:
            prefix = block[cursor:position].strip()
            if prefix:
                pieces.append(prefix)
        cursor = position
    tail = block[cursor:].strip()
    if tail:
        pieces.append(tail)

    # Count inserted paragraph boundaries, not merely detected labels.
    breaks = max(0, len(pieces) - 1)
    return pieces or [block.strip()], breaks


def _split_words_to_target(block: str, target: int) -> list[str]:
    """Split only at existing whitespace so fidelity normalization is exact."""
    words = block.strip().split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) if not current else len(word) + 1
        if current and current_len + added > target:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        chunks.append(" ".join(current))
    return chunks


def split_long_prose(block: str, sentence_target: int = 4, threshold: int = 900) -> list[str]:
    """Create readable prose paragraphs without changing any non-whitespace text.

    Sentence punctuation is preferred. If a source is essentially one long
    punctuation-free stream, fall back to an existing whitespace boundary near
    ``threshold``. This avoids leaving 5k-12k character paragraphs while still
    preserving exact normalized source text.
    """
    block = block.strip()
    if len(block) <= threshold:
        return [block]

    sentences = [part.strip() for part in SENTENCE_END_RE.split(block) if part.strip()]
    candidates: list[str] = []
    if len(sentences) >= 2:
        group_size = max(2, sentence_target)
        for index in range(0, len(sentences), group_size):
            candidates.append(" ".join(sentences[index:index + group_size]))
    else:
        candidates = [block]

    paragraphs: list[str] = []
    # A small amount above target is fine; very long sentence groups are capped
    # using word boundaries. The +20% avoids tiny spillover paragraphs.
    hard_target = max(300, int(threshold * 1.2))
    for candidate in candidates:
        if len(candidate) <= hard_target:
            paragraphs.append(candidate)
        else:
            paragraphs.extend(_split_words_to_target(candidate, threshold))
    return [paragraph for paragraph in paragraphs if paragraph]


def format_blocks(raw_blocks: list[str], *, sentence_target: int = 4, long_threshold: int = 900) -> tuple[list[str], int]:
    formatted: list[str] = []
    dialogue_breaks = 0
    inferred = infer_speaker_labels(raw_blocks)
    for raw_block in raw_blocks:
        dialogue_parts, added = split_dialogue(raw_block, inferred)
        dialogue_breaks += added
        for part in dialogue_parts:
            # Short dialogue turns stay intact. Exceptionally long turns are split
            # at sentence/word boundaries just like narration; otherwise a single
            # speaker could still leave a 5k+ character paragraph.
            formatted.extend(split_long_prose(part, sentence_target, long_threshold))
    return [p for p in formatted if p], dialogue_breaks


def formatting_quality(paragraphs: list[str], *, review_threshold: int, inferred_labels: set[str] | None = None) -> dict[str, object]:
    """Audit readability separately from exact text fidelity.

    ``integrity_exact`` answers whether characters survived. This audit answers
    whether dialogue is still glued together and whether any paragraph remains
    impractically long. Keeping the checks separate prevents malformed stories
    from being labelled verified merely because no words were lost.
    """
    inferred = inferred_labels or infer_speaker_labels(paragraphs)
    unsplit = 0
    dialogue_turns = 0
    for paragraph in paragraphs:
        positions = _speaker_positions(paragraph, inferred)
        dialogue_turns += len(positions)
        if positions:
            unsplit += len(positions) - (1 if positions[0] == 0 else 0)

    longest = max((len(paragraph) for paragraph in paragraphs), default=0)
    reasons: list[str] = []
    if unsplit:
        reasons.append(f"{unsplit} dialogue turn(s) are still joined inside paragraphs")
    if longest > review_threshold:
        reasons.append(f"Longest paragraph is {longest} characters")
    return {
        "quality_pass": not reasons,
        "unsplit_dialogue": unsplit,
        "dialogue_turns": dialogue_turns,
        "max_paragraph_chars": longest,
        "review_reason": "; ".join(reasons),
    }


def render_story_html(title: str, paragraphs: list[str], source_url: str = "") -> str:
    safe_title = html.escape(title or "Untitled")
    source_meta = html.escape(source_url, quote=True)
    body = "\n".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n</head>\n<body>\n"
        f'<header><h1>{safe_title}</h1></header>\n'
        f'<article id="story-body" data-source-url="{source_meta}">\n{body}\n</article>\n'
        "</body>\n</html>\n"
    )


def load_settings() -> dict:
    defaults = {
        "formatter_sentence_target": 4,
        "formatter_long_block_chars": 900,
        "formatter_review_block_chars": 1800,
        "auto_romanize": True,
    }
    try:
        data = json.loads(SETTINGS_FILE.read_text("utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def connect_library(path: Path = LIBRARY_DB) -> sqlite3.Connection:
    """Open (creating if needed) the story library DB, fully migrated.

    Schema ownership lives in ``db_migrations.py``: every table/column this
    app has ever needed — stories, links, FTS5 search, job history, Kindle
    change-tracking, backups — is applied here in one idempotent pass, so a
    v3.3.x database upgrades automatically on first launch and no recrawl or
    rescrape is required.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    db_migrations.migrate(conn)
    ensure_story_columns(conn)
    conn.commit()
    return conn


@dataclass
class ProcessResult:
    url: str
    title: str
    status: str
    integrity_exact: bool
    formatted_file: str
    romanized_file: str
    paragraphs: int
    dialogue_breaks: int
    dialogue_turns: int = 0
    unsplit_dialogue: int = 0
    max_paragraph_chars: int = 0
    quality_pass: bool = False
    telugu: bool = False
    romanized: bool = False
    review_reason: str = ""
    error: str = ""


class StoryFormatter:
    def __init__(self, output_dir: Path = OUTPUT_DIR) -> None:
        self.output_dir = output_dir
        self.raw_dir = output_dir / "pages"
        self.formatted_dir = output_dir / "formatted_pages"
        self.romanized_dir = output_dir / "romanized_pages"
        self.processing_manifest = output_dir / "processing_manifest.json"
        self.library_db = output_dir / "story_library.sqlite3"
        self.settings = load_settings()
        self.formatted_dir.mkdir(parents=True, exist_ok=True)
        self.romanized_dir.mkdir(parents=True, exist_ok=True)

        # User corrections must survive Docker/image upgrades, so keep the
        # editable dictionary under the persistent output volume.  Existing
        # installations are seeded once from the packaged defaults.
        self.romanizer_words = output_dir / "romanizer_words.tsv"
        if not self.romanizer_words.exists():
            seed = ROMANIZER_DIR / "my_words.tsv"
            if seed.is_file():
                atomic_text(self.romanizer_words, seed.read_text("utf-8"))

        self.romanizer = EnglishMatcher(
            ROMANIZER_DIR / "english_words.txt",
            self.romanizer_words,
            auto=True,
            dataset=ROMANIZER_DIR / "tenglish_words.tsv",
            common=ROMANIZER_DIR / "common_words.tsv",
        )

    def _manifest(self) -> dict:
        try:
            data = json.loads(self.processing_manifest.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("pages"), dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": FORMATTER_VERSION, "pages": {}, "updated_at": utc_now()}

    def _content_manifest(self) -> dict:
        path = self.output_dir / "manifest.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read content manifest: {exc}") from exc
        progress = self.output_dir / "progress.jsonl"
        if progress.is_file():
            for line in progress.read_text("utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("url") and entry.get("state"):
                        data.setdefault("pages", {})[entry["url"]] = entry["state"]
                except (json.JSONDecodeError, AttributeError):
                    continue
        return data

    def process_one(self, url: str, page: dict, conn: sqlite3.Connection) -> ProcessResult:
        raw_name, raw_path = resolve_raw_page(self.raw_dir, url, page)
        title_hint = decode_text_entities(str(page.get("title") or url))

        raw_html = raw_path.read_text("utf-8", errors="replace")
        raw_title, raw_blocks = extract_story(raw_html)
        title = decode_text_entities(raw_title or title_hint)
        original_text = plain_from_blocks(raw_blocks)
        original_norm = normalize_fidelity(original_text)
        if not original_norm:
            raise ValueError("No story text was extracted from raw HTML")

        sentence_target = max(2, min(8, int(self.settings.get("formatter_sentence_target", 4))))
        long_threshold = max(300, min(5000, int(self.settings.get("formatter_long_block_chars", 900))))
        review_threshold = max(800, min(20000, int(self.settings.get("formatter_review_block_chars", 1800))))
        inferred_labels = infer_speaker_labels(raw_blocks)
        paragraphs, dialogue_breaks = format_blocks(
            raw_blocks,
            sentence_target=sentence_target,
            long_threshold=long_threshold,
        )

        formatted_html = render_story_html(title, paragraphs, url)
        formatted_name = raw_name
        formatted_path = self.formatted_dir / formatted_name
        atomic_text(formatted_path, formatted_html)

        # Independent verification by re-reading/parsing the serialized output.
        saved_html = formatted_path.read_text("utf-8", errors="strict")
        _, saved_blocks = extract_story(saved_html, formatted_mode=True)
        formatted_text = plain_from_blocks(saved_blocks)
        exact = normalize_fidelity(formatted_text) == original_norm
        if not exact:
            formatted_path.unlink(missing_ok=True)
            raise ValueError("Integrity verification failed: formatted text differs from raw story")

        quality = formatting_quality(saved_blocks, review_threshold=review_threshold, inferred_labels=inferred_labels)
        status = "verified" if quality["quality_pass"] else "review"
        review_reason = str(quality["review_reason"])

        telugu = bool(TELUGU_RE.search(original_text))
        romanized_name = raw_name
        romanized_path = self.romanized_dir / romanized_name
        auto_romanize = bool(self.settings.get("auto_romanize", True))
        if telugu and auto_romanize:
            romanized_html = convert_html(formatted_html, "casual", False, self.romanizer, " ")
            romanized = True
        else:
            romanized_html = formatted_html
            romanized = False
        atomic_text(romanized_path, romanized_html)
        _, romanized_blocks = extract_story(romanized_html, formatted_mode=True)
        romanized_text = plain_from_blocks(romanized_blocks)

        source_host = (urlparse(url).hostname or "").lower()
        updated = utc_now()
        raw_hash = sha256_text(raw_html)
        words = int(page.get("words") or len(original_norm.split()))

        # v3.6: a previously verified/reviewed story whose raw content has
        # actually changed on re-scrape must not be silently overwritten --
        # the site may have edited or replaced the story since it was
        # accepted. Flag it back into the review queue instead, and remember
        # what the content used to hash to so the UI can explain why.
        source_changed = 0
        previous_raw_sha256 = ""
        existing = conn.execute(
            "SELECT status, raw_sha256 FROM stories WHERE url=?", (url,)
        ).fetchone()
        if existing and existing["raw_sha256"] and existing["raw_sha256"] != raw_hash \
                and existing["status"] in ("verified", "review"):
            source_changed = 1
            previous_raw_sha256 = existing["raw_sha256"]
            status = "review"
            review_reason = "; ".join(
                filter(None, [review_reason, f"Source content changed since it was last {existing['status']}."])
            )

        conn.execute(
            """
            INSERT INTO stories (
                url,title,source_host,words,raw_file,formatted_file,romanized_file,status,
                integrity_exact,telugu,romanized,paragraphs,dialogue_breaks,dialogue_turns,
                unsplit_dialogue,max_paragraph_chars,quality_pass,formatter_version,
                review_reason,error,raw_sha256,original_text,formatted_text,romanized_text,
                updated_at,manual_accept,source_changed,previous_raw_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title, source_host=excluded.source_host, words=excluded.words,
                raw_file=excluded.raw_file, formatted_file=excluded.formatted_file,
                romanized_file=excluded.romanized_file, status=excluded.status,
                integrity_exact=excluded.integrity_exact, telugu=excluded.telugu,
                romanized=excluded.romanized, paragraphs=excluded.paragraphs,
                dialogue_breaks=excluded.dialogue_breaks, dialogue_turns=excluded.dialogue_turns,
                unsplit_dialogue=excluded.unsplit_dialogue, max_paragraph_chars=excluded.max_paragraph_chars,
                quality_pass=excluded.quality_pass, formatter_version=excluded.formatter_version,
                review_reason=excluded.review_reason, error='', raw_sha256=excluded.raw_sha256,
                original_text=excluded.original_text, formatted_text=excluded.formatted_text,
                romanized_text=excluded.romanized_text, updated_at=excluded.updated_at,
                manual_accept=0, source_changed=excluded.source_changed,
                previous_raw_sha256=excluded.previous_raw_sha256
            """,
            (
                url, title, source_host, words, raw_name, formatted_name, romanized_name, status,
                1, int(telugu), int(romanized), len(saved_blocks), dialogue_breaks,
                int(quality["dialogue_turns"]), int(quality["unsplit_dialogue"]),
                int(quality["max_paragraph_chars"]), int(bool(quality["quality_pass"])), FORMATTER_VERSION,
                review_reason, "", raw_hash, original_text, formatted_text, romanized_text, updated,
                source_changed, previous_raw_sha256,
            ),
        )
        conn.commit()
        return ProcessResult(
            url=url,
            title=title,
            status=status,
            integrity_exact=True,
            formatted_file=formatted_name,
            romanized_file=romanized_name,
            paragraphs=len(saved_blocks),
            dialogue_breaks=dialogue_breaks,
            dialogue_turns=int(quality["dialogue_turns"]),
            unsplit_dialogue=int(quality["unsplit_dialogue"]),
            max_paragraph_chars=int(quality["max_paragraph_chars"]),
            quality_pass=bool(quality["quality_pass"]),
            telugu=telugu,
            romanized=romanized,
            review_reason=review_reason,
        )

    def record_failure(self, url: str, page: dict, error: Exception, conn: sqlite3.Connection) -> None:
        try:
            raw_name, _ = resolve_raw_page(self.raw_dir, url, page)
        except Exception:
            raw_name = Path(str(page.get("html_file") or "")).name
        source_host = (urlparse(url).hostname or "").lower()
        conn.execute(
            """
            INSERT INTO stories (url,title,source_host,words,raw_file,status,error,updated_at)
            VALUES (?,?,?,?,?,'failed',?,?)
            ON CONFLICT(url) DO UPDATE SET status='failed', error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                url, decode_text_entities(str(page.get("title") or url)), source_host, int(page.get("words") or 0),
                raw_name, str(error), utc_now(),
            ),
        )
        conn.commit()

    def run(self, *, force: bool = False, only_url: str | None = None, kindle_min_interval: float = 0.0) -> dict[str, int]:
        content = self._content_manifest()
        processing = self._manifest()
        processing["version"] = FORMATTER_VERSION
        conn = connect_library(self.library_db)
        counts = {"processed": 0, "verified": 0, "review": 0, "failed": 0, "skipped": 0}
        organized_urls: list[str] = []
        try:
            pages = content.get("pages", {})
            candidates = [
                (url, page) for url, page in pages.items()
                if isinstance(page, dict)
                and page.get("status") == "success"
                and page.get("html_file")
                and (only_url is None or url == only_url)
            ]
            total = len(candidates)
            print(f"Formatter   : {total} successful raw page(s) found")
            for index, (url, page) in enumerate(candidates, 1):
                try:
                    raw_name, raw_path = resolve_raw_page(self.raw_dir, url, page)
                except Exception as error:
                    self.record_failure(url, page, error, conn)
                    counts["failed"] += 1
                    print(f"[{index}/{total}] FAIL {url} — {error}")
                    continue
                raw_hash = sha256_text(raw_path.read_text("utf-8", errors="replace"))
                old = processing.get("pages", {}).get(url, {})
                formatted_exists = (self.formatted_dir / raw_name).is_file()
                romanized_exists = (self.romanized_dir / raw_name).is_file()
                if (
                    not force
                    and old.get("raw_sha256") == raw_hash
                    and old.get("formatter_version") == FORMATTER_VERSION
                    and old.get("status") in {"verified", "review"}
                    and formatted_exists
                    and romanized_exists
                ):
                    counts["skipped"] += 1
                    print(f"[{index}/{total}] SKIP {url}")
                    continue
                try:
                    result = self.process_one(url, page, conn)
                    processing.setdefault("pages", {})[url] = {
                        "status": result.status,
                        "formatter_version": FORMATTER_VERSION,
                        "title": result.title,
                        "raw_file": raw_name,
                        "formatted_file": result.formatted_file,
                        "romanized_file": result.romanized_file,
                        "raw_sha256": raw_hash,
                        "integrity_exact": result.integrity_exact,
                        "paragraphs": result.paragraphs,
                        "dialogue_breaks": result.dialogue_breaks,
                        "dialogue_turns": result.dialogue_turns,
                        "unsplit_dialogue": result.unsplit_dialogue,
                        "max_paragraph_chars": result.max_paragraph_chars,
                        "quality_pass": result.quality_pass,
                        "telugu": result.telugu,
                        "romanized": result.romanized,
                        "review_reason": result.review_reason,
                        "updated_at": utc_now(),
                    }
                    counts["processed"] += 1
                    counts[result.status] += 1
                    organized_urls.append(url)
                    label = "REVIEW" if result.status == "review" else "OK"
                    print(
                        f"[{index}/{total}] {label:<6} {result.paragraphs} paragraphs, "
                        f"{result.dialogue_breaks} dialogue breaks, max {result.max_paragraph_chars} chars — {url}"
                    )
                except Exception as exc:
                    self.record_failure(url, page, exc, conn)
                    processing.setdefault("pages", {})[url] = {
                        "status": "failed",
                        "formatter_version": FORMATTER_VERSION,
                        "title": str(page.get("title") or url),
                        "raw_file": raw_name,
                        "raw_sha256": raw_hash,
                        "integrity_exact": False,
                        "error": str(exc),
                        "updated_at": utc_now(),
                    }
                    counts["failed"] += 1
                    print(f"[{index}/{total}] FAIL   {url} — {exc}")
                processing["updated_at"] = utc_now()
                atomic_json(self.processing_manifest, processing)
            unorganized = int(conn.execute(
                "SELECT COUNT(*) FROM stories WHERE status IN ('verified','review') AND organized_file=''"
            ).fetchone()[0])
            if unorganized > len(organized_urls):
                organization = rebuild_library(conn, self.output_dir, self.settings.get("categories", []), self.settings.get("category_aliases", {}))
                mode_label = "rebuilt"
            else:
                organization = organize_urls(
                    conn, self.output_dir, self.settings.get("categories", []), organized_urls, self.settings.get("category_aliases", {}),
                    kindle_min_interval=kindle_min_interval,
                )
                mode_label = "organized"
            if organization["stories"]:
                print(
                    f"Library     : {mode_label} {organization['stories']} story file(s), "
                    f"{organization['multipart_groups']} multipart group(s)"
                )
        finally:
            conn.close()
        processing["updated_at"] = utc_now()
        atomic_json(self.processing_manifest, processing)
        print(
            "Formatter summary: "
            f"verified={counts['verified']} review={counts['review']} "
            f"failed={counts['failed']} skipped={counts['skipped']}"
        )
        return counts

    def reromanize(self, *, only_url: str | None = None) -> dict[str, int]:
        """Regenerate romanized artifacts from already-verified formatted HTML.

        This is intentionally independent from crawling and formatting so a
        romanizer-quality upgrade can be applied to an existing large library
        without re-scraping pages or changing paragraph structure.
        """
        conn = connect_library(self.library_db)
        counts = {"processed": 0, "failed": 0, "skipped": 0}
        organized_urls: list[str] = []
        try:
            sql = (
                "SELECT url,title,formatted_file,romanized_file FROM stories "
                "WHERE telugu=1 AND status IN ('verified','review')"
            )
            params: tuple[str, ...] = ()
            if only_url is not None:
                sql += " AND url=?"
                params = (only_url,)
            sql += " ORDER BY rowid"
            rows = conn.execute(sql, params).fetchall()
            total = len(rows)
            print(f"Romanizer   : {total} Telugu stor(ies) found")
            for index, row in enumerate(rows, 1):
                formatted_name = str(row["formatted_file"] or "")
                if not formatted_name:
                    counts["skipped"] += 1
                    print(f"[{index}/{total}] SKIP {row['url']} — no formatted file")
                    continue
                formatted_path = self.formatted_dir / formatted_name
                if not formatted_path.is_file():
                    counts["failed"] += 1
                    print(f"[{index}/{total}] FAIL {row['url']} — formatted file missing")
                    continue
                try:
                    formatted_html = formatted_path.read_text("utf-8", errors="strict")
                    romanized_html = convert_html(formatted_html, "casual", False, self.romanizer, " ")
                    romanized_name = str(row["romanized_file"] or formatted_name)
                    romanized_path = self.romanized_dir / romanized_name
                    atomic_text(romanized_path, romanized_html)
                    _, roman_blocks = extract_story(romanized_html, formatted_mode=True)
                    romanized_text = plain_from_blocks(roman_blocks)
                    conn.execute(
                        "UPDATE stories SET romanized_file=?, romanized_text=?, romanized=1 WHERE url=?",
                        (romanized_name, romanized_text, row["url"]),
                    )
                    counts["processed"] += 1
                    organized_urls.append(str(row["url"]))
                    if counts["processed"] % 100 == 0:
                        conn.commit()
                    print(f"[{index}/{total}] OK     {row['url']}")
                except Exception as exc:
                    counts["failed"] += 1
                    print(f"[{index}/{total}] FAIL   {row['url']} — {exc}")
            conn.commit()
            if organized_urls:
                organize_urls(
                    conn, self.output_dir, self.settings.get("categories", []), organized_urls,
                    self.settings.get("category_aliases", {}),
                )
            print(
                "Romanizer summary: "
                f"processed={counts['processed']} failed={counts['failed']} skipped={counts['skipped']}"
            )
            return counts
        finally:
            conn.close()

    def save_manual_format(self, url: str, edited_text: str) -> ProcessResult:
        """Save user-edited paragraph breaks only; changing text is rejected."""
        conn = connect_library(self.library_db)
        try:
            row = conn.execute("SELECT * FROM stories WHERE url=?", (url,)).fetchone()
            if row is None:
                raise ValueError("Story not found in library")
            original_text = row["original_text"]
            if normalize_fidelity(edited_text) != normalize_fidelity(original_text):
                raise ValueError("Text changed. Only paragraph/line breaks may be edited.")
            paragraphs = [SPACE_RE.sub(" ", p).strip() for p in re.split(r"\n\s*\n|\n", edited_text) if p.strip()]
            if not paragraphs:
                raise ValueError("Formatted story cannot be empty")

            formatted_name = str(row["formatted_file"] or row["raw_file"] or "")
            romanized_name = str(row["romanized_file"] or formatted_name)
            if not formatted_name:
                raise ValueError("Story has no writable formatted filename")

            formatted_html = render_story_html(decode_text_entities(row["title"]), paragraphs, url)
            formatted_path = self.formatted_dir / formatted_name
            atomic_text(formatted_path, formatted_html)
            _, check_blocks = extract_story(formatted_path.read_text("utf-8"), formatted_mode=True)
            formatted_text = plain_from_blocks(check_blocks)
            if normalize_fidelity(formatted_text) != normalize_fidelity(original_text):
                formatted_path.unlink(missing_ok=True)
                raise ValueError("Integrity verification failed after saving")

            review_threshold = max(800, min(20000, int(self.settings.get("formatter_review_block_chars", 1800))))
            inferred_labels = infer_speaker_labels([original_text])
            quality = formatting_quality(check_blocks, review_threshold=review_threshold, inferred_labels=inferred_labels)
            status = "verified" if quality["quality_pass"] else "review"
            review_reason = str(quality["review_reason"])

            telugu = bool(TELUGU_RE.search(original_text))
            if telugu and bool(self.settings.get("auto_romanize", True)):
                romanized_html = convert_html(formatted_html, "casual", False, self.romanizer, " ")
                romanized = True
            else:
                romanized_html = formatted_html
                romanized = False
            romanized_path = self.romanized_dir / romanized_name
            atomic_text(romanized_path, romanized_html)
            _, roman_blocks = extract_story(romanized_html, formatted_mode=True)
            romanized_text = plain_from_blocks(roman_blocks)

            conn.execute(
                """
                UPDATE stories SET status=?, integrity_exact=1, paragraphs=?, dialogue_turns=?,
                    unsplit_dialogue=?, max_paragraph_chars=?, quality_pass=?, formatter_version=?,
                    review_reason=?, error='', formatted_text=?, romanized_text=?, romanized=?,
                    formatted_file=?, romanized_file=?, manual_accept=1,
                    source_changed=0, previous_raw_sha256='', updated_at=? WHERE url=?
                """,
                (
                    status, len(check_blocks), int(quality["dialogue_turns"]),
                    int(quality["unsplit_dialogue"]), int(quality["max_paragraph_chars"]),
                    int(bool(quality["quality_pass"])), FORMATTER_VERSION, review_reason,
                    formatted_text, romanized_text, int(romanized), formatted_name, romanized_name,
                    utc_now(), url,
                ),
            )
            conn.commit()
            organize_urls(conn, self.output_dir, self.settings.get("categories", []), [url], self.settings.get("category_aliases", {}))
            return ProcessResult(
                url=url, title=row["title"], status=status, integrity_exact=True,
                formatted_file=formatted_name, romanized_file=romanized_name,
                paragraphs=len(check_blocks), dialogue_breaks=row["dialogue_breaks"],
                dialogue_turns=int(quality["dialogue_turns"]),
                unsplit_dialogue=int(quality["unsplit_dialogue"]),
                max_paragraph_chars=int(quality["max_paragraph_chars"]),
                quality_pass=bool(quality["quality_pass"]), telugu=telugu, romanized=romanized,
                review_reason=review_reason,
            )
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Format extracted stories without changing their text")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--force", action="store_true", help="reprocess even when raw content is unchanged")
    parser.add_argument("--reromanize", action="store_true",
                        help="regenerate romanized output from existing formatted stories only")
    parser.add_argument("--url", help="process one exact source URL")
    parser.add_argument(
        "--kindle-min-interval", type=float, default=0.0,
        help="skip rewriting library.json more often than this many seconds (0 = always write immediately)",
    )
    args = parser.parse_args()
    formatter = StoryFormatter(args.output.resolve())
    if args.reromanize:
        counts = formatter.reromanize(only_url=args.url)
        return 1 if counts["failed"] and not counts["processed"] else 0
    counts = formatter.run(force=args.force, only_url=args.url, kindle_min_interval=max(0.0, args.kindle_min_interval))
    return 1 if counts["failed"] and not (counts["verified"] or counts["review"] or counts["skipped"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
