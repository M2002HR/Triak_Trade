# Triak_Trade

Triak_Trade is a modular Telegram signal intelligence, backtesting, demo-trading, and monitoring system foundation.
AI classification is designed to run through Ajil Unified AI Gateway; deterministic regex parsing remains fallback/safety.

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

`/.env.local` in project root is the single runtime config source. Do not create `.env` inside `external/Ajil_Unified_AI_Gateway`.

## Start MySQL and Redis

```bash
docker compose up -d
```

## Commands

```bash
triak-trade version
triak-trade health
triak-trade config-check
triak-trade parse-message "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000"
triak-trade agent-dry-run
triak-trade ai-classify-dry-run "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000"
triak-trade telegram-check
triak-trade telegram-history-dry-run https://t.me/Tofan_Trade --limit 5
triak-trade telegram-tofan-dry-run --limit 5
pytest
ruff check .
mypy src
```

## Ajil Gateway

- Submodule location: `external/Ajil_Unified_AI_Gateway`
- Unit tests use fakes/mocks; no real gateway required.
- Real gateway tests must be explicitly guarded (`RUN_AI_GATEWAY_INTEGRATION_TESTS=1` with `AI_GATEWAY_ENABLED=true`).
- First future real-world evaluation channel is `https://t.me/Tofan_Trade` (not hard-coded in parser logic).

## Telegram Collection

- Telethon integration is behind `TelegramClientInterface`; unit tests use fakes only.
- `../Broccoli_Bot` may be used as setup/session-pattern reference only.
- Real Telegram tests are guarded by `RUN_TELEGRAM_INTEGRATION_TESTS=1`.
- Telethon sessions are local-only (`.sessions/`, `*.session*`) and must never be committed.
- `https://t.me/Tofan_Trade` is an integration target, not a hard-coded strategy rule.
