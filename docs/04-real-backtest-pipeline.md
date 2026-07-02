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

Even so, this remains one of the more performance-sensitive areas of the codebase.

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
