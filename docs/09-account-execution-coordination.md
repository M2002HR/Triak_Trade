# 09 - Account Execution Coordination

> Last reviewed against the running stack: 2026-07-31.

## Why This Layer Exists

Telegram channels produce independent logical trades, while Toobit exposes physical
position buckets for an account, symbol, and direction. Treating every channel session
as if it owned the whole exchange position can make one session cancel another
session's stop, consume its fill, or flatten its remaining quantity.

`AccountExecutionCoordinator` is the process-wide authority between those scopes. The
dashboard creates one coordinator and passes the same instance to every live/demo
session engine.

## Default Decision Policy

The defaults are intentionally conservative:

| Situation | Default behavior |
|-----------|------------------|
| Matching symbol, direction, entry, and stop inside the duplicate window | Record the later signal as consensus; do not add exposure |
| Same symbol and direction but materially different setup | Keep a separate logical leg and aggregate it into the physical exchange bucket |
| Opposite direction | Close opposite logical legs FIFO, then open only any residual requested quantity |
| Opposite pending entry | Block the new entry until the pending order is explicitly resolved |
| Missing owner engine or conflicting durable ownership | Fail closed and reject the new mutation |

`LIVE_TRADING_ACCOUNT_POSITION_POLICY` can be `net`, `hedge`, or `block`.
`LIVE_TRADING_SAME_DIRECTION_POLICY` can be `aggregate` or `block`.

The recommended production combination is `net` plus `aggregate`.

## Risk And Quantity

The coordinator applies an account-wide stop-risk cap per symbol after the normal
per-trade position manager and exchange risk-tier checks. Existing same-direction
logical legs consume the symbol risk budget. A new leg is resized only when necessary;
its leverage is not increased to manufacture risk capacity.

Take-profit feasibility is then recalculated from the final approved quantity and the
contract multiplier, minimum quantity, and step size. The planner creates the maximum
number of feasible targets without increasing the original position volume. If all
announced targets cannot receive valid exchange quantities, it places the largest valid
subset and preserves the target plan in the trade audit notes.

## Single Writer And Ownership

All exchange open, close, protection-sync, and account-reconciliation mutations pass
through one cross-thread asynchronous guard. This prevents two channel workers from
making contradictory exchange decisions at the same time.

Ownership is tracked for:

- entry order IDs
- take-profit order IDs
- v2 stop order IDs and client IDs
- close order IDs
- exchange fill IDs

Client IDs include the logical `trade_id`. Fill reconciliation first checks order
ownership and then claims the fill globally, so one fill cannot be applied to two
sessions.

## Stop-Loss And Take-Profit Safety

Production uses quantity-scoped Toobit v2 `STOP_MARKET` orders with
`triak_sl_<trade_id>_...` client IDs. Stop cancellation and discovery match only the
logical trade's exact ID or prefix. The engine never scans and cancels every stop for a
symbol in this mode.

Take-profit orders already use `triak_tp_<trade_id>_...` IDs and are treated the same
way. Every open trade must have:

- one verified owned stop-loss
- at least one take-profit
- every take-profit order that is feasible for its final contract quantity

If protection cannot be created or repaired after the configured retries, the engine
attempts to flatten only that logical leg and reports the failure. It does not leave the
leg intentionally unprotected.

Toobit's open-algo listing can temporarily omit an owned v2 stop even while a direct
order lookup reports `ORDER_NEW`. Protection verification therefore retains the tracked
order/client IDs and performs a direct algo-order lookup before declaring the stop
missing.

## Position Snapshot Convergence

Order history and aggregate position snapshots do not always converge at the same
instant. The first missing position response is recorded as
`exchange_position_missing_snapshot: pending_confirmation` with confirmation count and
elapsed time. By default, closure requires two missing observations and at least 15
seconds. If the position reappears, the pending state is cleared and a structured
`live_trading.exchange_position_missing_recovered` event is emitted.

This guard prevents the engine from abandoning a newly filled position during a normal
exchange API propagation delay. Confirmed protection fills are still reconciled before
the missing-position decision.

## Same-Side Physical Position Accounting

Several logical legs may share one same-direction exchange position. A full close of
one logical leg sends only that leg's remaining quantity. The residual-close loop that
flattens an entire physical bucket is disabled while another logical leg owns quantity
in that bucket.

Aggregate position margin and PnL snapshots are allocated proportionally to logical
remaining quantities for session display. Durable order/fill ownership is restored from
all stored sessions before recovered workers start.

When one logical leg owns the entire physical bucket, a full close first discovers
unowned Triak TP orders from already-closed trades. Those orphan reservations are
canceled before the market close, after which the engine re-reads the exchange position
and retries residual quantity up to the configured reconciliation limit.

## Operations

Run the safe interface check with no Telegram or exchange I/O:

```bash
triak-trade account-coordination-dry-run
```

The output verifies:

- duplicate consensus detection
- exact opposite-direction netting
- distinct same-direction aggregation
- the remaining logical quantity
- fail-closed behavior when the owner engine is unavailable

Relevant configuration:

```dotenv
LIVE_TRADING_ACCOUNT_POSITION_POLICY=net
LIVE_TRADING_SAME_DIRECTION_POLICY=aggregate
LIVE_TRADING_SYMBOL_RISK_CAP_PCT_OF_BALANCE=5
LIVE_TRADING_DUPLICATE_SIGNAL_WINDOW_SECONDS=300
LIVE_TRADING_DUPLICATE_PRICE_TOLERANCE_BPS=20
LIVE_TRADING_USE_OWNED_V2_STOP_ORDERS=true
```

## Deployment Boundary

The guard and registry coordinate threads and event loops inside one dashboard process.
They are not a distributed lock. Do not run multiple active dashboard executor replicas
against the same Toobit account. A future multi-replica deployment must move the
single-writer lease and ownership ledger to a transactional external service.
