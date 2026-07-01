"""Database repositories."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from triak_trade.core.logging import log_event
from triak_trade.db.models import (
    AuditLogORM,
    CandleORM,
    LiveMessageTraceORM,
    LiveSessionORM,
    LiveSignalSnapshotORM,
    LiveTradeORM,
    LLMCallLogORM,
    SignalORM,
    TelegramMessageORM,
)
from triak_trade.domain.enums import CandleSource, SignalStatus
from triak_trade.domain.models import (
    Candle,
    ParsedSignal,
    RawTelegramMessage,
    SignalState,
)
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSignalSnapshot,
    LiveTrade,
)

_log = logging.getLogger(__name__)


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
            row_id = self._insert(raw, version=1)
            log_event(
                _log,
                logging.INFO,
                "telegram_message_repository.inserted",
                channel_id=raw.channel_id,
                message_id=raw.message_id,
                version=1,
                row_id=row_id,
            )
            return row_id
        if self._equal_content(latest, raw):
            log_event(
                _log,
                logging.DEBUG,
                "telegram_message_repository.deduped",
                channel_id=raw.channel_id,
                message_id=raw.message_id,
                version=latest.version,
                row_id=latest.id,
            )
            return latest.id
        new_version = latest.version + 1
        row_id = self._insert(raw, version=new_version)
        log_event(
            _log,
            logging.INFO,
            "telegram_message_repository.version_inserted",
            channel_id=raw.channel_id,
            message_id=raw.message_id,
            version=new_version,
            row_id=row_id,
        )
        return row_id

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
            log_event(
                _log,
                logging.INFO,
                "signal_repository.inserted",
                signal_id=state.signal_id,
                channel_id=state.channel_id,
                status=state.status.value,
                version=state.version,
            )
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
        log_event(
            _log,
            logging.DEBUG,
            "signal_repository.updated",
            signal_id=state.signal_id,
            channel_id=state.channel_id,
            status=state.status.value,
            version=state.version,
        )

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
        log_event(
            _log,
            logging.INFO,
            "candle_repository.upserted",
            received_count=len(candles),
            inserted_count=inserted,
        )
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


class LiveTradingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_session(self, live_session: LiveSession) -> None:
        row = self.session.execute(
            select(LiveSessionORM).where(LiveSessionORM.session_id == live_session.session_id)
        ).scalar_one_or_none()
        payload = _model_to_json_payload(live_session)
        if row is None:
            row = LiveSessionORM(session_id=live_session.session_id)
            self.session.add(row)
        row.status = live_session.status
        row.trading_mode = live_session.trading_mode
        row.primary_channel_id = live_session.channels[0] if live_session.channels else None
        row.started_at = live_session.started_at
        row.stopped_at = live_session.stopped_at
        row.last_update_at = live_session.last_update_at
        row.payload = payload
        self.session.flush()

    def load_session(self, session_id: str) -> LiveSession | None:
        row = self.session.execute(
            select(LiveSessionORM).where(LiveSessionORM.session_id == session_id)
        ).scalar_one_or_none()
        if row is None:
            return None
        return LiveSession.model_validate(row.payload)

    def list_sessions(self, limit: int = 20) -> list[LiveSession]:
        rows = self.session.execute(
            select(LiveSessionORM)
            .order_by(LiveSessionORM.last_update_at.desc())
            .limit(limit)
        ).scalars().all()
        return [LiveSession.model_validate(row.payload) for row in rows]

    def save_trade(self, trade: LiveTrade) -> None:
        row = self.session.execute(
            select(LiveTradeORM).where(LiveTradeORM.trade_id == trade.trade_id)
        ).scalar_one_or_none()
        payload = _model_to_json_payload(trade)
        if row is None:
            row = LiveTradeORM(trade_id=trade.trade_id)
            self.session.add(row)
        row.session_id = trade.session_id
        row.signal_id = trade.signal_id
        row.channel_id = trade.channel_id
        row.symbol = trade.symbol
        row.side = trade.side
        row.status = trade.status
        row.is_open = trade.is_open
        row.opened_at = trade.opened_at
        row.closed_at = trade.closed_at
        row.updated_at = trade.updated_at
        row.payload = payload
        self.session.flush()

    def load_trade(self, session_id: str, trade_id: str) -> LiveTrade | None:
        row = self.session.execute(
            select(LiveTradeORM).where(
                LiveTradeORM.session_id == session_id,
                LiveTradeORM.trade_id == trade_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return LiveTrade.model_validate(row.payload)

    def list_trades(self, session_id: str, limit: int = 200) -> list[LiveTrade]:
        rows = self.session.execute(
            select(LiveTradeORM)
            .where(LiveTradeORM.session_id == session_id)
            .order_by(LiveTradeORM.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        return [LiveTrade.model_validate(row.payload) for row in rows]

    def delete_trade(self, session_id: str, trade_id: str) -> bool:
        row = self.session.execute(
            select(LiveTradeORM).where(
                LiveTradeORM.session_id == session_id,
                LiveTradeORM.trade_id == trade_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def save_message_trace(self, session_id: str, trace: LiveMessageTrace) -> None:
        row = self.session.execute(
            select(LiveMessageTraceORM).where(
                LiveMessageTraceORM.session_id == session_id,
                LiveMessageTraceORM.channel_id == trace.channel_id,
                LiveMessageTraceORM.message_id == trace.message_id,
            )
        ).scalar_one_or_none()
        payload = _model_to_json_payload(trace)
        if row is None:
            row = LiveMessageTraceORM(
                session_id=session_id,
                channel_id=trace.channel_id,
                message_id=trace.message_id,
            )
            self.session.add(row)
        row.signal_id = trace.signal_id
        row.trade_id = trace.trade_id
        row.final_status = trace.final_status
        row.message_date = trace.message_date
        row.received_at = trace.received_at
        row.payload = payload
        self.session.flush()

    def list_message_traces(self, session_id: str, limit: int = 200) -> list[LiveMessageTrace]:
        rows = self.session.execute(
            select(LiveMessageTraceORM)
            .where(LiveMessageTraceORM.session_id == session_id)
            .order_by(LiveMessageTraceORM.received_at.desc())
            .limit(limit)
        ).scalars().all()
        return [LiveMessageTrace.model_validate(row.payload) for row in rows]

    def delete_message_trace(self, session_id: str, message_id: int, channel_id: str) -> bool:
        row = self.session.execute(
            select(LiveMessageTraceORM).where(
                LiveMessageTraceORM.session_id == session_id,
                LiveMessageTraceORM.channel_id == channel_id,
                LiveMessageTraceORM.message_id == message_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def save_signal_snapshot(self, session_id: str, signal: LiveSignalSnapshot) -> None:
        row = self.session.execute(
            select(LiveSignalSnapshotORM).where(
                LiveSignalSnapshotORM.session_id == session_id,
                LiveSignalSnapshotORM.signal_id == signal.signal_id,
            )
        ).scalar_one_or_none()
        payload = _model_to_json_payload(signal)
        if row is None:
            row = LiveSignalSnapshotORM(session_id=session_id, signal_id=signal.signal_id)
            self.session.add(row)
        row.channel_id = signal.channel_id
        row.status = signal.status
        row.status_group = signal.status_group
        row.trade_id = signal.trade_id
        row.symbol = signal.symbol
        row.updated_at = signal.updated_at
        row.payload = payload
        self.session.flush()

    def load_signal_snapshot(self, session_id: str, signal_id: str) -> LiveSignalSnapshot | None:
        row = self.session.execute(
            select(LiveSignalSnapshotORM).where(
                LiveSignalSnapshotORM.session_id == session_id,
                LiveSignalSnapshotORM.signal_id == signal_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return LiveSignalSnapshot.model_validate(row.payload)

    def list_signal_snapshots(
        self,
        session_id: str,
        limit: int = 200,
    ) -> list[LiveSignalSnapshot]:
        rows = self.session.execute(
            select(LiveSignalSnapshotORM)
            .where(LiveSignalSnapshotORM.session_id == session_id)
            .order_by(LiveSignalSnapshotORM.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        return [LiveSignalSnapshot.model_validate(row.payload) for row in rows]

    def delete_signal_snapshot(self, session_id: str, signal_id: str) -> bool:
        row = self.session.execute(
            select(LiveSignalSnapshotORM).where(
                LiveSignalSnapshotORM.session_id == session_id,
                LiveSignalSnapshotORM.signal_id == signal_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def delete_session(self, session_id: str) -> bool:
        removed = False
        session_row = self.session.execute(
            select(LiveSessionORM).where(LiveSessionORM.session_id == session_id)
        ).scalar_one_or_none()
        if session_row is not None:
            self.session.delete(session_row)
            removed = True
        for model in (LiveTradeORM, LiveMessageTraceORM, LiveSignalSnapshotORM):
            rows = self.session.execute(
                select(model).where(model.session_id == session_id)
            ).scalars().all()
            for row in rows:
                self.session.delete(row)
                removed = True
        self.session.flush()
        return removed
