FROM python:3.10-slim

ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG ALL_PROXY=""
ARG NO_PROXY=""
ARG PIP_INDEX_URL=""
ARG PIP_EXTRA_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300

WORKDIR /app

# Prefer IPv4 during image builds; some hosts resolve PyPI over IPv6 but cannot
# complete the connection, which stalls `pip install` for minutes.
RUN printf 'precedence ::ffff:0:0/96  100\n' >> /etc/gai.conf

# ── Layer 1: install deps (cached until pyproject.toml changes) ──────────────
# Copy only the install manifests. Build a minimal stub package so setuptools
# can resolve the project's dependencies without needing the real source tree.
COPY pyproject.toml README.md AGENTS.md alembic.ini ./
RUN set -eux; \
    if [ -n "${PIP_INDEX_URL}" ]; then pip config set global.index-url "${PIP_INDEX_URL}"; fi; \
    if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then pip config set global.extra-index-url "${PIP_EXTRA_INDEX_URL}"; fi; \
    pip install --no-cache-dir --retries 10 --timeout 300 \
        'setuptools>=69' wheel; \
    mkdir -p src/triak_trade; \
    touch src/triak_trade/__init__.py; \
    pip install --no-cache-dir --retries 10 --timeout 300 --no-build-isolation .; \
    rm -rf src

# ── Layer 2: copy real source and reinstall only the package (fast, offline) ─
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts
COPY config ./config

RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python3 -c "import fastapi, httpx, redis, uvicorn, yaml; print('python_deps_ok')"

RUN mkdir -p /app/runtime /app/.sessions

EXPOSE 8088
