# 06 - Trade Strategies

The backtesting strategy layer is intentionally stateless so the same trade-management logic can be reused in backtesting and future execution flows.

## Strategy Protocol

`TradeStrategy` defines three responsibilities:

1. Provide a synthetic stop when a signal has no stop-loss.
2. Provide fallback take-profit targets when a signal has no TP ladder.
3. Decide what to do when each target is hit.

That decision can include:
- what fraction of remaining position to close
- whether to move stop-loss to entry
- whether to move stop-loss to a new explicit level

## Default Strategy

`DefaultRiskManagedStrategy` currently provides:
- synthetic stop based on a capped percent-of-balance loss model
- optional breakeven after the first TP
- synthetic TP ladder from configured profit-percentage steps
- partial exits using configured close fractions

Its defaults are much more realistic than the older "extremely far synthetic stop" behavior described in stale earlier docs.

## Trailing Strategy

`TrailingTakeProfitStrategy` extends the default behavior by promoting stop-loss to the previous TP after later TP hits.

In practice:
- TP1 can move to breakeven
- TP2 can move stop to TP1
- TP3 can move stop to TP2

## Registry And Config

The strategy registry:
- loads strategy configuration from `config/strategies.yaml`
- falls back to built-in defaults if the file is missing or invalid
- logs warnings before fallback on config parsing failures
- exposes a small catalog for dashboard use

Available built-in keys:
- `default_risk_managed`
- `tp_trailing_risk_managed`

## Why This Layer Matters

This separation is one of the cleaner parts of the architecture:
- it keeps simulation rules testable
- it avoids burying TP/SL behavior inside the simulator
- it prepares the codebase for backtest/live rule reuse
