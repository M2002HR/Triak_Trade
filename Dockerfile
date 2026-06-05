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
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

COPY pyproject.toml README.md AGENTS.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY scripts ./scripts

RUN set -eux; \
    if [ -n "${PIP_INDEX_URL}" ]; then pip config set global.index-url "${PIP_INDEX_URL}"; fi; \
    if [ -n "${PIP_EXTRA_INDEX_URL}" ]; then pip config set global.extra-index-url "${PIP_EXTRA_INDEX_URL}"; fi; \
    HTTP_PROXY="${HTTP_PROXY}" HTTPS_PROXY="${HTTPS_PROXY}" ALL_PROXY="${ALL_PROXY}" NO_PROXY="${NO_PROXY}" \
    pip install --no-cache-dir --retries 20 \
        'setuptools>=69' \
        wheel \
        'httpx[socks]>=0.27.0'; \
    HTTP_PROXY="${HTTP_PROXY}" HTTPS_PROXY="${HTTPS_PROXY}" ALL_PROXY="${ALL_PROXY}" NO_PROXY="${NO_PROXY}" \
    pip install --no-cache-dir --retries 20 --no-build-isolation . \
    && python3 -c "import fastapi, httpx, redis, uvicorn; print('python_deps_ok')"

RUN mkdir -p /app/runtime /app/.sessions

EXPOSE 8088
