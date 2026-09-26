#!/usr/bin/env python3
"""Run raw content extraction followed by verified story formatting."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTENT_ENGINE = ROOT / "cli" / "parser" / "content_engine.mjs"
FORMATTER = ROOT / "story_formatter.py"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--delay", type=int, default=500)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--browser-mode", choices=["background", "headless", "visible"], default="background")
    ap.add_argument(
        "--kindle-min-interval", type=float, default=0.0,
        help="forwarded to story_formatter.py; debounces library.json rewrites across rapid successive batches",
    )
    args = ap.parse_args()

    command = [
        "node", str(CONTENT_ENGINE), str(args.input),
        "-o", str(args.output),
        "--fetch-mode", "cloudscraper",
        "--html-only",
        "--browser-mode", args.browser_mode,
        "--retry-failed",
        "--concurrency", str(max(1, min(8, args.concurrency))),
        "--delay", str(max(0, min(60000, args.delay))),
    ]
    if args.limit:
        command.extend(["--limit", str(max(1, args.limit))])
    if args.force:
        command.append("--force")

    print("=== Stage 1/2: raw story extraction ===", flush=True)
    code = subprocess.call(command, cwd=ROOT)
    if code != 0:
        print(f"Raw extraction failed with exit code {code}; formatter was not run.", flush=True)
        return code

    print("\n=== Stage 2/2: story formatting + verification + romanization ===", flush=True)
    format_command = [sys.executable, str(FORMATTER), "--output", str(args.output)]
    if args.force:
        format_command.append("--force")
    if args.kindle_min_interval > 0:
        format_command.extend(["--kindle-min-interval", str(args.kindle_min_interval)])
    return subprocess.call(format_command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
