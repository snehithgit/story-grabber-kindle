#!/usr/bin/env python3
"""v3.6: library integrity checking, repair, and backup/restore.

Three independent capabilities, all reachable from a "Maintenance" panel:

* :func:`integrity_check` -- read-only. DB row <-> file existence in both
  directions, duplicate series/part numbers, and orphaned files in the
  processing directories.
* :func:`repair` -- re-derives everything that *can* be safely rebuilt from
  the database (the Kindle manifest, the FTS5 search index, the organized
  library tree). It never deletes a raw/formatted/romanized page: those are
  the app's source-of-truth evidence, and re-scraping is the only correct
  way to replace one that's actually missing.
* :func:`create_backup` / :func:`restore_backup` -- a portable zip of the
  SQLite database (via SQLite's own online backup API, so it's never a
  torn/inconsistent copy even while the server is writing) plus settings and
  the generated Kindle catalog. Raw HTML is intentionally excluded -- it can
  be many GB and is already what a re-scrape would recreate; the point of a
  backup here is "don't lose the day-to-day scrape, format, and category
  DECISIONS if the disk dies," matching what the app can recrawl versus what
  it cannot recompute from scratch (categorization choices, review
  acceptances, manual formatting edits).
"""
from __future__ import annotations

import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Integrity check
# ---------------------------------------------------------------------------

def integrity_check(conn: sqlite3.Connection, output_dir: Path) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    rows = conn.execute(
        "SELECT url, status, raw_file, formatted_file, romanized_file, organized_file, "
        "series_title, part_number FROM stories"
    ).fetchall()

    referenced = {"pages": set(), "formatted_pages": set(), "romanized_pages": set()}
    for row in rows:
        if row["raw_file"]:
            referenced["pages"].add(row["raw_file"])
            if not (output_dir / "pages" / row["raw_file"]).is_file():
                issues.append({"type": "missing_raw_file", "url": row["url"], "detail": row["raw_file"]})
        if row["status"] in {"verified", "review"}:
            if row["formatted_file"]:
                referenced["formatted_pages"].add(row["formatted_file"])
                if not (output_dir / "formatted_pages" / row["formatted_file"]).is_file():
                    issues.append({"type": "missing_formatted_file", "url": row["url"], "detail": row["formatted_file"]})
            if row["romanized_file"]:
                referenced["romanized_pages"].add(row["romanized_file"])
                if not (output_dir / "romanized_pages" / row["romanized_file"]).is_file():
                    issues.append({"type": "missing_romanized_file", "url": row["url"], "detail": row["romanized_file"]})
            if row["organized_file"] and not (output_dir / "library" / row["organized_file"]).is_file():
                issues.append({"type": "missing_organized_file", "url": row["url"], "detail": row["organized_file"]})

    for stage, referenced_names in referenced.items():
        directory = output_dir / stage
        if not directory.is_dir():
            continue
        for path in directory.glob("*.html"):
            if path.name not in referenced_names:
                issues.append({"type": f"orphan_{stage}_file", "url": "", "detail": path.name})

    dup_parts = conn.execute(
        """
        SELECT series_title, part_number, COUNT(*) AS c FROM stories
        WHERE part_number IS NOT NULL AND status IN ('verified','review')
        GROUP BY lower(series_title), part_number HAVING c > 1
        """
    ).fetchall()
    for row in dup_parts:
        issues.append({
            "type": "duplicate_part_number", "url": "",
            "detail": f'"{row["series_title"]}" part {row["part_number"]} ({row["c"]} stories)',
        })

    by_type: dict[str, int] = {}
    for issue in issues:
        by_type[issue["type"]] = by_type.get(issue["type"], 0) + 1

    return {"issues": issues, "issue_count": len(issues), "by_type": by_type, "checked_at": utc_now()}


# ---------------------------------------------------------------------------
# Repair (rebuild-from-DB only; never deletes source evidence)
# ---------------------------------------------------------------------------

def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    """Rebuild stories_fts from scratch. Cheap insurance against index drift."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stories_fts'").fetchone()
    if not exists:
        return 0
    conn.execute("INSERT INTO stories_fts(stories_fts) VALUES ('delete-all')")
    total = int(conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0])
    if total:
        conn.execute(
            "INSERT INTO stories_fts(rowid, title, url, original_text, romanized_text) "
            "SELECT rowid, title, url, original_text, romanized_text FROM stories"
        )
    conn.commit()
    return total


def repair(
    conn: sqlite3.Connection, output_dir: Path, categories: list[str], category_aliases: object = None,
    *, sublinks_path: Path | None = None,
) -> dict[str, Any]:
    from story_organizer import rebuild_library  # local import: avoids a module cycle at import time
    import links_store

    fts_rows = rebuild_fts_index(conn)
    if sublinks_path is not None:
        links_store.sync_sublinks(conn, sublinks_path, force=True)
    links_store.sync_progress(conn, output_dir, force=True)
    organization = rebuild_library(conn, output_dir, categories, category_aliases)
    return {
        "fts_reindexed_rows": fts_rows,
        "organization": organization,
        "repaired_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# Database maintenance
# ---------------------------------------------------------------------------

def analyze(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("ANALYZE")


def vacuum(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("VACUUM")


def storage_health(conn: sqlite3.Connection, db_path: Path, output_dir: Path) -> dict[str, Any]:
    def _dir_size(path: Path) -> int:
        if not path.is_dir():
            return 0
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    counts = conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    link_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    return {
        "database_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "story_count": int(counts),
        "link_count": int(link_count),
        "raw_pages_bytes": _dir_size(output_dir / "pages"),
        "formatted_pages_bytes": _dir_size(output_dir / "formatted_pages"),
        "romanized_pages_bytes": _dir_size(output_dir / "romanized_pages"),
        "library_bytes": _dir_size(output_dir / "library"),
    }


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

def create_backup(
    conn: sqlite3.Connection, db_path: Path, root: Path, output_dir: Path, backups_dir: Path,
    *, keep: int = 5, note: str = "",
) -> dict[str, Any]:
    """Zip a consistent DB snapshot + settings + Kindle catalog; rotate old backups."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond precision (not just seconds) so two backups triggered in
    # quick succession -- a manual click right after a scheduled one, or a
    # tight test loop -- never collide on the same filename.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    filename = f"backup-{timestamp}.zip"
    zip_path = backups_dir / filename
    while zip_path.exists():  # belt-and-suspenders against any residual collision
        timestamp += "1"
        filename = f"backup-{timestamp}.zip"
        zip_path = backups_dir / filename

    # SQLite's backup API copies a transactionally consistent snapshot even
    # while WAL writes are in flight elsewhere -- never a half-written file.
    snapshot_path = backups_dir / f".snapshot-{timestamp}.sqlite3"
    try:
        snapshot_conn = sqlite3.connect(snapshot_path)
        try:
            conn.backup(snapshot_conn)
            # Do not publish a transactionally consistent snapshot that is
            # already corrupt.  Verify the copy before it is zipped/recorded.
            integrity = snapshot_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup snapshot failed integrity check: {integrity}")
        finally:
            snapshot_conn.close()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot_path, "story_library.sqlite3")
            settings_path = root / "settings.json"
            if settings_path.is_file():
                zf.write(settings_path, "settings.json")
            sites_path = root / "sites.txt"
            if sites_path.is_file():
                zf.write(sites_path, "sites.txt")
            library_json = output_dir / "library" / "library.json"
            if library_json.is_file():
                zf.write(library_json, "library/library.json")
            changes_json = output_dir / "library" / "changes.json"
            if changes_json.is_file():
                zf.write(changes_json, "library/changes.json")
    except Exception:
        # A ZipFile can exist even when writing it failed (for example disk
        # full).  It is not a valid backup and is not recorded in SQLite, so
        # remove it rather than leaving an orphan that looks usable on disk.
        zip_path.unlink(missing_ok=True)
        raise
    finally:
        # Never leave the intermediate snapshot behind, including when the
        # backup or zip step above raised partway through.
        snapshot_path.unlink(missing_ok=True)

    size_bytes = zip_path.stat().st_size
    created_at = utc_now()
    conn.execute(
        "INSERT INTO backups(filename, created_at, size_bytes, note) VALUES (?,?,?,?)",
        (filename, created_at, size_bytes, note),
    )
    conn.commit()
    prune_backups(conn, backups_dir, keep=keep)
    return {"filename": filename, "size_bytes": size_bytes, "created_at": created_at}


def list_backups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, filename, created_at, size_bytes, note FROM backups ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def prune_backups(conn: sqlite3.Connection, backups_dir: Path, *, keep: int = 5) -> int:
    rows = conn.execute("SELECT id, filename FROM backups ORDER BY id DESC").fetchall()
    removed = 0
    for row in rows[max(0, keep):]:
        path = backups_dir / row["filename"]
        path.unlink(missing_ok=True)
        conn.execute("DELETE FROM backups WHERE id=?", (row["id"],))
        removed += 1
    conn.commit()
    return removed


def restore_backup(root: Path, output_dir: Path, db_path: Path, backups_dir: Path, filename: str) -> dict[str, Any]:
    """Restore the database and settings from a backup zip made by create_backup.

    The current database is moved aside (never deleted) as
    ``story_library.sqlite3.before-restore`` so a bad restore can be undone
    by hand.
    """
    zip_path = backups_dir / Path(filename).name  # Path(...).name defeats a path-traversal filename
    if not zip_path.is_file():
        raise FileNotFoundError(f"Backup not found: {filename}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        if "story_library.sqlite3" not in names:
            raise ValueError("Backup archive is missing story_library.sqlite3")
        if db_path.is_file():
            backup_aside = db_path.with_name(db_path.name + ".before-restore")
            backup_aside.unlink(missing_ok=True)
            # The live database uses WAL mode.  A plain copy of only the main
            # .sqlite3 file can miss committed pages that still live in -wal,
            # so take the rollback copy through SQLite's online backup API.
            live_conn = sqlite3.connect(db_path)
            aside_conn = sqlite3.connect(backup_aside)
            try:
                live_conn.backup(aside_conn)
            finally:
                aside_conn.close()
                live_conn.close()
        for wal_suffix in ("-wal", "-shm"):
            (db_path.with_name(db_path.name + wal_suffix)).unlink(missing_ok=True)
        with zf.open("story_library.sqlite3") as source, open(db_path, "wb") as target:
            shutil.copyfileobj(source, target)
        if "settings.json" in names:
            with zf.open("settings.json") as source, open(root / "settings.json", "wb") as target:
                shutil.copyfileobj(source, target)
        if "sites.txt" in names:
            with zf.open("sites.txt") as source, open(root / "sites.txt", "wb") as target:
                shutil.copyfileobj(source, target)
        if "library/library.json" in names:
            (output_dir / "library").mkdir(parents=True, exist_ok=True)
            with zf.open("library/library.json") as source, open(output_dir / "library" / "library.json", "wb") as target:
                shutil.copyfileobj(source, target)
    return {"restored_from": filename, "restored_at": utc_now()}
