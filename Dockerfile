FROM python:3.11-slim-bookworm

ENV PYTHONHASHSEED=42 \
    PYTHONUNBUFFERED=1 \
    CAUSALGUI_CHROMIUM=/usr/bin/chromium \
    CAUSALGUI_NODE_MODULES=/opt/t2repair/node_modules \
    CAUSALGUI_EVIDENCE_POLICY=base \
    CAUSALGUI_SCRATCH=/task/scratch

ARG DEBIAN_MIRROR=http://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/t2repair
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt package.json package-lock.json ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && npm ci --omit=dev
COPY pyproject.toml README.md ./
COPY code ./code
RUN python -m pip install --no-deps . \
    && git config --system --add safe.directory /task/repo
WORKDIR /task
ENTRYPOINT ["t2repair"]
