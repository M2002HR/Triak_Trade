# 02 - Backtesting Architecture

## Two Execution Paths

Triak_Trade has two backtesting paths that share the same simulator core:

1. `BacktestEngine`
   Uses deterministic fixture-style inputs or injected messages/candles. This is the safer path for tests and local development.

2. `RealBacktestRunner`
   Uses guarded real Telegram history, classifier selection, public market data, simulation, tracing, and persisted reports.

Both paths ultimately rely on `BacktestSimulator` for trade simulation.

## Core Backtesting Files

| File | Purpose |
|------|---------|
| `models.py` | Request and event models |
| `engine.py` | Orchestration for fixture/in-memory runs |
| `timeline.py` | Message-to-event transformation |
| `simulator.py` | Core trade simulation |
| `real_runner.py` | Guarded real backtest pipeline |
| `scoring.py` | Metrics and channel score calculation |
| `report.py` | JSON, Telegram-style, and Markdown summaries |
| `report_store.py` | Disk persistence for reports |
| `correlation.py` | Defensive follow-up-to-signal attachment |
| `directives.py` | Explicit text directive extraction |
| `strategies/` | Stateless trade-management rules |

## `BacktestEngine`

`BacktestEngine`:
- Builds events from messages using `BacktestTimelineBuilder`
- Runs both conservative and optimistic simulations
- Chooses the primary trade set based on the requested fill policy
- Scores the run and builds a `BacktestReport`

One important improvement already present in the code:
- The report now uses the same trade set as the selected `fill_policy`, so `report.trades`, `final_balance`, and `total_pnl` stay consistent.

## `RealBacktestRunner`

`RealBacktestRunner` adds:
- readiness checks
- Telegram history collection
- AI or regex classifier selection
- message tracing
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
5. Prefetch required candles
6. Simulate positions
7. Score results
8. Write reports
9. Emit dashboard/log summaries

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
- Correlation logic for follow-up messages is isolated and testable.
- Reports include explicit honesty flags such as whether AI or real market data were used.

## Architectural Caveats

- `RealBacktestRunner.readiness()` still mixes runtime gating with test-style env guards.
- `readiness()` creates directories as a side effect.
- The live-simulation preview inside the real pipeline is throttled, not fully incremental.
