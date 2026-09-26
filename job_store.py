#!/usr/bin/env python3
"""v3.6: persistent job history + a transient/permanent error classifier.

The in-memory ``JobManager`` in web_server.py already tracks the current run
of each engine (auto/links/content/format) for the life of the process. What
it never had was a durable record: if the process restarted mid-run, all
history vanished. ``job_runs`` (schema in db_migrations.py) is a simple
append-only log of every start/finish so "what happened last time" survives
a restart, and is the basis for the retry-failed-only action (only retrying
failures classified as transient below).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

TRANSIENT_PATTERNS = [
    re.compile(r"\btimeout\b", re.IGNORECASE),
    re.compile(r"\btimed?\s*out\b", re.IGNORECASE),
    re.compile(r"\b429\b"),
    re.compile(r"\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\b5\d{2}\b"),  # 500-599
    re.compile(r"\bconnection reset\b", re.IGNORECASE),
    re.compile(r"\bconnection refused\b", re.IGNORECASE),
    re.compile(r"\bECONNRESET\b"),
    re.compile(r"\bETIMEDOUT\b"),
    re.compile(r"\bEAI_AGAIN\b"),
    re.compile(r"\btemporarily unavailable\b", re.IGNORECASE),
    re.compile(r"\bnetwork error\b", re.IGNORECASE),
]

PERMANENT_PATTERNS = [
    re.compile(r"\b404\b"),
    re.compile(r"\bnot found\b", re.IGNORECASE),
    re.compile(r"\b410\b"),
    re.compile(r"\bgone\b", re.IGNORECASE),
    re.compile(r"\binvalid content\b", re.IGNORECASE),
    re.compile(r"\bno article\b", re.IGNORECASE),
    re.compile(r"\bcaptcha\b", re.IGNORECASE),
]


def classify_error(message: str) -> str:
    """Return ``"transient"``, ``"permanent"``, or ``"unknown"``."""
    text = str(message or "")
    if any(p.search(text) for p in PERMANENT_PATTERNS):
        return "permanent"
    if any(p.search(text) for p in TRANSIENT_PATTERNS):
        return "transient"
    return "unknown"


def is_transient_error(message: str) -> bool:
    """Retry policy: retry transient and unknown failures, never permanent ones."""
    return classify_error(message) != "permanent"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_run(conn: sqlite3.Connection, name: str, command: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO job_runs(name, command, state, started_at) VALUES (?,?,?,?)",
        (name, " ".join(command), "running", utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, state: str, exit_code: int | None, error: str = "") -> None:
    conn.execute(
        "UPDATE job_runs SET state=?, finished_at=?, exit_code=?, error=? WHERE id=?",
        (state, utc_now(), exit_code, error, run_id),
    )
    conn.commit()



def mark_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Mark runs left ``running`` by a previous process as interrupted.

    The in-memory JobManager cannot resume a subprocess after the web server
    restarts, so retaining a durable ``running`` state would be misleading.
    """
    now = utc_now()
    cur = conn.execute(
        "UPDATE job_runs SET state='interrupted', finished_at=?, "
        "error=CASE WHEN error='' THEN 'Web application restarted before the job finished.' ELSE error END "
        "WHERE state='running'",
        (now,),
    )
    conn.commit()
    return int(cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0)

def recent_runs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, command, state, started_at, finished_at, exit_code, error "
        "FROM job_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
