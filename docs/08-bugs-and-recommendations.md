# 08 - Bugs And Recommendations

This file captures current repository-level issues and follow-up work that remain relevant after the documentation cleanup.

## High-Priority Risks

### B1 - Real backtest readiness still behaves like a test harness

Location: `src/triak_trade/backtesting/real_runner.py`

Current behavior:
- requires `REAL_BACKTEST_ENABLED=true`
- also requires multiple integration-test style guard flags
- creates directories during `readiness()`

Recommendation:
- split runtime enablement from test enablement
- keep readiness side-effect free

### B2 - AI failure honesty can still be improved

Location: `src/triak_trade/backtesting/real_runner.py`

Current behavior:
- per-message AI failures are tolerated so runs stay resilient
- that is good for robustness
- but a high enough failure ratio can still look like a structurally successful run with weak practical value

Recommendation:
- escalate to an explicit failure or strong warning threshold when AI degradation is systemic

### B3 - Live preview simulation is improved but still replay-based

Location: `src/triak_trade/backtesting/real_runner.py`

Current behavior:
- throttled updates via `REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N`
- much better than replaying on every passive message
- still not a truly incremental simulation engine

Recommendation:
- move toward incremental position/state updates for dashboard previews

## Medium-Priority Risks

### M1 - Range-entry midpoint fill is still an approximation

Location: `src/triak_trade/backtesting/simulator.py`

Recommendation:
- replace midpoint filling with path-aware or edge-aware fill logic, or document it as an optimistic simplification

### M2 - Entry candle can still trigger same-candle exit logic

Location: `src/triak_trade/backtesting/simulator.py`

Recommendation:
- decide explicitly whether post-entry TP/SL evaluation should begin on the next candle for some entry modes

### M3 - Candle cache datetime normalization mixes naive and aware values

Location: `src/triak_trade/market_data/candle_cache.py`

Recommendation:
- normalize everything to aware UTC values internally

## Lower-Priority Cleanup

### L1 - Reduce drift between docs, `.env.example`, and CLI

This review already fixes the current known drift, but the project should keep guardrails in place so future documentation does not quietly go stale again.

Implemented in this review:
- repository docs rewritten in English
- README aligned with the actual CLI surface
- `.env.example` updated toward current settings
- tests added to catch basic CLI/config documentation drift

### L2 - Keep submodule docs separate from repo-owned docs

The Ajil gateway submodule has its own documentation lifecycle. Triak_Trade docs should describe how the repo uses the submodule, not attempt to become the upstream docs for it.
