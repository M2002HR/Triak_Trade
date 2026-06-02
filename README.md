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
triak-trade market-data-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade toobit-klines-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade admin-bot-smoke-test
triak-trade run-admin-bot --once
triak-trade admin-bot-status
triak-trade admin-bot-logs --lines 50
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

## Market Data

- Market data is behind `MarketDataProvider` interface.
- First provider is Toobit public klines (`/quote/v1/klines` by default, path configurable).
- No signed/private Toobit endpoints are used in this module.
- Unit tests use fakes/mocks only.
- Real Toobit checks are optional and guarded by `RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1`.
- Candle cache service stores/fetches OHLCV through repository for future backtesting.

## Toobit Signed Safety Layer

- Signed Toobit client exists for safe checks and Spot `orderTest` only.
- Unit tests use mocks only; real signed checks are guard-gated.
- Live trading remains blocked.
- Withdrawal endpoints are forbidden and not implemented.
- Runtime secrets come only from root `.env.local`.

## Admin Bot Approval

- Admin auth is username-based via `ADMIN_TELEGRAM_USERNAMES` (case-insensitive, `@` optional).
- `ADMIN_USER_IDS` is deprecated/backward-compatible only.
- Admin must start the bot once so username→chat_id can be registered.
- Real bot send tests are guard-gated with `RUN_TELEGRAM_BOT_INTEGRATION_TESTS=1`.
- Approval flow records decisions only; it does not execute trades.
- Runnable admin bot runtime is available but real polling is blocked unless `ADMIN_BOT_RUNTIME_ENABLED=true`.
- Fake smoke/runtime commands do not call Telegram: `admin-bot-smoke-test`, `run-admin-bot --once`, and `run-admin-bot --watch --max-runtime-seconds 10`.
- Real supervised start command: `triak-trade admin-bot-start --real --watch`.
- Runtime files live under `runtime/admin_bot/` and are gitignored.
- Bot logs/status must never include bot tokens, API keys, or session data.

## Backtesting

- Backtest engine is available via CLI (`backtest-fixture`, `backtest-dry-run`).
- Backtest flow can be initiated from admin workflow scaffolding by authorized usernames.
- Backtest is simulation-only and never executes trades.
- AI classification is target architecture; regex remains fallback/safety.
- `https://t.me/Tofan_Trade` is a guarded real-world test target, not a hard-coded rule.

## Verification Pack

- Run `triak-trade verify-system` for safe local verification.
- Run `triak-trade verify-system --mode safe --write-report` to generate JSON/Markdown reports.
- Run `triak-trade verify-real` only after setting `RUN_SYSTEM_REAL_SMOKE_TESTS=1` plus specific service guards.
- Use `triak-trade show-last-report` to inspect the latest generated report.
- Reports redact secrets and real checks never execute live trades.
