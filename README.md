# Triak_Trade

Triak_Trade is a modular Telegram signal intelligence platform focused on safe parsing, AI-assisted classification, backtesting, demo/live session monitoring, and operator visibility.

The project follows a few hard rules:
- Runtime configuration comes only from the root `.env.local`.
- Financial values use `Decimal`, never `float`.
- External services stay behind interfaces/adapters.
- Real integrations are always guard-gated.
- Backtesting is simulation-only and never places real trades.

## What Is In The Repo

- `src/triak_trade/agents`: channel state, consolidation, and message-driven actions.
- `src/triak_trade/ai`: Ajil Unified AI Gateway client, runtime helpers, prompts, and AI classifier.
- `src/triak_trade/backtesting`: fixture backtests, real Telegram backtest pipeline, simulator, scoring, and report storage.
- `src/triak_trade/dashboard`: local FastAPI/Jinja dashboard for backtests, reports, settings, and live/demo session monitoring.
- `src/triak_trade/exchange/toobit`: public market data access plus signed/demo-safe trading adapters.
- `src/triak_trade/live_trading`: session state and execution orchestration for demo/live workflows.
- `src/triak_trade/market_data`: Binance public, Toobit public, composite provider, and candle cache service.
- `src/triak_trade/observability`: processing audit, redaction, event bus, and Telegram log-channel reporting.
- `src/triak_trade/parsing`: normalizer, regex parser, and validator.
- `src/triak_trade/telegram`: Telethon-backed client interfaces, history sync, and live listener building blocks.
- `src/triak_trade/verification`: safe and guarded real verification checks with redacted reports.
- `docs/`: English architecture and operations notes for the current codebase.
- `external/Ajil_Unified_AI_Gateway`: git submodule for the AI gateway dependency.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Configuration

```bash
cp .env.example .env.local
```

Important rules:
- Keep runtime secrets only in the root `.env.local`.
- Do not create a separate `.env` inside `external/Ajil_Unified_AI_Gateway`.
- Do not commit `.sessions/` or `*.session*`.

Useful defaults:
- The local dashboard binds to `http://127.0.0.1:8088`.
- The local Ajil gateway binds to `http://127.0.0.1:8090`.
- Real backtesting is disabled until its guards are explicitly enabled.
- Live trading sessions are blocked until `LIVE_TRADING_LIVE_MODE_ENABLED=true`.

## Start The Local Stack

```bash
docker compose up --build
```

If Docker previously left stale project resources behind, use:

```bash
./scripts/stack_up.sh
```

That helper:
- Ensures `.env -> .env.local` exists for local compose substitution.
- Runs `docker compose down --remove-orphans` for this project.
- Restarts the stack with plain BuildKit progress.

The compose stack starts:
- MySQL
- Redis
- Ajil Unified AI Gateway
- Triak dashboard

## CLI Surface

Core:

```bash
triak-trade version
triak-trade health
triak-trade config-check
triak-trade db-check
triak-trade parse-message "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000"
triak-trade agent-dry-run
```

AI gateway:

```bash
triak-trade ai-classify-dry-run "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000"
triak-trade ai-gateway-check
triak-trade ai-gateway-start
triak-trade ai-gateway-status
triak-trade ai-gateway-stop
triak-trade ai-gateway-restart
triak-trade ai-gateway-logs
```

Telegram and market data:

```bash
triak-trade telegram-check
triak-trade telegram-history-dry-run https://t.me/Tofan_Trade --limit 5
triak-trade telegram-tofan-dry-run --limit 5
triak-trade market-data-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade toobit-klines-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade binance-public-klines-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade toobit-check
triak-trade toobit-public-check
triak-trade toobit-signed-check
triak-trade toobit-order-test --symbol BTCUSDT --side BUY --type LIMIT --quantity 0.001 --price 10000
```

Backtesting:

```bash
triak-trade backtest-fixture
triak-trade backtest-dry-run --channel https://t.me/Tofan_Trade --from 2026-06-01 --to 2026-06-02 --interval 1m
triak-trade real-backtest-check
triak-trade real-backtest-run --channel https://t.me/Tofan_Trade --hours 24 --interval 1m
triak-trade real-backtest-tofan --hours 24
triak-trade backtest-show-latest
```

Observability and dashboard:

```bash
triak-trade log-channel-check
triak-trade log-channel-format-dry-run
triak-trade log-channel-send-test --real
triak-trade process-message-audit-dry-run
triak-trade dashboard-check
triak-trade run-dashboard
triak-trade dashboard-start
triak-trade dashboard-status
triak-trade dashboard-stop
triak-trade dashboard-restart
triak-trade dashboard-logs --lines 100
triak-trade dashboard-smoke-test
triak-trade dashboard-token-hint
```

Verification:

```bash
triak-trade verify-system
triak-trade verify-system --mode safe --write-report
triak-trade verify-real
triak-trade show-last-report
```

## Real-Integration Guards

These checks are intentionally strict:

- AI gateway integration: `RUN_AI_GATEWAY_INTEGRATION_TESTS=1`
- Telegram integration: `RUN_TELEGRAM_INTEGRATION_TESTS=1`
- Binance public historical market data: `RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1`
- Toobit public market data: `RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1`
- Toobit signed checks: `RUN_TOOBIT_SIGNED_INTEGRATION_TESTS=1`
- Spot order test: `RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS=1`
- Real backtest pipeline: `REAL_BACKTEST_ENABLED=true` plus the required real-integration guards above
- Telegram log-channel sending: `TELEGRAM_LOG_CHANNEL_ENABLED=true`, `PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=true`, and `RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=1`
- Verification real smoke checks: `RUN_SYSTEM_REAL_SMOKE_TESTS=1`
- Live session unlock: `LIVE_TRADING_LIVE_MODE_ENABLED=true`

## Backtesting Notes

- The fixture path uses deterministic in-memory messages and candles.
- The real pipeline is driven by `RealBacktestRunner`.
- Real backtests read Telegram history, classify messages, fetch public market data, simulate trades, and write JSON/Markdown reports to `runtime/reports/backtests`.
- The simulator supports conservative and optimistic fill policy comparisons.
- Fees are modeled via `BACKTEST_FEE_RATE_PCT`.
- Strategy loading comes from `config/strategies.yaml` with safe fallback defaults.

Known behavior worth keeping in mind:
- `real-backtest-check` currently creates the report/cache directories as a side effect.
- Real backtest readiness currently requires multiple integration-style guard flags, not just one runtime flag.
- The live backtest dashboard still uses a throttled replay model rather than a fully incremental simulator.

## Dashboard And Live/Demo Workflows

- The dashboard is local-first and server-rendered with FastAPI, Jinja, and WebSockets.
- Dashboard auth uses `DASHBOARD_ADMIN_TOKEN` from the root `.env.local`.
- Auto Mode and Kill Switch are persisted as runtime state, not a replacement for live-execution gating.
- Demo sessions use connected Toobit account state and demo/private symbol rules such as `TBV_...` depending on exchange support.
- Live sessions remain blocked unless `LIVE_TRADING_LIVE_MODE_ENABLED=true`.

## Ajil Gateway

- The Ajil gateway lives in the git submodule at `external/Ajil_Unified_AI_Gateway`.
- Compose builds it from the submodule and injects runtime env from the root `.env.local`.
- Local host runtime helpers also read only the root `.env.local`.
- Unit tests use fakes/mocks; real gateway access is optional and guard-gated.

## Verification Before Finishing Work

Project policy requires:

```bash
ruff check .
mypy src
pytest
```

When a task touches a runtime interface, also run the smallest safe dry-run or smoke command for that module and inspect its output.

## Documentation

Start with [docs/README.md](docs/README.md). The `docs/` folder is now the English source of truth for the architecture and known issues of this repository itself. Documentation inside `external/Ajil_Unified_AI_Gateway` belongs to the submodule and is not treated as Triak_Trade-owned docs.
