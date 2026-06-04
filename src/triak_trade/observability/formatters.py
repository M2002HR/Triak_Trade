"""English Telegram log-channel formatters."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from triak_trade.observability.events import ProcessingAuditEvent, ProcessingAuditStatus
from triak_trade.observability.redaction import redact_text


def format_processing_audit_for_telegram(event: ProcessingAuditEvent) -> str:
    """Format a processing audit event for Telegram log channels in English."""

    lines = [
        "Message Processing Report",
        "",
        f"Source: {_tg(_source_label(event))}",
        f"Message ID: {event.message_id}",
        f"Message Link: {_tg(event.message_link or 'not available')}",
        f"Message Time: {_format_dt(event.message_date)}",
        "",
        "Processing:",
        f"Started: {_format_dt(event.processing_started_at)}",
        f"Finished: {_format_dt(event.processing_finished_at)}",
        f"Duration: {event.duration_ms} ms",
        f"Status: {_tg(event.status.value)}",
        "",
        "Classification:",
        f"Classifier: {_tg(event.classifier_name)}",
        f"Type: {_tg(event.classification)}",
        f"Parsed Action: {_tg(event.parsed_action)}",
        f"Symbol: {_tg(event.symbol or 'none')}",
        f"Side: {_tg(event.side or 'none')}",
        "",
        "Signal State:",
        f"Before: {_tg(event.state_before or 'none')}",
        f"After: {_tg(event.state_after or 'none')}",
        f"Signal ID: {_tg(event.signal_id or 'none')}",
        f"Related Signal: {_tg(event.related_signal_id or 'none')}",
        "",
        "Decision:",
        f"Validation: {_bool_label(event.validation_passed)}",
        f"Admin Approval Required: {_bool_label(event.admin_approval_required)}",
        f"Risk Increasing: {_bool_label(event.risk_increasing)}",
        f"Proposed Action: {_tg(_proposed_label(event))}",
    ]
    if event.status is ProcessingAuditStatus.IGNORED:
        lines.append("Decision Summary: No trading action was created.")
    if event.status is ProcessingAuditStatus.AMBIGUOUS:
        lines.append("Decision Summary: No trade action created; admin review may be required.")
    if event.error_type is not None:
        lines.extend(["", "Error:", f"Type: {_tg(event.error_type)}"])
        if event.error_message_redacted:
            lines.append(f"Message: {_tg(event.error_message_redacted)}")
    if event.safe_message_preview:
        lines.extend(["", "Message Preview:", _tg(event.safe_message_preview)])
    if event.reason:
        lines.extend(["", "Reason:", _tg(event.reason)])
    if event.debug_notes:
        lines.append("")
        lines.append("Debug Notes:")
        lines.extend(f"- {_tg(note)}" for note in event.debug_notes)
    return redact_text("\n".join(lines))


def _source_label(event: ProcessingAuditEvent) -> str:
    if event.channel_username:
        username = event.channel_username
        return username if username.startswith("@") else f"@{username}"
    return event.channel_id


def _format_dt(value: datetime) -> str:
    as_utc = value.astimezone(timezone.utc)
    return as_utc.strftime("%Y-%m-%d %H:%M:%S UTC")


def _bool_label(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _proposed_label(event: ProcessingAuditEvent) -> str:
    if event.proposed_action_type is None and event.proposed_action_id is None:
        return "none"
    return f"{event.proposed_action_type or 'unknown'} / {event.proposed_action_id or 'none'}"


def _tg(value: object) -> str:
    return escape(str(value), quote=False)
