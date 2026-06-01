"""Per-channel agent state machine."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from triak_trade.agents.classifier import MessageClassifier, RegexMessageClassifier
from triak_trade.agents.clock import Clock, SystemClock
from triak_trade.agents.context import ChannelContext
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import ProposedActionType, SignalStatus
from triak_trade.domain.ids import make_action_id, make_signal_id
from triak_trade.domain.models import ParsedSignal, ProposedAction, RawTelegramMessage, SignalState
from triak_trade.parsing.validator import ParsedSignalValidator


class ChannelAgent:
    def __init__(
        self,
        *,
        channel_id: str,
        settings: Settings,
        classifier: MessageClassifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.classifier = classifier or RegexMessageClassifier()
        self.validator = ParsedSignalValidator()
        self.context = ChannelContext(
            channel_id=channel_id,
            max_message_limit=settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
            max_update_window_hours=settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
        )
        self.pending_deadlines: dict[str, datetime] = {}
        self.debug_events: list[dict[str, object]] = []

    def ingest_message(self, raw_message: RawTelegramMessage) -> list[ProposedAction]:
        actions: list[ProposedAction] = []
        self.context.add_recent_message(raw_message)
        classified = self.classifier.classify(raw_message, self.context)
        parsed = classified.parsed_signal

        if classified.is_potential_new_signal:
            signal_id = make_signal_id(raw_message.channel_id, raw_message.message_id)
            state = SignalState(
                signal_id=signal_id,
                channel_id=raw_message.channel_id,
                status=SignalStatus.PENDING_CONSOLIDATION,
                created_from_message_id=raw_message.message_id,
                related_message_ids=[raw_message.message_id],
                current_signal=parsed,
                version=1,
                created_at=self.clock.now(),
                updated_at=self.clock.now(),
                expires_at=None,
            )
            self.context.add_signal(state, pending=True)
            self.pending_deadlines[signal_id] = self.clock.now() + timedelta(
                seconds=self.settings.SIGNAL_CONSOLIDATION_SECONDS
            )
            self.debug_events.append(
                {
                    "channel_id": raw_message.channel_id,
                    "message_id": raw_message.message_id,
                    "signal_id": signal_id,
                    "status": "pending_consolidation",
                    "reason": "new potential signal",
                    "confidence": str(parsed.confidence),
                }
            )
            return actions

        related_signal_id = classified.related_signal_id
        if related_signal_id is not None and self.context.get_signal(related_signal_id) is not None:
            signal = self.context.get_signal(related_signal_id)
            assert signal is not None
            if not self.context.is_within_update_window(signal, self.clock.now()):
                return actions
            self.context.attach_message(related_signal_id, raw_message)
            self.context.merge_signal(related_signal_id, parsed, raw_message.date)
            self.debug_events.append(
                {
                    "channel_id": raw_message.channel_id,
                    "message_id": raw_message.message_id,
                    "signal_id": related_signal_id,
                    "status": signal.status.value,
                    "reason": classified.relation_reason or "related",
                    "confidence": str(parsed.confidence),
                }
            )
            if signal.status is not SignalStatus.PENDING_CONSOLIDATION:
                followup = self._build_followup_action(signal_id=related_signal_id, parsed=parsed)
                if followup is not None:
                    actions.append(followup)
            return actions

        if classified.is_related_to_existing_signal:
            request = self._make_action(
                signal_id=None,
                action_type=ProposedActionType.REQUEST_ADMIN_CONFIRMATION,
                confidence=parsed.confidence,
                reason="ambiguous relation update",
                payload={"message_id": raw_message.message_id, "action": parsed.action.value},
                risk_increasing=False,
                requires_admin_approval=True,
            )
            actions.append(request)

        return actions

    def tick(self, now: datetime | None = None) -> list[ProposedAction]:
        current = now or self.clock.now()
        actions: list[ProposedAction] = []

        for signal_id, deadline in list(self.pending_deadlines.items()):
            if current < deadline:
                continue
            signal = self.context.get_signal(signal_id)
            if signal is None or signal.current_signal is None:
                self.pending_deadlines.pop(signal_id, None)
                continue

            ok, errors = self.validator.validate_for_proposal(
                signal.current_signal,
                max_leverage=self.settings.MAX_LEVERAGE,
                require_stop_loss=self.settings.REQUIRE_STOP_LOSS,
            )
            if ok:
                action = self._make_action(
                    signal_id=signal.signal_id,
                    action_type=ProposedActionType.CREATE_ORDER,
                    confidence=signal.current_signal.confidence,
                    reason="consolidation completed",
                    payload={
                        "channel_id": signal.channel_id,
                        "related_message_ids": signal.related_message_ids,
                    },
                    risk_increasing=True,
                    requires_admin_approval=True,
                )
                signal.status = SignalStatus.PROPOSED_TO_ADMIN
                actions.append(action)
            else:
                action = self._make_action(
                    signal_id=signal.signal_id,
                    action_type=ProposedActionType.REQUEST_ADMIN_CONFIRMATION,
                    confidence=signal.current_signal.confidence,
                    reason="invalid after consolidation",
                    payload={"errors": errors},
                    risk_increasing=False,
                    requires_admin_approval=True,
                )
                signal.status = SignalStatus.INVALID
                actions.append(action)

            signal.updated_at = current
            self.context.add_signal(signal, pending=False)
            self.pending_deadlines.pop(signal_id, None)

        return actions

    def get_context_snapshot(self) -> dict[str, object]:
        snapshot = self.context.snapshot()
        snapshot["pending_signal_ids"] = sorted(self.context.pending_signal_ids)
        snapshot["pending_deadlines"] = {
            key: value.isoformat() for key, value in self.pending_deadlines.items()
        }
        snapshot["debug_events"] = self.debug_events[-20:]
        return snapshot

    def _build_followup_action(
        self,
        *,
        signal_id: str,
        parsed: ParsedSignal,
    ) -> ProposedAction | None:
        mapping = {
            "cancel": ProposedActionType.CANCEL_PENDING_ORDER,
            "close": ProposedActionType.CLOSE_POSITION_FULL,
            "update_leverage": ProposedActionType.UPDATE_LEVERAGE,
            "update_sl": ProposedActionType.MOVE_STOP_LOSS,
            "update_tp": ProposedActionType.UPDATE_TAKE_PROFIT,
        }
        action_type = mapping.get(parsed.action.value)
        if action_type is None:
            if parsed.action.value == "unknown":
                return self._make_action(
                    signal_id=signal_id,
                    action_type=ProposedActionType.REQUEST_ADMIN_CONFIRMATION,
                    confidence=parsed.confidence,
                    reason="ambiguous follow-up",
                    payload={"source_action": parsed.action.value},
                    risk_increasing=False,
                    requires_admin_approval=True,
                )
            return None

        risk_increasing = action_type in {
            ProposedActionType.UPDATE_LEVERAGE,
            ProposedActionType.CLOSE_POSITION_PARTIAL,
            ProposedActionType.CLOSE_POSITION_FULL,
        }
        return self._make_action(
            signal_id=signal_id,
            action_type=action_type,
            confidence=parsed.confidence,
            reason="related follow-up message",
            payload={"source_action": parsed.action.value},
            risk_increasing=risk_increasing,
            requires_admin_approval=True,
        )

    def _make_action(
        self,
        *,
        signal_id: str | None,
        action_type: ProposedActionType,
        confidence: Decimal,
        reason: str,
        payload: dict[str, object],
        risk_increasing: bool,
        requires_admin_approval: bool,
    ) -> ProposedAction:
        base_signal_id = signal_id or "global"
        action_id = make_action_id(base_signal_id, action_type.value, len(self.debug_events) + 1)
        return ProposedAction(
            action_id=action_id,
            action_type=action_type,
            signal_id=signal_id,
            risk_increasing=risk_increasing,
            requires_admin_approval=requires_admin_approval,
            confidence=confidence,
            reason=reason,
            payload=payload,
            created_at=self.clock.now(),
        )
