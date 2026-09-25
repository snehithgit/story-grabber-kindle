#!/usr/bin/env python3
"""Organize verified stories into human-readable category/series folders.

The processing pipeline keeps immutable raw/formatted/romanized artifacts in
its normal stage directories.  This module builds a separate, disposable
library view under content_output/library.  It can therefore be rebuilt safely
whenever titles, categories, or grouping rules change.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

from kindle_export import write_kindle_manifest

# Explicit multipart labels remain the strongest signal.
PART_RE = re.compile(
    r"^(?P<base>.+?)\s*(?:[-–—|:/]\s*)?(?:\(|\[)?\s*(?:part|pt\.?)[\s._-]*(?P<num>\d{1,4})\s*(?:\)|\])?\s*$",
    re.IGNORECASE,
)
# Some sites publish multipart stories as only "Story Name 1", "Story Name 2",
# etc.  Accept a short bare trailing integer as the part number.  Four-digit
# suffixes are intentionally excluded to avoid treating years as parts.
BARE_PART_RE = re.compile(
    r"^(?P<base>.*?\D)\s*(?:[-–—|:/]\s*)?(?P<num>\d{1,3})\s*$",
    re.IGNORECASE,
)
# A bare number after these labels is semantically a chapter/episode/etc., not
# the filename convention requested for multipart story grouping.
BARE_PART_BLOCKED_BASE_RE = re.compile(
    r"(?:^|\s)(?:chapter|chap|ch\.?|episode|ep\.?|season|volume|vol\.?|book)\s*$",
    re.IGNORECASE,
)
INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTISPACE = re.compile(r"\s+")


def clean_component(value: str, fallback: str = "Story", max_length: int = 120) -> str:
    """Return a readable Windows/Linux-safe path component."""
    value = INVALID_FS.sub(" ", str(value or ""))
    value = MULTISPACE.sub(" ", value).strip(" .")
    if not value:
        value = fallback
    # Windows reserved device names.
    if value.casefold() in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or fallback


def parse_story_part(title: str) -> tuple[str, int | None]:
    """Return (series/base title, part number) for a story title.

    Supported examples include ``Story Part 1``, ``Story Pt. 2``, and the
    common site convention ``Story 1`` / ``Story 2``.  Labels such as
    ``Chapter 3`` and ``Episode 4`` remain ordinary titles.
    """
    normalized = MULTISPACE.sub(" ", str(title or "")).strip()
    match = PART_RE.match(normalized)
    if match:
        base = match.group("base").strip(" -–—|:/._")
        if base:
            number = int(match.group("num"))
            if number >= 1:
                return base, number

    bare = BARE_PART_RE.match(normalized)
    if bare:
        base = bare.group("base").strip(" -–—|:/._")
        number = int(bare.group("num"))
        if base and number >= 1 and not BARE_PART_BLOCKED_BASE_RE.search(base):
            return base, number

    return normalized or "Untitled Story", None


def normalize_categories(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = MULTISPACE.sub(" ", str(value or "")).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result[:200]


def _category_in_text(categories: Iterable[str], text: str) -> str | None:
    """Return the first configured category found in one text field."""
    haystack = str(text or "").casefold()
    for category in normalize_categories(categories):
        needle = category.casefold()
        # Phrase boundary that works for both normal words and punctuation-heavy names.
        pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
        if re.search(pattern, haystack, re.IGNORECASE):
            return category
    return None


def match_category_with_source(categories: Iterable[str], title: str, *story_texts: str) -> tuple[str, str]:
    """Categorize with strict priority: title first, story content second.

    The category list order is respected within each phase.  A category found
    anywhere in the title therefore always wins over every body-only match.
    Only when the title contains no configured category do we inspect story
    content.
    """
    title_match = _category_in_text(categories, title)
    if title_match:
        return title_match, "title"

    body = "\n".join(str(text or "") for text in story_texts)
    body_match = _category_in_text(categories, body)
    if body_match:
        return body_match, "content"
    return "Uncategorized", "none"


def match_category(categories: Iterable[str], title: str, *story_texts: str) -> str:
    """Return category using title-first, content-second matching."""
    return match_category_with_source(categories, title, *story_texts)[0]


def ensure_story_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    additions = {
        "series_title": "TEXT NOT NULL DEFAULT ''",
        "part_number": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
        "organized_file": "TEXT NOT NULL DEFAULT ''",
        "added_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE stories ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_series ON stories(series_title, part_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stories_category ON stories(category)")
    conn.execute("UPDATE stories SET added_at=updated_at WHERE added_at='' AND updated_at<>''")
    conn.commit()


def _preferred_source(row: sqlite3.Row, output_dir: Path) -> Path | None:
    romanized = output_dir / "romanized_pages" / str(row["romanized_file"] or "")
    formatted = output_dir / "formatted_pages" / str(row["formatted_file"] or "")
    raw = output_dir / "pages" / str(row["raw_file"] or "")
    if row["romanized_file"] and romanized.is_file():
        return romanized
    if row["formatted_file"] and formatted.is_file():
        return formatted
    if row["raw_file"] and raw.is_file():
        return raw
    return None


def rebuild_library(conn: sqlite3.Connection, output_dir: Path, categories: Iterable[str]) -> dict[str, int]:
    """Rebuild the disposable human-readable library tree from the database."""
    ensure_story_columns(conn)
    library_dir = output_dir / "library"
    temp_dir = output_dir / ".library_build"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute("SELECT * FROM stories WHERE status IN ('verified','review') ORDER BY title COLLATE NOCASE").fetchall()
    prepared: list[dict[str, object]] = []
    for row in rows:
        series_title, part_number = parse_story_part(row["title"])
        category, category_source = match_category_with_source(
            categories,
            row["title"],
            row["original_text"],
            row["formatted_text"],
            row["romanized_text"],
        )
        prepared.append({
            "row": row, "series": series_title, "part": part_number,
            "category": category, "category_source": category_source,
        })

    # Multipart stories stay in one category.  A title match in ANY part wins
    # over every content-only match in the series.  Within the same phase, the
    # configured category order remains deterministic.
    configured = normalize_categories(categories)
    order = {name.casefold(): index for index, name in enumerate(configured)}
    source_rank = {"title": 0, "content": 1, "none": 2}
    series_best: dict[str, tuple[tuple[int, int], str]] = {}
    for item in prepared:
        if item["part"] is None:
            continue
        key = str(item["series"]).casefold()
        candidate = str(item["category"])
        scope = str(item["category_source"])
        rank = (source_rank.get(scope, 2), order.get(candidate.casefold(), 10**9))
        current = series_best.get(key)
        if current is None or rank < current[0]:
            series_best[key] = (rank, candidate)
    series_categories = {key: value[1] for key, value in series_best.items()}

    copied = 0
    groups: set[str] = set()
    updates: list[tuple[str, int | None, str, str, str]] = []
    used_paths: set[str] = set()
    for item in prepared:
        row = item["row"]
        assert isinstance(row, sqlite3.Row)
        series = str(item["series"])
        part = item["part"] if isinstance(item["part"], int) else None
        category = series_categories.get(series.casefold(), str(item["category"])) if part is not None else str(item["category"])
        source = _preferred_source(row, output_dir)
        if source is None:
            updates.append((series, part, category, "", row["url"]))
            continue

        category_dir = temp_dir / clean_component(category, "Uncategorized")
        if part is not None:
            series_dir = category_dir / clean_component(series, "Story")
            target = series_dir / f"Part {part:03d}.html"
            groups.add(f"{category.casefold()}::{series.casefold()}")
        else:
            target = category_dir / f"{clean_component(row['title'], 'Story')}.html"

        # Avoid overwriting unrelated stories that happen to share a title.
        relative_key = str(target.relative_to(temp_dir)).casefold()
        if relative_key in used_paths:
            suffix = hashlib.sha1(str(row["url"]).encode("utf-8")).hexdigest()[:6]
            target = target.with_name(f"{target.stem} [{suffix}]{target.suffix}")
            relative_key = str(target.relative_to(temp_dir)).casefold()
        used_paths.add(relative_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
        updates.append((series, part, category, str(target.relative_to(temp_dir)), row["url"]))

    # Replace only the generated library view, never raw processing artifacts.
    if library_dir.exists():
        shutil.rmtree(library_dir)
    temp_dir.replace(library_dir)

    conn.executemany(
        "UPDATE stories SET series_title=?,part_number=?,category=?,organized_file=? WHERE url=?",
        updates,
    )
    conn.commit()
    kindle = write_kindle_manifest(conn, output_dir)
    return {"stories": copied, "multipart_groups": len(groups), **kindle}


def _remove_organized_file(output_dir: Path, relative: str) -> None:
    if not relative:
        return
    root = (output_dir / "library").resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return
    if target.is_file():
        target.unlink(missing_ok=True)
    parent = target.parent
    while parent != root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def organize_urls(conn: sqlite3.Connection, output_dir: Path, categories: Iterable[str], urls: Iterable[str]) -> dict[str, int]:
    """Incrementally organize changed stories and their multipart siblings."""
    ensure_story_columns(conn)
    wanted = [str(url) for url in dict.fromkeys(urls) if str(url)]
    if not wanted:
        kindle = write_kindle_manifest(conn, output_dir)
        return {"stories": 0, "multipart_groups": 0, **kindle}

    placeholders = ",".join("?" for _ in wanted)
    changed = conn.execute(f"SELECT * FROM stories WHERE url IN ({placeholders})", wanted).fetchall()
    affected_series: set[str] = set()
    single_urls: set[str] = set()
    for row in changed:
        series, part = parse_story_part(row["title"])
        category = match_category(categories, row["title"], row["original_text"], row["formatted_text"], row["romanized_text"])
        conn.execute(
            "UPDATE stories SET series_title=?,part_number=?,category=? WHERE url=?",
            (series, part, category, row["url"]),
        )
        if part is None:
            single_urls.add(row["url"])
        else:
            affected_series.add(series.casefold())
    conn.commit()

    targets: dict[str, sqlite3.Row] = {}
    configured = normalize_categories(categories)
    order = {name.casefold(): index for index, name in enumerate(configured)}
    source_rank = {"title": 0, "content": 1, "none": 2}
    for series_key in affected_series:
        siblings = conn.execute(
            "SELECT * FROM stories WHERE part_number IS NOT NULL AND lower(series_title)=? AND status IN ('verified','review')",
            (series_key,),
        ).fetchall()
        if not siblings:
            continue
        ranked = []
        for sibling in siblings:
            category, scope = match_category_with_source(
                configured,
                sibling["title"],
                sibling["original_text"],
                sibling["formatted_text"],
                sibling["romanized_text"],
            )
            ranked.append(((source_rank.get(scope, 2), order.get(category.casefold(), 10**9)), category))
        group_category = min(ranked, key=lambda item: item[0])[1]
        conn.execute(
            "UPDATE stories SET category=? WHERE part_number IS NOT NULL AND lower(series_title)=?",
            (group_category, series_key),
        )
        for row in siblings:
            targets[row["url"]] = row
    if single_urls:
        placeholders = ",".join("?" for _ in single_urls)
        for row in conn.execute(f"SELECT * FROM stories WHERE url IN ({placeholders}) AND status IN ('verified','review')", list(single_urls)):
            targets[row["url"]] = row
    conn.commit()

    # Re-fetch after group-category updates.
    target_urls = list(targets)
    if target_urls:
        placeholders = ",".join("?" for _ in target_urls)
        rows = conn.execute(f"SELECT * FROM stories WHERE url IN ({placeholders})", target_urls).fetchall()
    else:
        rows = []

    library_dir = output_dir / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for row in rows:
        _remove_organized_file(output_dir, str(row["organized_file"] or ""))
        source = _preferred_source(row, output_dir)
        if source is None:
            conn.execute("UPDATE stories SET organized_file='' WHERE url=?", (row["url"],))
            continue
        category_dir = library_dir / clean_component(row["category"], "Uncategorized")
        if row["part_number"] is not None:
            target = category_dir / clean_component(row["series_title"], "Story") / f"Part {int(row['part_number']):03d}.html"
        else:
            target = category_dir / f"{clean_component(row['title'], 'Story')}.html"
        relative = str(target.relative_to(library_dir))
        conflict = conn.execute("SELECT 1 FROM stories WHERE organized_file=? AND url<>? LIMIT 1", (relative, row["url"])).fetchone()
        if conflict:
            suffix = hashlib.sha1(str(row["url"]).encode("utf-8")).hexdigest()[:6]
            target = target.with_name(f"{target.stem} [{suffix}]{target.suffix}")
            relative = str(target.relative_to(library_dir))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        conn.execute("UPDATE stories SET organized_file=? WHERE url=?", (relative, row["url"]))
        copied += 1
    conn.commit()
    kindle = write_kindle_manifest(conn, output_dir)
    return {"stories": copied, "multipart_groups": len(affected_series), **kindle}
