#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f .env.local ]]; then
  echo "root .env.local is required" >&2
  exit 1
fi

if [[ ! -e .env ]]; then
  ln -s .env.local .env
fi

# Docker can keep stale `triak_trade_*` containers/endpoints around after an
# interrupted `docker compose up`, which then fails with:
# "failed to create endpoint ... network ... does not exist".
# Clean only this project's stopped resources before recreating the stack.
docker compose down --remove-orphans || true

docker compose up --build -d
docker compose ps
