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

docker compose up --build -d
docker compose ps
