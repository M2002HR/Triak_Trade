# 02 - Backtesting Architecture

> Last reviewed against the running stack: 2026-07-31.

## Public Execution Path

Triak_Trade exposes one backtesting path:

`BacktestRunner` uses guarded Telegram history, classifier selection, public market data,
per-signal simulation, tracing, and persisted reports. Internal engine components remain
injectable for deterministic unit tests, but they are not separate user-facing workflows.

## Core Backtesting Files

| File | Purpose |
|------|---------|
| `models.py` | Request and event models |
| `engine.py` | Orchestration for fixture/in-memory runs |
| `timeline.py` | Message-to-event transformation |
| `simulator.py` | Core trade simulation |
| `backtest_runner.py` | Public guarded backtest pipeline |
| `real_runner.py` | Internal shared history/classification infrastructure |
| `scoring.py` | Metrics and channel score calculation |
| `report.py` | JSON, Telegram-style, and Markdown summaries |
| `report_store.py` | Disk persistence for reports |
| `correlation.py` | Defensive follow-up-to-signal attachment |
| `directives.py` | Explicit text directive extraction |
| `strategies/` | Stateless trade-management rules |

## Internal Engine

`BacktestEngine`:
- Builds events from messages using `BacktestTimelineBuilder`
- Runs both conservative and optimistic simulations
- Chooses the primary trade set based on the requested fill policy
- Scores the run and builds a `BacktestReport`

One important improvement already present in the code:
- The report now uses the same trade set as the selected `fill_policy`, so `report.trades`, `final_balance`, and `total_pnl` stay consistent.

## `BacktestRunner`

`BacktestRunner` provides:
- readiness checks
- Telegram history collection
- AI or regex classifier selection
- message tracing
- the same signal-consolidation delay and pending-update merge used by live trading
- per-symbol market-data fetches
- simulator replay with live-progress snapshots
- disk report persistence
- optional Telegram log-channel summary hooks

This is the most complex part of the backtesting subsystem and also where most performance and readiness caveats live.

## Data Flow

High-level flow:

1. Fetch messages
2. Preprocess message text and media context
3. Classify message
4. Build or attach `BacktestEvent`
5. Merge updates received during the live consolidation window and move entry evaluation
   to the consolidation deadline
6. Normalize execution geometry through the validator shared with live trading
7. Prefetch required candles
8. Simulate positions
9. Score results
10. Write reports
11. Emit dashboard/log summaries

## Event Model

`BacktestEvent` is the simulator input unit. It captures:
- timestamp
- action
- signal id
- parsed signal
- related signal id
- source message metadata
- close fractions and move-to-entry directives when relevant

This separation is important because the simulator does not need Telegram or AI concepts directly. It only needs normalized events plus candles.

## Architectural Strengths

- Simulation logic is separate from Telegram/network code.
- Strategy logic is stateless and reusable across backtest and live/demo paths.
- Live and backtest share execution-side inference, geometry rejection, and pending-signal
  merge rules.
- Explicit stop-loss sizing and unsafe stop-update rejection use the same account-risk
  helpers as live trading.
- Correlation logic for follow-up messages is backtest and testable.
- Reports include explicit honesty flags such as whether AI or real market data were used.

## Architectural Caveats

- Historical OHLC candles cannot reproduce tick ordering, order-book depth, slippage,
  exchange rejection, or exchange-side partial fills exactly. Conservative fill policy
  is used when one candle touches conflicting levels.
- Per-signal simulation deliberately cannot reproduce shared-account effects exactly,
  including account position netting, duplicate blocking, global position limits,
  cross-signal margin contention, and stop cooldowns.
- `BacktestRunner.readiness()` still mixes runtime gating with test-style env guards.
- `readiness()` creates directories as a side effect.
- The live-simulation preview inside the real pipeline is throttled, not fully incremental.

## Dashboard Integration

The dashboard exposes only the unified Backtest workflow. Run creation, progress,
analysis, and report browsing all use the same `BacktestRunner` artifacts. The former
separate backtest/report surfaces and their duplicate frontend assets are no longer
public routes.

Backtest and Live Trade resolve channel choices through the same saved-channel service,
so a channel saved or removed in either tab is immediately visible to both.
