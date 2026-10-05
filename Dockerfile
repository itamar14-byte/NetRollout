# NetRollout app image.
#
#   docker build -t netrollout .
#
# The version is the VERSION file (copied in; the app reads it). The release
# job passes the same value as the VERSION build arg for the image label, and
# the build fails if the two differ.
#
# Build context = the repo root, filtered by .dockerignore (a whitelist:
# src/, templates/, requirements.lock, LICENSE, VERSION, Grafana's setup and
# dashboards). One stage: every locked
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

COPY LICENSE VERSION ./
COPY templates/ templates/
COPY src/ src/
# Grafana's setup service runs on this image: its script and the shipped
# dashboards (deploy/grafana/setup.py)
COPY deploy/grafana/setup.py grafana/setup.py
COPY deploy/grafana/dashboards/ grafana/dashboards/

# A release's build arg must match the VERSION file (one source); then
# precompile (the code is read-only at run time) and create the unprivileged
# user that owns /data.
RUN if [ -n "$VERSION" ] && [ "${VERSION#v}" != "$(tr -d ' \r\n' < VERSION)" ]; then \
        echo "VERSION build arg '$VERSION' differs from the VERSION file" >&2; exit 1; \
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
