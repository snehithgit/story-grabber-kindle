[CmdletBinding()]
param(
    [string]$Repo = "snehithgit/story-grabber-kindle",
    [string]$Branch = "main",
    [string]$CommitMessage = "Update Story Grabber source",
    [switch]$BuildLocal,
    [switch]$ForceDockerFiles
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Image = "ghcr.io/snehithgit/story-grabber-kindle"

function Assert-LastExitCode {
    param([string]$Message)
    if ($LASTEXITCODE -ne 0) {
        throw "$Message (exit code $LASTEXITCODE)"
    }
}

function Write-Utf8NoBomLf {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Content,
        [switch]$Force
    )

    if ((Test-Path -LiteralPath $Path -PathType Leaf) -and (-not $Force)) {
        Write-Host "Keeping existing: $Path"
        return
    }

    $Parent = Split-Path -Parent $Path
    if ($Parent -and (-not (Test-Path -LiteralPath $Parent))) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }

    $Content = $Content -replace "`r`n", "`n"
    $Content = $Content -replace "`r", "`n"

    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        [System.IO.Path]::GetFullPath($Path),
        $Content,
        $Utf8NoBom
    )

    Write-Host "Wrote: $Path"
}

function Ensure-GitIgnoreLine {
    param([Parameter(Mandatory=$true)][string]$Line)

    $Path = ".\.gitignore"

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        [System.IO.File]::WriteAllText(
            [System.IO.Path]::GetFullPath($Path),
            "$Line`n",
            (New-Object System.Text.UTF8Encoding($false))
        )
        return
    }

    $Existing = @(Get-Content -LiteralPath $Path)
    if ($Existing -notcontains $Line) {
        Add-Content -LiteralPath $Path -Value $Line -Encoding UTF8
    }
}

# ---------------------------------------------------------------------------
# 1. Verify project root
# ---------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath ".\web_server.py" -PathType Leaf) -or
    -not (Test-Path -LiteralPath ".\story_pipeline.py" -PathType Leaf) -or
    -not (Test-Path -LiteralPath ".\cli" -PathType Container) -or
    -not (Test-Path -LiteralPath ".\web" -PathType Container)) {
    throw "Run this script from the Story Grabber project root."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not installed or not in PATH."
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is not installed or not in PATH."
}

Write-Host ""
Write-Host "==> Checking GitHub authentication" -ForegroundColor Cyan

& gh auth status
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run: gh auth login"
}

& gh auth setup-git
Assert-LastExitCode "Failed to configure GitHub authentication for Git"

$Login = (& gh api user --jq ".login").Trim()
Assert-LastExitCode "Failed to read GitHub login"

$UserId = (& gh api user --jq ".id").Trim()
Assert-LastExitCode "Failed to read GitHub user ID"

Write-Host "GitHub user: $Login" -ForegroundColor Green
Write-Host "Repository : https://github.com/$Repo" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. Generate Docker/GitHub Actions files
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "==> Preparing Docker files" -ForegroundColor Cyan

$Dockerfile = @'
FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-pip \
       python3-venv \
       git \
       ca-certificates \
       socat \
       tini \
    && rm -rf /var/lib/apt/lists/*

COPY cli/requirements.txt /app/cli/requirements.txt

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r /app/cli/requirements.txt

ENV PATH="/opt/venv/bin:${PATH}"

COPY cli/parser/package.json cli/parser/package-lock.json /app/cli/parser/

RUN cd /app/cli/parser \
    && npm ci \
    && npx playwright install --with-deps chromium \
    && rm -rf /root/.npm

COPY . /app/

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
'@

$Entrypoint = @'
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
'@

$Compose = @'
services:
  story-grabber:
    image: ghcr.io/snehithgit/story-grabber-kindle:latest
    container_name: story-grabber-kindle
    restart: unless-stopped

    ports:
      - "127.0.0.1:8000:8080"

    volumes:
      - ./story-grabber-data:/data

    shm_size: "1gb"
'@

$DockerIgnore = @'
.git
.github
content_output
story-grabber-data
sublinks.json
sublinks.json.progress
sites.txt
cli/parser/node_modules
cli/content_browser_profile
__pycache__
*.pyc
*.pyo
*.zip
*.log
.vscode
.idea
'@

$Workflow = @'
name: Build and publish Docker image

on:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read
  packages: write

concurrency:
  group: story-grabber-docker-${{ github.ref }}
  cancel-in-progress: true

jobs:
  docker:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout source
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GitHub Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Read Story Grabber version
        id: version
        shell: bash
        run: |
          if [ -f VERSION ]; then
            VERSION="$(tr -d '\r\n ' < VERSION)"
          else
            VERSION="dev"
          fi

          VERSION="${VERSION#v}"

          if [ -z "$VERSION" ]; then
            VERSION="dev"
          fi

          echo "value=$VERSION" >> "$GITHUB_OUTPUT"

      - name: Build and publish image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: ./Dockerfile
          platforms: linux/amd64
          push: true
          tags: |
            ghcr.io/snehithgit/story-grabber-kindle:latest
            ghcr.io/snehithgit/story-grabber-kindle:v${{ steps.version.outputs.value }}
            ghcr.io/snehithgit/story-grabber-kindle:sha-${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          provenance: mode=max
          sbom: true
'@

Write-Utf8NoBomLf -Path ".\Dockerfile" -Content $Dockerfile -Force:$ForceDockerFiles
Write-Utf8NoBomLf -Path ".\docker-entrypoint.sh" -Content $Entrypoint -Force:$ForceDockerFiles
Write-Utf8NoBomLf -Path ".\docker-compose.yml" -Content $Compose -Force:$ForceDockerFiles
Write-Utf8NoBomLf -Path ".\.dockerignore" -Content $DockerIgnore -Force:$ForceDockerFiles
Write-Utf8NoBomLf -Path ".\.github\workflows\docker-publish.yml" -Content $Workflow -Force:$ForceDockerFiles

Ensure-GitIgnoreLine "content_output/"
Ensure-GitIgnoreLine "story-grabber-data/"
Ensure-GitIgnoreLine "sublinks.json"
Ensure-GitIgnoreLine "sublinks.json.progress"
Ensure-GitIgnoreLine "sites.txt"
Ensure-GitIgnoreLine "cli/parser/node_modules/"
Ensure-GitIgnoreLine "cli/content_browser_profile/"
Ensure-GitIgnoreLine "__pycache__/"
Ensure-GitIgnoreLine "*.pyc"
Ensure-GitIgnoreLine "*.zip"
Ensure-GitIgnoreLine "*.log"

# ---------------------------------------------------------------------------
# 3. Optional local Docker build
# ---------------------------------------------------------------------------

if ($BuildLocal) {
    Write-Host ""
    Write-Host "==> Building Docker image locally" -ForegroundColor Cyan

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not installed or not in PATH."
    }

    & docker version
    Assert-LastExitCode "Docker is not available"

    & docker build --pull -t "$Image`:local" .
    Assert-LastExitCode "Local Docker build failed"

    Write-Host "Local image built: $Image`:local" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 4. Prepare Git exactly like the user's known-good publisher
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "==> Preparing Git repository" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath ".\.git" -PathType Container)) {
    & git init
    Assert-LastExitCode "git init failed"

    & git remote add origin "https://github.com/$Repo.git"
    Assert-LastExitCode "Failed to add GitHub remote"

    # If the GitHub repository already has history, attach to it.
    # For a truly empty repository this fetch simply fails, and we create main.
    & git fetch origin $Branch *> $null

    if ($LASTEXITCODE -eq 0) {
        & git update-ref "refs/heads/$Branch" FETCH_HEAD
        Assert-LastExitCode "Failed to attach local branch to existing GitHub history"

        & git symbolic-ref HEAD "refs/heads/$Branch"
        Assert-LastExitCode "Failed to select branch"

        & git reset --mixed "refs/heads/$Branch" *> $null
        Assert-LastExitCode "Failed to prepare existing GitHub history"
    }
    else {
        & git branch -M $Branch
        Assert-LastExitCode "Failed to create branch $Branch"
    }
}
else {
    $CurrentBranch = (& git branch --show-current).Trim()
    Assert-LastExitCode "Failed to read current Git branch"

    if ([string]::IsNullOrWhiteSpace($CurrentBranch)) {
        & git branch -M $Branch
        Assert-LastExitCode "Failed to select branch $Branch"
    }
    elseif ($CurrentBranch -ne $Branch) {
        throw "Current branch is '$CurrentBranch'. Checkout '$Branch' before publishing."
    }

    & git remote get-url origin *> $null

    if ($LASTEXITCODE -eq 0) {
        & git remote set-url origin "https://github.com/$Repo.git"
        Assert-LastExitCode "Failed to update GitHub remote"
    }
    else {
        & git remote add origin "https://github.com/$Repo.git"
        Assert-LastExitCode "Failed to add GitHub remote"
    }
}

& git config user.name $Login
Assert-LastExitCode "Failed to set Git user name"

& git config user.email "$UserId+$Login@users.noreply.github.com"
Assert-LastExitCode "Failed to set Git user email"

# ---------------------------------------------------------------------------
# 5. Safety check: refuse runtime/private files
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "==> Checking publish safety" -ForegroundColor Cyan

& git add -A
Assert-LastExitCode "git add failed"

$Forbidden = '(^|/)(\.env($|\.)|content_output/|story-grabber-data/|cli/content_browser_profile/|sublinks\.json($|\.)|sites\.txt$|[^/]+\.(db|sqlite|sqlite3)($|-))'

$Tracked = @(& git ls-files)
Assert-LastExitCode "Failed to inspect tracked files"

$TrackedUnsafe = @(
    $Tracked |
    Where-Object {
        $_ -notmatch '(^|/)\.env\.example$' -and
        $_ -match $Forbidden
    }
)

if ($TrackedUnsafe.Count -gt 0) {
    Write-Error (
        "Refusing to publish because runtime/private files are already tracked:`n" +
        ($TrackedUnsafe -join "`n")
    )
}

$Staged = @(& git diff --cached --name-only)
Assert-LastExitCode "Failed to inspect staged files"

$Unsafe = @(
    $Staged |
    Where-Object {
        $_ -notmatch '(^|/)\.env\.example$' -and
        $_ -match $Forbidden
    }
)

if ($Unsafe.Count -gt 0) {
    Write-Error (
        "Refusing to publish because runtime/private files are staged:`n" +
        ($Unsafe -join "`n")
    )
}

# ---------------------------------------------------------------------------
# 6. Commit and push
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Files/changes to publish:" -ForegroundColor Cyan

& git status --short
Assert-LastExitCode "git status failed"

& git diff --cached --quiet

if ($LASTEXITCODE -eq 0) {
    Write-Host "No source changes to publish." -ForegroundColor Yellow
}
else {
    & git commit -m $CommitMessage
    Assert-LastExitCode "git commit failed"

    # Normal push only. Never force-push.
    & git push -u origin $Branch
    Assert-LastExitCode "GitHub push failed; remote history was left unchanged"

    Write-Host ""
    Write-Host "Source uploaded without rewriting Git history." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 7. Show Docker workflow status
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "==> GitHub Actions / Docker image" -ForegroundColor Cyan

Start-Sleep -Seconds 3

& gh run list --repo $Repo --workflow docker-publish.yml --limit 5

if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not list workflow runs yet. The Git push itself succeeded."
}

Write-Host ""
Write-Host "Repository:" -ForegroundColor Green
Write-Host "  https://github.com/$Repo"

Write-Host ""
Write-Host "Docker image after the workflow completes:" -ForegroundColor Green
Write-Host "  $Image`:latest"

if (Test-Path -LiteralPath ".\VERSION" -PathType Leaf) {
    $Version = (Get-Content -LiteralPath ".\VERSION" -Raw).Trim()
    if (-not [string]::IsNullOrWhiteSpace($Version)) {
        $Version = $Version.TrimStart("v")
        Write-Host "  $Image`:v$Version"
    }
}

Write-Host ""
Write-Host "To watch the newest workflow:" -ForegroundColor Cyan
Write-Host "  gh run watch --repo $Repo"

Write-Host ""
Write-Host "To run the image later:" -ForegroundColor Cyan
Write-Host "  docker compose pull"
Write-Host "  docker compose up -d"

Write-Host ""
Write-Host "Open:" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:8000"
