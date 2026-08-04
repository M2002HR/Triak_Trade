# 08 - Bugs And Recommendations

> Last reviewed against the running stack: 2026-08-04.

This file captures current repository-level issues and follow-up work that remain relevant after the documentation cleanup.

## Recently Resolved Live-Safety Incidents

### R1 - Missing exchange positions no longer create synthetic zero-PnL closes

A filled position could appear in order history before Toobit's aggregate positions
endpoint converged. The sync loop used to close local state without a complete owned
close-fill trail. Missing positions now require the configured confirmation count plus
grace time, and still remain unresolved unless owned user-trade fills can account for
the close. The session becomes critical and new entries are blocked instead of inventing
PnL. Recovery is logged when the exchange position reappears.

### R2 - Full close releases orphan Triak TP reservations

A closed trade's stale TP orders could reserve exchange quantity and make a later
full-position market close fill only partially. Exclusive-bucket closes now cancel only
unowned `triak_tp_...` close-limit reservations before closing and reconciling the real
remaining position. Manual orders and orders owned by active logical legs are excluded.

### R3 - Historical TP fills are reconciled before quantity and protection

The exchange can fill a TP while local polling misses the transition. Sync now reads up
to 100 history orders and user trades, reconstructs owned TP/SL identity from client
IDs, claims fills account-wide, and applies quantity, realized PnL, commission, and
`targets_hit` idempotently before protection is evaluated.

### R4 - Contract quantities are normalized to asset quantities

Toobit position snapshots expose contract counts. The live snapshot now applies the
contract multiplier before comparing a physical position with a logical trade. A
smaller authoritative exchange allocation is tracked with
`pending_fill_audit_quantity`; later-arriving fills book PnL and fees without reducing
quantity twice.

### R5 - Protection mutation preserves the existing stop

TP feasibility is checked before destructive mutations. Stop replacement submits the
new stop before releasing an owned old stop. Full closes remove TP reservations first
but retain the stop until the close is confirmed. Repair and flatten failures open an
exponential-backoff circuit, mark the session critical, and block entries.

### R6 - Session lifecycle no longer abandons open trades silently

Manual stop is rejected while open or pending trades exist. An engine worker that exits
with open trades is rebuilt after a bounded delay under recovery supervision. On
startup, inactive sessions with unresolved trades are persisted as critical and their
account buckets remain blocked.

### R7 - Durable ownership and attribution are account-wide and idempotent

Fill ownership is global across sessions, duplicate Telegram message attribution is
merged, pending-entry timestamps are updated on real state transitions, and unchanged
two-second pending polls no longer rewrite durable state.

### R8 - Operational logs retain daily history without poll noise

The dashboard handler rotates at UTC midnight and retains seven days by default, with a
validated minimum of three. Telegram history poll start/completion heartbeats are DEBUG
events, while financial mutations and failures remain at INFO or above.

### R9 - Snapshot clamps and direct protection fills share one audit ledger

The BICOUSDT incident exposed two independent fill consumers. The position snapshot
first reduced the logical remainder from `1966` to `767` and recorded `1199` units in
`pending_fill_audit_quantity`. The direct protection-fill path then subtracted the same
two TP fills (`688 + 511`) again, falsely closed the logical trade, and removed its
protection while `767` units remained short on the exchange.

Both direct protection reconciliation and delayed history reconciliation now consume
the same pending audit ledger. Quantity is reduced at most once, while realized PnL,
commission, processed fill IDs, `targets_hit`, and the trailing stop still advance from
the authoritative fills. A regression test reproduces the exact BICO quantities.

### R10 - Inactive exposure can run under entry-disabled recovery

An inactive session with physical exposure can be resumed with `recovery_only=true`.
This mode restores durable ownership and runs account, price, fill, and protection
reconciliation, but does not bootstrap or poll Telegram, submit pending entries, or run
signal consolidation. It exits only after its unresolved logical trades converge.

## High-Priority Risks

### B0 - Live funding is not attributed to logical trades

Location: `src/triak_trade/live_trading/models.py` and Toobit account-ledger adapter

Current behavior:
- `LiveTrade.fees` contains entry/exit trading commission recovered from order fills
- Toobit funding is posted separately as balance flow type `32`
- live trade cards and aggregate logical-trade PnL do not include that funding
- highly leveraged or imbalanced small-cap contracts can therefore show materially
  different logical PnL and net account balance impact

Recommendation:
- add a signed, read-only funding-flow adapter
- attribute flows by symbol, side, settlement time, and owned quantity snapshots
- keep ambiguous same-symbol multi-leg funding at account scope instead of guessing
- expose trading fees and funding paid/received as separate report fields

### B1 - Backtest readiness still behaves like a test harness

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
