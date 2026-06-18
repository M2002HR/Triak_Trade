"""Deterministic regex-based signal parser."""

from __future__ import annotations

import re
from decimal import Decimal

from triak_trade.domain.enums import EntryType, MarketType, SignalAction, TradeSide
from triak_trade.domain.models import NormalizedMessage, ParsedSignal

_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_STOP_SECTION_RE = re.compile(
    r"(?:\bsl\b|stop\s*loss|stoploss|\bstop\b|\u062d\u062f\s*\u0636\u0631\u0631)"
    r"[^\d]{0,40}(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LEVERAGE_RE = re.compile(
    r"(?:leverage|lev|\u0627\u0647\u0631\u0645)[^\d]{0,20}(\d+)\s*x?",
    re.IGNORECASE,
)
_X_LEVERAGE_RE = re.compile(r"\b(\d+)\s*x\b", re.IGNORECASE)
_TP_SECTION_MARKERS = (
    "tp",
    "target",
    "targets",
    "\u062a\u0627\u0631\u06af\u062a",
)
_SECTION_BREAK_RE = re.compile(
    r"(?:\bsl\b|stop\s*loss|stoploss|\bstop\b|entry|entries|leverage|lev|cancel|close|\u062d\u062f\s*\u0636\u0631\u0631)",
    re.IGNORECASE,
)


class RegexSignalParser:
    """Parse obvious messages into ParsedSignal."""

    def parse(self, normalized: NormalizedMessage) -> ParsedSignal:
        text = normalized.normalized_text
        lower = text.lower()

        action = self._classify_action(lower)
        symbol = normalized.detected_symbols[0] if normalized.detected_symbols else None
        side = self._extract_side(lower)
        entry_type, entry_low, entry_high = self._extract_entry(lower)
        stop_loss = self._extract_stop_loss(lower)
        take_profits = self._extract_take_profits(text)
        leverage = self._extract_leverage(lower)
        market = self._extract_market(lower, side, leverage)

        confidence = self._calc_confidence(
            action=action,
            symbol=symbol,
            side=side,
            entry_type=entry_type,
            stop_loss=stop_loss,
            take_profits=take_profits,
            lower=lower,
        )

        invalid_reason = None
        if action in {SignalAction.UNKNOWN, SignalAction.IGNORE}:
            invalid_reason = "non-proposable or ambiguous message"

        return ParsedSignal(
            action=action,
            market=market,
            symbol=symbol,
            side=side,
            entry_type=entry_type,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
            leverage=leverage,
            confidence=confidence,
            invalid_reason=invalid_reason,
            source_channel_id=normalized.raw.channel_id,
            source_message_id=normalized.raw.message_id,
            parser_version="regex-v1",
        )

    @staticmethod
    def _classify_action(lower: str) -> SignalAction:
        if any(
            token in lower
            for token in ["promo", "vip", "giveaway", "join now", "subscribe"]
        ):
            return SignalAction.IGNORE
        if any(
            token in lower
            for token in ["profit", "tp1 hit", "target reached", "+120%", "sl hit"]
        ):
            return SignalAction.IGNORE
        if "cancel" in lower or "\u0644\u063a\u0648" in lower:
            return SignalAction.CANCEL
        if (
            re.search(r"\bclose\b", lower)
            or "\u0628\u0633\u062a\u0646" in lower
            or "\u0628\u0628\u0646\u062f" in lower
        ):
            return SignalAction.CLOSE
        if any(
            token in lower
            for token in ["move sl", "stop to", "sl to be", "breakeven", "break even"]
        ):
            return SignalAction.UPDATE_SL
        if any(token in lower for token in ["tp updated", "new target", "targets updated"]):
            return SignalAction.UPDATE_TP
        if "update leverage" in lower or re.search(r"\blev(?:erage)?\b.*\bto\b", lower):
            return SignalAction.UPDATE_LEVERAGE
        open_tokens = [
            "long",
            "short",
            "buy",
            "sell",
            "\u0644\u0627\u0646\u06af",
            "\u0634\u0648\u0631\u062a",
            "\u062e\u0631\u06cc\u062f",
            "\u0641\u0631\u0648\u0634",
        ]
        if any(token in lower for token in open_tokens):
            return SignalAction.OPEN
        return SignalAction.UNKNOWN

    @staticmethod
    def _extract_side(lower: str) -> TradeSide:
        if "long" in lower or "\u0644\u0627\u0646\u06af" in lower:
            return TradeSide.LONG
        if "short" in lower or "\u0634\u0648\u0631\u062a" in lower:
            return TradeSide.SHORT
        if re.search(r"\bbuy\b", lower) or "\u062e\u0631\u06cc\u062f" in lower:
            return TradeSide.BUY
        if re.search(r"\bsell\b", lower) or "\u0641\u0631\u0648\u0634" in lower:
            return TradeSide.SELL
        return TradeSide.UNKNOWN

    @staticmethod
    def _extract_entry(lower: str) -> tuple[EntryType, Decimal | None, Decimal | None]:
        if "market" in lower or "now market" in lower:
            return EntryType.MARKET, None, None

        range_match = re.search(
            r"(?:entry|entries|zone|buy zone|entry zone|\u0648\u0631\u0648\u062f)"
            r"[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:-|to|/)\s*(\d+(?:\.\d+)?)",
            lower,
        )
        if range_match:
            first = Decimal(range_match.group(1))
            second = Decimal(range_match.group(2))
            entry_low, entry_high = sorted((first, second))
            return EntryType.RANGE, entry_low, entry_high

        single_match = re.search(
            r"(?:entry|entries|zone|\u0648\u0631\u0648\u062f)[^\d]{0,20}(\d+(?:\.\d+)?)",
            lower,
        )
        if single_match:
            return EntryType.LIMIT, Decimal(single_match.group(1)), None

        return EntryType.UNKNOWN, None, None

    @staticmethod
    def _extract_stop_loss(lower: str) -> Decimal | None:
        match = _STOP_SECTION_RE.search(lower)
        return Decimal(match.group(1)) if match else None

    @staticmethod
    def _extract_take_profits(text: str) -> list[Decimal]:
        lower = text.lower()
        section: str | None = None
        for marker in _TP_SECTION_MARKERS:
            index = lower.find(marker)
            if index == -1:
                continue
            candidate = text[index:]
            break_match = _SECTION_BREAK_RE.search(candidate[1:])
            if break_match is not None:
                section = candidate[: break_match.start() + 1]
            else:
                section = candidate
            break
        if section is None:
            return []

        values: list[Decimal] = []
        for number in _NUMBER_RE.findall(section):
            if len(number) == 1:
                continue
            value = Decimal(number)
            if value not in values:
                values.append(value)
        return values

    @staticmethod
    def _extract_leverage(lower: str) -> int | None:
        match = _LEVERAGE_RE.search(lower)
        if match:
            return int(match.group(1))
        x_match = _X_LEVERAGE_RE.search(lower)
        return int(x_match.group(1)) if x_match else None

    @staticmethod
    def _extract_market(lower: str, side: TradeSide, leverage: int | None) -> MarketType:
        if "spot" in lower:
            return MarketType.SPOT
        if any(
            token in lower for token in ["futures", "contract", "\u0641\u06cc\u0648\u0686\u0631"]
        ):
            return MarketType.FUTURES
        if side in {TradeSide.LONG, TradeSide.SHORT}:
            return MarketType.FUTURES
        if leverage is not None:
            return MarketType.FUTURES
        return MarketType.UNKNOWN

    @staticmethod
    def _calc_confidence(
        *,
        action: SignalAction,
        symbol: str | None,
        side: TradeSide,
        entry_type: EntryType,
        stop_loss: Decimal | None,
        take_profits: list[Decimal],
        lower: str,
    ) -> Decimal:
        if action is SignalAction.OPEN:
            has_core = (
                symbol is not None
                and side is not TradeSide.UNKNOWN
                and entry_type is not EntryType.UNKNOWN
            )
            if has_core and stop_loss is not None and bool(take_profits):
                return Decimal("0.92")
            if has_core and (stop_loss is not None or bool(take_profits)):
                return Decimal("0.82")
            if symbol and side is not TradeSide.UNKNOWN:
                return Decimal("0.65")
            return Decimal("0.30")

        update_actions = {
            SignalAction.CANCEL,
            SignalAction.CLOSE,
            SignalAction.UPDATE_SL,
            SignalAction.UPDATE_TP,
            SignalAction.UPDATE_LEVERAGE,
        }
        if action in update_actions:
            if symbol or "sl" in lower or "leverage" in lower:
                return Decimal("0.80")
            return Decimal("0.60")

        if action is SignalAction.IGNORE:
            return Decimal("0.80")
        return Decimal("0.30")
