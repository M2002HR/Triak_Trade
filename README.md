# Triak Trade

**A modular Telegram signal-intelligence platform with parsing, AI-assisted classification, historical backtesting, guarded exchange integrations, and an operator dashboard.**

Triak ingests trading-signal messages, normalizes and validates them, enriches ambiguous content through the Ajil Unified AI Gateway, retrieves market data, runs simulation-only backtests, and exposes operational workflows through a local dashboard and CLI.

> This is an engineering and simulation project, not financial advice. Backtesting does not prove future performance. Real exchange actions are disabled unless explicit safety gates are enabled.

## Engineering highlights

- Structured parsing and validation of Telegram trading signals
- AI-assisted classification behind a provider gateway
- Decimal-based financial calculations
- Historical backtesting with configurable fill, fee, risk, and strategy rules
- Toobit and Binance market-data adapters
- Guarded signed-exchange checks and demo/live session workflows
- FastAPI/Jinja dashboard with WebSocket progress updates
- MySQL and Redis service stack
- Audit events, redaction, rotating logs, and Telegram operator alerts
- Account-level execution coordination and logical position ownership
- Safe verification commands and opt-in real-integration tests
- Ruff, mypy, and pytest verification

## Technology

`Python` · `FastAPI` · `Jinja` · `WebSockets` · `Telethon` · `MySQL` · `Redis` · `Docker Compose` · `Decimal` · `pytest` · `mypy` · `Ruff`

## Architecture

```text
Telegram channels
       │
       ▼
History sync / live listener
       │
       ▼
Normalizer and deterministic parser
       │
       ├── valid signal ─────────────┐
       └── ambiguous content ─► Ajil AI classifier
                                      │
                                      ▼
                             Validated signal model
                                      │
                   ┌──────────────────┴──────────────────┐
                   ▼                                     ▼
          Backtest simulator                    Demo/live coordinator
                   │                                     │
          Public market data                 Guarded exchange adapters
                   │                                     │
                   └──────────────────┬──────────────────┘
                                      ▼
                     Reports, audit events, dashboard
```

## Core modules

| Module | Responsibility |
| --- | --- |
| `parsing` | Message normalization, regex parsing, validation, and structured signal models |
| `ai` | Ajil client, prompts, and AI-assisted classification |
| `telegram` | History synchronization and live listener building blocks |
| `market_data` | Toobit, Binance, composite provider, and candle cache |
| `backtesting` | Simulation, scoring, report generation, and comparison |
| `live_trading` | Guarded session state and exchange execution coordination |
| `exchange/toobit` | Public data and signed/demo-safe exchange adapters |
| `dashboard` | Backtest, settings, reports, and session monitoring |
| `observability` | Redaction, audit events, logs, and operator notifications |
| `verification` | Safe checks and guarded real-integration verification |

## Safety principles

- financial values use `Decimal`, not binary floating point
- external systems stay behind interfaces and adapters
- backtests never place real orders
- signed and real-integration tests require explicit environment gates
- live execution remains blocked by default
- uncertain reconciliation does not invent fills or profit/loss values
- one trade's reconciliation failure cannot block owned-fill processing for other trades
- protection and recovery logic fail closed when state cannot be verified
- runtime secrets come from the root `.env.local`

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env.local
git submodule update --init --recursive
```

Start the local stack:

```bash
docker compose up --build
```

The stack includes:

- MySQL
- Redis
- Ajil Unified AI Gateway
- Triak dashboard

A helper is available for clearing stale project resources before startup:

```bash
./scripts/stack_up.sh
```

## CLI examples

### Health and parsing

```bash
triak-trade health
triak-trade config-check
triak-trade db-check
triak-trade parse-message \
  "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000"
```

### AI and integrations

```bash
triak-trade ai-classify-dry-run "BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000"
triak-trade ai-gateway-check
triak-trade telegram-check
triak-trade market-data-dry-run BTCUSDT --interval 1m --minutes 5
triak-trade toobit-public-check
```

### Backtesting

```bash
triak-trade backtest-check
triak-trade backtest-run \
  --channel https://t.me/example_channel \
  --hours 24 \
  --interval 1m
triak-trade backtest-show-latest
```

### Dashboard and verification

```bash
triak-trade run-dashboard
triak-trade dashboard-smoke-test
triak-trade verify-system --mode safe --write-report
triak-trade show-last-report
```

## Backtesting workflow

The backtest pipeline:

1. reads historical Telegram messages
2. parses and classifies signal candidates
3. retrieves public historical market data
4. applies configurable entry, stop, take-profit, fee, and fill rules
5. simulates each signal without placing orders
6. writes JSON and Markdown reports
7. exposes progress and comparisons in the dashboard

Reports can include per-signal outcomes, period profit/loss buckets, strategy metadata, fee assumptions, and conservative/optimistic fill comparisons.

## Dashboard

The local dashboard provides:

- backtest launch and progress
- stored report inspection
- saved-channel management
- runtime settings
- demo/live session monitoring
- account and position visibility
- operator controls such as auto mode and kill switch

Dashboard authentication uses a token from `.env.local`. Do not expose it directly to the public internet.

## Guarded integrations

Real or signed integrations are opt-in. Examples include:

```env
RUN_AI_GATEWAY_INTEGRATION_TESTS=1
RUN_TELEGRAM_INTEGRATION_TESTS=1
RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1
RUN_TOOBIT_SIGNED_INTEGRATION_TESTS=1
RUN_SYSTEM_REAL_SMOKE_TESTS=1
LIVE_TRADING_LIVE_MODE_ENABLED=true
```

Enable only the minimum gate required for a controlled verification step. Order tests and live execution require additional configuration and must not be run against unintended accounts.

## Account coordination

The execution coordinator serializes exchange mutations for one account, tracks logical ownership of aggregate positions, deduplicates compatible signals, and preserves protection ownership across stops and take-profit orders.

Important operational boundaries:

- run only one executor process per exchange account
- unresolved exchange state blocks new entries
- open or pending trades prevent unsafe session shutdown
- replacement protection is submitted before existing protection is removed
- recovery uses signed trade history before reconciling snapshots
- funding remains an account-level ledger item rather than per-trade attribution

See:

- [Account execution coordination](docs/09-account-execution-coordination.md)
- [Live-trading operations](docs/10-live-trading-operations.md)

## Verification

```bash
ruff check .
mypy src
pytest
triak-trade verify-system --mode safe --write-report
```

When changing an integration boundary, also run the smallest safe dry-run or smoke command for that module and inspect the generated report.

## Configuration and secrets

The root `.env.local` is the only runtime secret source. Do not create or commit separate secret files inside the Ajil submodule.

Never commit:

- Telegram sessions
- exchange API credentials
- gateway tokens
- dashboard tokens
- private channel history
- generated reports containing sensitive account data

## Documentation

Start with [docs/README.md](docs/README.md). The documentation covers architecture, configuration, backtesting, exchange coordination, recovery, logging, and known operational limitations.

## Project status

Triak Trade demonstrates production-minded Python architecture, event parsing, AI-assisted classification, market-data integration, safe simulation, guarded external actions, dashboard development, observability, and failure-aware financial state management.
