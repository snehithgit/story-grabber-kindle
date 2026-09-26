#!/usr/bin/env python3
"""v3.5: duplicate-story detection.

Three independent, increasingly fuzzy passes, each cheap enough to run from
a button in the Library UI on a normal-sized story database:

1. **Exact content hash** -- two different URLs whose raw HTML hashed to the
   same ``raw_sha256`` are the same page, scraped twice (a re-crawl, or two
   sites mirroring each other verbatim).
2. **Normalized title** -- singles sharing a casefolded/whitespace-collapsed
   title, or two different multipart series claiming the same series name
   *and* the same part number. Legitimate multipart siblings (same series,
   different part numbers) are never flagged.
3. **Fuzzy text similarity** -- titles that are *almost* the same (typo, a
   site-specific suffix, a re-punctuated re-post) among stories of a similar
   length, using :mod:`difflib` so nothing beyond the standard library is
   required. Bounded per bucket so this stays cheap even at scale.

Nothing here is an AI quality score -- every check is a deterministic
comparison over data already in the ``stories`` table.
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from typing import Any

MULTISPACE_RE = re.compile(r"\s+")
FUZZY_BUCKET_MAX = 40  # skip fuzzy comparison inside oversized buckets (O(n^2) guard)
FUZZY_RATIO_THRESHOLD = 0.88


def _normalize_title(title: str) -> str:
    return MULTISPACE_RE.sub(" ", str(title or "")).strip().casefold()


def _story_brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "url": row["url"],
        "title": row["title"],
        "status": row["status"],
        "category": row["category"] if "category" in row.keys() else "",
        "series_title": row["series_title"] if "series_title" in row.keys() else "",
        "part_number": row["part_number"] if "part_number" in row.keys() else None,
        "added_at": row["added_at"] if "added_at" in row.keys() else row["updated_at"],
    }


def exact_content_duplicates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT raw_sha256, url, title, status, category, series_title, part_number, added_at
        FROM stories WHERE raw_sha256 <> '' ORDER BY raw_sha256
        """
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["raw_sha256"], []).append(row)
    result = []
    for digest, members in groups.items():
        if len(members) > 1:
            result.append({
                "reason": "exact_content_hash",
                "detail": "Identical raw page content (same source, or a mirrored/re-crawled URL).",
                "stories": [_story_brief(m) for m in members],
            })
    return result


def normalized_title_duplicates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT url, title, status, category, series_title, part_number, added_at FROM stories"
    ).fetchall()
    singles: dict[str, list[sqlite3.Row]] = {}
    parts: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for row in rows:
        if row["part_number"] is None:
            singles.setdefault(_normalize_title(row["title"]), []).append(row)
        else:
            key = (_normalize_title(row["series_title"] or row["title"]), int(row["part_number"]))
            parts.setdefault(key, []).append(row)

    result = []
    for _, members in singles.items():
        if len(members) > 1:
            result.append({
                "reason": "duplicate_title",
                "detail": "Two single (non-multipart) stories share the same title.",
                "stories": [_story_brief(m) for m in members],
            })
    for (series, part_number), members in parts.items():
        urls = {m["url"] for m in members}
        if len(urls) > 1:
            result.append({
                "reason": "duplicate_series_part",
                "detail": f"Two different URLs both claim to be \"{series.title()}\" part {part_number}.",
                "stories": [_story_brief(m) for m in members],
            })
    return result


def fuzzy_title_duplicates(
    conn: sqlite3.Connection, *, already_flagged: set[str] | None = None, stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    already_flagged = already_flagged or set()
    if stats is not None:
        stats.setdefault("skipped_buckets", 0)
        stats.setdefault("skipped_stories", 0)
    rows = conn.execute(
        "SELECT url, title, status, category, series_title, part_number, added_at, words FROM stories "
        "WHERE part_number IS NULL"
    ).fetchall()
    candidates = [row for row in rows if row["url"] not in already_flagged]
    buckets: dict[int, list[sqlite3.Row]] = {}
    for row in candidates:
        bucket_key = int(row["words"] or 0) // 30
        buckets.setdefault(bucket_key, []).append(row)

    result: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
    for bucket_key, members in buckets.items():
        if len(members) < 2:
            continue
        if len(members) > FUZZY_BUCKET_MAX:
            if stats is not None:
                stats["skipped_buckets"] += 1
                stats["skipped_stories"] += len(members)
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a["url"] == b["url"]:
                    continue
                ratio = SequenceMatcher(None, _normalize_title(a["title"]), _normalize_title(b["title"])).ratio()
                if ratio >= FUZZY_RATIO_THRESHOLD and _normalize_title(a["title"]) != _normalize_title(b["title"]):
                    pair_key = frozenset((a["url"], b["url"]))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    result.append({
                        "reason": "similar_title",
                        "detail": f"Titles are {ratio:.0%} similar.",
                        "stories": [_story_brief(a), _story_brief(b)],
                    })
    return result


def find_duplicates(conn: sqlite3.Connection, *, include_fuzzy: bool = True) -> dict[str, Any]:
    """Run all detection passes. Returns groups plus a flat summary count."""
    exact = exact_content_duplicates(conn)
    titles = normalized_title_duplicates(conn)
    flagged_urls = {s["url"] for group in (exact + titles) for s in group["stories"]}
    fuzzy_stats = {"skipped_buckets": 0, "skipped_stories": 0}
    fuzzy = fuzzy_title_duplicates(conn, already_flagged=flagged_urls, stats=fuzzy_stats) if include_fuzzy else []
    groups = exact + titles + fuzzy
    return {
        "groups": groups,
        "group_count": len(groups),
        "story_count": len({s["url"] for group in groups for s in group["stories"]}),
        "fuzzy_skipped_buckets": fuzzy_stats["skipped_buckets"],
        "fuzzy_skipped_stories": fuzzy_stats["skipped_stories"],
    }
