#!/usr/bin/env python3
"""Story Grabber web application.

Workflow:
    crawl links -> extract immutable raw HTML -> format + exact verification
    -> optional Telugu romanization -> searchable story library

Native launches are localhost-only by default. Docker/LAN access must be enabled
explicitly with --allow-lan; requests are then limited to private/loopback clients
and private/loopback Host headers, with same-origin POST protection.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SITES_FILE = ROOT / "sites.txt"
SUBLINKS_FILE = ROOT / "sublinks.json"
OUTPUT_DIR = ROOT / "content_output"
RAW_DIR = OUTPUT_DIR / "pages"
FORMATTED_DIR = OUTPUT_DIR / "formatted_pages"
ROMANIZED_DIR = OUTPUT_DIR / "romanized_pages"
LIBRARY_DB = OUTPUT_DIR / "story_library.sqlite3"
SETTINGS_FILE = ROOT / "settings.json"
SELECTED_LINKS_FILE = OUTPUT_DIR / "selected_links.json"
CRAWLER = ROOT / "cli" / "site_crawler.py"
PIPELINE = ROOT / "story_pipeline.py"
FORMATTER = ROOT / "story_formatter.py"
AUTO_SCRAPER = ROOT / "auto_scrape.py"
LIBRARY_DIR = OUTPUT_DIR / "library"
BACKUPS_DIR = ROOT / "backups"

from story_formatter import StoryFormatter, connect_library  # noqa: E402
from story_organizer import (  # noqa: E402
    bulk_set_category, bulk_unlock_category, list_series, normalize_categories,
    normalize_category_aliases, rebuild_library,
)
from kindle_export import write_kindle_manifest  # noqa: E402
import links_store  # noqa: E402
import job_store  # noqa: E402
import db_migrations  # noqa: E402
import duplicate_detector  # noqa: E402
import maintenance  # noqa: E402

DEFAULT_SETTINGS: dict[str, Any] = {
    "crawler_depth": 1,
    "crawler_max_pages": 1000,
    "crawler_delay": 0.25,
    "content_workers": 3,
    "content_delay_ms": 500,
    "content_limit": 0,
    "browser_mode": "background",
    "auto_romanize": True,
    "formatter_sentence_target": 4,
    "formatter_long_block_chars": 900,
    "formatter_review_block_chars": 1800,
    "stories_per_page": 50,
    "scrape_mode": "manual",
    "auto_scrape_batch_size": 20,
    "categories": [],
    "category_aliases": {},
    "site_profiles": {},
}

SETTINGS_LOCK = threading.Lock()
# Serializes destructive maintenance against engine startup.  The gate is
# always acquired before JobManager.lock to avoid a restore/start TOCTOU race.
MAINTENANCE_GATE = threading.Lock()


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def load_settings() -> dict[str, Any]:
    with SETTINGS_LOCK:
        result = dict(DEFAULT_SETTINGS)
        try:
            data = json.loads(SETTINGS_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                result.update(data)
        except (OSError, json.JSONDecodeError):
            pass
        return result


def sanitize_settings(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    current.update({
        "crawler_depth": clamp_int(payload.get("crawler_depth"), current["crawler_depth"], 0, 10),
        "crawler_max_pages": clamp_int(payload.get("crawler_max_pages"), current["crawler_max_pages"], 1, 250000),
        "crawler_delay": clamp_float(payload.get("crawler_delay"), current["crawler_delay"], 0.0, 60.0),
        "content_workers": clamp_int(payload.get("content_workers"), current["content_workers"], 1, 8),
        "content_delay_ms": clamp_int(payload.get("content_delay_ms"), current["content_delay_ms"], 0, 60000),
        "content_limit": clamp_int(payload.get("content_limit"), current["content_limit"], 0, 250000),
        "auto_romanize": bool(payload.get("auto_romanize", current["auto_romanize"])),
        "formatter_sentence_target": clamp_int(payload.get("formatter_sentence_target"), current["formatter_sentence_target"], 2, 8),
        "formatter_long_block_chars": clamp_int(payload.get("formatter_long_block_chars"), current["formatter_long_block_chars"], 300, 5000),
        "formatter_review_block_chars": clamp_int(payload.get("formatter_review_block_chars"), current["formatter_review_block_chars"], 800, 20000),
        "stories_per_page": clamp_int(payload.get("stories_per_page"), current["stories_per_page"], 10, 100),
        "auto_scrape_batch_size": clamp_int(payload.get("auto_scrape_batch_size"), current.get("auto_scrape_batch_size", 20), 1, 200),
    })
    mode = str(payload.get("browser_mode", current.get("browser_mode", "background")))
    current["browser_mode"] = mode if mode in {"background", "headless", "visible"} else "background"
    scrape_mode = str(payload.get("scrape_mode", current.get("scrape_mode", "manual"))).lower()
    current["scrape_mode"] = scrape_mode if scrape_mode in {"manual", "auto"} else "manual"
    categories = payload.get("categories", current.get("categories", []))
    if isinstance(categories, str):
        categories = categories.splitlines()
    current["categories"] = normalize_categories(categories if isinstance(categories, list) else [])
    aliases = payload.get("category_aliases", current.get("category_aliases", {}))
    current["category_aliases"] = normalize_category_aliases(aliases, current["categories"])
    current["site_profiles"] = normalize_site_profiles(payload.get("site_profiles", current.get("site_profiles", {})))
    return current


def normalize_site_profiles(raw: Any) -> dict[str, float]:
    """v3.7: per-host crawl-delay floor, e.g. ``{"slow-site.example": 3.0}``.

    Accepts either a dict (as sent by the JSON API) or "host = seconds"
    lines (as typed into the Settings textarea, mirroring how category
    aliases are edited). Unparseable entries are dropped rather than
    rejecting the whole settings save.
    """
    if isinstance(raw, str):
        pairs: list[tuple[Any, Any]] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            host, _, value = stripped.partition("=")
            pairs.append((host.strip(), value.strip()))
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        pairs = []
    result: dict[str, float] = {}
    for host, value in pairs:
        host_name = str(host).strip().lower()
        if not host_name:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        result[host_name] = max(0.0, min(60.0, seconds))
    return result


def valid_sites(raw: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid HTTP(S) URL: {value}")
        if value not in seen:
            result.append(value)
            seen.add(value)
    if not result:
        raise ValueError("Enter at least one website URL.")
    if len(result) > 25:
        raise ValueError("A maximum of 25 starting URLs is allowed per crawl.")
    return result


def sync_links(conn: sqlite3.Connection) -> None:
    """Bring the ``links`` table up to date with sublinks.json/manifest.json/progress.jsonl.

    Cheap to call on every request: each source is guarded by a file token
    (sublinks.json, manifest.json) or a byte offset (progress.jsonl), so an
    unchanged file costs one stat() call rather than a full re-parse. See
    links_store.py for why this replaced the old per-request JSON merge.
    """
    links_store.sync_all(conn, SUBLINKS_FILE, OUTPUT_DIR)


@dataclass
class Job:
    name: str
    state: str = "idle"
    command: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    stop_requested: bool = False
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    run_id: int | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "logs": self.logs[-400:],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "error": self.error,
            "stop_requested": self.stop_requested,
        }


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs = {name: Job(name) for name in ("auto", "links", "content", "format")}

    def snapshot(self, name: str) -> dict[str, Any]:
        with self.lock:
            if name not in self.jobs:
                raise KeyError(name)
            return self.jobs[name].snapshot()

    def all_snapshots(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {name: job.snapshot() for name, job in self.jobs.items()}

    def any_running(self) -> bool:
        with self.lock:
            return any(job.state in {"running", "stopping"} for job in self.jobs.values())

    def start(self, name: str, command: list[str]) -> None:
        # Destructive maintenance (restore/rebuild/vacuum) holds the same gate
        # for its complete critical section.  Marking the job running while
        # holding it closes the old check-then-start race.
        with MAINTENANCE_GATE:
            with self.lock:
                if name not in self.jobs:
                    raise RuntimeError(f"Unknown engine: {name}")
                if any(job.state in {"running", "stopping"} for job in self.jobs.values()):
                    raise RuntimeError("Another engine is already running.")
                job = self.jobs[name]
                job.state = "running"
                job.command = list(command)
                job.logs = ["Starting engine..."]
                job.started_at = time.time()
                job.finished_at = None
                job.exit_code = None
                job.error = None
                job.stop_requested = False
                job.process = None
                job.run_id = None
            try:
                conn = connect_library(LIBRARY_DB)
                try:
                    job.run_id = job_store.start_run(conn, name, command)
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                job.run_id = None  # history is best-effort; never block the engine on it
            threading.Thread(target=self._run, args=(name,), daemon=True).start()

    def stop(self, name: str) -> None:
        with self.lock:
            if name not in self.jobs:
                raise RuntimeError(f"Unknown engine: {name}")
            job = self.jobs[name]
            if job.state not in {"running", "stopping"}:
                raise RuntimeError(f"The {name} engine is not running.")
            job.stop_requested = True
            job.state = "stopping"
            job.logs.append("Stop requested; terminating the engine...")
            process = job.process
        if process is not None:
            self._terminate(process)
            threading.Thread(target=self._force_after_grace, args=(process,), daemon=True).start()

    def shutdown(self) -> None:
        with self.lock:
            processes = []
            for job in self.jobs.values():
                if job.process and job.process.poll() is None:
                    job.stop_requested = True
                    job.state = "stopping"
                    processes.append(job.process)
        for process in processes:
            self._terminate(process)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._terminate(process, force=True)

    @staticmethod
    def _terminate(process: subprocess.Popen[str], force: bool = False) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                if force:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                    )
                else:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass

    @classmethod
    def _force_after_grace(cls, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls._terminate(process, force=True)

    def _run(self, name: str) -> None:
        with self.lock:
            command = list(self.jobs[name].command)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHON"] = sys.executable
        try:
            options: dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, **options,
            )
            with self.lock:
                job = self.jobs[name]
                job.process = process
                should_stop = job.stop_requested
            if should_stop:
                self._terminate(process)
            assert process.stdout is not None
            for line in process.stdout:
                with self.lock:
                    logs = self.jobs[name].logs
                    logs.append(line.rstrip())
                    if len(logs) > 1500:
                        del logs[:300]
            process.stdout.close()
            code = process.wait()
            with self.lock:
                job = self.jobs[name]
                job.exit_code = code
                job.finished_at = time.time()
                job.process = None
                if job.stop_requested:
                    job.state = "stopped"
                    job.error = None
                    job.logs.append("Engine stopped by user.")
                elif code == 0:
                    job.state = "complete"
                    job.error = None
                    job.logs.append("Engine completed successfully.")
                else:
                    job.state = "failed"
                    job.error = f"Engine exited with code {code}."
                run_id, final_state, exit_code, error = job.run_id, job.state, job.exit_code, job.error
            self._record_finish(run_id, final_state, exit_code, error)
        except Exception as exc:
            with self.lock:
                job = self.jobs[name]
                job.state = "failed"
                job.error = str(exc)
                job.logs.append(f"ERROR: {exc}")
                job.finished_at = time.time()
                job.process = None
                run_id = job.run_id
            self._record_finish(run_id, "failed", None, str(exc))

    @staticmethod
    def _record_finish(run_id: int | None, state: str, exit_code: int | None, error: str | None) -> None:
        """Best-effort job-history write; never lets history recording break the engine."""
        if run_id is None:
            return
        try:
            conn = connect_library(LIBRARY_DB)
            try:
                job_store.finish_run(conn, run_id, state=state, exit_code=exit_code, error=error or "")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


JOBS = JobManager()


def _fts_match_expression(q: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression, or None if unusable.

    Each token is double-quoted (so punctuation/URLs can't be interpreted as
    FTS5 query syntax) and ANDed together; the final token gets a trailing
    ``*`` for prefix matching, which gives live-search-as-you-type behavior.
    """
    tokens = [t.replace('"', '""') for t in q.strip().split() if t]
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens[:-1]] + [f'"{tokens[-1]}"*']
    return " AND ".join(quoted)


def query_library_conn(
    conn: sqlite3.Connection, *, q: str = "", status: str = "all", language: str = "all",
    category: str = "all", page: int = 1, per_page: int = 50, sort: str = "newest",
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status in {"verified", "review", "failed"}:
        where.append("status=?")
        params.append(status)
    if language == "telugu":
        where.append("telugu=1")
    elif language == "romanized":
        where.append("romanized=1")
    elif language == "english":
        where.append("telugu=0")
    if category and category != "all":
        where.append("category=?")
        params.append(category)
    q = q.strip()
    columns = (
        "url,title,source_host,words,status,integrity_exact,telugu,romanized,"
        "paragraphs,dialogue_breaks,dialogue_turns,unsplit_dialogue,max_paragraph_chars,"
        "quality_pass,formatter_version,review_reason,error,updated_at,manual_accept,"
        "series_title,part_number,category,category_source,organized_file"
    )
    order = {
        "newest": "updated_at DESC",
        "oldest": "updated_at ASC",
        "title": "title COLLATE NOCASE ASC",
        "longest": "words DESC",
    }.get(sort, "updated_at DESC")

    match_expr = _fts_match_expression(q) if q else None
    if match_expr and db_migrations.fts_available(conn):
        # v3.4 P0: FTS5 instead of a %LIKE% full-table scan across large text
        # columns. Falls back to LIKE automatically if FTS5 isn't compiled in.
        fts_clause = "s.rowid IN (SELECT rowid FROM stories_fts WHERE stories_fts MATCH ?)"
        where_all = [fts_clause, *where]
        params_all = [match_expr, *params]
        clause = " WHERE " + " AND ".join(where_all)
        total = int(conn.execute(f"SELECT COUNT(*) FROM stories s{clause}", params_all).fetchone()[0])
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"SELECT {columns} FROM stories s{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params_all, per_page, offset],
        ).fetchall()
    else:
        if q:
            like = f"%{q}%"
            where.append("(title LIKE ? OR url LIKE ? OR original_text LIKE ? OR romanized_text LIKE ?)")
            params.extend([like, like, like, like])
        clause = " WHERE " + " AND ".join(where) if where else ""
        total = int(conn.execute(f"SELECT COUNT(*) FROM stories{clause}", params).fetchone()[0])
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"SELECT {columns} FROM stories{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, per_page, offset],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total else 0,
    }


def query_library(**kwargs: Any) -> dict[str, Any]:
    if not LIBRARY_DB.is_file():
        page = int(kwargs.get("page", 1))
        per_page = int(kwargs.get("per_page", 50))
        return {"items": [], "total": 0, "page": page, "per_page": per_page, "pages": 0}
    conn = connect_library(LIBRARY_DB)
    try:
        return query_library_conn(conn, **kwargs)
    finally:
        conn.close()


def get_story(url: str) -> dict[str, Any] | None:
    if not LIBRARY_DB.is_file():
        return None
    conn = connect_library(LIBRARY_DB)
    try:
        row = conn.execute("SELECT * FROM stories WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def story_counts_conn(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {"total": 0, "verified": 0, "review": 0, "failed": 0, "romanized": 0}
    for status, count in conn.execute("SELECT status,COUNT(*) FROM stories GROUP BY status"):
        counts["total"] += int(count)
        if status in counts:
            counts[status] = int(count)
    counts["romanized"] = int(conn.execute("SELECT COUNT(*) FROM stories WHERE romanized=1").fetchone()[0])
    return counts


def story_counts() -> dict[str, int]:
    if not LIBRARY_DB.is_file():
        return {"total": 0, "verified": 0, "review": 0, "failed": 0, "romanized": 0}
    conn = connect_library(LIBRARY_DB)
    try:
        return story_counts_conn(conn)
    finally:
        conn.close()


def build_summary() -> dict[str, Any]:
    """v3.4 P0/P1: a handful of indexed SQL queries instead of a full JSON merge.

    Everything here now comes from one shared connection: sync links from
    disk (cheap when nothing changed), then read aggregate counts straight
    from SQLite -- no more looping every discovered link/story in Python on
    every Dashboard refresh.
    """
    settings = load_settings()
    conn = connect_library(LIBRARY_DB)
    try:
        sync_links(conn)
        counts = links_store.dashboard_counts(conn)
        stories = story_counts_conn(conn)
        recent = query_library_conn(conn, page=1, per_page=8, sort="newest")["items"]
    finally:
        conn.close()
    sites = []
    if SITES_FILE.is_file():
        try:
            sites = [line.strip() for line in SITES_FILE.read_text("utf-8").splitlines() if line.strip()]
        except OSError:
            pass
    return {
        "sites": sites,
        "site_count": len(counts["sources"]) or len(sites),
        "links": counts["links"],
        "raw_success": counts["scraped"],
        "raw_failed": counts["failed"],
        "pending_links": counts["pending"],
        "stories": stories,
        "sources": counts["sources"],
        "recent": recent,
        "categories": settings.get("categories", []),
        "settings": settings,
    }


def list_links(params: dict[str, list[str]]) -> dict[str, Any]:
    q = params.get("q", [""])[0] or ""
    status_filter = (params.get("status", ["all"])[0] or "all").lower()
    page = clamp_int(params.get("page", [1])[0], 1, 1, 1_000_000)
    per_page = clamp_int(params.get("per_page", [50])[0], 50, 10, 100)
    conn = connect_library(LIBRARY_DB)
    try:
        sync_links(conn)
        return links_store.list_links_db(conn, q=q, status=status_filter, page=page, per_page=per_page)
    finally:
        conn.close()


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AppHandler(BaseHTTPRequestHandler):
    server_version = "StoryGrabber"
    sys_version = ""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()

    @staticmethod
    def _private_or_loopback(address: ipaddress._BaseAddress) -> bool:
        return bool(address.is_loopback or address.is_private or address.is_link_local)

    def _request_hostname(self) -> str:
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return ""
        try:
            return (urlparse("//" + raw).hostname or "").rstrip(".").lower()
        except ValueError:
            return ""

    def local_request_allowed(self) -> bool:
        try:
            client = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False

        hostname = self._request_hostname()
        if not hostname:
            return False

        if hostname == "localhost":
            host_ok = True
        else:
            try:
                host_ip = ipaddress.ip_address(hostname)
            except ValueError:
                host_ok = False
            else:
                host_ok = self._private_or_loopback(host_ip)

        allow_lan = bool(getattr(self.server, "allow_lan", False))
        if allow_lan:
            return self._private_or_loopback(client) and host_ok
        return client.is_loopback and host_ok and (hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback)

    def same_origin_post_allowed(self) -> bool:
        # Non-browser clients may omit Origin; the network/Host checks above still apply.
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        request_host = (self.headers.get("Host") or "").strip().lower()
        return bool(parsed.netloc and parsed.netloc.lower() == request_host)

    def reject_non_local(self) -> bool:
        if self.local_request_allowed():
            return False
        message = "Private LAN/local access only" if getattr(self.server, "allow_lan", False) else "Local access only"
        self.send_error(HTTPStatus.FORBIDDEN, message)
        return True

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = clamp_int(self.headers.get("Content-Length"), 0, 0, 2_000_000)
        if not length:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.reject_non_local():
            return
        route, params = self._query()
        if route.startswith("/static/"):
            name = Path(unquote(route.removeprefix("/static/"))).name
            self.send_file(WEB_DIR / name)
            return
        if route.startswith("/output/"):
            parts = route.strip("/").split("/", 2)
            if len(parts) != 3:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            _, stage, encoded_name = parts
            name = Path(unquote(encoded_name)).name
            directory = {"raw": RAW_DIR, "formatted": FORMATTED_DIR, "romanized": ROMANIZED_DIR}.get(stage)
            if directory is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_file(directory / name, "text/html")
            return
        if route == "/api/summary" or route == "/api/data":
            self.send_json(build_summary())
            return
        if route == "/api/links":
            self.send_json(list_links(params))
            return
        if route == "/api/stories":
            settings = load_settings()
            page = clamp_int(params.get("page", [1])[0], 1, 1, 1_000_000)
            per_page = clamp_int(params.get("per_page", [settings["stories_per_page"]])[0], settings["stories_per_page"], 10, 100)
            result = query_library(
                q=params.get("q", [""])[0], status=params.get("status", ["all"])[0],
                language=params.get("language", ["all"])[0], category=params.get("category", ["all"])[0],
                page=page, per_page=per_page, sort=params.get("sort", ["newest"])[0],
            )
            self.send_json(result)
            return
        if route == "/api/story":
            url = params.get("url", [""])[0]
            row = get_story(url)
            if row is None:
                self.send_json({"ok": False, "error": "Story not found."}, 404)
            else:
                row["raw_href"] = f"/output/raw/{quote(row['raw_file'], safe='')}" if row.get("raw_file") else ""
                row["formatted_href"] = f"/output/formatted/{quote(row['formatted_file'], safe='')}" if row.get("formatted_file") else ""
                row["romanized_href"] = f"/output/romanized/{quote(row['romanized_file'], safe='')}" if row.get("romanized_file") else ""
                self.send_json({"ok": True, "story": row})
            return
        if route == "/api/settings":
            self.send_json(load_settings())
            return
        if route == "/api/library/duplicates":
            if not LIBRARY_DB.is_file():
                self.send_json({"groups": [], "group_count": 0, "story_count": 0})
                return
            include_fuzzy = (params.get("fuzzy", ["1"])[0] or "1") not in {"0", "false"}
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json(duplicate_detector.find_duplicates(conn, include_fuzzy=include_fuzzy))
            finally:
                conn.close()
            return
        if route == "/api/library/series":
            if not LIBRARY_DB.is_file():
                self.send_json({"series": []})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json({"series": list_series(conn)})
            finally:
                conn.close()
            return
        if route == "/api/jobs/history":
            if not LIBRARY_DB.is_file():
                self.send_json({"runs": []})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json({"runs": job_store.recent_runs(conn)})
            finally:
                conn.close()
            return
        if route == "/api/maintenance/integrity":
            if not LIBRARY_DB.is_file():
                self.send_json({"issues": [], "issue_count": 0, "by_type": {}, "checked_at": maintenance.utc_now()})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json(maintenance.integrity_check(conn, OUTPUT_DIR))
            finally:
                conn.close()
            return
        if route == "/api/maintenance/storage":
            if not LIBRARY_DB.is_file():
                self.send_json({"database_bytes": 0, "story_count": 0, "link_count": 0})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json(maintenance.storage_health(conn, LIBRARY_DB, OUTPUT_DIR))
            finally:
                conn.close()
            return
        if route == "/api/maintenance/backups":
            if not LIBRARY_DB.is_file():
                self.send_json({"backups": []})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json({"backups": maintenance.list_backups(conn)})
            finally:
                conn.close()
            return
        if route == "/api/links/retry-failed":
            if not LIBRARY_DB.is_file():
                self.send_json({"urls": []})
                return
            conn = connect_library(LIBRARY_DB)
            try:
                self.send_json({"urls": links_store.retryable_failed(conn)})
            finally:
                conn.close()
            return
        if route == "/api/status/all":
            self.send_json(JOBS.all_snapshots())
            return
        if route.startswith("/api/status/"):
            name = route.rsplit("/", 1)[-1]
            try:
                self.send_json(JOBS.snapshot(name))
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if route in {"/", "/dashboard", "/sources", "/stories", "/review", "/activity", "/settings", "/story"}:
            self.send_file(WEB_DIR / "index.html", "text/html")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.reject_non_local():
            return
        if not self.same_origin_post_allowed():
            self.send_error(HTTPStatus.FORBIDDEN, "Cross-origin request rejected")
            return
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch_site == "cross-site":
            self.send_error(HTTPStatus.FORBIDDEN, "Cross-site request rejected")
            return
        route, _ = self._query()
        try:
            payload = self.read_json()
            if route == "/api/start/links":
                settings = load_settings()
                sites = valid_sites(str(payload.get("sites", "")))
                atomic_text(SITES_FILE, "\n".join(sites) + "\n")
                site_profiles = settings.get("site_profiles", {})
                command = [
                    sys.executable, str(CRAWLER), str(SITES_FILE), "-o", str(SUBLINKS_FILE),
                    "--depth", str(clamp_int(payload.get("depth"), settings["crawler_depth"], 0, 10)),
                    "--max-pages", str(clamp_int(payload.get("max_pages"), settings["crawler_max_pages"], 1, 250000)),
                    "--delay", str(clamp_float(payload.get("delay"), settings["crawler_delay"], 0, 60)),
                ]
                if site_profiles:
                    command.extend(["--site-profiles", json.dumps(site_profiles)])
                if payload.get("fresh") is True:
                    command.append("--fresh")
                scrape_mode = str(payload.get("scrape_mode", settings.get("scrape_mode", "manual"))).lower()
                if scrape_mode == "auto":
                    auto_command = [
                        sys.executable, str(AUTO_SCRAPER),
                        "--sites", str(SITES_FILE),
                        "--sublinks", str(SUBLINKS_FILE),
                        "--output", str(OUTPUT_DIR),
                        "--depth", str(clamp_int(payload.get("depth"), settings["crawler_depth"], 0, 10)),
                        "--max-pages", str(clamp_int(payload.get("max_pages"), settings["crawler_max_pages"], 1, 250000)),
                        "--crawl-delay", str(clamp_float(payload.get("delay"), settings["crawler_delay"], 0, 60)),
                        "--content-delay", str(settings["content_delay_ms"]),
                        "--concurrency", str(settings["content_workers"]),
                        "--batch-size", str(settings.get("auto_scrape_batch_size", 20)),
                        "--browser-mode", str(settings.get("browser_mode", "background")),
                    ]
                    if site_profiles:
                        auto_command.extend(["--site-profiles", json.dumps(site_profiles)])
                    if payload.get("fresh") is True:
                        auto_command.append("--fresh")
                    JOBS.start("auto", auto_command)
                    self.send_json({"ok": True, "message": "Auto mode started. New links will be scraped as the crawler discovers them.", "mode": "auto"}, 202)
                else:
                    JOBS.start("links", command)
                    self.send_json({"ok": True, "message": "Manual crawl started. Found links will wait in the queue until you scrape them.", "mode": "manual"}, 202)
                return

            if route == "/api/start/content":
                if not SUBLINKS_FILE.is_file():
                    raise ValueError("No sublinks.json exists. Crawl links first.")
                settings = load_settings()
                input_file = SUBLINKS_FILE
                requested_urls = payload.get("urls")
                conn = connect_library(LIBRARY_DB)
                try:
                    sync_links(conn)
                    if payload.get("pending_only") is True:
                        rows = conn.execute("SELECT url, title FROM links WHERE status='pending'").fetchall()
                        selected = [{"link": row["url"], "title": row["title"]} for row in rows]
                        if not selected:
                            raise ValueError("There are no pending links to scrape.")
                        atomic_text(SELECTED_LINKS_FILE, json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
                        input_file = SELECTED_LINKS_FILE
                    elif isinstance(requested_urls, list) and requested_urls:
                        wanted = {str(item) for item in requested_urls if str(item).startswith(("http://", "https://"))}
                        if not wanted:
                            raise ValueError("No valid selected URLs were supplied.")
                        placeholders = ",".join("?" for _ in wanted)
                        rows = conn.execute(
                            f"SELECT url, title FROM links WHERE url IN ({placeholders})", list(wanted)
                        ).fetchall()
                        selected = [{"link": row["url"], "title": row["title"]} for row in rows]
                        if not selected:
                            raise ValueError("Selected URLs were not found in the crawl results.")
                        atomic_text(SELECTED_LINKS_FILE, json.dumps(selected, ensure_ascii=False, indent=2) + "\n")
                        input_file = SELECTED_LINKS_FILE
                finally:
                    conn.close()
                mode = str(payload.get("browser_mode", settings.get("browser_mode", "background")))
                if mode not in {"background", "headless", "visible"}:
                    mode = "background"
                command = [
                    sys.executable, str(PIPELINE), "--input", str(input_file), "--output", str(OUTPUT_DIR),
                    "--concurrency", str(clamp_int(payload.get("concurrency"), settings["content_workers"], 1, 8)),
                    "--delay", str(clamp_int(payload.get("delay"), settings["content_delay_ms"], 0, 60000)),
                    "--browser-mode", mode,
                ]
                limit = clamp_int(payload.get("limit"), settings["content_limit"], 0, 250000)
                if limit:
                    command.extend(["--limit", str(limit)])
                if payload.get("force") is True:
                    command.append("--force")
                JOBS.start("content", command)
                self.send_json({"ok": True, "message": "Story extraction pipeline started."}, 202)
                return

            if route == "/api/start/format":
                command = [sys.executable, str(FORMATTER), "--output", str(OUTPUT_DIR)]
                if payload.get("force") is True:
                    command.append("--force")
                JOBS.start("format", command)
                self.send_json({"ok": True, "message": "Story formatter started."}, 202)
                return

            if route == "/api/library/recategorize":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                settings = load_settings()
                with MAINTENANCE_GATE:
                    if JOBS.any_running():
                        raise ValueError("Stop all running engines before re-categorizing the library.")
                    conn = connect_library(LIBRARY_DB)
                    try:
                        result = rebuild_library(conn, OUTPUT_DIR, settings.get("categories", []), settings.get("category_aliases", {}))
                    finally:
                        conn.close()
                self.send_json({
                    "ok": True,
                    "message": f"Re-categorized {result.get('stories', 0)} story files with title-first alias rules.",
                    "organization": result,
                })
                return

            if route == "/api/library/bulk":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                urls = payload.get("urls")
                if not isinstance(urls, list) or not urls:
                    raise ValueError("Select at least one story.")
                action = str(payload.get("action") or "")
                settings = load_settings()
                conn = connect_library(LIBRARY_DB)
                try:
                    if action == "set_category":
                        category = str(payload.get("category") or "").strip()
                        if not category:
                            raise ValueError("Choose a category to move these stories to.")
                        result = bulk_set_category(conn, OUTPUT_DIR, urls, category)
                        affected = int(result.get("affected", 0))
                        copied = int(result.get("stories", 0))
                        message = (
                            f"Updated category for {affected} story record(s); "
                            f"moved {copied} available story file(s) to \"{category}\"."
                        )
                    elif action == "unlock":
                        result = bulk_unlock_category(
                            conn, OUTPUT_DIR, urls, settings.get("categories", []), settings.get("category_aliases", {}),
                        )
                        message = f"Returned {result.get('stories', 0)} story(ies) to automatic categorization."
                    else:
                        raise ValueError(f"Unknown bulk action: {action}")
                finally:
                    conn.close()
                self.send_json({"ok": True, "message": message, "result": result})
                return

            if route == "/api/maintenance/repair":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                settings = load_settings()
                with MAINTENANCE_GATE:
                    if JOBS.any_running():
                        raise ValueError("Stop all running engines before repairing the library.")
                    conn = connect_library(LIBRARY_DB)
                    try:
                        result = maintenance.repair(
                            conn, OUTPUT_DIR, settings.get("categories", []), settings.get("category_aliases", {}),
                            sublinks_path=SUBLINKS_FILE,
                        )
                    finally:
                        conn.close()
                self.send_json({"ok": True, "message": "Repair completed.", "result": result})
                return

            if route == "/api/maintenance/analyze":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                conn = connect_library(LIBRARY_DB)
                try:
                    maintenance.analyze(conn)
                finally:
                    conn.close()
                self.send_json({"ok": True, "message": "Query planner statistics refreshed."})
                return

            if route == "/api/maintenance/vacuum":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                with MAINTENANCE_GATE:
                    if JOBS.any_running():
                        raise ValueError("Stop all running engines before compacting the database.")
                    conn = connect_library(LIBRARY_DB)
                    try:
                        maintenance.vacuum(conn)
                    finally:
                        conn.close()
                self.send_json({"ok": True, "message": "Database compacted."})
                return

            if route == "/api/maintenance/backup":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                note = str(payload.get("note") or "")
                conn = connect_library(LIBRARY_DB)
                try:
                    result = maintenance.create_backup(conn, LIBRARY_DB, ROOT, OUTPUT_DIR, BACKUPS_DIR, keep=5, note=note)
                finally:
                    conn.close()
                self.send_json({"ok": True, "message": f"Backup created: {result['filename']}", "backup": result})
                return

            if route == "/api/maintenance/restore":
                filename = str(payload.get("filename") or "")
                if not filename:
                    raise ValueError("Choose a backup to restore.")
                with MAINTENANCE_GATE:
                    if JOBS.any_running():
                        raise ValueError("Stop all running engines before restoring a backup.")
                    result = maintenance.restore_backup(ROOT, OUTPUT_DIR, LIBRARY_DB, BACKUPS_DIR, filename)
                self.send_json({"ok": True, "message": f"Restored from {filename}. Restart the server to pick up the restored database cleanly.", "result": result})
                return

            if route == "/api/links/retry-failed":
                if not LIBRARY_DB.is_file():
                    raise ValueError("Story library database does not exist yet.")
                conn = connect_library(LIBRARY_DB)
                try:
                    urls = links_store.retryable_failed(conn)
                    if urls:
                        conn.executemany(
                            "UPDATE links SET status='pending', error='' WHERE url=?",
                            [(u,) for u in urls],
                        )
                        conn.commit()
                finally:
                    conn.close()
                self.send_json({"ok": True, "message": f"Queued {len(urls)} transient failure(s) for retry.", "urls": urls})
                return

            if route.startswith("/api/stop/"):
                name = route.rsplit("/", 1)[-1]
                JOBS.stop(name)
                self.send_json({"ok": True, "message": f"Stopping {name} engine."}, 202)
                return

            if route == "/api/settings":
                old_settings = load_settings()
                settings = sanitize_settings(payload)
                with SETTINGS_LOCK:
                    atomic_text(SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
                organization = None
                if (settings.get("categories", []) != old_settings.get("categories", []) or settings.get("category_aliases", {}) != old_settings.get("category_aliases", {})) and LIBRARY_DB.is_file():
                    with MAINTENANCE_GATE:
                        if JOBS.any_running():
                            raise ValueError("Stop all running engines before changing category rules.")
                        conn = connect_library(LIBRARY_DB)
                        try:
                            organization = rebuild_library(conn, OUTPUT_DIR, settings.get("categories", []), settings.get("category_aliases", {}))
                        finally:
                            conn.close()
                self.send_json({"ok": True, "settings": settings, "organization": organization})
                return

            if route == "/api/story/accept":
                url = str(payload.get("url", ""))
                row = get_story(url)
                if not row:
                    raise ValueError("Story not found.")
                if not row.get("integrity_exact"):
                    raise ValueError("Cannot accept a story that failed text-integrity verification.")
                conn = connect_library(LIBRARY_DB)
                try:
                    conn.execute(
                        "UPDATE stories SET status='verified',review_reason='',manual_accept=1,"
                        "source_changed=0,previous_raw_sha256='',updated_at=? WHERE url=?",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), url),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.send_json({"ok": True, "message": "Story accepted."})
                return

            if route == "/api/story/save-format":
                url = str(payload.get("url", ""))
                text_value = str(payload.get("text", ""))
                result = StoryFormatter(OUTPUT_DIR).save_manual_format(url, text_value)
                message = "Formatting saved and verified." if result.status == "verified" else "Formatting saved; readability still needs review."
                self.send_json({"ok": True, "message": message, "status": result.status})
                return

            if route == "/api/story/use-original":
                url = str(payload.get("url", ""))
                row = get_story(url)
                if not row:
                    raise ValueError("Story not found.")
                result = StoryFormatter(OUTPUT_DIR).save_manual_format(url, str(row.get("original_text", "")))
                message = "Original paragraph structure saved." if result.status == "verified" else "Original paragraph structure restored; readability still needs review."
                self.send_json({"ok": True, "message": message, "status": result.status})
                return

            if route == "/api/story/reformat":
                url = str(payload.get("url", ""))
                if not url:
                    raise ValueError("Story URL is required.")
                formatter = StoryFormatter(OUTPUT_DIR)
                counts = formatter.run(force=True, only_url=url)
                row = get_story(url)
                self.send_json({"ok": bool(row), "counts": counts, "story": row})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


def main() -> int:
    parser = argparse.ArgumentParser(description="Story Grabber web application")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default=os.environ.get("STORY_GRABBER_HOST", "127.0.0.1"))
    parser.add_argument(
        "--allow-lan",
        action="store_true",
        default=os.environ.get("STORY_GRABBER_ALLOW_LAN", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Allow private-LAN clients/hosts. Native default remains localhost-only.",
    )
    args = parser.parse_args()
    settings = load_settings()
    if not SETTINGS_FILE.is_file():
        atomic_text(SETTINGS_FILE, json.dumps(settings, indent=2) + "\n")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect_library(LIBRARY_DB)
    try:
        interrupted = job_store.mark_interrupted_runs(conn)
        if interrupted:
            print(f"Job history: marked {interrupted} interrupted run(s) from the previous server process.")
        unorganized = int(conn.execute(
            "SELECT COUNT(*) FROM stories WHERE status IN ('verified','review') AND organized_file=''"
        ).fetchone()[0])
        if unorganized:
            print(f"Library migration: organizing {unorganized} existing story/stories...")
            result = rebuild_library(conn, OUTPUT_DIR, settings.get("categories", []), settings.get("category_aliases", {}))
        else:
            result = write_kindle_manifest(conn, OUTPUT_DIR)
        if result.get("kindle_stories", 0):
            print(
                f"Kindle catalog: {result.get('kindle_stories', 0)} stories, "
                f"{result.get('kindle_parts', 0)} parts ready in content_output/library/library.json"
            )
    finally:
        conn.close()
    server = LocalThreadingHTTPServer((args.host, args.port), AppHandler)
    server.allow_lan = bool(args.allow_lan)
    if args.allow_lan:
        print(f"Story Grabber: http://127.0.0.1:{args.port} (private LAN enabled on {args.host}:{args.port})")
    else:
        print(f"Story Grabber: http://127.0.0.1:{args.port}")
    print("Raw pages are preserved; formatted/romanized copies are verified separately.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        JOBS.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
