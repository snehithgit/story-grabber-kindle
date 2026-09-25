#!/bin/sh
set -eu

APP_DIR="/app"
DATA_DIR="${STORY_GRABBER_DATA_DIR:-/data}"
APP_PORT="${STORY_GRABBER_INTERNAL_PORT:-8000}"
PROXY_PORT="${STORY_GRABBER_PROXY_PORT:-8080}"

mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/content_output"

if [ ! -f "$DATA_DIR/settings.json" ]; then
    if [ -f "$APP_DIR/settings.json" ]; then
        cp "$APP_DIR/settings.json" "$DATA_DIR/settings.json"
    else
        printf '%s\n' '{}' > "$DATA_DIR/settings.json"
    fi
fi

if [ ! -f "$DATA_DIR/sites.txt" ]; then
    : > "$DATA_DIR/sites.txt"
fi

if [ ! -f "$DATA_DIR/sublinks.json" ]; then
    printf '%s\n' '[]' > "$DATA_DIR/sublinks.json"
fi

python3 - "$DATA_DIR/settings.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

# A visible Chromium window is not useful inside Docker.
data["browser_mode"] = "headless"

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")

os.replace(tmp, path)
PY

rm -rf "$APP_DIR/content_output"
ln -s "$DATA_DIR/content_output" "$APP_DIR/content_output"

rm -f "$APP_DIR/settings.json"
rm -f "$APP_DIR/sites.txt"
rm -f "$APP_DIR/sublinks.json"

ln -s "$DATA_DIR/settings.json" "$APP_DIR/settings.json"
ln -s "$DATA_DIR/sites.txt" "$APP_DIR/sites.txt"
ln -s "$DATA_DIR/sublinks.json" "$APP_DIR/sublinks.json"

cd "$APP_DIR"

# Story Grabber intentionally binds to loopback. Keep that security behavior.
python3 web_server.py --port "$APP_PORT" &
APP_PID=$!

# Expose the loopback-bound app only through this tiny container-local proxy.
socat \
    "TCP-LISTEN:${PROXY_PORT},reuseaddr,fork,bind=0.0.0.0" \
    "TCP:127.0.0.1:${APP_PORT}" &
PROXY_PID=$!

cleanup() {
    kill "$PROXY_PID" 2>/dev/null || true
    kill "$APP_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
}

trap cleanup INT TERM EXIT

while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$PROXY_PID" 2>/dev/null; do
    sleep 1
done

exit 1