# Triak_Trade

> Last reviewed against the running stack: 2026-08-04.

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
- `src/triak_trade/backtesting`: unified Telegram backtest pipeline, deterministic test fixtures, simulator, scoring, and report storage.
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
- Backtesting is disabled until its guards are explicitly enabled.
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
triak-trade backtest-check
triak-trade backtest-run --channel https://t.me/Tofan_Trade --hours 24 --interval 1m
triak-trade backtest-tofan --hours 24
triak-trade backtest-show-latest
```

Backtest defaults track the live strategy, consolidation delay, leverage/allocation
limits, stop-risk caps, and fee rate. Toobit public klines are primary and Binance
public data is the default fallback. Open positions are not force-closed at the end of
the requested range unless that option is explicitly enabled.

Observability and dashboard:

```bash
triak-trade account-coordination-dry-run
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
- Backtest pipeline: `REAL_BACKTEST_ENABLED=true` plus the required real-integration guards above
- Telegram log-channel sending: `TELEGRAM_LOG_CHANNEL_ENABLED=true`, `PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=true`, and `RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=1`
- Verification real smoke checks: `RUN_SYSTEM_REAL_SMOKE_TESTS=1`
- Live session unlock: `LIVE_TRADING_LIVE_MODE_ENABLED=true`

## Backtesting Notes

- The single public pipeline is driven by `BacktestRunner`.
- Backtests read Telegram history, classify messages, fetch public market data, simulate each signal on its own capital base, and write JSON/Markdown reports to `runtime/reports/backtests`.
- The backtest dashboard now tracks live message progress and total elapsed runtime for each run.
- Stored reports now include richer comparison analytics such as period PnL buckets, per-signal rows, trade-outcome summaries, and strategy/risk metadata.
- The simulator supports conservative and optimistic fill policy comparisons.
- Fees are modeled via `BACKTEST_FEE_RATE_PCT`.
- Strategy loading comes from `config/strategies.yaml` with safe fallback defaults.

Known behavior worth keeping in mind:
- `backtest-check` currently creates the report/cache directories as a side effect.
- Backtest readiness currently requires multiple integration-style guard flags, not just one runtime flag.
- The live backtest dashboard still uses a throttled replay model rather than a fully incremental simulator.

## Dashboard And Live/Demo Workflows

- The dashboard is local-first and server-rendered with FastAPI, Jinja, and WebSockets.
- Dashboard auth uses `DASHBOARD_ADMIN_TOKEN` from the root `.env.local`.
- Backtest and Live Trade use one saved-channel library. Saving or removing a channel
  from either tab immediately changes the list returned to both workflows.
- Auto Mode and Kill Switch are persisted as runtime state, not a replacement for live-execution gating.
- Demo sessions use connected Toobit account state and demo/private symbol rules such as `TBV_...` depending on exchange support.
- Live sessions remain blocked unless `LIVE_TRADING_LIVE_MODE_ENABLED=true`.
- All dashboard sessions share one account execution coordinator. It serializes exchange mutations, nets opposite signals by default, deduplicates matching same-direction signals as consensus, and keeps distinct same-direction signals as separately owned logical legs in the aggregate exchange position.
- Every exchange-executed logical leg must have an owned quantity-scoped stop and all feasible take-profit orders. Protection setup and repair fail closed by flattening the affected logical quantity when protection cannot be verified.
- A live entry with distinct range endpoints splits the original risk-sized volume into 25% at the lower endpoint, 50% at the midpoint, and 25% at the upper endpoint. TP1 after a midpoint fill cancels the last leg; if only the first leg has filled, TP1 keeps both later orders and TP2 cancels them. Stop/manual exits still cancel all pending legs, and undersized positions fall back to one midpoint order without increasing volume.
- Protection replacement preserves the existing stop until the replacement submission succeeds. If repair and emergency flattening both fail, an exponential-backoff circuit breaker blocks new entries instead of continuously mutating the exchange.
- Exchange-position disappearance is confirmed across at least two snapshots and a 15-second grace window. Without complete owned close-fill evidence, the local trade remains unresolved, the session becomes critical, and new entries are blocked; the engine never invents a zero-PnL close.
- Filled close orders are recovered from signed user-trade history before position snapshots are reconciled. Exchange contract counts are converted to asset quantities, and delayed fills cannot reduce logical quantity twice.
- A session with open or pending trades cannot be manually stopped. Unexpected worker exits are retried under recovery supervision, and inactive sessions with unresolved trades are marked critical and blocked from reuse.
- Full-position closes first release unowned Triak take-profit reservations from already-closed trades, then reconcile the remaining exchange quantity. Manual/non-Triak orders and protection owned by another active leg are left untouched.
- `risk_per_trade_pct=120` is a legacy API name for an allocation factor. At `10x` leverage it starts from `12%` margin allocation before min/max allocation and stop-risk caps; it does not permit a 120% account loss.
- Live trade PnL currently tracks exchange fills and trading commissions but does not attribute futures funding flows to individual logical trades. Funding remains a separate account-ledger item and must be included when reconciling net account performance.
- Dashboard file logs rotate at UTC midnight and retain seven daily backups by default. High-frequency Telegram polling heartbeats are DEBUG-only; financial and failure events remain visible at INFO or above.
- Run only one dashboard executor process per Toobit account; coordination is process-wide, not a distributed lock across multiple replicas.

The full policy and recovery model is documented in
[docs/09-account-execution-coordination.md](docs/09-account-execution-coordination.md).
Operational state, recovery, logging, and funding boundaries are documented in
[docs/10-live-trading-operations.md](docs/10-live-trading-operations.md).

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
