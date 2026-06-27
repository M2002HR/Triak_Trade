"""Processing audit service around ChannelAgent message ingestion."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, Field

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.config.settings import Settings
from triak_trade.db.repositories import AuditLogRepository
from triak_trade.domain.enums import SignalAction
from triak_trade.domain.models import ProposedAction, RawTelegramMessage
from triak_trade.observability.events import (
    ProcessingAuditEvent,
    ProcessingAuditStatus,
    build_message_link,
)
from triak_trade.observability.formatters import format_processing_audit_for_telegram
from triak_trade.observability.redaction import redact, redact_text
from triak_trade.observability.telegram_log_channel import (
    TelegramLogChannelClient,
    TelegramLogSendResult,
)


class ClockLike(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ProcessingAuditResult(BaseModel):
    event: ProcessingAuditEvent
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    formatted_message: str
    log_send_result: dict[str, Any] | None = None


class ProcessingAuditService:
    def __init__(
        self,
        *,
        settings: Settings,
        audit_repository: AuditLogRepository | None = None,
        log_channel_client: TelegramLogChannelClient | None = None,
        clock: ClockLike | None = None,
    ) -> None:
        self.settings = settings
        self.audit_repository = audit_repository
        self.log_channel_client = log_channel_client
        self.clock = clock or SystemClock()

    def process_message_with_audit(
        self,
        raw_message: RawTelegramMessage,
        channel_agent: ChannelAgent,
    ) -> ProcessingAuditResult:
        started = self.clock.now()
        state_before_snapshot = channel_agent.get_context_snapshot()
        try:
            classified = channel_agent.classifier.classify(
                raw_message,
                channel_agent.context,
            )
            actions = channel_agent.ingest_message(raw_message)
            finished = self.clock.now()
            state_after_snapshot = channel_agent.get_context_snapshot()
            event = self._build_success_event(
                raw_message=raw_message,
                started=started,
                finished=finished,
                classifier_name=channel_agent.classifier.__class__.__name__,
                classified=classified,
                actions=actions,
                state_before_snapshot=state_before_snapshot,
                state_after_snapshot=state_after_snapshot,
            )
        except Exception as exc:
            finished = self.clock.now()
            event = self._build_error_event(
                raw_message=raw_message,
                started=started,
                finished=finished,
                state_before_snapshot=state_before_snapshot,
                exc=exc,
            )
            actions = []

        formatted = format_processing_audit_for_telegram(event)
        self._save_event(event)
        send_result = self._send_event_if_enabled(event)
        return ProcessingAuditResult(
            event=event,
            proposed_actions=actions,
            formatted_message=formatted,
            log_send_result=send_result,
        )

    def _build_success_event(
        self,
        *,
        raw_message: RawTelegramMessage,
        started: datetime,
        finished: datetime,
        classifier_name: str,
        classified: Any,
        actions: list[ProposedAction],
        state_before_snapshot: dict[str, Any],
        state_after_snapshot: dict[str, Any],
    ) -> ProcessingAuditEvent:
        parsed = classified.parsed_signal
        first_action = actions[0] if actions else None
        classification = _classification_from_parsed(parsed.action)
        status = _status_from_classification(classification, actions)
        signal_id = _latest_signal_id(state_after_snapshot)
        state_before = _state_label(state_before_snapshot, signal_id)
        state_after = _state_label(state_after_snapshot, signal_id)
        validation_passed = (
            first_action is not None and first_action.action_type.value == "create_order"
        )
        reason = (
            first_action.reason
            if first_action is not None
            else _reason_for_no_action(classification)
        )
        preview = _safe_preview(
            raw_message.text,
            include_full=self.settings.TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT,
            max_chars=self.settings.TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS,
        )
        return ProcessingAuditEvent(
            channel_id=raw_message.channel_id,
            channel_username=raw_message.channel_username,
            message_id=raw_message.message_id,
            message_link=build_message_link(raw_message.channel_username, raw_message.message_id),
            message_date=raw_message.date,
            processing_started_at=started,
            processing_finished_at=finished,
            duration_ms=_duration_ms(started, finished),
            classifier_name=classifier_name,
            classification=classification,
            parsed_action=parsed.action.value.upper(),
            symbol=parsed.symbol,
            side=parsed.side.value.upper(),
            signal_id=signal_id,
            related_signal_id=classified.related_signal_id,
            proposed_action_id=first_action.action_id if first_action is not None else None,
            proposed_action_type=(
                first_action.action_type.value.upper() if first_action is not None else None
            ),
            state_before=state_before,
            state_after=state_after,
            validation_passed=validation_passed,
            risk_increasing=first_action.risk_increasing if first_action is not None else False,
            status=status,
            reason=reason,
            debug_notes=list(classified.debug_notes) + _debug_notes(state_after_snapshot),
            safe_message_preview=preview,
        )

    def _build_error_event(
        self,
        *,
        raw_message: RawTelegramMessage,
        started: datetime,
        finished: datetime,
        state_before_snapshot: dict[str, Any],
        exc: Exception,
    ) -> ProcessingAuditEvent:
        return ProcessingAuditEvent(
            event_type="message_processing_error",
            channel_id=raw_message.channel_id,
            channel_username=raw_message.channel_username,
            message_id=raw_message.message_id,
            message_link=build_message_link(raw_message.channel_username, raw_message.message_id),
            message_date=raw_message.date,
            processing_started_at=started,
            processing_finished_at=finished,
            duration_ms=_duration_ms(started, finished),
            classifier_name="unknown",
            classification="UNKNOWN",
            parsed_action="UNKNOWN",
            signal_id=_latest_signal_id(state_before_snapshot),
            state_before=_state_label(
                state_before_snapshot,
                _latest_signal_id(state_before_snapshot),
            ),
            state_after="error",
            validation_passed=False,
            risk_increasing=False,
            status=ProcessingAuditStatus.ERROR,
            reason="Message processing failed safely.",
            debug_notes=["exception captured by processing audit service"],
            error_type=type(exc).__name__,
            error_message_redacted=redact_text(str(exc)),
            safe_message_preview=_safe_preview(
                raw_message.text,
                include_full=self.settings.TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT,
                max_chars=self.settings.TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS,
            ),
        )

    def _save_event(self, event: ProcessingAuditEvent) -> None:
        if not self.settings.PROCESSING_AUDIT_ENABLED:
            return
        if not self.settings.PROCESSING_AUDIT_STORE_IN_DB:
            return
        if self.audit_repository is None:
            return
        self.audit_repository.add_event(
            event=event.event_type,
            level="ERROR" if event.status is ProcessingAuditStatus.ERROR else "INFO",
            module="processing_audit",
            correlation_id=event.event_id,
            channel_id=event.channel_id,
            signal_id=event.signal_id,
            action_id=event.proposed_action_id,
            message=event.reason or event.status.value,
            payload=redact(event.model_dump(mode="json")),
        )

    def _send_event_if_enabled(self, event: ProcessingAuditEvent) -> dict[str, Any] | None:
        if not self.settings.PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL:
            return None
        if self.log_channel_client is None:
            return None
        result: TelegramLogSendResult = asyncio.run(
            self.log_channel_client.send_event(event, real=True)
        )
        return result.__dict__


def _duration_ms(started: datetime, finished: datetime) -> int:
    return max(0, int((finished - started).total_seconds() * 1000))


def _classification_from_parsed(action: SignalAction) -> str:
    mapping = {
        SignalAction.OPEN: "NEW_SIGNAL",
        SignalAction.CANCEL: "CANCEL",
        SignalAction.CLOSE: "CLOSE",
        SignalAction.UPDATE_SL: "SIGNAL_UPDATE",
        SignalAction.UPDATE_TP: "SIGNAL_UPDATE",
        SignalAction.UPDATE_ENTRY: "SIGNAL_UPDATE",
        SignalAction.UPDATE_LEVERAGE: "SIGNAL_UPDATE",
        SignalAction.IGNORE: "UNRELATED",
        SignalAction.UNKNOWN: "UNKNOWN",
    }
    return mapping.get(action, "UNKNOWN")


def _status_from_classification(
    classification: str,
    actions: list[ProposedAction],
) -> ProcessingAuditStatus:
    if classification in {"UNRELATED", "ADVERTISEMENT", "RESULT_REPORT", "GENERAL_ANALYSIS"}:
        return ProcessingAuditStatus.IGNORED
    if classification in {"AMBIGUOUS", "UNKNOWN"}:
        return ProcessingAuditStatus.AMBIGUOUS
    if actions:
        return ProcessingAuditStatus.SUCCESS
    if classification == "NEW_SIGNAL":
        return ProcessingAuditStatus.SUCCESS
    return ProcessingAuditStatus.REJECTED


def _latest_signal_id(snapshot: dict[str, Any]) -> str | None:
    signals = snapshot.get("signals")
    if not isinstance(signals, dict) or not signals:
        return None
    return sorted(str(key) for key in signals)[-1]


def _state_label(snapshot: dict[str, Any], signal_id: str | None) -> str | None:
    if signal_id is None:
        return None
    signals = snapshot.get("signals")
    if not isinstance(signals, dict):
        return None
    value = signals.get(signal_id)
    if isinstance(value, dict):
        status = value.get("status")
        return str(status) if status is not None else None
    if isinstance(value, str):
        return value
    return None


def _debug_notes(snapshot: dict[str, Any]) -> list[str]:
    events = snapshot.get("debug_events")
    if not isinstance(events, list):
        return []
    notes: list[str] = []
    for item in events[-3:]:
        if isinstance(item, dict):
            reason = item.get("reason")
            status = item.get("status")
            if reason or status:
                notes.append(f"{status or 'event'}: {reason or 'no reason'}")
    return notes


def _reason_for_no_action(classification: str) -> str:
    if classification == "NEW_SIGNAL":
        return "New signal detected and queued for consolidation."
    if classification == "UNRELATED":
        return "Message was ignored because it is not actionable."
    if classification == "UNKNOWN":
        return "Message is ambiguous or not specific enough."
    return "No proposed action was created."


def _safe_preview(text: str | None, *, include_full: bool, max_chars: int) -> str | None:
    if not text:
        return None
    preview = text if include_full else text[:max_chars]
    return redact_text(preview, max_chars=max_chars)


def build_sample_processing_audit_event(settings: Settings) -> ProcessingAuditEvent:
    started = datetime(2026, 6, 2, 18, 30, 3, tzinfo=timezone.utc)
    finished = datetime(2026, 6, 2, 18, 30, 4, 842000, tzinfo=timezone.utc)
    return ProcessingAuditEvent(
        event_id="audit_sample",
        channel_id="tofan_trade",
        channel_username="@Tofan_Trade",
        message_id=12345,
        message_link=build_message_link("@Tofan_Trade", 12345),
        message_date=datetime(2026, 6, 2, 18, 30, tzinfo=timezone.utc),
        processing_started_at=started,
        processing_finished_at=finished,
        duration_ms=_duration_ms(started, finished),
        classifier_name="AIMessageClassifier",
        classification="NEW_SIGNAL",
        parsed_action="OPEN",
        symbol="BTCUSDT",
        side="LONG",
        signal_id="sig_sample",
        related_signal_id=None,
        proposed_action_id="act_sample",
        proposed_action_type="CREATE_ORDER",
        state_before=None,
        state_after="PENDING_CONSOLIDATION",
        validation_passed=True,
        risk_increasing=True,
        status=ProcessingAuditStatus.SUCCESS,
        reason="New BTCUSDT LONG signal detected and queued for consolidation.",
        debug_notes=[
            "Consolidation window started",
            "Waiting for related SL/TP updates",
        ],
        safe_message_preview=_safe_preview(
            "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
            include_full=settings.TELEGRAM_LOG_CHANNEL_SEND_FULL_TEXT,
            max_chars=settings.TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS,
        ),
    )
