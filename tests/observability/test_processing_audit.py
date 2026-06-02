from __future__ import annotations

from datetime import datetime, timezone

import pytest

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.config.settings import Settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.observability.events import (
    ProcessingAuditEvent,
    ProcessingAuditStatus,
    build_message_link,
)
from triak_trade.observability.processing_audit import ProcessingAuditService


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def add_event(self, **kwargs: object) -> int:
        self.events.append(kwargs)
        return 1


class FailingAgent:
    def get_context_snapshot(self) -> dict[str, object]:
        return {"signals": {}}

    def ingest_message(self, raw_message: RawTelegramMessage) -> list[object]:
        raise RuntimeError("failure with token=123456:abcdefghijklmnopqrstuvwxyzABCDE")

    @property
    def classifier(self) -> object:
        class FailingClassifier:
            def classify(self, message: object, context: object) -> object:
                raise RuntimeError("classifier failed")

        return FailingClassifier()

    @property
    def context(self) -> object:
        return object()


def settings() -> Settings:
    return Settings(
        _env_file=None,
        PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=False,
        PROCESSING_AUDIT_STORE_IN_DB=True,
        TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS=80,
    )


def raw_message(text: str) -> RawTelegramMessage:
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    return RawTelegramMessage(
        channel_id="tofan_trade",
        channel_username="@Tofan_Trade",
        message_id=123,
        text=text,
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )


def test_message_link_builder_public_channel() -> None:
    assert build_message_link("@Tofan_Trade", 123) == "https://t.me/Tofan_Trade/123"


def test_message_link_builder_missing_username() -> None:
    assert build_message_link(None, 123) is None
    assert build_message_link("", 123) is None


def test_processing_audit_event_for_valid_signal() -> None:
    cfg = settings()
    clock = FakeClock(datetime(2026, 6, 2, tzinfo=timezone.utc))
    agent = ChannelAgent(channel_id="tofan_trade", settings=cfg, clock=clock)
    service = ProcessingAuditService(settings=cfg, clock=clock)

    result = service.process_message_with_audit(
        raw_message("BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000"),
        agent,
    )

    assert result.event.status is ProcessingAuditStatus.SUCCESS
    assert result.event.classification == "NEW_SIGNAL"
    assert result.event.parsed_action == "OPEN"
    assert result.event.signal_id is not None
    assert result.event.state_before is None
    assert result.event.state_after == "pending_consolidation"
    assert result.proposed_actions == []


def test_processing_audit_event_for_ignored_ad() -> None:
    cfg = settings()
    clock = FakeClock(datetime(2026, 6, 2, tzinfo=timezone.utc))
    agent = ChannelAgent(channel_id="tofan_trade", settings=cfg, clock=clock)
    service = ProcessingAuditService(settings=cfg, clock=clock)

    result = service.process_message_with_audit(raw_message("Join VIP promo giveaway now"), agent)

    assert result.event.status is ProcessingAuditStatus.IGNORED
    assert result.event.parsed_action == "IGNORE"
    assert result.proposed_actions == []


def test_processing_audit_error_event_redacts_message() -> None:
    cfg = settings()
    clock = FakeClock(datetime(2026, 6, 2, tzinfo=timezone.utc))
    service = ProcessingAuditService(settings=cfg, clock=clock)

    result = service.process_message_with_audit(raw_message("BTC looking good"), FailingAgent())  # type: ignore[arg-type]

    assert result.event.status is ProcessingAuditStatus.ERROR
    assert result.event.error_type == "RuntimeError"
    assert "123456:abcdefghijklmnopqrstuvwxyz" not in result.event.error_message_redacted


def test_processing_audit_can_save_to_audit_repo() -> None:
    cfg = settings()
    repo = FakeAuditRepo()
    clock = FakeClock(datetime(2026, 6, 2, tzinfo=timezone.utc))
    agent = ChannelAgent(channel_id="tofan_trade", settings=cfg, clock=clock)
    service = ProcessingAuditService(
        settings=cfg,
        audit_repository=repo,  # type: ignore[arg-type]
        clock=clock,
    )

    service.process_message_with_audit(
        raw_message("BTCUSDT LONG MARKET SL: 67400 TP: 69000"),
        agent,
    )

    assert len(repo.events) == 1
    assert repo.events[0]["event"] == "message_processed"
    assert repo.events[0]["module"] == "processing_audit"


def test_processing_audit_event_rejects_negative_duration() -> None:
    cfg = settings()
    assert cfg.TELEGRAM_LOG_CHANNEL_MAX_TEXT_CHARS == 80
    with pytest.raises(ValueError):
        from triak_trade.observability.processing_audit import build_sample_processing_audit_event

        sample = build_sample_processing_audit_event(cfg)
        data = sample.model_dump()
        data["duration_ms"] = -1
        ProcessingAuditEvent.model_validate(data)
