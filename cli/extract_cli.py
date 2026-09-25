#!/usr/bin/env python3
"""Cross-platform launcher for Story Grabber's command-line engines."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path


CLI_DIR = Path(__file__).resolve().parent
CRAWLER = CLI_DIR / "site_crawler.py"
CONTENT_ENGINE = CLI_DIR / "parser" / "content_engine.mjs"
PYTHON_REQUIREMENTS = CLI_DIR / "requirements.txt"
CLOUDSCRAPER_REQUIREMENT = CLI_DIR / "cloudscraper-requirement.txt"
NODE_PACKAGE = CLI_DIR / "parser"


def system_browser_available() -> bool:
    if os.name != "nt":
        return False
    roots = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
    relatives = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    ]
    return any(
        (Path(root) / relative).is_file()
        for root in roots if root
        for relative in relatives
    )


def usage() -> None:
    print(
        """Story Grabber CLI

Usage:
  python extract_cli.py setup
  python extract_cli.py doctor
  python extract_cli.py links [SITES_FILE] [crawler options]
  python extract_cli.py content [SUBLINKS_FILE] [content options]

Examples:
  python extract_cli.py links sites.txt -o sublinks.json --depth 1
  python extract_cli.py content sublinks.json -o content_output
  python extract_cli.py content sublinks.json -o content_output --limit 10

Run `python extract_cli.py links --help` or
`python extract_cli.py content --help` for engine-specific options.
"""
    )


def has_option(args: list[str], *names: str) -> bool:
    return any(arg in names or any(arg.startswith(name + "=") for name in names) for arg in args)


def terminate_tree(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        if not force:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                return
            except (OSError, ValueError):
                pass
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        except ProcessLookupError:
            pass


def run(command: list[str], *, env: dict[str, str] | None = None) -> int:
    print("Running:", " ".join(command), flush=True)
    popen_options = {"cwd": Path.cwd(), "env": env}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(command, **popen_options)
    try:
        return process.wait()
    except KeyboardInterrupt:
        print("\nStopping engine...", file=sys.stderr, flush=True)
        terminate_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_tree(process, force=True)
            process.wait(timeout=5)
        return 130


def setup() -> int:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    python_result = run(
        [sys.executable, "-m", "pip", "install", "-r", str(PYTHON_REQUIREMENTS)],
        env=environment,
    )
    if python_result:
        return python_result
    cloudscraper_result = run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps",
            "-r", str(CLOUDSCRAPER_REQUIREMENT),
        ],
        env=environment,
    )
    if cloudscraper_result:
        return cloudscraper_result
    # Cloudscraper supports Node as its JavaScript interpreter. Remove js2py,
    # which is not used by this project and has no patched release for
    # CVE-2024-28397.
    uninstall_result = run(
        [sys.executable, "-m", "pip", "uninstall", "-y", "js2py"],
        env=environment,
    )
    if uninstall_result:
        return uninstall_result
    npm = shutil.which("npm")
    if not npm:
        print("ERROR: npm was not found. Install Node.js 20 or newer.", file=sys.stderr)
        return 1
    npm_result = run([npm, "ci", "--prefix", str(NODE_PACKAGE)])
    if npm_result:
        return npm_result
    if system_browser_available():
        print("Browser      : using installed Microsoft Edge or Google Chrome")
        return 0
    node = shutil.which("node")
    playwright_cli = NODE_PACKAGE / "node_modules" / "playwright" / "cli.js"
    browser_environment = environment.copy()
    browser_environment.setdefault("PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT", "120000")
    return run([node, str(playwright_cli), "install", "chromium"], env=browser_environment)


def doctor() -> int:
    problems: list[str] = []
    print(f"Python       : {sys.version.split()[0]} ({sys.executable})")
    node = shutil.which("node")
    if node:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
        print(f"Node.js      : {result.stdout.strip()} ({node})")
    else:
        problems.append("Node.js is not installed or is not on PATH.")
    try:
        import cloudscraper  # type: ignore

        print(f"Cloudscraper : {getattr(cloudscraper, '__version__', 'installed')}")
    except ImportError:
        problems.append("Cloudscraper is not installed. Run the setup command.")
    for required in (
        CRAWLER, CONTENT_ENGINE, PYTHON_REQUIREMENTS, CLOUDSCRAPER_REQUIREMENT,
        NODE_PACKAGE / "package.json",
    ):
        if not required.is_file():
            problems.append(f"Missing file: {required}")
    node_modules = NODE_PACKAGE / "node_modules"
    if not node_modules.is_dir():
        problems.append("Node dependencies are not installed. Run the setup command.")
    if problems:
        for problem in problems:
            print(f"ERROR        : {problem}")
        return 1
    print("Status       : ready")
    return 0


def links(arguments: list[str]) -> int:
    return run([sys.executable, str(CRAWLER), *arguments])


def content(arguments: list[str]) -> int:
    node = shutil.which("node")
    if not node:
        print("ERROR: Node.js 20 or newer is required.", file=sys.stderr)
        return 1
    args = list(arguments)
    no_html_only = "--no-html-only" in args
    args = [arg for arg in args if arg != "--no-html-only"]
    if not args or args[0].startswith("-"):
        args.insert(0, str(Path.cwd() / "sublinks.json"))
    if not has_option(args, "-o", "--output"):
        args.extend(["-o", str(Path.cwd() / "content_output")])
    if not has_option(args, "--fetch-mode"):
        args.extend(["--fetch-mode", "cloudscraper"])
    if not no_html_only and not has_option(args, "--html-only"):
        args.append("--html-only")
    environment = os.environ.copy()
    environment["PYTHON"] = sys.executable
    environment["PYTHONUTF8"] = "1"
    return run([node, str(CONTENT_ENGINE), *args], env=environment)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        usage()
        return 0
    command, arguments = sys.argv[1], sys.argv[2:]
    if command == "setup":
        return setup()
    if command == "doctor":
        return doctor()
    if command == "links":
        return links(arguments)
    if command == "content":
        return content(arguments)
    print(f"ERROR: Unknown command: {command}", file=sys.stderr)
    usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
