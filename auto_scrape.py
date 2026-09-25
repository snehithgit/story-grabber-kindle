#!/usr/bin/env python3
"""Incremental crawl + scrape coordinator.

The crawler already refreshes sublinks.json with partial results every few
seconds.  This coordinator watches that file and immediately feeds newly found
links to the existing story pipeline in small batches while crawling continues.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CRAWLER = ROOT / "cli" / "site_crawler.py"
PIPELINE = ROOT / "story_pipeline.py"

STOP = threading.Event()
CHILDREN: list[subprocess.Popen[str]] = []
CHILD_LOCK = threading.Lock()


def log(message: str) -> None:
    print(message, flush=True)


def stop_handler(signum: int, _frame: object) -> None:
    STOP.set()
    log(f"Stop requested (signal {signum}).")
    with CHILD_LOCK:
        children = list(CHILDREN)
    for child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def discovered_links(path: Path) -> list[dict[str, str]]:
    payload = load_json(path, [])
    groups = payload if isinstance(payload, list) else [payload]
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        for entry in group.get("sublinks", []) or []:
            if isinstance(entry, str):
                url, title = entry, entry
            elif isinstance(entry, dict):
                url = str(entry.get("link") or entry.get("url") or "")
                title = str(entry.get("title") or entry.get("name") or url)
            else:
                continue
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                result.append({"link": url, "title": title})
    return result


def terminal_urls(output_dir: Path) -> set[str]:
    manifest = load_json(output_dir / "manifest.json", {"pages": {}})
    states = manifest.get("pages", {}) if isinstance(manifest, dict) else {}
    progress = output_dir / "progress.jsonl"
    if progress.is_file():
        try:
            for line in progress.read_text("utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("url") and entry.get("state"):
                    states[entry["url"]] = entry["state"]
        except OSError:
            pass
    return {
        url for url, state in states.items()
        if isinstance(state, dict) and state.get("status") in {"success", "failed"}
    }


def start_process(command: list[str], prefix: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    with CHILD_LOCK:
        CHILDREN.append(process)

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            log(f"[{prefix}] {line.rstrip()}")

    threading.Thread(target=pump, daemon=True).start()
    return process


def wait_process(process: subprocess.Popen[str]) -> int:
    while process.poll() is None and not STOP.is_set():
        time.sleep(0.2)
    if STOP.is_set() and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    code = process.wait()
    with CHILD_LOCK:
        if process in CHILDREN:
            CHILDREN.remove(process)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl and scrape links incrementally")
    ap.add_argument("--sites", type=Path, required=True)
    ap.add_argument("--sublinks", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--max-pages", type=int, default=1000)
    ap.add_argument("--crawl-delay", type=float, default=0.25)
    ap.add_argument("--content-delay", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--browser-mode", choices=["background", "headless", "visible"], default="background")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    batch_file = args.output / "auto_batch.json"
    crawler_command = [
        sys.executable, str(CRAWLER), str(args.sites), "-o", str(args.sublinks),
        "--depth", str(max(0, args.depth)),
        "--max-pages", str(max(1, args.max_pages)),
        "--delay", str(max(0.0, args.crawl_delay)),
    ]
    if args.fresh:
        crawler_command.append("--fresh")

    log("AUTO MODE: crawler and incremental story scraping started.")
    crawler = start_process(crawler_command, "crawl")
    batch_number = 0
    crawler_code: int | None = None
    attempted_this_run: set[str] = set()

    try:
        while not STOP.is_set():
            if crawler_code is None and crawler.poll() is not None:
                crawler_code = wait_process(crawler)
                log(f"Crawler finished with exit code {crawler_code}.")

            discovered = discovered_links(args.sublinks)
            done = terminal_urls(args.output)
            pending = [
                item for item in discovered
                if item["link"] not in done and item["link"] not in attempted_this_run
            ]

            if pending:
                batch_number += 1
                batch = pending[: max(1, min(200, args.batch_size))]
                attempted_this_run.update(item["link"] for item in batch)
                batch_file.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                log(f"Auto-scrape batch {batch_number}: {len(batch)} newly discovered link(s).")
                command = [
                    sys.executable, str(PIPELINE),
                    "--input", str(batch_file),
                    "--output", str(args.output),
                    "--concurrency", str(max(1, min(8, args.concurrency))),
                    "--delay", str(max(0, min(60000, args.content_delay))),
                    "--browser-mode", args.browser_mode,
                ]
                pipeline = start_process(command, "scrape")
                code = wait_process(pipeline)
                if STOP.is_set():
                    break
                if code != 0:
                    log(f"Story batch finished with exit code {code}; continuing with newly found links.")
                continue

            if crawler_code is not None:
                break
            time.sleep(1.0)
    finally:
        STOP.set()
        with CHILD_LOCK:
            children = list(CHILDREN)
        for child in children:
            if child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    pass

    if STOP.is_set() and crawler_code is None:
        return 130
    log("AUTO MODE complete: no newly discovered links remain pending.")
    return 0 if crawler_code in {None, 0} else crawler_code


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_handler)
    raise SystemExit(main())
