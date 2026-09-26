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
from typing import Any, Iterable, Mapping

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


def normalize_category_aliases(values: object, categories: Iterable[str] = ()) -> dict[str, list[str]]:
    """Normalize canonical-category -> alias mappings.

    Canonical category names are matched case-insensitively against the configured
    category list, so ``thammudu`` and ``Thammudu`` refer to the same category.
    Unknown canonical keys are ignored: aliases never create a new category by
    themselves.
    """
    canonical = normalize_categories(categories)
    canonical_by_key = {name.casefold(): name for name in canonical}
    result: dict[str, list[str]] = {name: [] for name in canonical}

    if isinstance(values, Mapping):
        items = values.items()
    elif isinstance(values, list):
        # Also accept [{"category": "X", "aliases": [...]}, ...] for forwards compatibility.
        items = []
        for item in values:
            if isinstance(item, Mapping):
                items.append((item.get("category", ""), item.get("aliases", [])))
    else:
        items = []

    for raw_name, raw_aliases in items:
        name = canonical_by_key.get(MULTISPACE.sub(" ", str(raw_name or "")).strip().casefold())
        if not name:
            continue
        if isinstance(raw_aliases, str):
            raw_aliases = re.split(r"[,|]", raw_aliases)
        if not isinstance(raw_aliases, Iterable):
            continue
        seen = {name.casefold()}
        aliases: list[str] = []
        for raw_alias in raw_aliases:
            alias = MULTISPACE.sub(" ", str(raw_alias or "")).strip()
            key = alias.casefold()
            if alias and key not in seen:
                seen.add(key)
                aliases.append(alias)
        result[name] = aliases[:50]
    return {name: aliases for name, aliases in result.items() if aliases}


def _category_in_text(categories: Iterable[str], text: str, category_aliases: object = None) -> str | None:
    """Return the first canonical category whose name or alias is found in text."""
    configured = normalize_categories(categories)
    aliases = normalize_category_aliases(category_aliases or {}, configured)
    haystack = str(text or "").casefold()
    for category in configured:
        terms = [category] + aliases.get(category, [])
        for term in terms:
            needle = term.casefold()
            # Phrase boundary that works for both normal words and punctuation-heavy names.
            pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
            if re.search(pattern, haystack, re.IGNORECASE):
                return category
    return None


def match_category_with_source(categories: Iterable[str], title: str, *story_texts: str, category_aliases: object = None) -> tuple[str, str]:
    """Categorize with strict priority: title first, story content second.

    The category list order is respected within each phase.  A category found
    anywhere in the title therefore always wins over every body-only match.
    Only when the title contains no configured category do we inspect story
    content.
    """
    title_match = _category_in_text(categories, title, category_aliases)
    if title_match:
        return title_match, "title"

    body = "\n".join(str(text or "") for text in story_texts)
    body_match = _category_in_text(categories, body, category_aliases)
    if body_match:
        return body_match, "content"
    return "Uncategorized", "none"


def match_category(categories: Iterable[str], title: str, *story_texts: str, category_aliases: object = None) -> str:
    """Return canonical category using title-first, content-second matching."""
    return match_category_with_source(categories, title, *story_texts, category_aliases=category_aliases)[0]


def ensure_story_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    additions = {
        "series_title": "TEXT NOT NULL DEFAULT ''",
        "part_number": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'Uncategorized'",
        "category_source": "TEXT NOT NULL DEFAULT 'none'",
        "category_locked": "INTEGER NOT NULL DEFAULT 0",
        "organized_file": "TEXT NOT NULL DEFAULT ''",
        "added_at": "TEXT NOT NULL DEFAULT ''",
        "source_changed": "INTEGER NOT NULL DEFAULT 0",
        "previous_raw_sha256": "TEXT NOT NULL DEFAULT ''",
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


def rebuild_library(conn: sqlite3.Connection, output_dir: Path, categories: Iterable[str], category_aliases: object = None) -> dict[str, int]:
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
        if row["category_locked"]:
            # A bulk category edit pinned this story; never let a rebuild
            # (recategorize, settings-alias edit, first-launch migration)
            # silently move it back to an automatic match.
            category, category_source = row["category"], "manual"
        else:
            category, category_source = match_category_with_source(
                categories,
                row["title"],
                row["original_text"],
                row["formatted_text"],
                row["romanized_text"],
                category_aliases=category_aliases,
            )
        prepared.append({
            "row": row, "series": series_title, "part": part_number,
            "category": category, "category_source": category_source,
        })

    # Multipart stories stay in one category.  A title match in ANY part wins
    # over every content-only match in the series, and a manually locked part
    # always wins outright. Within the same phase, the configured category
    # order remains deterministic.
    configured = normalize_categories(categories)
    order = {name.casefold(): index for index, name in enumerate(configured)}
    source_rank = {"manual": -1, "title": 0, "content": 1, "none": 2}
    series_best: dict[str, tuple[tuple[int, int], str, str]] = {}
    for item in prepared:
        if item["part"] is None:
            continue
        key = str(item["series"]).casefold()
        candidate = str(item["category"])
        scope = str(item["category_source"])
        rank = (source_rank.get(scope, 2), order.get(candidate.casefold(), 10**9))
        current = series_best.get(key)
        if current is None or rank < current[0]:
            series_best[key] = (rank, candidate, scope)
    series_categories = {key: (value[1], value[2]) for key, value in series_best.items()}

    copied = 0
    groups: set[str] = set()
    updates: list[tuple[str, int | None, str, str, int, str, str]] = []
    used_paths: set[str] = set()
    for item in prepared:
        row = item["row"]
        assert isinstance(row, sqlite3.Row)
        series = str(item["series"])
        part = item["part"] if isinstance(item["part"], int) else None
        if part is not None:
            category, category_source = series_categories.get(series.casefold(), (str(item["category"]), str(item["category_source"])))
        else:
            category, category_source = str(item["category"]), str(item["category_source"])
        locked = 1 if category_source == "manual" else 0
        source = _preferred_source(row, output_dir)
        if source is None:
            updates.append((series, part, category, category_source, locked, "", row["url"]))
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
        updates.append((series, part, category, category_source, locked, str(target.relative_to(temp_dir)), row["url"]))

    # Replace only the generated library view, never raw processing artifacts.
    if library_dir.exists():
        shutil.rmtree(library_dir)
    temp_dir.replace(library_dir)

    conn.executemany(
        "UPDATE stories SET series_title=?,part_number=?,category=?,category_source=?,category_locked=?,organized_file=? WHERE url=?",
        updates,
    )
    conn.commit()
    # An explicit rebuild (recategorize, settings change, first-launch
    # migration) always writes immediately -- force=True bypasses debounce.
    kindle = write_kindle_manifest(conn, output_dir, force=True)
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


def organize_urls(
    conn: sqlite3.Connection, output_dir: Path, categories: Iterable[str], urls: Iterable[str],
    category_aliases: object = None, *, kindle_min_interval: float = 0.0,
) -> dict[str, int]:
    """Incrementally organize changed stories and their multipart siblings.

    ``kindle_min_interval`` is 0 (always write immediately) unless a caller
    that runs this in a tight per-item loop -- auto_scrape.py with a very
    small batch size -- opts into debouncing the library.json rewrite; see
    kindle_export.write_kindle_manifest.
    """
    ensure_story_columns(conn)
    wanted = [str(url) for url in dict.fromkeys(urls) if str(url)]
    if not wanted:
        kindle = write_kindle_manifest(conn, output_dir, min_interval=kindle_min_interval)
        return {"stories": 0, "multipart_groups": 0, **kindle}

    placeholders = ",".join("?" for _ in wanted)
    changed = conn.execute(f"SELECT * FROM stories WHERE url IN ({placeholders})", wanted).fetchall()
    affected_series: set[str] = set()
    single_urls: set[str] = set()
    for row in changed:
        series, part = parse_story_part(row["title"])
        if row["category_locked"]:
            # A bulk category edit (bulk_set_category) pinned this story;
            # leave its category alone, only refresh series/part placement.
            conn.execute("UPDATE stories SET series_title=?,part_number=? WHERE url=?", (series, part, row["url"]))
        else:
            category, category_source = match_category_with_source(
                categories, row["title"], row["original_text"], row["formatted_text"], row["romanized_text"],
                category_aliases=category_aliases,
            )
            conn.execute(
                "UPDATE stories SET series_title=?,part_number=?,category=?,category_source=? WHERE url=?",
                (series, part, category, category_source, row["url"]),
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
        locked_sibling = next((s for s in siblings if s["category_locked"]), None)
        if locked_sibling is not None:
            # One part of this series was manually pinned (bulk_set_category)
            # -- the whole series follows it, and the lock spreads to every
            # sibling so a future scrape of another part doesn't drift away.
            group_category, group_category_source = locked_sibling["category"], "manual"
            conn.execute(
                "UPDATE stories SET category=?,category_source=?,category_locked=1 "
                "WHERE part_number IS NOT NULL AND lower(series_title)=?",
                (group_category, group_category_source, series_key),
            )
        else:
            ranked = []
            for sibling in siblings:
                category, scope = match_category_with_source(
                    configured,
                    sibling["title"],
                    sibling["original_text"],
                    sibling["formatted_text"],
                    sibling["romanized_text"],
                    category_aliases=category_aliases,
                )
                ranked.append(((source_rank.get(scope, 2), order.get(category.casefold(), 10**9)), category, scope))
            best = min(ranked, key=lambda item: item[0])
            group_category, group_category_source = best[1], best[2]
            conn.execute(
                "UPDATE stories SET category=?,category_source=? WHERE part_number IS NOT NULL AND lower(series_title)=?",
                (group_category, group_category_source, series_key),
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

    copied = _place_rows(conn, output_dir, rows)
    conn.commit()
    kindle = write_kindle_manifest(conn, output_dir, min_interval=kindle_min_interval)
    return {"stories": copied, "multipart_groups": len(affected_series), **kindle}


def _place_rows(conn: sqlite3.Connection, output_dir: Path, rows: Iterable[sqlite3.Row]) -> int:
    """Copy each row's preferred source file into its category/series folder.

    Shared by :func:`organize_urls` and :func:`bulk_set_category` -- both end
    up needing "these specific rows' category/series just changed, put their
    files where they now belong" without touching anything else in the
    library tree.
    """
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
    return copied


def bulk_set_category(
    conn: sqlite3.Connection, output_dir: Path, urls: Iterable[str], category: str,
) -> dict[str, int]:
    """v3.5 bulk category editor: force a category onto specific stories.

    Any multipart series touched is expanded to include every sibling part
    (a series always lives in one category folder) and the whole group is
    marked ``category_locked`` so a later recategorize/settings-alias-edit
    pass leaves the manual choice alone -- see :func:`organize_urls` and
    :func:`rebuild_library`, which both check the flag before recomputing a
    row's category. Use :func:`bulk_unlock_category` to hand a story back to
    automatic title/content matching.
    """
    ensure_story_columns(conn)
    category = clean_component(category, "Uncategorized") if category.strip() else "Uncategorized"
    wanted = [str(url) for url in dict.fromkeys(urls) if str(url)]
    if not wanted:
        return {"stories": 0}

    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(f"SELECT url, series_title, part_number FROM stories WHERE url IN ({placeholders})", wanted).fetchall()
    expanded: set[str] = set(wanted)
    series_keys = {row["series_title"].casefold() for row in rows if row["part_number"] is not None and row["series_title"]}
    if series_keys:
        placeholders2 = ",".join("?" for _ in series_keys)
        for row in conn.execute(f"SELECT url FROM stories WHERE lower(series_title) IN ({placeholders2})", list(series_keys)):
            expanded.add(row["url"])

    placeholders3 = ",".join("?" for _ in expanded)
    conn.execute(
        f"UPDATE stories SET category=?, category_source='manual', category_locked=1 WHERE url IN ({placeholders3})",
        [category, *expanded],
    )
    conn.commit()

    changed_rows = conn.execute(
        f"SELECT * FROM stories WHERE url IN ({placeholders3}) AND status IN ('verified','review')", list(expanded)
    ).fetchall()
    copied = _place_rows(conn, output_dir, changed_rows)
    conn.commit()
    kindle = write_kindle_manifest(conn, output_dir, force=True)
    return {"stories": copied, "affected": len(expanded), **kindle}


def bulk_unlock_category(
    conn: sqlite3.Connection, output_dir: Path, urls: Iterable[str], categories: Iterable[str],
    category_aliases: object = None,
) -> dict[str, int]:
    """Undo :func:`bulk_set_category`: release the lock and recompute normally."""
    ensure_story_columns(conn)
    wanted = [str(url) for url in dict.fromkeys(urls) if str(url)]
    if not wanted:
        return {"stories": 0}
    rows = conn.execute(
        f"SELECT url, series_title, part_number FROM stories WHERE url IN ({','.join('?' for _ in wanted)})", wanted
    ).fetchall()
    expanded: set[str] = set(wanted)
    series_keys = {row["series_title"].casefold() for row in rows if row["part_number"] is not None and row["series_title"]}
    if series_keys:
        placeholders2 = ",".join("?" for _ in series_keys)
        for row in conn.execute(f"SELECT url FROM stories WHERE lower(series_title) IN ({placeholders2})", list(series_keys)):
            expanded.add(row["url"])
    placeholders3 = ",".join("?" for _ in expanded)
    conn.execute(f"UPDATE stories SET category_locked=0 WHERE url IN ({placeholders3})", list(expanded))
    conn.commit()
    return organize_urls(conn, output_dir, categories, expanded, category_aliases)


def list_series(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """v3.5 series manager: every multipart series with its parts and gaps."""
    ensure_story_columns(conn)
    rows = conn.execute(
        "SELECT url, title, series_title, part_number, category, status, organized_file FROM stories "
        "WHERE part_number IS NOT NULL ORDER BY series_title COLLATE NOCASE, part_number"
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["series_title"].casefold()
        group = groups.setdefault(key, {"series_title": row["series_title"], "category": row["category"], "parts": []})
        group["parts"].append({
            "url": row["url"], "title": row["title"], "part_number": row["part_number"],
            "status": row["status"], "organized_file": row["organized_file"],
        })
    result = []
    for group in groups.values():
        numbers = sorted(p["part_number"] for p in group["parts"])
        missing = [n for n in range(1, numbers[-1]) if n not in numbers] if numbers else []
        result.append({**group, "part_count": len(numbers), "missing_parts": missing})
    result.sort(key=lambda g: str(g["series_title"]).casefold())
    return result
