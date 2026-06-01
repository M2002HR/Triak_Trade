from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tempfile import NamedTemporaryFile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from triak_trade.db.base import Base
from triak_trade.db.repositories import (
    AdminDecisionRepository,
    AuditLogRepository,
    CandleRepository,
    LLMCallLogRepository,
    ProposedActionRepository,
    SignalRepository,
    TelegramMessageRepository,
)
from triak_trade.domain.enums import (
    AdminDecisionType,
    CandleSource,
    EntryType,
    MarketType,
    ProposedActionType,
    SignalAction,
    SignalStatus,
    TradeSide,
)
from triak_trade.domain.models import (
    AdminDecision,
    Candle,
    ParsedSignal,
    ProposedAction,
    RawTelegramMessage,
    SignalState,
)


def _session() -> Session:
    tmp = NamedTemporaryFile(suffix=".db")
    engine = create_engine(f"sqlite+pysqlite:///{tmp.name}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.info["_tmpfile"] = tmp
    return session


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _raw_message(
    *,
    text: str = "OPEN BTC",
    edited_at: datetime | None = None,
    deleted: bool = False,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id="chan-1",
        channel_username="chan",
        message_id=100,
        text=text,
        date=_now(),
        edited_at=edited_at,
        deleted=deleted,
        reply_to_msg_id=None,
        raw_payload={"k": "v"},
    )


def _signal_state() -> SignalState:
    now = _now()
    parsed = ParsedSignal(
        action=SignalAction.OPEN,
        market=MarketType.FUTURES,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        entry_type=EntryType.LIMIT,
        entry_low=Decimal("100"),
        entry_high=Decimal("101"),
        stop_loss=Decimal("95"),
        take_profits=[Decimal("110")],
        leverage=3,
        confidence=Decimal("0.8"),
        invalid_reason=None,
        source_channel_id="chan-1",
        source_message_id=100,
        parser_version="v1",
    )
    return SignalState(
        signal_id="sig-1",
        channel_id="chan-1",
        status=SignalStatus.PENDING_CONSOLIDATION,
        created_from_message_id=100,
        related_message_ids=[100],
        current_signal=parsed,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )


def _action(*, action_id: str = "act-1") -> ProposedAction:
    return ProposedAction(
        action_id=action_id,
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="sig-1",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.75"),
        reason="entry",
        payload={"price": "100"},
        created_at=_now(),
    )


def test_telegram_message_versioning() -> None:
    session = _session()
    repo = TelegramMessageRepository(session)
    base = _raw_message()

    first_id = repo.add_raw_message(base)
    dup_id = repo.add_raw_message(base)
    assert first_id == dup_id

    edited_id = repo.add_message_version(
        _raw_message(
            text="OPEN BTC UPDATED",
            edited_at=base.date + timedelta(seconds=1),
        )
    )
    deleted_id = repo.add_message_version(_raw_message(text="OPEN BTC UPDATED", deleted=True))

    assert edited_id != first_id
    assert deleted_id != edited_id

    latest = repo.get_latest_message("chan-1", 100)
    assert latest is not None
    assert latest.deleted is True

    listed = repo.list_messages("chan-1")
    assert len(listed) == 3


def test_signal_state_roundtrip() -> None:
    session = _session()
    repo = SignalRepository(session)
    state = _signal_state()

    repo.save_signal(state)
    read = repo.get_signal(state.signal_id)

    assert read is not None
    assert read.signal_id == state.signal_id
    assert read.current_signal is not None
    assert read.current_signal.entry_low == Decimal("100")


def test_proposed_action_and_pending_filter() -> None:
    session = _session()
    actions = ProposedActionRepository(session)
    decisions = AdminDecisionRepository(session)

    action = _action()
    actions.save_action(action)

    pending = actions.list_pending_admin_actions()
    assert len(pending) == 1
    assert pending[0].requires_admin_approval is True

    decision = AdminDecision(
        action_id=action.action_id,
        decision=AdminDecisionType.APPROVE,
        admin_user_id=1,
        reason="ok",
        decided_at=_now(),
    )
    decisions.save_decision(decision)

    loaded_decision = decisions.get_decision(action.action_id)
    assert loaded_decision is not None
    assert loaded_decision.decision is AdminDecisionType.APPROVE
    assert len(actions.list_pending_admin_actions()) == 0


def test_candle_upsert_and_query_and_decimal_roundtrip() -> None:
    session = _session()
    repo = CandleRepository(session)
    now = _now()

    candles = [
        Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now,
            close_time=now + timedelta(minutes=1),
            open=Decimal("100.123456"),
            high=Decimal("101.123456"),
            low=Decimal("99.123456"),
            close=Decimal("100.923456"),
            volume=Decimal("12.500000"),
            source=CandleSource.FIXTURE,
        ),
        Candle(
            symbol="BTCUSDT",
            interval="1m",
            open_time=now + timedelta(minutes=1),
            close_time=now + timedelta(minutes=2),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=Decimal("11"),
            source=CandleSource.FIXTURE,
        ),
    ]

    assert repo.upsert_candles(candles) == 2
    assert repo.upsert_candles(candles) == 0

    result = repo.list_candles(
        "btcusdt",
        "1m",
        now - timedelta(minutes=1),
        now + timedelta(minutes=3),
    )
    assert len(result) == 2
    assert result[0].open == Decimal("100.123456")
    assert result[0].open_time < result[1].open_time


def test_audit_log_and_llm_log_roundtrip() -> None:
    session = _session()
    audit_repo = AuditLogRepository(session)
    llm_repo = LLMCallLogRepository(session)

    audit_id = audit_repo.add_event(
        event="signal_parsed",
        level="INFO",
        module="tests",
        correlation_id="cid-1",
        channel_id="chan-1",
        signal_id="sig-1",
        action_id="act-1",
        message="parsed",
        payload={"a": 1},
    )
    assert audit_id > 0
    assert len(audit_repo.list_recent()) == 1

    llm_id = llm_repo.add_call_log(
        provider="gemini",
        model="test-model",
        success=False,
        latency_ms=120,
        error_type="timeout",
        prompt_hash="abc",
        response_hash="def",
        metadata={"note": "hash only"},
    )
    assert llm_id > 0
    logs = llm_repo.list_recent()
    assert len(logs) == 1
    assert "prompt" not in logs[0]["metadata"]
