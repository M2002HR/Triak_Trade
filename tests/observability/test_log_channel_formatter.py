from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.config.settings import Settings
from triak_trade.observability.events import ProcessingAuditEvent, ProcessingAuditStatus
from triak_trade.observability.formatters import format_processing_audit_for_telegram
from triak_trade.observability.processing_audit import build_sample_processing_audit_event


def test_formatter_includes_required_processing_fields() -> None:
    event = build_sample_processing_audit_event(Settings(_env_file=None))

    text = format_processing_audit_for_telegram(event)

    assert "Message Processing Report" in text
    assert "Source: @Tofan_Trade" in text
    assert "Message Link: https://t.me/Tofan_Trade/12345" in text
    assert "Duration: 1842 ms" in text
    assert "Classifier: AIMessageClassifier" in text
    assert "Type: NEW_SIGNAL" in text
    assert "Parsed Action: OPEN" in text
    assert "After: PENDING_CONSOLIDATION" in text
    assert "Proposed Action: CREATE_ORDER / act_sample" in text


def test_formatter_formats_ignored_message() -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    event = ProcessingAuditEvent(
        channel_id="chan",
        message_id=1,
        message_date=now,
        processing_started_at=now,
        processing_finished_at=now,
        duration_ms=0,
        classifier_name="RegexMessageClassifier",
        classification="ADVERTISEMENT",
        parsed_action="IGNORE",
        status=ProcessingAuditStatus.IGNORED,
        reason="Message appears promotional.",
    )

    text = format_processing_audit_for_telegram(event)

    assert "Status: IGNORED" in text
    assert "Type: ADVERTISEMENT" in text
    assert "Parsed Action: IGNORE" in text
    assert "No trading action was created" in text


def test_formatter_formats_ambiguous_message() -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    event = ProcessingAuditEvent(
        channel_id="chan",
        message_id=1,
        message_date=now,
        processing_started_at=now,
        processing_finished_at=now,
        duration_ms=0,
        classifier_name="AIMessageClassifier",
        classification="AMBIGUOUS",
        parsed_action="UNKNOWN",
        status=ProcessingAuditStatus.AMBIGUOUS,
        reason="Message is trading-related but not specific enough.",
    )

    text = format_processing_audit_for_telegram(event)

    assert "Status: AMBIGUOUS" in text
    assert "No trade action created" in text
    assert "not specific enough" in text


def test_formatter_redacts_fake_secrets() -> None:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    event = ProcessingAuditEvent(
        channel_id="chan",
        message_id=1,
        message_date=now,
        processing_started_at=now,
        processing_finished_at=now,
        duration_ms=0,
        classifier_name="RegexMessageClassifier",
        classification="UNKNOWN",
        parsed_action="UNKNOWN",
        status=ProcessingAuditStatus.ERROR,
        reason="signature=abcdef1234567890abcdef1234567890",
        error_type="RuntimeError",
        error_message_redacted="bot123456789:abcdefghijklmnopqrstuvwxyzABCDEFG",
    )

    text = format_processing_audit_for_telegram(event)

    assert "abcdef123456" not in text
    assert "bot123456789:abcdefghijklmnopqrstuvwxyz" not in text
    assert "***REDACTED***" in text
