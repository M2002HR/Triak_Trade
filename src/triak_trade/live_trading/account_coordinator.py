"""Account-wide coordination for multi-session live execution.

Telegram channels own logical trade legs.  The exchange account owns physical
positions.  This module is the single in-process authority that maps between
those two scopes and serializes all exchange mutations.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Protocol

from triak_trade.live_trading.models import LiveTrade


class AccountPositionPolicy(str, Enum):
    """How a shared exchange account handles opposite directions."""

    NET = "net"
    HEDGE = "hedge"
    BLOCK = "block"


class SameDirectionPolicy(str, Enum):
    """How a shared exchange account handles same-direction signals."""

    AGGREGATE = "aggregate"
    BLOCK = "block"


class AccountExecutionConflictError(ValueError):
    """Raised when account-wide invariants reject an execution request."""


@dataclass(frozen=True)
class AccountBucketKey:
    """Physical exchange position bucket."""

    trading_mode: str
    symbol: str
    side: str


@dataclass
class AccountTradeLeg:
    """Logical channel-owned quantity inside a physical position bucket."""

    trade_id: str
    session_id: str
    signal_id: str
    channel_id: str
    trading_mode: str
    symbol: str
    side: str
    entry_price: Decimal
    stop_loss: Decimal | None
    remaining_quantity: Decimal
    balance_at_entry: Decimal
    opened_at: datetime
    status: str
    close_handler: NetCloseHandler | None = None
    order_ids: set[str] = field(default_factory=set)

    @property
    def bucket(self) -> AccountBucketKey:
        return AccountBucketKey(
            trading_mode=self.trading_mode,
            symbol=self.symbol,
            side=self.side,
        )


@dataclass(frozen=True)
class AccountOpenDecision:
    """Result of applying account-wide netting and risk policy."""

    action: str
    original_quantity: Decimal
    approved_quantity: Decimal
    offset_quantity: Decimal = Decimal("0")
    duplicate_trade_id: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def should_open(self) -> bool:
        return self.action == "open" and self.approved_quantity > Decimal("0")


class NetCloseHandler(Protocol):
    async def __call__(
        self,
        trade_id: str,
        quantity: Decimal,
        reason: str,
    ) -> Decimal: ...


class TradeStoreReader(Protocol):
    def list_sessions(self, limit: int = 20) -> Sequence[object]: ...

    def list_trades(
        self,
        session_id: str,
        limit: int = 200,
    ) -> Sequence[LiveTrade]: ...


class AccountExecutionCoordinator:
    """Single-writer account coordinator shared by every live session engine."""

    def __init__(
        self,
        *,
        position_policy: AccountPositionPolicy = AccountPositionPolicy.NET,
        same_direction_policy: SameDirectionPolicy = SameDirectionPolicy.AGGREGATE,
        symbol_risk_cap_pct: Decimal = Decimal("5"),
        fee_rate_pct: Decimal = Decimal("0.04"),
        duplicate_window_seconds: int = 300,
        duplicate_price_tolerance_bps: Decimal = Decimal("20"),
    ) -> None:
        self.position_policy = position_policy
        self.same_direction_policy = same_direction_policy
        self.symbol_risk_cap_pct = max(symbol_risk_cap_pct, Decimal("0"))
        self.fee_rate_pct = max(fee_rate_pct, Decimal("0"))
        self.duplicate_window_seconds = max(duplicate_window_seconds, 0)
        self.duplicate_price_tolerance_bps = max(
            duplicate_price_tolerance_bps,
            Decimal("0"),
        )

        self._execution_lock = threading.Lock()
        self._registry_lock = threading.RLock()
        self._guard_held = contextvars.ContextVar(
            f"triak_account_execution_guard_{id(self)}",
            default=False,
        )
        self._legs: dict[str, AccountTradeLeg] = {}
        self._order_owners: dict[str, str] = {}
        self._fill_owners: dict[str, str] = {}
        self._blocked_buckets: dict[AccountBucketKey, str] = {}

    @classmethod
    def from_settings(cls, settings: object) -> AccountExecutionCoordinator:
        position_policy = getattr(
            settings,
            "LIVE_TRADING_ACCOUNT_POSITION_POLICY",
            AccountPositionPolicy.NET.value,
        )
        if not isinstance(position_policy, (str, AccountPositionPolicy)):
            position_policy = AccountPositionPolicy.NET.value
        same_direction_policy = getattr(
            settings,
            "LIVE_TRADING_SAME_DIRECTION_POLICY",
            SameDirectionPolicy.AGGREGATE.value,
        )
        if not isinstance(same_direction_policy, (str, SameDirectionPolicy)):
            same_direction_policy = SameDirectionPolicy.AGGREGATE.value

        def decimal_setting(name: str, default: Decimal) -> Decimal:
            value = getattr(settings, name, default)
            if not isinstance(value, (Decimal, int, str)):
                return default
            try:
                return Decimal(str(value))
            except (ArithmeticError, ValueError):
                return default

        duplicate_window = getattr(
            settings,
            "LIVE_TRADING_DUPLICATE_SIGNAL_WINDOW_SECONDS",
            300,
        )
        if not isinstance(duplicate_window, int):
            duplicate_window = 300
        return cls(
            position_policy=AccountPositionPolicy(
                position_policy.value
                if isinstance(position_policy, AccountPositionPolicy)
                else position_policy.lower()
            ),
            same_direction_policy=SameDirectionPolicy(
                same_direction_policy.value
                if isinstance(same_direction_policy, SameDirectionPolicy)
                else same_direction_policy.lower()
            ),
            symbol_risk_cap_pct=decimal_setting(
                "LIVE_TRADING_SYMBOL_RISK_CAP_PCT_OF_BALANCE",
                Decimal("5"),
            ),
            fee_rate_pct=decimal_setting(
                "LIVE_TRADING_FEE_RATE_PCT",
                Decimal("0.04"),
            ),
            duplicate_window_seconds=duplicate_window,
            duplicate_price_tolerance_bps=decimal_setting(
                "LIVE_TRADING_DUPLICATE_PRICE_TOLERANCE_BPS",
                Decimal("20"),
            ),
        )

    @asynccontextmanager
    async def execution_guard(self) -> AsyncIterator[None]:
        """Serialize exchange reads/writes across threads and asyncio loops."""

        if self._guard_held.get():
            yield
            return
        acquired = False
        while not acquired:
            acquired = self._execution_lock.acquire(blocking=False)
            if not acquired:
                await asyncio.sleep(0.01)
        token = self._guard_held.set(True)
        try:
            yield
        finally:
            self._guard_held.reset(token)
            self._execution_lock.release()

    def restore_from_store(self, store: TradeStoreReader, *, session_limit: int = 500) -> None:
        """Restore account ownership before any worker threads are started."""

        for session in store.list_sessions(limit=session_limit):
            session_id = str(getattr(session, "session_id", ""))
            trading_mode = str(getattr(session, "trading_mode", "demo"))
            if not session_id:
                continue
            for trade in store.list_trades(session_id, limit=5000):
                self.restore_trade(trade, trading_mode=trading_mode)

    def restore_trade(self, trade: LiveTrade, *, trading_mode: str) -> None:
        """Restore durable fill/order ownership and any still-open logical leg."""

        if trade.is_open:
            self.register_trade(trade, trading_mode=trading_mode)
        for fill_id in trade.processed_exchange_fill_ids:
            self._restore_fill_owner(fill_id, trade, trading_mode=trading_mode)
        for order_id in self._trade_order_ids(trade):
            self._restore_order_owner(order_id, trade, trading_mode=trading_mode)

    def register_trade(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
        close_handler: NetCloseHandler | None = None,
    ) -> None:
        """Register or refresh a logical trade leg."""

        leg = AccountTradeLeg(
            trade_id=trade.trade_id,
            session_id=trade.session_id,
            signal_id=trade.signal_id,
            channel_id=trade.channel_id,
            trading_mode=trading_mode,
            symbol=trade.symbol.upper(),
            side=trade.side.lower(),
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
            remaining_quantity=max(trade.remaining_quantity, Decimal("0")),
            balance_at_entry=max(trade.balance_at_entry, Decimal("0")),
            opened_at=trade.opened_at,
            status=trade.status,
            close_handler=close_handler,
            order_ids=set(self._trade_order_ids(trade)),
        )
        with self._registry_lock:
            existing = self._legs.get(trade.trade_id)
            if existing is not None and close_handler is None:
                leg.close_handler = existing.close_handler
            self._legs[trade.trade_id] = leg
            for order_id in leg.order_ids:
                self._claim_order_locked(order_id, trade.trade_id, leg.bucket)

    def unregister_trade(self, trade_id: str) -> None:
        with self._registry_lock:
            self._legs.pop(trade_id, None)

    def mark_order_owner(
        self,
        order_id: str | None,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> None:
        if not order_id:
            return
        bucket = self._bucket_for_trade(trade, trading_mode=trading_mode)
        with self._registry_lock:
            self._claim_order_locked(str(order_id), trade.trade_id, bucket)
            leg = self._legs.get(trade.trade_id)
            if leg is not None:
                leg.order_ids.add(str(order_id))

    def release_order_owner(self, order_id: str | None, trade_id: str) -> None:
        if not order_id:
            return
        with self._registry_lock:
            if self._order_owners.get(str(order_id)) == trade_id:
                self._order_owners.pop(str(order_id), None)
            leg = self._legs.get(trade_id)
            if leg is not None:
                leg.order_ids.discard(str(order_id))

    def order_is_owned_by(self, order_id: str | None, trade_id: str) -> bool:
        if not order_id:
            return False
        with self._registry_lock:
            return self._order_owners.get(str(order_id)) == trade_id

    def order_is_available(self, order_id: str | None, trade_id: str) -> bool:
        if not order_id:
            return True
        with self._registry_lock:
            owner = self._order_owners.get(str(order_id))
            return owner is None or owner == trade_id

    def fill_is_available(self, fill_id: str, trade_id: str) -> bool:
        with self._registry_lock:
            owner = self._fill_owners.get(fill_id)
            return owner is None or owner == trade_id

    def mark_fill_owner(
        self,
        fill_id: str,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> bool:
        bucket = self._bucket_for_trade(trade, trading_mode=trading_mode)
        with self._registry_lock:
            owner = self._fill_owners.get(fill_id)
            if owner is not None:
                if owner != trade.trade_id:
                    self._blocked_buckets[bucket] = (
                        f"exchange fill {fill_id} is already owned by trade {owner}"
                    )
                    return False
                return True
            self._fill_owners[fill_id] = trade.trade_id
            return True

    def bucket_logical_quantity(
        self,
        *,
        trading_mode: str,
        symbol: str,
        side: str,
        exclude_trade_id: str | None = None,
    ) -> Decimal:
        key = AccountBucketKey(trading_mode, symbol.upper(), side.lower())
        with self._registry_lock:
            return sum(
                (
                    leg.remaining_quantity
                    for leg in self._legs.values()
                    if leg.bucket == key
                    and leg.trade_id != exclude_trade_id
                    and leg.status in {"waiting_entry", "open", "partial_close"}
                ),
                Decimal("0"),
            )

    def has_other_open_legs(self, trade: LiveTrade, *, trading_mode: str) -> bool:
        return (
            self.bucket_logical_quantity(
                trading_mode=trading_mode,
                symbol=trade.symbol,
                side=trade.side,
                exclude_trade_id=trade.trade_id,
            )
            > Decimal("0")
        )

    def bucket_block_reason(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> str | None:
        key = self._bucket_for_trade(trade, trading_mode=trading_mode)
        with self._registry_lock:
            return self._blocked_buckets.get(key)

    async def coordinate_open(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> AccountOpenDecision:
        """Apply duplicate, opposite-direction netting, and symbol risk policy."""

        original_quantity = trade.quantity
        if original_quantity <= Decimal("0"):
            raise AccountExecutionConflictError("candidate quantity must be positive")

        blocked_reason = self.bucket_block_reason(trade, trading_mode=trading_mode)
        if blocked_reason:
            raise AccountExecutionConflictError(
                f"account bucket is blocked by reconciliation: {blocked_reason}"
            )

        offset_quantity = Decimal("0")
        notes: list[str] = []
        if self.position_policy is AccountPositionPolicy.BLOCK:
            opposite = self._opposite_legs(trade, trading_mode=trading_mode)
            if opposite:
                raise AccountExecutionConflictError(
                    "opposite-direction exposure exists and account policy is block"
                )
        elif self.position_policy is AccountPositionPolicy.NET:
            offset_quantity = await self._net_opposite_legs(
                trade,
                trading_mode=trading_mode,
            )
            if offset_quantity:
                notes.append(f"account_net_offset_quantity={offset_quantity}")
            residual = max(original_quantity - offset_quantity, Decimal("0"))
            if residual <= Decimal("0"):
                return AccountOpenDecision(
                    action="netted",
                    original_quantity=original_quantity,
                    approved_quantity=Decimal("0"),
                    offset_quantity=offset_quantity,
                    notes=tuple(notes),
                )
            trade.quantity = residual
            trade.remaining_quantity = residual

        same_side_legs = self._same_side_legs(trade, trading_mode=trading_mode)
        duplicate = self._find_duplicate(trade, same_side_legs)
        if duplicate is not None:
            notes.append(f"consensus_duplicate_of={duplicate.trade_id}")
            return AccountOpenDecision(
                action="duplicate",
                original_quantity=original_quantity,
                approved_quantity=Decimal("0"),
                offset_quantity=offset_quantity,
                duplicate_trade_id=duplicate.trade_id,
                notes=tuple(notes),
            )
        if same_side_legs and self.same_direction_policy is SameDirectionPolicy.BLOCK:
            raise AccountExecutionConflictError(
                "same-direction exposure exists and same-direction policy is block"
            )

        approved_quantity = self._apply_symbol_risk_cap(trade, same_side_legs)
        if approved_quantity <= Decimal("0"):
            raise AccountExecutionConflictError(
                "account symbol risk cap leaves no executable quantity"
            )
        if approved_quantity < trade.quantity:
            notes.append(
                f"symbol_risk_quantity_reduced={trade.quantity}->{approved_quantity}"
            )
            trade.quantity = approved_quantity
            trade.remaining_quantity = approved_quantity
        return AccountOpenDecision(
            action="open",
            original_quantity=original_quantity,
            approved_quantity=approved_quantity,
            offset_quantity=offset_quantity,
            notes=tuple(notes),
        )

    def _same_side_legs(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> list[AccountTradeLeg]:
        key = self._bucket_for_trade(trade, trading_mode=trading_mode)
        with self._registry_lock:
            return [
                leg
                for leg in self._legs.values()
                if leg.bucket == key
                and leg.trade_id != trade.trade_id
                and leg.remaining_quantity > Decimal("0")
                and leg.status in {"waiting_entry", "open", "partial_close"}
            ]

    def _opposite_legs(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> list[AccountTradeLeg]:
        opposite_side = "short" if trade.side.lower() == "long" else "long"
        key = AccountBucketKey(trading_mode, trade.symbol.upper(), opposite_side)
        with self._registry_lock:
            result = [
                leg
                for leg in self._legs.values()
                if leg.bucket == key
                and leg.remaining_quantity > Decimal("0")
                and leg.status in {"waiting_entry", "open", "partial_close"}
            ]
        return sorted(result, key=lambda item: (item.opened_at, item.trade_id))

    async def _net_opposite_legs(
        self,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> Decimal:
        requested = trade.quantity
        offset = Decimal("0")
        for leg in self._opposite_legs(trade, trading_mode=trading_mode):
            if offset >= requested:
                break
            if leg.status == "waiting_entry":
                raise AccountExecutionConflictError(
                    "cannot net against an opposite pending entry; cancel it first"
                )
            if leg.close_handler is None:
                raise AccountExecutionConflictError(
                    f"opposite trade {leg.trade_id} has no active owner engine"
                )
            close_quantity = min(leg.remaining_quantity, requested - offset)
            executed = await leg.close_handler(
                leg.trade_id,
                close_quantity,
                f"netted_by_signal:{trade.signal_id}",
            )
            if executed <= Decimal("0"):
                raise AccountExecutionConflictError(
                    f"opposite trade {leg.trade_id} could not be reduced"
                )
            offset += min(executed, close_quantity)
        return offset

    def _find_duplicate(
        self,
        trade: LiveTrade,
        legs: Iterable[AccountTradeLeg],
    ) -> AccountTradeLeg | None:
        if self.duplicate_window_seconds <= 0:
            return None
        for leg in legs:
            age = abs((trade.opened_at - leg.opened_at).total_seconds())
            if age > self.duplicate_window_seconds:
                continue
            if not self._prices_close(trade.entry_price, leg.entry_price):
                continue
            if trade.stop_loss is None or leg.stop_loss is None:
                if trade.stop_loss != leg.stop_loss:
                    continue
            elif not self._prices_close(trade.stop_loss, leg.stop_loss):
                continue
            return leg
        return None

    def _prices_close(self, first: Decimal, second: Decimal) -> bool:
        reference = max(abs(first), abs(second))
        if reference <= Decimal("0"):
            return first == second
        difference_bps = abs(first - second) * Decimal("10000") / reference
        return difference_bps <= self.duplicate_price_tolerance_bps

    def _apply_symbol_risk_cap(
        self,
        trade: LiveTrade,
        same_side_legs: Iterable[AccountTradeLeg],
    ) -> Decimal:
        existing_legs = list(same_side_legs)
        if self.symbol_risk_cap_pct <= Decimal("0"):
            return trade.quantity
        balance = max(trade.balance_at_entry, Decimal("0"))
        if balance <= Decimal("0"):
            if not existing_legs:
                # Older restored records may predate balance snapshots. Their
                # per-trade size was already risk checked by the position
                # manager; only aggregation requires an account-level budget.
                return trade.quantity
            raise AccountExecutionConflictError(
                "balance at entry is unavailable for symbol risk cap"
            )
        risk_cap = balance * self.symbol_risk_cap_pct / Decimal("100")
        existing_risk = sum(
            (self._leg_stop_risk(leg) for leg in existing_legs),
            Decimal("0"),
        )
        available_risk = max(risk_cap - existing_risk, Decimal("0"))
        risk_per_unit = self._stop_risk_per_unit(
            side=trade.side,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
        )
        if risk_per_unit <= Decimal("0"):
            raise AccountExecutionConflictError(
                "candidate has no measurable protected stop risk"
            )
        return min(trade.quantity, available_risk / risk_per_unit)

    def _leg_stop_risk(self, leg: AccountTradeLeg) -> Decimal:
        return self._stop_risk_per_unit(
            side=leg.side,
            entry_price=leg.entry_price,
            stop_loss=leg.stop_loss,
        ) * leg.remaining_quantity

    def _stop_risk_per_unit(
        self,
        *,
        side: str,
        entry_price: Decimal,
        stop_loss: Decimal | None,
    ) -> Decimal:
        if entry_price <= Decimal("0") or stop_loss is None or stop_loss <= Decimal("0"):
            return Decimal("0")
        gross_pnl = (
            stop_loss - entry_price
            if side.lower() == "long"
            else entry_price - stop_loss
        )
        fee_rate = self.fee_rate_pct / Decimal("100")
        fees = (entry_price + stop_loss) * fee_rate
        return max(-(gross_pnl - fees), Decimal("0"))

    def _restore_fill_owner(
        self,
        fill_id: str,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> None:
        with self._registry_lock:
            owner = self._fill_owners.get(fill_id)
            if owner is None:
                self._fill_owners[fill_id] = trade.trade_id
                return
            if owner != trade.trade_id:
                self._blocked_buckets[
                    self._bucket_for_trade(trade, trading_mode=trading_mode)
                ] = f"duplicate durable fill ownership: {fill_id} ({owner}, {trade.trade_id})"

    def _restore_order_owner(
        self,
        order_id: str,
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> None:
        with self._registry_lock:
            bucket = self._bucket_for_trade(trade, trading_mode=trading_mode)
            self._claim_order_locked(order_id, trade.trade_id, bucket)

    def _claim_order_locked(
        self,
        order_id: str,
        trade_id: str,
        bucket: AccountBucketKey,
    ) -> None:
        owner = self._order_owners.get(order_id)
        if owner is None or owner == trade_id:
            self._order_owners[order_id] = trade_id
            return
        self._blocked_buckets[bucket] = (
            f"exchange order {order_id} has multiple logical owners: {owner}, {trade_id}"
        )

    @staticmethod
    def _trade_order_ids(trade: LiveTrade) -> list[str]:
        order_ids = [
            trade.entry_order_id,
            trade.sl_order_id,
            *trade.tp_order_ids,
            *(item.order_id for item in trade.tp_order_plan),
        ]
        return [str(item) for item in order_ids if item]

    @staticmethod
    def _bucket_for_trade(
        trade: LiveTrade,
        *,
        trading_mode: str,
    ) -> AccountBucketKey:
        return AccountBucketKey(
            trading_mode=trading_mode,
            symbol=trade.symbol.upper(),
            side=trade.side.lower(),
        )


async def run_account_coordination_dry_run() -> dict[str, object]:
    """Exercise duplicate, netting, aggregation, and ownership without I/O."""

    coordinator = AccountExecutionCoordinator()
    opened_at = datetime.now(timezone.utc)

    def make_trade(
        *,
        trade_id: str,
        signal_id: str,
        channel_id: str,
        side: str,
        entry_price: Decimal,
        stop_loss: Decimal,
        quantity: Decimal,
    ) -> LiveTrade:
        return LiveTrade(
            trade_id=trade_id,
            session_id=f"session_{trade_id}",
            signal_id=signal_id,
            channel_id=channel_id,
            channel_input=channel_id,
            channel_label=channel_id,
            symbol="BTCUSDT",
            side=side,
            leverage=5,
            entry_price=entry_price,
            quantity=quantity,
            remaining_quantity=quantity,
            stop_loss=stop_loss,
            take_profits=[Decimal("110") if side == "long" else Decimal("90")],
            balance_at_entry=Decimal("1000"),
            opened_at=opened_at,
            status="open",
        )

    primary = make_trade(
        trade_id="dry_primary",
        signal_id="dry_signal_primary",
        channel_id="@channel_a",
        side="long",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        quantity=Decimal("1"),
    )

    async def close_primary(
        trade_id: str,
        quantity: Decimal,
        reason: str,
    ) -> Decimal:
        del reason
        if trade_id != primary.trade_id:
            return Decimal("0")
        primary.remaining_quantity -= quantity
        primary.status = "partial_close" if primary.remaining_quantity > 0 else "closed"
        if primary.is_open:
            coordinator.register_trade(
                primary,
                trading_mode="live",
                close_handler=close_primary,
            )
        else:
            coordinator.unregister_trade(primary.trade_id)
        return quantity

    coordinator.register_trade(
        primary,
        trading_mode="live",
        close_handler=close_primary,
    )
    duplicate = make_trade(
        trade_id="dry_duplicate",
        signal_id="dry_signal_duplicate",
        channel_id="@channel_b",
        side="long",
        entry_price=Decimal("100.05"),
        stop_loss=Decimal("95.05"),
        quantity=Decimal("1"),
    )
    duplicate_decision = await coordinator.coordinate_open(
        duplicate,
        trading_mode="live",
    )
    opposite = make_trade(
        trade_id="dry_opposite",
        signal_id="dry_signal_opposite",
        channel_id="@channel_c",
        side="short",
        entry_price=Decimal("100"),
        stop_loss=Decimal("105"),
        quantity=Decimal("0.4"),
    )
    opposite_decision = await coordinator.coordinate_open(
        opposite,
        trading_mode="live",
    )
    aggregate = make_trade(
        trade_id="dry_aggregate",
        signal_id="dry_signal_aggregate",
        channel_id="@channel_d",
        side="long",
        entry_price=Decimal("110"),
        stop_loss=Decimal("104"),
        quantity=Decimal("0.5"),
    )
    aggregate_decision = await coordinator.coordinate_open(
        aggregate,
        trading_mode="live",
    )
    orphan_coordinator = AccountExecutionCoordinator()
    orphan_primary = make_trade(
        trade_id="dry_orphan",
        signal_id="dry_signal_orphan",
        channel_id="@orphan",
        side="long",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        quantity=Decimal("1"),
    )
    orphan_coordinator.register_trade(orphan_primary, trading_mode="live")
    orphan_candidate = make_trade(
        trade_id="dry_orphan_opposite",
        signal_id="dry_signal_orphan_opposite",
        channel_id="@orphan_opposite",
        side="short",
        entry_price=Decimal("100"),
        stop_loss=Decimal("105"),
        quantity=Decimal("1"),
    )
    orphan_error: str | None = None
    try:
        await orphan_coordinator.coordinate_open(
            orphan_candidate,
            trading_mode="live",
        )
    except AccountExecutionConflictError as exc:
        orphan_error = str(exc)
    checks = {
        "duplicate_is_consensus": duplicate_decision.action == "duplicate",
        "opposite_is_netted": opposite_decision.action == "netted",
        "netted_quantity_is_exact": opposite_decision.offset_quantity
        == Decimal("0.4"),
        "primary_logical_remainder_is_exact": primary.remaining_quantity
        == Decimal("0.6"),
        "distinct_same_direction_is_aggregated": aggregate_decision.should_open,
        "missing_owner_engine_fails_closed": bool(
            orphan_error and "no active owner engine" in orphan_error
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "duplicate_action": duplicate_decision.action,
        "duplicate_of": duplicate_decision.duplicate_trade_id,
        "opposite_action": opposite_decision.action,
        "opposite_offset_quantity": str(opposite_decision.offset_quantity),
        "primary_remaining_quantity": str(primary.remaining_quantity),
        "aggregate_action": aggregate_decision.action,
        "aggregate_approved_quantity": str(aggregate_decision.approved_quantity),
        "failure_case": orphan_error,
    }
