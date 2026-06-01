"""Database repositories."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from triak_trade.db.models import (
    AdminDecisionORM,
    AuditLogORM,
    CandleORM,
    LLMCallLogORM,
    ProposedActionORM,
    SignalORM,
    TelegramMessageORM,
)
from triak_trade.domain.enums import (
    AdminDecisionType,
    CandleSource,
    ProposedActionType,
    SignalStatus,
)
from triak_trade.domain.models import (
    AdminDecision,
    Candle,
    ParsedSignal,
    ProposedAction,
    RawTelegramMessage,
    SignalState,
)


def _model_to_json_payload(model: Any) -> dict[str, Any]:
    return cast(dict[str, Any], model.model_dump(mode="json"))


def _parse_signal_payload(payload: dict[str, Any]) -> ParsedSignal:
    return ParsedSignal.model_validate(payload)


class TelegramMessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _latest_row(self, channel_id: str, message_id: int) -> TelegramMessageORM | None:
        stmt = (
            select(TelegramMessageORM)
            .where(
                TelegramMessageORM.channel_id == channel_id,
                TelegramMessageORM.message_id == message_id,
            )
            .order_by(desc(TelegramMessageORM.version))
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _equal_content(row: TelegramMessageORM, raw: RawTelegramMessage) -> bool:
        return (
            row.channel_username == raw.channel_username
            and row.text == raw.text
            and row.edited_at == raw.edited_at
            and row.deleted == raw.deleted
            and row.reply_to_msg_id == raw.reply_to_msg_id
            and row.raw_payload == raw.raw_payload
        )

    def _insert(self, raw: RawTelegramMessage, version: int) -> int:
        row = TelegramMessageORM(
            channel_id=raw.channel_id,
            channel_username=raw.channel_username,
            message_id=raw.message_id,
            version=version,
            text=raw.text,
            date=raw.date,
            edited_at=raw.edited_at,
            deleted=raw.deleted,
            reply_to_msg_id=raw.reply_to_msg_id,
            raw_payload=raw.raw_payload,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def add_raw_message(self, raw: RawTelegramMessage) -> int:
        latest = self._latest_row(raw.channel_id, raw.message_id)
        if latest is None:
            return self._insert(raw, version=1)
        if self._equal_content(latest, raw):
            return latest.id
        return self._insert(raw, version=latest.version + 1)

    def add_message_version(self, raw: RawTelegramMessage) -> int:
        return self.add_raw_message(raw)

    def get_latest_message(self, channel_id: str, message_id: int) -> RawTelegramMessage | None:
        row = self._latest_row(channel_id, message_id)
        if row is None:
            return None
        return RawTelegramMessage(
            channel_id=row.channel_id,
            channel_username=row.channel_username,
            message_id=row.message_id,
            text=row.text,
            date=row.date,
            edited_at=row.edited_at,
            deleted=row.deleted,
            reply_to_msg_id=row.reply_to_msg_id,
            raw_payload=row.raw_payload,
        )

    def list_messages(
        self,
        channel_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[RawTelegramMessage]:
        stmt = select(TelegramMessageORM).where(TelegramMessageORM.channel_id == channel_id)
        if start is not None:
            stmt = stmt.where(TelegramMessageORM.date >= start)
        if end is not None:
            stmt = stmt.where(TelegramMessageORM.date <= end)
        stmt = stmt.order_by(TelegramMessageORM.date.asc(), TelegramMessageORM.version.asc())
        rows = self.session.execute(stmt).scalars().all()
        return [
            RawTelegramMessage(
                channel_id=row.channel_id,
                channel_username=row.channel_username,
                message_id=row.message_id,
                text=row.text,
                date=row.date,
                edited_at=row.edited_at,
                deleted=row.deleted,
                reply_to_msg_id=row.reply_to_msg_id,
                raw_payload=row.raw_payload,
            )
            for row in rows
        ]


class SignalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_signal(self, state: SignalState) -> None:
        row = self.session.execute(
            select(SignalORM).where(SignalORM.signal_id == state.signal_id)
        ).scalar_one_or_none()

        payload = _model_to_json_payload(state.current_signal) if state.current_signal else None
        if row is None:
            row = SignalORM(
                signal_id=state.signal_id,
                channel_id=state.channel_id,
                status=state.status.value,
                created_from_message_id=state.created_from_message_id,
                related_message_ids=state.related_message_ids,
                current_signal=payload,
                version=state.version,
                created_at=state.created_at,
                updated_at=state.updated_at,
                expires_at=state.expires_at,
            )
            self.session.add(row)
            self.session.flush()
            return

        row.channel_id = state.channel_id
        row.status = state.status.value
        row.created_from_message_id = state.created_from_message_id
        row.related_message_ids = state.related_message_ids
        row.current_signal = payload
        row.version = state.version
        row.created_at = state.created_at
        row.updated_at = state.updated_at
        row.expires_at = state.expires_at
        self.session.flush()

    def get_signal(self, signal_id: str) -> SignalState | None:
        row = self.session.execute(
            select(SignalORM).where(SignalORM.signal_id == signal_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        parsed = _parse_signal_payload(row.current_signal) if row.current_signal else None
        return SignalState(
            signal_id=row.signal_id,
            channel_id=row.channel_id,
            status=SignalStatus(row.status),
            created_from_message_id=row.created_from_message_id,
            related_message_ids=row.related_message_ids,
            current_signal=parsed,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
        )

    def list_channel_signals(self, channel_id: str) -> list[SignalState]:
        rows = self.session.execute(
            select(SignalORM)
            .where(SignalORM.channel_id == channel_id)
            .order_by(SignalORM.created_at.asc())
        ).scalars().all()
        result: list[SignalState] = []
        for row in rows:
            parsed = _parse_signal_payload(row.current_signal) if row.current_signal else None
            result.append(
                SignalState(
                    signal_id=row.signal_id,
                    channel_id=row.channel_id,
                    status=SignalStatus(row.status),
                    created_from_message_id=row.created_from_message_id,
                    related_message_ids=row.related_message_ids,
                    current_signal=parsed,
                    version=row.version,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    expires_at=row.expires_at,
                )
            )
        return result


class ProposedActionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_action(self, action: ProposedAction) -> None:
        row = self.session.execute(
            select(ProposedActionORM).where(ProposedActionORM.action_id == action.action_id)
        ).scalar_one_or_none()
        if row is None:
            row = ProposedActionORM(action_id=action.action_id)
            self.session.add(row)

        row.action_type = action.action_type.value
        row.signal_id = action.signal_id
        row.risk_increasing = action.risk_increasing
        row.requires_admin_approval = action.requires_admin_approval
        row.confidence = action.confidence
        row.reason = action.reason
        row.payload = action.payload
        row.created_at = action.created_at
        self.session.flush()

    def get_action(self, action_id: str) -> ProposedAction | None:
        row = self.session.execute(
            select(ProposedActionORM).where(ProposedActionORM.action_id == action_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return ProposedAction(
            action_id=row.action_id,
            action_type=ProposedActionType(row.action_type),
            signal_id=row.signal_id,
            risk_increasing=row.risk_increasing,
            requires_admin_approval=row.requires_admin_approval,
            confidence=Decimal(str(row.confidence)),
            reason=row.reason,
            payload=row.payload,
            created_at=row.created_at,
        )

    def list_pending_admin_actions(self) -> list[ProposedAction]:
        stmt = (
            select(ProposedActionORM)
            .where(ProposedActionORM.requires_admin_approval.is_(True))
            .where(~ProposedActionORM.action_id.in_(select(AdminDecisionORM.action_id)))
            .order_by(ProposedActionORM.created_at.asc())
        )
        rows = self.session.execute(stmt).scalars().all()
        return [
            ProposedAction(
                action_id=row.action_id,
                action_type=ProposedActionType(row.action_type),
                signal_id=row.signal_id,
                risk_increasing=row.risk_increasing,
                requires_admin_approval=row.requires_admin_approval,
                confidence=Decimal(str(row.confidence)),
                reason=row.reason,
                payload=row.payload,
                created_at=row.created_at,
            )
            for row in rows
        ]


class AdminDecisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_decision(self, decision: AdminDecision) -> None:
        row = self.session.execute(
            select(AdminDecisionORM).where(AdminDecisionORM.action_id == decision.action_id)
        ).scalar_one_or_none()
        if row is None:
            row = AdminDecisionORM(action_id=decision.action_id)
            self.session.add(row)

        row.decision = decision.decision.value
        row.admin_user_id = decision.admin_user_id
        row.reason = decision.reason
        row.decided_at = decision.decided_at
        self.session.flush()

    def get_decision(self, action_id: str) -> AdminDecision | None:
        row = self.session.execute(
            select(AdminDecisionORM).where(AdminDecisionORM.action_id == action_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return AdminDecision(
            action_id=row.action_id,
            decision=AdminDecisionType(row.decision),
            admin_user_id=row.admin_user_id,
            reason=row.reason,
            decided_at=row.decided_at,
        )


class CandleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_candles(self, candles: list[Candle]) -> int:
        inserted = 0
        for candle in candles:
            exists = self.session.execute(
                select(CandleORM.id).where(
                    CandleORM.symbol == candle.symbol,
                    CandleORM.interval == candle.interval,
                    CandleORM.open_time == candle.open_time,
                    CandleORM.source == candle.source.value,
                )
            ).scalar_one_or_none()
            if exists is not None:
                continue
            self.session.add(
                CandleORM(
                    symbol=candle.symbol,
                    interval=candle.interval,
                    open_time=candle.open_time,
                    close_time=candle.close_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    source=candle.source.value,
                )
            )
            inserted += 1
        self.session.flush()
        return inserted

    def list_candles(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        rows = self.session.execute(
            select(CandleORM)
            .where(
                and_(
                    CandleORM.symbol == symbol.upper().strip(),
                    CandleORM.interval == interval,
                    CandleORM.open_time >= start,
                    CandleORM.open_time <= end,
                )
            )
            .order_by(CandleORM.open_time.asc())
        ).scalars().all()

        return [
            Candle(
                symbol=row.symbol,
                interval=row.interval,
                open_time=row.open_time,
                close_time=row.close_time,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=Decimal(str(row.volume)),
                source=CandleSource(row.source),
            )
            for row in rows
        ]


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_event(
        self,
        *,
        event: str,
        level: str,
        module: str | None,
        correlation_id: str | None,
        channel_id: str | None,
        signal_id: str | None,
        action_id: str | None,
        message: str,
        payload: dict[str, Any],
    ) -> int:
        row = AuditLogORM(
            event=event,
            level=level,
            module=module,
            correlation_id=correlation_id,
            channel_id=channel_id,
            signal_id=signal_id,
            action_id=action_id,
            message=message,
            payload=payload,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(AuditLogORM).order_by(AuditLogORM.created_at.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "event": row.event,
                "level": row.level,
                "message": row.message,
                "payload": row.payload,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]


class LLMCallLogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_call_log(
        self,
        *,
        provider: str,
        model: str | None,
        success: bool,
        latency_ms: int | None,
        error_type: str | None,
        prompt_hash: str | None,
        response_hash: str | None,
        metadata: dict[str, Any],
    ) -> int:
        row = LLMCallLogORM(
            provider=provider,
            model=model,
            success=success,
            latency_ms=latency_ms,
            error_type=error_type,
            prompt_hash=prompt_hash,
            response_hash=response_hash,
            metadata_json=metadata,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(LLMCallLogORM).order_by(LLMCallLogORM.created_at.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "success": row.success,
                "latency_ms": row.latency_ms,
                "error_type": row.error_type,
                "prompt_hash": row.prompt_hash,
                "response_hash": row.response_hash,
                "metadata": row.metadata_json,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
