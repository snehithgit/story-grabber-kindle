FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-pip python3-venv git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

COPY cli/requirements.txt cli/cloudscraper-requirement.txt /app/cli/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r /app/cli/requirements.txt \
    && /opt/venv/bin/pip install --no-deps -r /app/cli/cloudscraper-requirement.txt
ENV PATH="/opt/venv/bin:${PATH}"

COPY cli/parser/package.json cli/parser/package-lock.json /app/cli/parser/
RUN cd /app/cli/parser \
    && npm ci \
    && npx playwright install --with-deps chromium \
    && rm -rf /root/.npm

COPY . /app/
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/summary', timeout=3).read()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
