# 05 - Scoring And Reports

## Metrics

`ChannelScorer` builds channel metrics from events, trades, and total PnL.

Important current rules:
- `parsed_signals` counts OPEN events
- `filled_trades` excludes `not_filled`
- `wins` means filled trades with `pnl > 0`
- `win_rate` is computed over filled trades only
- `profit_factor` is based on gross wins vs gross losses
- `expectancy` uses total PnL over total trades
- `max_drawdown` is computed from realized trade progression

Two meaningful fixes are already in place:
- win-rate denominator no longer includes `not_filled`
- drawdown/equity progression now sorts trades by `exit_time`

## Score Composition

The final score is a weighted combination of:
- profitability
- win rate
- profit factor
- drawdown control
- fill rate
- consistency between conservative and optimistic outcomes
- sample confidence

This is intentionally multi-factor so a channel cannot score well only by having a few profitable trades with poor risk behavior.

## Report Formatting

`report.py` provides:
- `report_to_json`
- `report_to_telegram_summary`
- `report_to_markdown_summary`
- `extract_channel_score`

JSON reports also include:
- score breakdown
- trade status counts
- per-symbol summary
- equity curve

## Report Persistence

`BacktestReportStore` writes real-run artifacts under `runtime/reports/backtests`.

Each stored run typically produces:
- a JSON report file
- a Markdown report file

These artifacts are what `backtest-show-latest` and the dashboard report views surface.

## Follow-Up Correlation

Backtest reporting quality depends on correct follow-up attachment. `correlation.py` provides a defensive resolver that tries, in order:
- valid AI related-signal id
- reply-chain ownership
- symbol matching
- single-open-signal fallback
- last-resort attach when explicitly enabled

That logic is intentionally separate from AI output so the system can distrust and correct ambiguous model responses.

## Current Limit

Drawdown and equity are still realized-trade based. They do not model intra-trade unrealized equity swings.
