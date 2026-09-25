#!/bin/sh
set -eu

APP_DIR="/app"
DATA_DIR="${STORY_GRABBER_DATA_DIR:-/data}"
PORT="${STORY_GRABBER_PORT:-8000}"

mkdir -p "$DATA_DIR" "$DATA_DIR/content_output"

if [ ! -f "$DATA_DIR/settings.json" ]; then
    if [ -f "$APP_DIR/settings.json" ]; then
        cp "$APP_DIR/settings.json" "$DATA_DIR/settings.json"
    else
        printf '%s\n' '{}' > "$DATA_DIR/settings.json"
    fi
fi

[ -f "$DATA_DIR/sites.txt" ] || : > "$DATA_DIR/sites.txt"
[ -f "$DATA_DIR/sublinks.json" ] || printf '%s\n' '[]' > "$DATA_DIR/sublinks.json"

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

# Docker is headless by definition.
data["browser_mode"] = "headless"

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY

rm -rf "$APP_DIR/content_output"
ln -s "$DATA_DIR/content_output" "$APP_DIR/content_output"

rm -f "$APP_DIR/settings.json" "$APP_DIR/sites.txt" "$APP_DIR/sublinks.json"
ln -s "$DATA_DIR/settings.json" "$APP_DIR/settings.json"
ln -s "$DATA_DIR/sites.txt" "$APP_DIR/sites.txt"
ln -s "$DATA_DIR/sublinks.json" "$APP_DIR/sublinks.json"

cd "$APP_DIR"
exec python3 web_server.py --host 0.0.0.0 --port "$PORT" --allow-lan
