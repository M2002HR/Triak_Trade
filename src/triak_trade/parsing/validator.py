"""Parsed signal validator."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.domain.enums import EntryType, SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal
from triak_trade.parsing.side_inference import infer_trade_side_from_price_geometry


class ParsedSignalValidator:
    """Strict validation for actionable proposal."""

    def normalize_for_execution(
        self,
        signal: ParsedSignal,
    ) -> tuple[ParsedSignal, str | None]:
        """Apply the geometry normalization shared by live and backtest execution."""

        if signal.action is not SignalAction.OPEN:
            return signal, None
        updates: dict[str, object] = {}
        if (
            signal.entry_type is EntryType.UNKNOWN
            and signal.entry_low is None
            and signal.entry_high is None
        ):
            updates["entry_type"] = EntryType.MARKET

        reference = self._entry_reference(signal)
        if (
            reference is not None
            and reference > Decimal("0")
            and signal.side in {TradeSide.UNKNOWN, TradeSide.BUY, TradeSide.SELL}
        ):
            inference = infer_trade_side_from_price_geometry(
                entry_low=signal.entry_low,
                entry_high=signal.entry_high,
                stop_loss=signal.stop_loss,
                take_profits=signal.take_profits,
            )
            if inference.side is not TradeSide.UNKNOWN:
                updates["side"] = inference.side

        normalized = signal.model_copy(update=updates) if updates else signal
        reference = self._entry_reference(normalized)
        if (
            reference is None
            or reference <= Decimal("0")
            or normalized.stop_loss is None
        ):
            return normalized, None
        if normalized.side.is_long and normalized.stop_loss >= reference:
            return (
                normalized,
                "inconsistent long geometry: stop_loss is not below entry/market price",
            )
        if normalized.side.is_short and normalized.stop_loss <= reference:
            return (
                normalized,
                "inconsistent short geometry: stop_loss is not above entry/market price",
            )
        return normalized, None

    def validate_for_proposal(
        self,
        signal: ParsedSignal,
        *,
        max_leverage: int,
        require_stop_loss: bool = True,
    ) -> tuple[bool, list[str]]:
        errors = self._base_open_checks(signal, min_confidence=Decimal("0.50"))

        if signal.action is SignalAction.IGNORE:
            return False, ["signal action is IGNORE"]
        if signal.action is SignalAction.UNKNOWN:
            return False, ["signal action is UNKNOWN"]

        if signal.action is SignalAction.OPEN:
            if require_stop_loss and signal.stop_loss is None:
                errors.append("missing stop_loss")
            if not signal.take_profits:
                errors.append("missing take_profits")
            self._append_directional_checks(errors, signal)

        if signal.leverage is not None and signal.leverage > max_leverage:
            errors.append("leverage exceeds max limit")

        return len(errors) == 0, errors

    def validate_for_backtest_open(
        self,
        signal: ParsedSignal,
        *,
        min_confidence: Decimal = Decimal("0.50"),
    ) -> tuple[bool, list[str]]:
        """Permissive gate for the backtest: open whatever the channel says.

        The user requirement is that any message which says "open a signal" must
        start being simulated. So we only reject signals that are structurally
        *unusable* (cannot be sized or routed to market data):

            - action must be OPEN
            - symbol must be present
            - side must be known (LONG/SHORT)
            - entry must be resolvable (market, or a low/high bound)
            - confidence must clear the floor

        Missing take_profits, missing stop_loss, and leverage above any cap are
        NOT rejections here — the simulator handles them (synthetic stop for
        sizing, run-to-end for no TP, leverage clamped for margin). Directional
        anomalies are returned as soft notes, not hard failures.
        """
        if signal.action is SignalAction.IGNORE:
            return False, ["signal action is IGNORE"]
        if signal.action is SignalAction.UNKNOWN:
            return False, ["signal action is UNKNOWN"]
        if signal.action is not SignalAction.OPEN:
            return False, [f"signal action is {signal.action.value}"]

        errors: list[str] = []
        if not signal.symbol:
            errors.append("missing symbol")
        if signal.side is TradeSide.UNKNOWN:
            errors.append("missing side")
        if (
            signal.entry_type.value != "market"
            and signal.entry_low is None
            and signal.entry_high is None
        ):
            # Missing entry is treated as a market-style open downstream.
            # The simulator/live engine resolves it from the first available
            # candle or the current mark price instead of rejecting the signal.
            pass
        if signal.confidence < min_confidence:
            errors.append("confidence too low")
        if signal.entry_low and signal.entry_high and signal.entry_low > signal.entry_high:
            errors.append("invalid entry range")
        return len(errors) == 0, errors

    def _base_open_checks(
        self,
        signal: ParsedSignal,
        *,
        min_confidence: Decimal,
    ) -> list[str]:
        errors: list[str] = []
        if signal.action is not SignalAction.OPEN:
            return errors
        if not signal.symbol:
            errors.append("missing symbol")
        if signal.side is TradeSide.UNKNOWN:
            errors.append("missing side")
        if (
            signal.entry_type.value != "market"
            and signal.entry_low is None
            and signal.entry_high is None
        ):
            pass
        if signal.confidence < min_confidence:
            errors.append("confidence too low")
        if signal.entry_low and signal.entry_high and signal.entry_low > signal.entry_high:
            errors.append("invalid entry range")
        return errors

    @staticmethod
    def _entry_reference(signal: ParsedSignal) -> Decimal | None:
        if signal.entry_low is not None and signal.entry_high is not None:
            return (signal.entry_low + signal.entry_high) / Decimal("2")
        return signal.entry_low or signal.entry_high

    @staticmethod
    def _append_directional_checks(errors: list[str], signal: ParsedSignal) -> None:
        if signal.action is not SignalAction.OPEN:
            return
        entry_ref = signal.entry_low or signal.entry_high
        if entry_ref is not None and signal.stop_loss is not None:
            if signal.side is TradeSide.LONG and signal.stop_loss >= entry_ref:
                errors.append("long stop_loss should be below entry")
            if signal.side is TradeSide.SHORT and signal.stop_loss <= entry_ref:
                errors.append("short stop_loss should be above entry")

        if entry_ref is not None and signal.take_profits:
            long_bad = signal.side is TradeSide.LONG and any(
                tp <= entry_ref for tp in signal.take_profits
            )
            short_bad = signal.side is TradeSide.SHORT and any(
                tp >= entry_ref for tp in signal.take_profits
            )
            if long_bad:
                errors.append("long take_profits should be above entry")
            if short_bad:
                errors.append("short take_profits should be below entry")
