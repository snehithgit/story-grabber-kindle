#!/usr/bin/env python3
"""Build the Kindle Story Reader manifest from the organized story library.

The generated ``content_output/library/library.json`` is intentionally a
portable, read-only catalog.  Story Grabber remains authoritative for titles,
categories, multipart grouping and part ordering; the Kindle tracks only local
reading state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _epoch(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        # Formatter timestamps are ISO-8601 and normally include +00:00.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int(dt.timestamp()))
    except (ValueError, OverflowError, OSError):
        return 0


def _stable_url_id(prefix: str, url: str) -> str:
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _manifest_part(row: sqlite3.Row) -> dict[str, Any]:
    number = int(row["part_number"] or 1)
    if row["part_number"] is None:
        title = str(row["title"] or "Story")
    else:
        title = f"Part {number:03d}"
    return {
        "number": number,
        "title": title,
        "path": str(row["organized_file"] or "").replace("\\", "/"),
    }


def build_manifest(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a Kindle-compatible catalog from already-organized DB rows."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}
    added_expr = "added_at" if "added_at" in columns else "updated_at AS added_at"
    rows = conn.execute(
        f"""
        SELECT url,title,category,series_title,part_number,organized_file,
               {added_expr},updated_at
        FROM stories
        WHERE status IN ('verified','review') AND organized_file<>''
        ORDER BY category COLLATE NOCASE, series_title COLLATE NOCASE,
                 CASE WHEN part_number IS NULL THEN 0 ELSE part_number END,
                 title COLLATE NOCASE
        """
    ).fetchall()

    singles: list[dict[str, Any]] = []
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["part_number"] is None:
            added = _epoch(row["added_at"]) or _epoch(row["updated_at"])
            singles.append({
                "id": _stable_url_id("story", row["url"]),
                "title": str(row["title"] or "Untitled Story"),
                "category": str(row["category"] or "Uncategorized"),
                "addedAt": added,
                "parts": [_manifest_part(row)],
            })
            continue
        # Group identity intentionally ignores category so recategorization does
        # not reset Kindle reading progress. Series titles are normalized by the
        # organizer before this point.
        key = str(row["series_title"] or row["title"] or "Untitled Story").casefold()
        groups.setdefault(key, []).append(row)

    multipart: list[dict[str, Any]] = []
    for group_rows in groups.values():
        ordered = sorted(group_rows, key=lambda r: (int(r["part_number"] or 0), str(r["url"])))
        # Prefer Part 1 as the permanent anchor. If an incomplete series was
        # first imported without Part 1, use the current lowest numbered part.
        anchor = next((r for r in ordered if int(r["part_number"] or 0) == 1), ordered[0])
        added_values = [_epoch(r["added_at"]) or _epoch(r["updated_at"]) for r in ordered]
        multipart.append({
            "id": _stable_url_id("series", anchor["url"]),
            "title": str(anchor["series_title"] or anchor["title"] or "Untitled Story"),
            "category": str(anchor["category"] or "Uncategorized"),
            # A newly added part should make the series appear in Recently Added.
            "addedAt": max(added_values or [0]),
            "parts": [_manifest_part(row) for row in ordered],
        })

    stories = singles + multipart
    stories.sort(key=lambda item: (str(item["title"]).casefold(), str(item["id"])))
    part_count = sum(len(item["parts"]) for item in stories)
    return {
        "version": 1,
        "generatedBy": "story-grabber-v3.3",
        "generatedAt": int(datetime.now(timezone.utc).timestamp()),
        "storyCount": len(stories),
        "partCount": part_count,
        "stories": stories,
    }


def _kindle_state_table(conn: sqlite3.Connection) -> None:
    # Callers that open a bare sqlite3.connect() (kindle_export.py's own CLI,
    # and older tests) never run db_migrations.migrate(); create defensively.
    conn.execute("CREATE TABLE IF NOT EXISTS kindle_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")


def _kindle_state_get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM kindle_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def _kindle_state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kindle_state(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _fingerprint(manifest: dict[str, Any]) -> dict[str, str]:
    """story id -> short hash of its parts, used to diff against the last export."""
    out: dict[str, str] = {}
    for story in manifest["stories"]:
        digest = hashlib.sha256(json.dumps(story["parts"], sort_keys=True).encode("utf-8")).hexdigest()[:12]
        out[str(story["id"])] = digest
    return out


def write_kindle_manifest(
    conn: sqlite3.Connection, output_dir: Path, *, min_interval: float = 0.0, force: bool = False,
) -> dict[str, int]:
    """Write ``library/library.json`` (+ ``changes.json``) and return summary counts.

    ``min_interval`` (v3.4 Phase 5, "debounced Kindle export") lets a caller
    that runs in a tight per-item loop -- auto_scrape.py with a very small
    batch size is the realistic case -- skip rewriting the whole catalog more
    often than every ``min_interval`` seconds. The guard is stored in
    ``kindle_state`` so it works across separate process invocations, not
    just within one. It defaults to 0 (always write immediately), which is
    what every existing caller and test relies on; only auto_scrape.py opts
    into a nonzero interval.

    ``changes.json`` records what moved since the previous export --
    ``{"added": N, "updated": N, "removed": N, ...Ids}`` -- so a future Kindle
    client could sync incrementally instead of re-reading the whole catalog.
    """
    library_dir = output_dir / "library"
    library_dir.mkdir(parents=True, exist_ok=True)
    _kindle_state_table(conn)

    now = time.time()
    if not force and min_interval > 0:
        last_write = _kindle_state_get(conn, "last_write_at")
        cached_counts = _kindle_state_get(conn, "last_counts")
        if last_write and cached_counts:
            try:
                if now - float(last_write) < min_interval:
                    return json.loads(cached_counts)
            except ValueError:
                pass

    manifest = build_manifest(conn)
    atomic_json(library_dir / "library.json", manifest)

    fingerprint = _fingerprint(manifest)
    previous_raw = _kindle_state_get(conn, "fingerprint")
    previous = json.loads(previous_raw) if previous_raw else {}
    added = sorted(set(fingerprint) - set(previous))
    removed = sorted(set(previous) - set(fingerprint))
    updated = sorted(key for key in (set(fingerprint) & set(previous)) if fingerprint[key] != previous[key])
    changes = {
        "generatedAt": int(now),
        "added": len(added), "updated": len(updated), "removed": len(removed),
        "addedIds": added[:200], "updatedIds": updated[:200], "removedIds": removed[:200],
    }
    atomic_json(library_dir / "changes.json", changes)

    counts = {"kindle_stories": int(manifest["storyCount"]), "kindle_parts": int(manifest["partCount"])}
    _kindle_state_set(conn, "fingerprint", json.dumps(fingerprint))
    _kindle_state_set(conn, "last_write_at", str(now))
    _kindle_state_set(conn, "last_counts", json.dumps(counts))
    conn.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Kindle Story Reader library.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "content_output")
    args = parser.parse_args()
    db = args.output / "story_library.sqlite3"
    if not db.is_file():
        raise SystemExit(f"Story library database not found: {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        summary = write_kindle_manifest(conn, args.output)
    finally:
        conn.close()
    print(
        f"Kindle manifest: {summary['kindle_stories']} stories, "
        f"{summary['kindle_parts']} part files -> {args.output / 'library' / 'library.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
