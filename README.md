# Triak_Trade

Triak_Trade is a modular Telegram signal intelligence, backtesting, demo-trading, and monitoring system foundation.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Environment

```bash
cp .env.example .env.local
```

## Start MySQL and Redis

```bash
docker compose up -d
```

## Commands

```bash
triak-trade version
triak-trade health
triak-trade config-check
pytest
ruff check .
mypy src
```
