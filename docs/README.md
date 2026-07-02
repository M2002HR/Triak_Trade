# Triak_Trade Documentation

This directory contains repository-owned technical documentation for Triak_Trade, with extra depth around backtesting, reporting, and runtime controls.

Scope:
- These files document the code that lives in this repository.
- The Ajil gateway submodule keeps its own upstream documentation under `external/Ajil_Unified_AI_Gateway/`.

## Reading Order

| File | Focus |
|------|-------|
| [01-overview.md](01-overview.md) | Product shape, module map, and user-facing capabilities |
| [02-backtesting-architecture.md](02-backtesting-architecture.md) | Backtesting architecture and execution paths |
| [03-simulator-internals.md](03-simulator-internals.md) | Simulator behavior, fills, sizing, and PnL rules |
| [04-real-backtest-pipeline.md](04-real-backtest-pipeline.md) | Guarded Telegram + AI + market-data pipeline |
| [05-scoring-and-reports.md](05-scoring-and-reports.md) | Metrics, scoring, summaries, and report artifacts |
| [06-strategies.md](06-strategies.md) | Stateless trade-management strategies |
| [07-data-and-config.md](07-data-and-config.md) | Market data, settings, and safety gates |
| [08-bugs-and-recommendations.md](08-bugs-and-recommendations.md) | Current risks, behavior gaps, and follow-up work |

## Current Intent

Triak_Trade is a modular platform for:
- Telegram signal parsing and classification
- Backtesting and report generation
- Demo/live session monitoring through the dashboard
- Safe operator observability and verification

Key architectural rules reflected in the docs:
- `Decimal` for financial logic
- Adapters/interfaces around external services
- Guard-gated real integrations
- Simulation-only backtesting
- Root `.env.local` as the single runtime configuration source

## Notes About Accuracy

- These docs were updated against the current repository state on `2026-07-02`.
- They intentionally prefer present behavior over aspirational behavior.
- Known mismatches and open risks are tracked explicitly in [08-bugs-and-recommendations.md](08-bugs-and-recommendations.md).
