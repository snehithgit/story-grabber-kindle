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