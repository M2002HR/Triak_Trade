"""Parsed signal validator."""

from __future__ import annotations

from decimal import Decimal

from triak_trade.domain.enums import SignalAction, TradeSide
from triak_trade.domain.models import ParsedSignal


class ParsedSignalValidator:
    """Strict validation for actionable proposal."""

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

    def validate_for_backtest(
        self,
        signal: ParsedSignal,
        *,
        min_confidence: Decimal = Decimal("0.50"),
    ) -> tuple[bool, list[str]]:
        if signal.action is SignalAction.IGNORE:
            return False, ["signal action is IGNORE"]
        if signal.action is SignalAction.UNKNOWN:
            return False, ["signal action is UNKNOWN"]
        if signal.action is not SignalAction.OPEN:
            return False, [f"signal action is {signal.action.value}"]

        errors = self._base_open_checks(signal, min_confidence=min_confidence)
        self._append_directional_checks(errors, signal)
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
        if signal.entry_type.value != "market" and not (signal.entry_low or signal.entry_high):
            errors.append("missing entry")
        if signal.confidence < min_confidence:
            errors.append("confidence too low")
        if signal.entry_low and signal.entry_high and signal.entry_low > signal.entry_high:
            errors.append("invalid entry range")
        return errors

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
