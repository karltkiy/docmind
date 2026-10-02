# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: compiles wheels that need a toolchain (asyncpg, etc.).
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY demo ./demo

# Build the virtualenv with every runtime dependency, then drop pip. The venv
# is the only Python environment in the runtime image and pip ships vendored
# copies of dependencies (msgpack, urllib3, ...) that are not used at runtime
# yet keep triggering container scans.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[demo]" \
    && rm -rf /opt/venv/lib/python*/site-packages/pip \
              /opt/venv/lib/python*/site-packages/pip-*.dist-info \
              /opt/venv/bin/pip /opt/venv/bin/pip3* /opt/venv/bin/easy_install*

# ---------------------------------------------------------------------------
# Runtime: slim image without any build toolchain.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DEBIAN_FRONTEND=noninteractive

# curl is required by the Compose healthcheck.
# `apt-get upgrade` applies base-image security updates (e.g. libpcre2-8-0
# CVE-2026-103111). The base image's pip/setuptools are then removed: the venv
# on PATH provides every runtime dependency, while pip's vendored copies of
# msgpack/urllib3/setuptools are what the image scan flags.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/python*/site-packages/pip \
              /usr/local/lib/python*/site-packages/pip-*.dist-info \
              /usr/local/lib/python*/site-packages/setuptools \
              /usr/local/lib/python*/site-packages/setuptools-*.dist-info \
              /usr/local/lib/python*/site-packages/pkg_resources \
              /usr/local/lib/python*/site-packages/_distutils_hack \
              /usr/local/lib/python*/site-packages/wheel \
              /usr/local/lib/python*/site-packages/wheel-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3* /usr/local/bin/easy_install*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

# Non-root runtime user with a writable upload directory.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000 8501

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
