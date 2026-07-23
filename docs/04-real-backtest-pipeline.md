# 04 - Real Backtest Pipeline

`RealBacktestRunner` is the guarded path that turns real Telegram history into simulated trading results and stored reports.

## Injected Dependencies

The runner is designed so unit tests can replace external components with fakes:
- Telegram client
- market-data provider
- report store
- log-channel client
- strategy
- validator

That keeps the real pipeline testable without network access.

## Readiness Model

Before a run starts, `readiness()` checks:
- `REAL_BACKTEST_ENABLED`
- `RUN_BACKTEST_INTEGRATION_TESTS`
- `RUN_TELEGRAM_INTEGRATION_TESTS`
- `RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS`
- Telegram credentials
- Telegram session configuration
- historical market-data settings

Current caveats:
- the method still uses integration-test style env guards as runtime gates
- it creates directories as a side effect

## Main Run Phases

High-level `run()` flow:

1. Check readiness
2. Select classifier
3. Fetch Telegram history
4. Build events and message traces
5. Collect relevant symbols
6. Fetch and reuse market data
7. Simulate
8. Enrich traces with simulation outcomes
9. Write JSON and Markdown reports
10. Optionally send summary output to the log channel

## Classifier Selection

The runner chooses between:
- `AIMessageClassifier` when AI is enabled and available
- `RegexMessageClassifier` otherwise

If a run explicitly requires AI but the gateway is disabled, the run fails rather than silently pretending AI was used.

## Message Processing Behavior

The runner does more than simple one-message parsing:
- handles text/media preprocessing
- tolerates classifier exceptions per message instead of crashing the whole run
- reroutes certain OPEN-like outputs into follow-up actions when context makes that safer
- attaches follow-up directives using correlation rules and reply chains
- can promote a reply parent into an OPEN signal when the follow-up reveals the parent was the real originating signal

## Live Progress Simulation

For dashboard progress, the runner rebuilds live simulation state during message processing.

This has already been improved:
- full replay is throttled with `REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N`
- signal-bearing messages still force an update
- interval snapshots are emitted incrementally instead of fully replayed each time
- run-level elapsed runtime is emitted on progress events and persisted to the final report
- dashboard runs persist `started_at`, `finished_at`, and `runtime_duration_ms`

Even so, this remains one of the more performance-sensitive areas of the codebase.

## Candle Window Guard

`REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL` no longer truncates every symbol from the global
backtest start date forward.

Current behavior:
- the fetch window is aligned around the actual signal time
- cached candle ranges extend only when a later signal needs more history/future coverage
- long backtests therefore keep later signals fillable without forcing unbounded symbol caches

Isolated runs add a stricter safety boundary because they can fetch many independent 1m
series in one dashboard worker. `ISOLATED_BACKTEST_MAX_CANDLES_PER_SYMBOL` defaults to
10,000 and `ISOLATED_BACKTEST_MARKET_DATA_MAX_CONCURRENCY` defaults to one. An oversized
window is explicitly skipped (never silently truncated or included in aggregate rankings),
and `ISOLATED_BACKTEST_MARKET_DATA_TIMEOUT_SECONDS` is an end-to-end deadline covering
archive reads, fallback, and parsing for one symbol.

## Outputs

Successful runs can produce:
- `RealBacktestResult`
- JSON report under `runtime/reports/backtests`
- Markdown summary under the same directory
- dashboard-facing progress events
- optional Telegram log-channel summary

The output explicitly carries honesty flags such as:
- `real_telegram_used`
- `real_market_data_used`
- `ai_used`
- `regex_fallback_used`

## Important Known Risk

If the AI gateway fails per-message often enough, the current behavior can still degrade into many ignored/failed messages while the run itself may look structurally successful. That is an honesty and usability edge worth tightening further.
