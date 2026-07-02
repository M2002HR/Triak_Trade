# 03 - Simulator Internals

`BacktestSimulator` is the deterministic trade-simulation core. It accepts normalized `BacktestEvent` inputs plus candle data and returns simulated trades, balance outcomes, and optionally snapshots.

## Main Responsibilities

- Find entries from message-derived trade intent
- Size positions
- Apply stop-loss and take-profit logic
- Handle manual close, cancel, and update events
- Track partial exits
- Model fees
- Produce consistent final trade records

## Entry Rules

Current entry behavior includes:
- `MARKET`: first candle open at or after the signal timestamp
- `LIMIT`: fill when the target price is touched
- `RANGE`: fill at the midpoint once the range is touched

Known caveat:
- `RANGE` entries still fill at midpoint, which can be mildly optimistic if only one edge was touched.

## Position Sizing

The simulator uses a leverage-aware allocation model. Important settings include:
- `BACKTEST_DEFAULT_RISK_PER_TRADE_PCT`
- `BACKTEST_MIN_ALLOCATION_PCT`
- `BACKTEST_MAX_ALLOCATION_PCT`
- `BACKTEST_MAX_EFFECTIVE_LEVERAGE`
- `BACKTEST_DEFAULT_SIGNAL_LEVERAGE`

This value is better thought of as a sizing factor than a literal direct balance percentage.

## Synthetic Protection

If a signal has no explicit stop-loss:
- The strategy can provide a synthetic stop.
- Otherwise a default stop percentage is used.

The current default strategy caps worst-case synthetic-stop loss as a percent of balance at entry, which is more realistic than the earlier extremely distant synthetic-stop approach.

## Take-Profit Handling

The simulator supports:
- explicit TP ladders from the signal
- synthetic fallback TP ladders from strategy rules
- partial exits per TP
- breakeven or trailing stop adjustment after TP hits

Conservative and optimistic fills differ mainly when the same candle can touch both SL and TP.

## Fees

Fees are now modeled through `BACKTEST_FEE_RATE_PCT`:
- entry fee is charged on entry notional
- exit fee is charged on each full or partial exit
- trade `pnl` is net of fees

This means balances, scores, and equity curves all reflect fee-aware net results.

## End-Of-Run Closing

Open positions can be closed at the end of the candle series with statuses such as:
- `open_until_end`
- `partial_tp_open_until_end`

This keeps reporting complete even when a signal never reaches a hard terminal condition during the available candle window.

## Current Risks

- The simulator still processes the entry candle itself for post-entry TP/SL evaluation, which can create same-candle exits.
- `RANGE` midpoint fills remain an approximation rather than a true path-aware fill model.
- The model does not yet include slippage or funding.
