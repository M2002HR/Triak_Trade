# 09 - Account Execution Coordination

> Last reviewed against the running stack: 2026-08-04.

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

The legacy field name `risk_per_trade_pct` is an allocation factor. Its raw allocation
is `factor / leverage`, bounded by configured min/max allocation, and explicit stop risk
is then capped separately. The default `120` therefore means `12%` starting margin at
`10x`, not a permitted 120% balance loss.

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

Protection replacement is ordered to avoid an unprotected window:

1. normalize and preflight feasible take-profit quantities
2. discover the trade's currently owned protection orders
3. retain the existing stop while take-profit orders are replaced
4. submit and verify a replacement stop before canceling its owned predecessor
5. verify the final stop and take-profit set against the remaining quantity

A full-close flow may cancel owned take-profit reservations, but keeps the owned stop
until the exchange confirms closure. If both protection repair and emergency flattening
fail, the session enters a bounded exponential protection circuit, emits a critical
health state, and blocks new entries until recovery succeeds.

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
the missing-position decision. Even after the missing-snapshot threshold is reached,
the engine does not invent a zero-PnL local close: it requires complete, owned user-fill
evidence for the logical remainder. If that evidence is unavailable, the trade stays
unresolved, the session becomes critical, and new entries remain blocked.

Exchange fills are applied before aggregate position snapshots. Contract quantities are
converted to asset quantities with the symbol contract multiplier, and each pending fill
records its expected snapshot effect. This prevents a delayed aggregate snapshot from
subtracting the same fill a second time.

## Same-Side Physical Position Accounting

Several logical legs may share one same-direction exchange position. A full close of
one logical leg sends only that leg's remaining quantity. The residual-close loop that
flattens an entire physical bucket is disabled while another logical leg owns quantity
in that bucket.

Aggregate position margin and PnL snapshots are allocated proportionally to logical
remaining quantities for session display. Durable order/fill ownership is restored from
all stored sessions before recovered workers start.

A stopped session with unresolved open exposure is excluded from normal same-side
quantity allocation, but it is not treated as harmless or complete. It remains visible
as a critical ownership conflict and blocks unsafe mutations until its worker is
recovered or the exchange state is conclusively reconciled.

When one logical leg owns the entire physical bucket, a full close first discovers
unowned Triak TP orders from already-closed trades. Those orphan reservations are
canceled before the market close, after which the engine re-reads the exchange position
and retries residual quantity up to the configured reconciliation limit.

## Session Lifecycle And Recovery

Stopping a session is a convergence operation, not merely canceling a worker task. New
entries are blocked first, pending mutations are allowed to settle, open logical trades
are reconciled, and only a clean session becomes inactive. A session with unresolved
exposure remains in a recovery state and is retried every
`LIVE_TRADING_ENGINE_RECOVERY_RETRY_SECONDS` by default.

On process startup, durable sessions and ownership ledgers are loaded before automatic
workers resume. This allows fills, protection orders, and pending entries created before
a restart to retain their logical owners.

## PnL, Commission, And Funding Boundary

`LiveTrade.fees` contains trading commission reconstructed from owned exchange fills.
Realized and unrealized PnL are likewise logical-trade values. Futures funding is not an
order fill and is not included in either field today. Toobit posts it as account balance
flow type `32` (`FUNDING_SETTLEMENT`).

Account equity reconciliation must therefore treat these as separate components:

```text
net account effect = realized PnL + unrealized PnL - trading commission + funding flow
```

Funding attribution is intentionally not guessed when multiple logical legs share the
same physical symbol/side position. Until the funding-flow adapter and deterministic
allocation ledger are implemented, the signed exchange balance ledger is authoritative.

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
LIVE_TRADING_PROTECTION_CIRCUIT_BASE_SECONDS=60
LIVE_TRADING_PROTECTION_CIRCUIT_MAX_SECONDS=1800
LIVE_TRADING_EXCHANGE_POSITION_MISS_CONFIRMATIONS=2
LIVE_TRADING_EXCHANGE_POSITION_MISS_GRACE_SECONDS=15
LIVE_TRADING_ENGINE_RECOVERY_RETRY_SECONDS=30
```

## Deployment Boundary

The guard and registry coordinate threads and event loops inside one dashboard process.
They are not a distributed lock. Do not run multiple active dashboard executor replicas
against the same Toobit account. A future multi-replica deployment must move the
single-writer lease and ownership ledger to a transactional external service.
