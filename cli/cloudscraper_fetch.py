#!/usr/bin/env python3
"""Persistent JSON-lines fetch bridge for the Node content engine."""

from __future__ import annotations

import base64
import codecs
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import cloudscraper
from charset_normalizer import from_bytes


OUTPUT_LOCK = threading.Lock()
SESSIONS_LOCK = threading.Lock()
SESSIONS: dict[str, tuple[Any, threading.Lock]] = {}


def scraper_for(url: str) -> tuple[Any, threading.Lock]:
    parts = urlsplit(url)
    origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    with SESSIONS_LOCK:
        value = SESSIONS.get(origin)
        if value is None:
            value = (
                cloudscraper.create_scraper(
                    interpreter="nodejs",
                    enable_stealth=False,
                ),
                threading.Lock(),
            )
            SESSIONS[origin] = value
        return value


CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._:+-]+)", re.I)


def valid_encoding(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_html(content: bytes, content_type: str) -> str:
    header_match = CHARSET_RE.search(content_type)
    encoding = valid_encoding(header_match.group(1) if header_match else None)
    if not encoding:
        if content.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
    if not encoding:
        prefix = content[:8192].decode("ascii", errors="ignore")
        meta_match = CHARSET_RE.search(prefix)
        encoding = valid_encoding(meta_match.group(1) if meta_match else None)
    if not encoding:
        best = from_bytes(content).best()
        encoding = valid_encoding(best.encoding if best else None) or "utf-8"
    return content.decode(encoding, errors="replace")


def emit(value: dict[str, Any]) -> None:
    line = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    with OUTPUT_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def fetch(request: dict[str, Any]) -> None:
    request_id = request.get("id")
    try:
        url = str(request["url"])
        timeout = max(1.0, float(request.get("timeout", 30)))
        max_bytes = max(1, int(request.get("max_bytes", 8 * 1024 * 1024)))
        instance, instance_lock = scraper_for(url)
        # A requests Session is not thread-safe. One session per origin also lets
        # all requests reuse the Cloudflare clearance cookie instead of solving
        # the challenge once per worker.
        with instance_lock:
            with instance.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                stream=True,
                headers={"Accept-Language": "en-US,en;q=0.8"},
            ) as response:
                content_type = response.headers.get("content-type", "")
                if response.status_code >= 400:
                    emit({
                        "id": request_id,
                        "ok": False,
                        "status": response.status_code,
                        "final_url": response.url,
                        "error": f"HTTP {response.status_code} {response.reason}",
                    })
                    return
                if "text/html" not in content_type.lower() and "xhtml" not in content_type.lower():
                    raise ValueError(f"Not HTML: {content_type or 'unknown content type'}")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(
                            f"Response too large: over {max_bytes} bytes"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
                final_url = response.url
        utf8_content = decode_html(content, content_type).encode("utf-8")
        emit({
            "id": request_id,
            "ok": True,
            "status": response.status_code,
            "final_url": final_url,
            "content_type": content_type,
            "html_base64": base64.b64encode(utf8_content).decode("ascii"),
        })
    except Exception as exc:
        emit({"id": request_id, "ok": False, "error": str(exc)})


def main() -> int:
    workers = max(1, min(16, int(os.environ.get("CLOUDSCRAPER_WORKERS", "3"))))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cloudscraper") as pool:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                pool.submit(fetch, request)
            except Exception as exc:
                emit({"id": None, "ok": False, "error": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
