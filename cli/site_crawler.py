#!/usr/bin/env python3
"""Crawl links below each supplied starting URL path and save them to JSON.

Example:
    https://a.com/b/

Allowed:
    https://a.com/b/c/
    https://a.com/b/c/page

Rejected:
    https://a.com/
    https://a.com/c/
    https://other.com/
"""

from __future__ import annotations

import argparse
import codecs
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import signal
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import OpenerDirector, build_opener
from urllib.robotparser import RobotFileParser

from charset_normalizer import from_bytes


SKIPPED_EXTENSIONS = {
    ".7z", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".eot",
    ".epub", ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js",
    ".json", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg",
    ".ogg", ".ogv", ".otf", ".pdf", ".png", ".ppt", ".pptx", ".rar",
    ".rss", ".svg", ".tar", ".tgz", ".tif", ".tiff", ".ttf", ".wav",
    ".webm", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".xml", ".zip",
}

# v3.7: mirrors links_store.TRACKING_PARAMS exactly. A URL the crawler
# already deduped during a crawl must normalize the same way once it lands
# in the SQLite ``links`` table (see links_store.normalize_url) -- two
# independent tracking-param lists would silently let near-duplicate pages
# (same story, different utm_source) through one layer and not the other.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "gclid", "fbclid", "msclkid",
    "mc_cid", "mc_eid", "ref", "ref_src", "refsrc", "igshid", "spm", "_ga",
    "yclid", "vero_id",
}


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive_float(value: str) -> float:
    number = non_negative_float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def normalize_url(url: str, *, add_scheme: bool = False) -> str:
    """Return a canonical HTTP(S) URL without a fragment.

    Also strips known tracking parameters (utm_*, gclid, fbclid, ref, ...)
    and sorts whatever query parameters remain, so ``/story?id=1&utm_source=x``
    and ``/story?utm_source=y&id=1`` collapse to the same crawl-queue entry
    instead of being fetched, extracted, and stored twice as "different"
    pages.
    """
    value = url.strip()
    if add_scheme and "://" not in value:
        value = "https://" + value

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError(f"invalid website URL: {url}")

    port = parts.port
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{display_host}:{port}"
    else:
        netloc = display_host

    raw_path = parts.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if raw_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path

    query_pairs = sorted(
        (key, qvalue) for key, qvalue in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    )
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, normalized_path, query, ""))


def is_internal(url: str, main_host: str, include_subdomains: bool) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == main_host or (include_subdomains and host.endswith("." + main_host))


def crawl_root_path(main_site: str) -> str:
    """Return the starting path used as the hard crawl boundary."""
    path = urlsplit(main_site).path or "/"

    # Treat a supplied path such as /b as the subtree /b/.
    if path != "/" and not path.endswith("/"):
        path += "/"

    return path


def is_in_crawl_scope(
    url: str,
    main_site: str,
    include_subdomains: bool,
) -> bool:
    """Return True only for URLs on the allowed host and below the start path."""
    main_parts = urlsplit(main_site)
    main_host = (main_parts.hostname or "").lower()

    if not is_internal(url, main_host, include_subdomains):
        return False

    root_path = crawl_root_path(main_site)
    candidate_path = urlsplit(url).path or "/"

    if root_path == "/":
        return True

    # Allow the root itself with or without its trailing slash.
    if candidate_path.rstrip("/") == root_path.rstrip("/"):
        return True

    # Everything else must be a descendant of the supplied path.
    return candidate_path.startswith(root_path)


def is_html_candidate(url: str) -> bool:
    """Reject obvious downloads and non-page resources before requesting them."""
    path = unquote(urlsplit(url).path).lower()
    return not any(path.endswith(extension) for extension in SKIPPED_EXTENSIONS)


def clean_title(value: str) -> str:
    return " ".join(value.split()).strip()


def load_sites(path: Path) -> list[str]:
    """Load sites from TXT, JSON, or JSONL input."""
    if not path.exists():
        raise ValueError(f"site list does not exist: {path}")

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read site list: {exc}") from exc

    raw_sites: list[str] = []
    suffix = path.suffix.lower()

    if suffix == ".json":
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON site list: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("JSON site list must be an array")
        for item in data:
            if isinstance(item, str):
                raw_sites.append(item)
            elif isinstance(item, dict):
                value = item.get("url") or item.get("site") or item.get("main_site")
                if isinstance(value, str):
                    raw_sites.append(value)
    elif suffix == ".jsonl":
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL on line {line_number}: {exc}") from exc
            if isinstance(item, str):
                raw_sites.append(item)
            elif isinstance(item, dict):
                value = item.get("url") or item.get("site") or item.get("main_site")
                if isinstance(value, str):
                    raw_sites.append(value)
    else:
        raw_sites = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    sites: list[str] = []
    seen: set[str] = set()
    for raw_site in raw_sites:
        site = normalize_url(raw_site, add_scheme=True)
        if site not in seen:
            seen.add(site)
            sites.append(site)

    if not sites:
        raise ValueError("site list contains no valid website URLs")
    return sites


class PageHTMLParser(HTMLParser):
    """Collect a page title, first H1, and anchor links from tolerant HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.links: list[tuple[str, str, set[str]]] = []
        self.base_href: str | None = None
        self._in_title = False
        self._title_done = False
        self._in_h1 = False
        self._anchor_href: str | None = None
        self._anchor_rel: set[str] = set()
        self._anchor_parts: list[str] = []
        self._svg_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "base" and not self._svg_depth and not self.base_href and attributes.get("href"):
            self.base_href = attributes["href"]
        elif tag == "title" and not self._svg_depth and not self._title_done:
            self._in_title = True
        elif tag == "h1" and not self._svg_depth and not self.h1_parts:
            self._in_h1 = True
        elif tag == "a" and attributes.get("href"):
            self._finish_anchor()
            self._anchor_href = attributes["href"]
            self._anchor_rel = {
                value.lower() for value in attributes.get("rel", "").split()
            }
            self._anchor_parts = []
            # Record immediately so malformed or unclosed anchors are retained.
            self.links.append((self._anchor_href, "", self._anchor_rel))

    def _finish_anchor(self) -> None:
        if self._anchor_href is None:
            return
        title = clean_title(" ".join(self._anchor_parts))
        if self.links:
            self.links[-1] = (self._anchor_href, title, self._anchor_rel)
        self._anchor_href = None
        self._anchor_rel = set()
        self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "svg":
            self._svg_depth = max(0, self._svg_depth - 1)
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "a" and self._anchor_href is not None:
            self._finish_anchor()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
            # Keep the already-recorded link title current even without </a>.
            if self.links:
                self.links[-1] = (
                    self._anchor_href,
                    clean_title(" ".join(self._anchor_parts)),
                    self._anchor_rel,
                )

    def close(self) -> None:
        self._finish_anchor()
        super().close()


def page_title(parser: PageHTMLParser, fallback: str) -> str:
    title = clean_title(" ".join(parser.title_parts))
    if title:
        return title
    title = clean_title(" ".join(parser.h1_parts))
    return title or fallback


def make_opener(user_agent: str) -> OpenerDirector:
    opener = build_opener()
    opener.addheaders = [
        ("User-Agent", user_agent),
        (
            "Accept",
            "text/html,application/xhtml+xml,application/xml,text/xml,*/*;q=0.5",
        ),
    ]
    return opener


CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?\s*([A-Za-z0-9._:+-]+)", re.I)


def decode_content(content: bytes, declared_charset: str = "", *, sniff_html: bool = False) -> str:
    candidates = [declared_charset]
    if content.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    if sniff_html:
        prefix = content[:8192].decode("ascii", errors="ignore")
        match = CHARSET_RE.search(prefix)
        if match:
            candidates.append(match.group(1))
    best = from_bytes(content).best()
    if best and best.encoding:
        candidates.append(best.encoding)
    candidates.append("utf-8")
    for encoding in candidates:
        if not encoding:
            continue
        try:
            codecs.lookup(encoding)
            return content.decode(encoding, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


def fetch_resource(
    opener: OpenerDirector,
    url: str,
    timeout: float,
    max_bytes: int,
) -> tuple[str, bytes, str, str]:
    """Fetch a bounded resource with retries."""
    retry_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with opener.open(url, timeout=timeout) as response:
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} bytes")
                content_type = response.headers.get_content_type().lower()
                charset = response.headers.get_content_charset() or ""
                return response.geturl(), content, content_type, charset
        except HTTPError as exc:
            last_error = exc
            if exc.code not in retry_statuses or attempt == 2:
                raise
        except URLError as exc:
            last_error = exc
            if attempt == 2:
                raise
        time.sleep(0.5 * (2**attempt))
    if last_error:
        raise last_error
    raise RuntimeError("resource fetch failed")


def get_robots(
    opener: OpenerDirector,
    main_site: str,
    timeout: float,
) -> tuple[RobotFileParser, list[str]]:
    """Return robots rules and any sitemap locations declared there."""
    parts = urlsplit(main_site)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    parser = RobotFileParser()
    parser.set_url(robots_url)
    sitemap_urls: list[str] = []
    try:
        _, content, _, charset = fetch_resource(
            opener, robots_url, timeout, 1024 * 1024
        )
        lines = decode_content(content, charset).splitlines()
        parser.parse(lines)
        for line in lines:
            name, separator, value = line.partition(":")
            if separator and name.strip().lower() == "sitemap" and value.strip():
                try:
                    sitemap_urls.append(normalize_url(value.strip()))
                except ValueError:
                    continue
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            # RFC 9309 "unavailable": crawlers may access resources.
            parser.parse([])
        else:
            parser.disallow_all = True
    except (URLError, TimeoutError, socket.timeout):
        # RFC 9309 "unreachable": conservatively disallow until reachable.
        parser.disallow_all = True
    except Exception:
        parser.disallow_all = True
    return parser, sitemap_urls


def fetch_html(
    opener: OpenerDirector,
    url: str,
    timeout: float,
    max_bytes: int,
) -> tuple[str, str] | None:
    """Fetch bounded HTML with retries and return its final URL and text."""
    final_url, content, content_type, charset = fetch_resource(
        opener, url, timeout, max_bytes
    )
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return None
    html = decode_content(content, charset, sniff_html=True)
    return normalize_url(final_url), html


def sitemap_candidates(main_site: str, robots_sitemaps: Iterable[str]) -> list[str]:
    """Build likely sitemap locations, including common forum conventions."""
    parts = urlsplit(main_site)
    origin = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    candidates = [
        *robots_sitemaps,
        urljoin(origin, "sitemap.xml"),
        urljoin(origin, "sitemap_index.xml"),
        urljoin(origin, "sitemap.php"),
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = normalize_url(candidate)
        except ValueError:
            continue
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def parse_sitemap(
    content: bytes,
    source_url: str,
    max_uncompressed_bytes: int = 50 * 1024 * 1024,
) -> tuple[str, list[str]]:
    """Parse one sitemap and return its type plus normalized locations."""
    if source_url.lower().endswith(".gz") or content.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            content = stream.read(max_uncompressed_bytes + 1)
        if len(content) > max_uncompressed_bytes:
            raise ValueError("decompressed sitemap exceeds size limit")

    root_name: str | None = None
    locations: list[str] = []
    for event, element in ET.iterparse(io.BytesIO(content), events=("start", "end")):
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if root_name is None and event == "start":
            root_name = local_name
            if root_name not in {"sitemapindex", "urlset"}:
                raise ValueError("unsupported sitemap XML root")
        if event != "end" or local_name not in {"url", "sitemap"}:
            continue
        location_element = next(
            (
                child for child in list(element)
                if child.tag.rsplit("}", 1)[-1].lower() == "loc"
                and (
                    not child.tag.startswith("{")
                    or child.tag.startswith("{http://www.sitemaps.org/schemas/sitemap/")
                )
            ),
            None,
        )
        if location_element is None or not location_element.text:
            element.clear()
            continue
        try:
            locations.append(normalize_url(location_element.text.strip()))
        except ValueError:
            pass
        element.clear()
    if root_name is None:
        raise ValueError("empty sitemap XML")
    return root_name, locations


def discover_sitemap_urls(
    opener: OpenerDirector,
    main_site: str,
    initial_sitemaps: Iterable[str],
    *,
    timeout: float,
    delay: float,
    max_sitemaps: int,
    max_urls: int,
    include_subdomains: bool,
) -> set[str]:
    """Follow sitemap indexes and collect internal page URLs without fetching them."""
    main_host = (urlsplit(main_site).hostname or "").lower()
    queue: deque[str] = deque(sitemap_candidates(main_site, initial_sitemaps))
    queued = set(queue)
    visited: set[str] = set()
    page_urls: set[str] = set()

    while queue and len(visited) < max_sitemaps and len(page_urls) < max_urls and not STOP.is_set():
        sitemap_url = queue.popleft()
        queued.discard(sitemap_url)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        if delay and len(visited) > 1:
            time.sleep(delay)
        try:
            final_url, content, _, _ = fetch_resource(
                opener, sitemap_url, timeout, 50 * 1024 * 1024
            )
            sitemap_type, locations = parse_sitemap(content, final_url)
        except Exception:
            continue

        if sitemap_type == "sitemapindex":
            for location in locations:
                if location not in visited and location not in queued:
                    queue.append(location)
                    queued.add(location)
            continue

        for location in locations:
            if location == main_site:
                continue
            if not is_in_crawl_scope(location, main_site, include_subdomains):
                continue
            if not is_html_candidate(location):
                continue
            page_urls.add(location)
            if len(page_urls) >= max_urls:
                break
    return page_urls


def title_from_url(url: str) -> str:
    """Create a readable fallback title for an unfetched sitemap URL."""
    path = unquote(urlsplit(url).path).rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else "Home"
    title = clean_title(segment.replace("-", " ").replace("_", " "))
    return title or url


def iter_links(parser: PageHTMLParser, base_url: str) -> Iterable[tuple[str, str]]:
    effective_base = urljoin(base_url, parser.base_href) if parser.base_href else base_url
    for href, title, rel in parser.links:
        if "nofollow" in rel:
            continue
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            link = normalize_url(urljoin(effective_base, href))
        except (ValueError, TypeError):
            continue
        yield link, title



# ---------------------------------------------------------------------------
# Crash-safe progress
#
# Every processed page is appended to a small per-site journal and forced to
# disk (fsync) before the crawler moves on. After a power cut or kill, the next
# run replays the journal and continues where it stopped; sublinks.json is also
# refreshed with partial results every few seconds while crawling.
# ---------------------------------------------------------------------------

STOP = threading.Event()
PROGRESS_INTERVAL = 5.0      # seconds between partial saves + progress lines


def fsync_dir(path: Path) -> None:
    """Persist a rename in the directory itself (not possible on Windows)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class SiteJournal:
    """Append-only, fsync'd record of one site's crawl progress."""

    def __init__(self, directory: Path, site: str, settings: dict[str, Any]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(site.encode("utf-8")).hexdigest()[:16]
        self.path = directory / f"{digest}.jsonl"
        self.site = site
        self.settings = settings
        self.records: list[dict[str, Any]] = []
        self.resumed = False
        self._load()
        self._file = open(self.path, "a", encoding="utf-8", newline="\n")
        if not self.resumed:
            self.write({"t": "start", "site": site, "settings": settings})

    def _load(self) -> None:
        if not self.path.is_file():
            return
        lines = self.path.read_text("utf-8", errors="replace").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                break          # torn last line from a power cut: ignore it
        head = records[0] if records else {}
        if head.get("t") == "start" and head.get("site") == self.site and head.get("settings") == self.settings:
            self.records = records[1:]
            self.resumed = True
            # rewrite without any torn tail so new records append cleanly
            if len(records) != len(lines):
                clean = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
                self.path.write_text(clean, encoding="utf-8")
        else:
            # different settings or unreadable: keep the old file, start fresh
            self.path.replace(self.path.with_suffix(f".old-{int(time.time())}.jsonl"))

    @property
    def finished(self) -> dict[str, Any] | None:
        for record in reversed(self.records):
            if record.get("t") == "done":
                return record.get("result")
        return None

    def write(self, record: dict[str, Any]) -> None:
        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


def crawl_site(
    main_site: str,
    *,
    depth: int,
    max_pages: int,
    timeout: float,
    delay: float,
    max_page_mb: int,
    include_subdomains: bool,
    respect_robots: bool,
    user_agent: str,
    use_sitemaps: bool = True,
    max_sitemaps: int = 200,
    max_sitemap_urls: int = 250000,
    site_profiles: dict[str, float] | None = None,
    journal: SiteJournal | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    if journal and journal.finished is not None:
        print(f"  already finished in an earlier run: {main_site}")
        return journal.finished
    site_profiles = site_profiles or {}
    opener = make_opener(user_agent)
    main_host = (urlsplit(main_site).hostname or "").lower()
    # v3.7: a per-host floor on top of --delay/robots.txt, for sites that
    # need to be crawled more gently than the rest (settings.site_profiles).
    base_delay = max(delay, float(site_profiles.get(main_host, 0.0)))
    if respect_robots or use_sitemaps:
        robots_parser, robots_sitemaps = get_robots(opener, main_site, timeout)
    else:
        robots_parser = RobotFileParser()
        robots_parser.parse([])
        robots_sitemaps = []
    robots = robots_parser if respect_robots else None
    queue: deque[tuple[str, int]] = deque([(main_site, 0)])
    queued = {main_site}
    visited: set[str] = set()
    sublinks: dict[str, str] = {}
    discovered_titles: dict[str, str] = {}
    errors = 0
    fetched_pages = 0
    max_bytes = max_page_mb * 1024 * 1024
    sitemap_urls: set[str] = set()
    sitemap_done = False

    if journal and journal.resumed:
        # Replay the journal: rebuild exactly the state we had before the crash.
        order: list[tuple[str, int]] = [(main_site, 0)]
        for record in journal.records:
            kind = record.get("t")
            if kind == "sitemap":
                sitemap_done = True
                sitemap_urls = set(record.get("found", []))
                for link, title in record.get("q", []):
                    discovered_titles.setdefault(link, title)
                    sublinks.setdefault(link, title)
                    order.append((link, 1))
            elif kind == "v":
                requested, final = record["u"], record.get("f")
                visited.add(requested)
                if final:
                    visited.add(final)
                status = record.get("s")
                if status == "err":
                    errors += 1
                if status == "ok":
                    fetched_pages += 1
                    if record.get("title") is not None:
                        sublinks.pop(requested, None)
                        sublinks[final] = record["title"]
                else:
                    sublinks.pop(requested, None)
                for link, depth_value, title in record.get("q", []):
                    discovered_titles.setdefault(link, title)
                    if link not in visited:
                        sublinks.setdefault(link, title)
                    order.append((link, depth_value))
        queue = deque()
        queued = set()
        for link, depth_value in order:
            if link not in visited and link not in queued:
                queue.append((link, depth_value))
                queued.add(link)
        print(f"  resuming: {fetched_pages} page(s) done, {len(sublinks)} link(s) saved, {len(queue)} queued")

    def log(record: dict[str, Any]) -> None:
        if journal:
            journal.write(record)

    def snapshot(partial: bool) -> dict[str, Any]:
        ordered = [{"title": title, "link": link} for link, title in sorted(sublinks.items())]
        result: dict[str, Any] = {
            "main_site": main_site,
            "crawl_root_path": crawl_root_path(main_site),
            "total_sublinks": len(ordered),
            "sitemap_urls_discovered": len(sitemap_urls),
            "sublinks": ordered,
        }
        if partial:
            result["partial"] = True
        return result

    last_progress = time.monotonic()

    def report(force: bool = False) -> None:
        nonlocal last_progress
        if not force and time.monotonic() - last_progress < PROGRESS_INTERVAL:
            return
        last_progress = time.monotonic()
        total = fetched_pages + len(queue)
        print(f"  progress: {fetched_pages}/{total} pages checked · {len(sublinks)} links saved", flush=True)
        if on_progress:
            on_progress(snapshot(partial=True))

    if use_sitemaps and not sitemap_done:
        print("  checking sitemaps")
        sitemap_urls = discover_sitemap_urls(
            opener,
            main_site,
            robots_sitemaps,
            timeout=timeout,
            delay=base_delay,
            max_sitemaps=max_sitemaps,
            max_urls=min(max_sitemap_urls, max_pages),
            include_subdomains=include_subdomains,
        )
        added: list[list[str]] = []
        for link in sorted(sitemap_urls):
            if len(queue) + len(visited) >= max_pages:
                break
            discovered_titles.setdefault(link, title_from_url(link))
            if link not in queued:
                queue.append((link, 1))
                queued.add(link)
                sublinks.setdefault(link, discovered_titles[link])   # visible right away
                added.append([link, discovered_titles[link]])
        if STOP.is_set():                 # scan was cut short: redo it next run
            print(f"  paused {main_site} during sitemap scan: rerun to continue")
            return snapshot(partial=True)
        log({"t": "sitemap", "found": sorted(sitemap_urls), "q": added})
        if sitemap_urls:
            print(f"  discovered {len(sitemap_urls)} URL(s) from sitemaps")
    report(force=True)          # save what we already know before visiting pages

    while queue and fetched_pages < max_pages:
        if STOP.is_set():
            break
        report()
        requested_url, current_depth = queue.popleft()
        queued.discard(requested_url)
        if requested_url in visited:
            continue
        visited.add(requested_url)

        if robots and not robots.can_fetch(user_agent, requested_url):
            log({"t": "v", "u": requested_url, "s": "skip"})
            continue
        request_host = (urlsplit(requested_url).hostname or "").lower()
        effective_delay = max(delay, float(site_profiles.get(request_host, 0.0)))
        if robots:
            robots_delay = robots.crawl_delay(user_agent) or robots.crawl_delay("*") or 0
            effective_delay = max(effective_delay, float(robots_delay))
        if effective_delay and fetched_pages and STOP.wait(effective_delay):
            queue.appendleft((requested_url, current_depth))   # not fetched yet
            visited.discard(requested_url)
            break

        try:
            fetched = fetch_html(opener, requested_url, timeout, max_bytes)
        except Exception as exc:
            errors += 1
            sublinks.pop(requested_url, None)
            log({"t": "v", "u": requested_url, "s": "err"})
            print(f"  skipped {requested_url}: {exc}", file=sys.stderr)
            continue
        if fetched is None:
            sublinks.pop(requested_url, None)
            log({"t": "v", "u": requested_url, "s": "skip"})
            continue

        final_url, html = fetched
        if not is_in_crawl_scope(final_url, main_site, include_subdomains):
            sublinks.pop(requested_url, None)
            log({"t": "v", "u": requested_url, "s": "skip"})
            print(f"  skipped redirect outside crawl path: {final_url}", file=sys.stderr)
            continue

        fetched_pages += 1
        visited.add(final_url)
        parser = PageHTMLParser()
        parser.feed(html)
        saved_title: str | None = None
        if requested_url != main_site and final_url != main_site:
            sublinks.pop(requested_url, None)
            saved_title = page_title(
                parser,
                discovered_titles.get(requested_url)
                or discovered_titles.get(final_url)
                or final_url,
            )
            sublinks[final_url] = saved_title

        newly_queued: list[list[Any]] = []
        record = {"t": "v", "u": requested_url, "f": final_url, "s": "ok", "title": saved_title, "q": newly_queued}
        if current_depth >= depth:
            log(record)
            continue

        for link, anchor_title in iter_links(parser, final_url):
            if link == main_site or not is_in_crawl_scope(
                link, main_site, include_subdomains
            ):
                continue
            if not is_html_candidate(link):
                continue

            discovered_titles.setdefault(link, anchor_title or link)
            if link not in visited and link not in queued and len(visited) + len(queue) < max_pages:
                queue.append((link, current_depth + 1))
                queued.add(link)
                sublinks.setdefault(link, discovered_titles[link])
                newly_queued.append([link, current_depth + 1, discovered_titles[link]])
        log(record)

    if STOP.is_set():
        print(f"  paused {main_site}: progress saved, rerun to continue")
        return snapshot(partial=True)

    ordered_links = [
        {"title": title, "link": link}
        for link, title in sorted(sublinks.items(), key=lambda item: item[0])
    ]
    result: dict[str, Any] = {
        "main_site": main_site,
        "crawl_root_path": crawl_root_path(main_site),
        "total_sublinks": len(ordered_links),
        "sitemap_urls_discovered": len(sitemap_urls),
        "sublinks": ordered_links,
    }
    if fetched_pages == 0:
        result["error"] = "No HTML pages could be fetched from this site."
    elif errors:
        result["skipped_pages"] = errors
    log({"t": "done", "result": result})
    return result


def save_json(path: Path, data: list[dict[str, Any]]) -> None:
    """Atomically replace *path*; the data is on disk before the swap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    fsync_dir(path.parent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Crawl sublinks below each supplied starting URL path and save JSON. "
            "A start URL such as https://a.com/b/ never crawls back to https://a.com/."
        )
    )
    parser.add_argument(
        "sites_file", nargs="?", type=Path, default=Path("sites.txt"),
        help="TXT, JSON, or JSONL site list (default: sites.txt)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("sublinks.json"),
        help="output JSON file (default: sublinks.json)",
    )
    parser.add_argument(
        "--depth", type=non_negative_int, default=1,
        help="link depth to crawl; 1 means direct sublinks (default: 1)",
    )
    parser.add_argument(
        "--max-pages", type=positive_int, default=100,
        help="maximum HTML pages to fetch per main site (default: 100)",
    )
    parser.add_argument(
        "--timeout", type=positive_float, default=15.0,
        help="request timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--site-workers", type=positive_int, default=3,
        help="sites to crawl concurrently (default: 3)",
    )
    parser.add_argument(
        "--delay", type=non_negative_float, default=0.25,
        help="delay between page requests in seconds (default: 0.25)",
    )
    parser.add_argument(
        "--max-page-mb", type=positive_int, default=5,
        help="maximum HTML response size in MiB (default: 5)",
    )
    parser.add_argument(
        "--include-subdomains", action="store_true",
        help="also crawl subdomains of each main site",
    )
    parser.add_argument(
        "--no-sitemaps", action="store_true",
        help="disable robots.txt and common sitemap URL discovery",
    )
    parser.add_argument(
        "--max-sitemaps", type=positive_int, default=200,
        help="maximum sitemap files to inspect per site (default: 200)",
    )
    parser.add_argument(
        "--max-sitemap-urls", type=positive_int, default=250000,
        help="maximum URLs to collect from sitemaps per site (default: 250000)",
    )
    parser.add_argument(
        "--ignore-robots", action="store_true",
        help="ignore robots.txt (use only on sites you own or have permission to crawl)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="ignore progress saved by an interrupted run and start over",
    )
    parser.add_argument(
        "--user-agent", default="StoryGrabberCrawler/2.0",
        help="HTTP user-agent name (default: StoryGrabberCrawler/2.0)",
    )
    parser.add_argument(
        "--site-profiles", default="",
        help='JSON object mapping host -> minimum delay in seconds, e.g. \'{"slow.example.com": 3.0}\'. '
             "Acts as a floor on top of --delay and robots.txt crawl-delay for that host only (default: none)",
    )
    return parser


def parse_site_profiles(raw: str) -> dict[str, float]:
    """Parse --site-profiles' JSON. Malformed input is ignored rather than
    fatal -- a rate-limit hint should never be able to abort a crawl."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ignoring --site-profiles (invalid JSON): {raw!r}", file=sys.stderr)
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, float] = {}
    for host, seconds in parsed.items():
        try:
            result[str(host).strip().lower()] = max(0.0, float(seconds))
        except (TypeError, ValueError):
            continue
    return result


def journal_dir_for(output: Path) -> Path:
    return output.with_name(output.name + ".progress")


def install_stop_signals() -> None:
    """Turn stop requests (Ctrl+C, Ctrl+Break, SIGTERM) into a clean pause."""
    def request_stop(signum: int, frame: Any) -> None:  # noqa: ARG001
        if not STOP.is_set():
            print("\nStop requested: saving progress...", flush=True)
        STOP.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), request_stop)
            except (OSError, ValueError):
                pass


def main() -> int:
    args = build_parser().parse_args()
    install_stop_signals()
    journal_dir = journal_dir_for(args.output)
    try:
        sites = load_sites(args.sites_file)
        site_profiles = parse_site_profiles(args.site_profiles)
        if args.fresh and journal_dir.exists():
            shutil.rmtree(journal_dir)
            print("Starting fresh: discarded saved progress.")
        settings = {
            "depth": args.depth,
            "max_pages": args.max_pages,
            "include_subdomains": args.include_subdomains,
            "sitemaps": not args.no_sitemaps,
            "robots": not args.ignore_robots,
        }
        results: list[dict[str, Any] | None] = [None] * len(sites)
        results_lock = threading.Lock()
        save_lock = threading.Lock()

        def save_current() -> None:
            with results_lock:
                current = [entry for entry in results if entry is not None]
            with save_lock:
                save_json(args.output, current)

        def crawl_indexed(index: int, site: str) -> tuple[int, dict[str, Any] | None]:
            if STOP.is_set():
                return index, None            # never started; next run does it
            print(f"[{index + 1}/{len(sites)}] Crawling {site}")
            journal = SiteJournal(journal_dir, site, settings)

            def on_progress(partial: dict[str, Any]) -> None:
                with results_lock:
                    results[index] = partial
                save_current()

            try:
                result = crawl_site(
                    site,
                    depth=args.depth,
                    max_pages=args.max_pages,
                    timeout=args.timeout,
                    delay=args.delay,
                    max_page_mb=args.max_page_mb,
                    include_subdomains=args.include_subdomains,
                    respect_robots=not args.ignore_robots,
                    user_agent=args.user_agent,
                    use_sitemaps=not args.no_sitemaps,
                    max_sitemaps=args.max_sitemaps,
                    max_sitemap_urls=args.max_sitemap_urls,
                    site_profiles=site_profiles,
                    journal=journal,
                    on_progress=on_progress,
                )
            finally:
                journal.close()
            return index, result

        executor = ThreadPoolExecutor(max_workers=min(args.site_workers, len(sites)))
        pending = {executor.submit(crawl_indexed, index, site) for index, site in enumerate(sites)}
        try:
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    index, result = future.result()
                    if result is None:
                        continue
                    with results_lock:
                        results[index] = result
                    save_current()
                    state = "saved so far" if result.get("partial") else "unique sublink(s)"
                    print(f"  found {result['total_sublinks']} {state} — {result['main_site']}")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        save_current()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if STOP.is_set():
        print(f"Stopped. Partial results saved to {args.output}; run again to resume.")
        return 130
    shutil.rmtree(journal_dir, ignore_errors=True)   # everything finished cleanly
    print(f"Saved results to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
