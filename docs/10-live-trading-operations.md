# 10 - Live Trading Operations

> Last reviewed against the running stack: 2026-08-04.

## Scope

This document describes the production behavior that must remain true across signal
classification, order entry, protection, exchange reconciliation, process restarts,
logging, and account-level PnL review. The exchange is authoritative for physical
positions and account balance; Triak's durable store is authoritative for logical-trade
ownership and intent.

## Session Lifecycle

A live session moves through durable and runtime states:

1. configuration is validated and the channel is unlocked for live execution
2. ownership ledgers are restored before its worker starts
3. the worker classifies messages and submits only approved entry mutations
4. every open or pending trade is continuously reconciled with owned orders and fills
5. stop requests block new entries before worker shutdown begins
6. a session becomes inactive only after pending mutations and exposure converge

If a worker exits unexpectedly while open or pending exposure remains, the dashboard
does not silently leave the session inactive. It reports a critical recovery state and
retries worker recovery using `LIVE_TRADING_ENGINE_RECOVERY_RETRY_SECONDS`.

For explicitly recovered inactive exposure, `recovery_only` is a durable fail-closed
mode. It enables exchange/account refresh, owned-fill reconciliation, and protection
repair. It disables Telegram history bootstrap and polling, consolidation, and the
pending-entry submission worker, so recovery cannot create a new trade from a channel
message or an old unfilled entry intent.

## Entry And Quantity Planning

`risk_per_trade_pct` is a legacy field name. It is an allocation factor, not the
percentage of balance that may be lost:

```text
raw margin allocation % = allocation factor / leverage
approved margin allocation % = clamp(raw, configured minimum, configured maximum)
```

The default factor `120` gives `12%` raw margin allocation at `10x` and `8%` at `15x`.
Explicit and synthetic stop risk is then capped independently, and the account
coordinator also enforces its per-symbol stop-risk budget. All money and quantity math
uses `Decimal`.

For a true entry range with distinct endpoints, the risk-sized total quantity is placed
as three simultaneous limit legs: 25% at the lower endpoint, 50% at the midpoint, and
25% at the upper endpoint. Contract-step rounding is absorbed by the midpoint leg, so
the submitted total never exceeds the quantity that the earlier single midpoint order
would have used. If the exchange minimum quantity cannot support three valid legs, the
engine falls back to one midpoint order and never increases exposure to force the split.

Each range leg has a durable ownership and fill ledger. The first confirmed partial fill
activates stop/target protection; later fills update cumulative quantity, fees, and the
weighted entry price exactly once, then resize protection without resetting target
progress or moving an already-trailed stop backward.

Pending range legs follow a target-aware retirement policy:

1. After only the 25% lower-end leg has filled, TP1 keeps the midpoint and upper-end
   orders active.
2. TP2 after that first-leg fill cancels both the 50% midpoint remainder and the 25%
   upper-end remainder.
3. Once any midpoint quantity has filled, TP1 cancels the 25% upper-end remainder.
4. If TP1 happened before the midpoint fill, a later midpoint fill immediately applies
   rule 3 and cancels the upper-end order.

Every policy cancellation confirms exchange state and reconciles a possible cancel/fill
race. Stop-loss, manual close, entry replacement, expiry, and non-target exits still
cancel every outstanding entry leg before continuing. Unfilled active legs remain
reserved in account-level risk calculations, and opposite-side netting is blocked until
those pending orders are cancelled and reconciled.

For all limit entries, protection is sized only from confirmed filled quantity. The
original signal targets are retained, but the planner submits only exchange-valid
quantities that respect contract multiplier, minimum quantity, step size, and the total
remaining position.

## Stop And Target Protection

Each logical live trade owns its orders through deterministic client-ID prefixes:

- `triak_tp_<trade_id>_...` for take profits
- `triak_sl_<trade_id>_...` for v2 stop-market orders

The engine never cancels all symbol-level stops. Replacement keeps the existing stop
active while take profits are rebuilt, submits and verifies the replacement stop, then
cancels only the owned predecessor. Full-close handling may release owned take-profit
reservations, but retains the stop until closure is confirmed.

After configured retries, failed protection repair triggers a logical-leg emergency
flatten. If flattening also fails, a bounded exponential circuit blocks new entries and
keeps session health critical. This condition requires operational investigation; it is
not downgraded to a warning merely because the worker loop is still alive.

## Exchange Reconciliation

Reconciliation is ordered to prevent stale snapshots and duplicate accounting:

1. load owned order history and user fills
2. claim each exchange fill ID globally and at most once
3. convert contract counts to asset quantities using the symbol multiplier
4. apply owned fills to logical trades
5. read aggregate physical position snapshots
6. consume pending snapshot effects in every fill path so neither direct nor delayed
   fills can reapply quantity
7. verify owned stop and take-profit coverage for the logical remainder

Protection reconciliation is isolated per logical trade. A contract-spec, order-query,
fill-conversion, or protection-repair failure for one symbol is persisted on that trade
and emitted as `live_trading.exchange_trade_reconciliation_failed_isolated`; it does not
abort reconciliation for later trades in the same account snapshot. This guarantees
that an unrelated stale symbol cannot prevent a confirmed TP fill from advancing
`targets_hit`, reconciling remaining quantity, and applying the strategy stop update.
The failed trade remains visible for retry and investigation rather than being silently
treated as synchronized.

A missing aggregate position snapshot is provisional for two observations and at least
15 seconds by default. Reappearance clears the pending state. Reaching the threshold
still does not authorize a fabricated local close: complete owned close-fill evidence is
required. Without it, the logical trade remains open and unresolved, session health is
critical, and further entries are blocked.

Multiple same-direction logical legs may share one physical position. Each leg closes
only its owned remainder, and margin/unrealized PnL display values are allocated by
remaining quantity. Opposite-direction requests follow the configured account policy
and default to FIFO netting before any residual entry is opened.

## PnL, Trading Fees, And Funding

Triak currently separates two accounting domains:

| Component | Current source | Included in logical trade |
|-----------|----------------|---------------------------|
| Realized PnL | owned exchange fills | yes |
| Unrealized PnL | exchange position snapshot, allocated by quantity | yes |
| Trading commission | owned exchange fills | yes, in `LiveTrade.fees` |
| Futures funding | signed account balance flow type `32` | no |

Funding is charged on physical position notional at settlement, not on posted margin.
Its sign depends on the funding rate and side: at a negative rate, shorts pay longs.
Because multiple logical sessions can share one physical position, funding must stay at
account scope unless a deterministic time-weighted allocation can be proven from the
ownership ledger. Reports must never label funding as trading commission or silently
include it in logical PnL.

The production account reconciliation identity is:

```text
net account effect = realized PnL + unrealized PnL - trading commission + signed funding
```

## Health And Entry Blocking

Operational health is derived from exposure safety, not only worker liveness. New live
entries are blocked when any of these conditions is unresolved:

- missing or unverifiable owned protection
- protection repair/flatten circuit is open
- confirmed position absence lacks complete owned close fills
- durable ownership conflicts with another session
- a stopped worker still owns open or pending exposure
- required AI classification or exchange services are unavailable

Critical health events are structured and include stable session/trade/order identifiers
without credentials or raw authorization data.

## Log Retention

Dashboard file logs use structured JSON, rotate at UTC midnight, and retain seven dated
backups by default. `DASHBOARD_LOG_RETENTION_DAYS` has a minimum of three. The default
file level is `INFO`: order, fill, protection, PnL, lifecycle, recovery, and failure
events remain visible, while high-frequency polling heartbeats require `DEBUG`.

`DASHBOARD_LOG_MAX_BYTES` and `DASHBOARD_LOG_BACKUP_COUNT` are legacy compatibility
settings and do not control the current time-based file handler. Secrets are redacted
before serialization.

## Operator Verification

Safe checks that do not submit private exchange mutations:

```bash
triak-trade account-coordination-dry-run
triak-trade tp-capacity-dry-run
```

Before production completion, run:

```bash
ruff check .
mypy src
pytest
docker compose build
docker compose up -d
docker compose ps
```

Then inspect dashboard health and recent structured logs. Real integration checks must
remain behind their explicit environment guards and use credentials only from the root
`.env.local`.
