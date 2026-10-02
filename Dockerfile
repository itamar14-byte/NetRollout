# NetRollout app image.
#
#   docker build -t netrollout .                              (dev: 1.0.0.dev0)
#   docker build --build-arg VERSION=1.0.0 -t netrollout .    (a release)
#
# Pass VERSION without the tag's "v" (CI: ${GITHUB_REF_NAME#v}). A leading v
# is stripped for the app, but the image's version label shows it as given.
#
# Build context = the repo root, filtered by .dockerignore (a whitelist:
# src/, templates/, requirements.lock, LICENSE). One stage: every locked
# package installs as a prebuilt wheel, so there is no compiler to leave behind.
# Persistent state lives outside the image: the database in Postgres, and
# /data/{logs,config,certs} mounted by compose.

FROM python:3.12-slim-bookworm

ARG VERSION=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NETROLLOUT_DEPLOYMENT=docker \
    NETROLLOUT_HOME=/data \
    PORT=8080

WORKDIR /app

# Dependencies first: this layer is reused while only the code changes
COPY requirements.lock ./
RUN pip install --root-user-action=ignore -r requirements.lock

COPY LICENSE ./
COPY templates/ templates/
COPY src/ src/

# A release stamps its version (tag vX.Y.Z → X.Y.Z) into src/runtime.py — one
# source for the health endpoint, the footer and its source link; the build
# fails if the line wasn't found (the pattern ends at the closing quote, so a
# Windows checkout's CRLF line endings don't matter). Then precompile (the code is read-only at
# run time) and create the unprivileged user that owns /data.
RUN if [ -n "$VERSION" ]; then \
        v="${VERSION#v}"; \
        sed -i "s/^VERSION = \"[^\"]*\"/VERSION = \"$v\"/" src/runtime.py && \
        grep -q "^VERSION = \"$v\"" src/runtime.py; \
    fi && \
    python -m compileall -q src && \
    useradd --system --uid 10001 --user-group --no-create-home \
            --home-dir /data netrollout && \
    mkdir -p /data/logs /data/config /data/certs && \
    chown -R netrollout:netrollout /data

LABEL org.opencontainers.image.title="NetRollout" \
      org.opencontainers.image.description="Push configuration to many network devices at once" \
      org.opencontainers.image.version="${VERSION:-dev}" \
      org.opencontainers.image.source="https://github.com/itamar14-byte/NetRollout" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

USER netrollout
EXPOSE 8080

# 200 only when Postgres and Redis are both up; the start period covers the
# migrations a new version runs at its first start
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/_netrollout/health', timeout=4)"]

CMD ["python", "-m", "src.webapp"]
