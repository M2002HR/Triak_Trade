"""Real Telegram + public market-data backtest runner."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from triak_trade.agents.classifier import (
    ClassifiedMessage,
    MessageClassifier,
    RegexMessageClassifier,
)
from triak_trade.agents.context import ChannelContext
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.backtesting.correlation import resolve_related_signal_id
from triak_trade.backtesting.directives import (
    apply_text_directive_action,
    build_ignored_signal,
    detect_move_stop_to_entry,
    detect_tp_list_update,
    extract_close_fraction,
    normalize_related_signal_action,
)
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestEvent, BacktestRequest
from triak_trade.backtesting.report import report_to_json, report_to_markdown_summary
from triak_trade.backtesting.report_store import BacktestReportStore
from triak_trade.backtesting.simulator import SimulationSnapshot
from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.backtesting.strategies.registry import load_strategy
from triak_trade.backtesting.symbol_mapper import (
    market_symbol_candidates,
    normalize_market_symbol,
)
from triak_trade.backtesting.telegram_source import BacktestTelegramSource
from triak_trade.config.settings import Settings
from triak_trade.core.time import TEHRAN_TZ
from triak_trade.domain.enums import BacktestFillPolicy, SignalAction, SignalStatus
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import ParsedSignal, RawTelegramMessage, SignalState
from triak_trade.market_data.factory import build_backtest_market_data_provider
from triak_trade.market_data.interfaces import MarketDataProvider
from triak_trade.observability.events import build_message_link
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.client import TelegramClientInterface
from triak_trade.telegram.telethon_client import TelegramCredentialError, TelethonTelegramClient


class RealBacktestMessageStage(BaseModel):
    key: str
    label: str
    status: Literal["pending", "active", "completed", "failed", "skipped"] = "pending"
    detail: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class RealBacktestMessageTrace(BaseModel):
    message_id: int
    channel_id: str
    channel_username: str | None = None
    message_link: str | None = None
    message_date: datetime
    full_text: str | None = None
    preview_text: str = ""
    classification: str | None = None
    parsed_action: str | None = None
    symbol: str | None = None
    side: str | None = None
    confidence: str | None = None
    signal_id: str | None = None
    final_status: str = "queued"
    result_summary: str | None = None
    current_stage: str = "received"
    last_updated_at: datetime
    processing_duration_ms: int | None = None
    debug_notes: list[str] = Field(default_factory=list)
    stages: list[RealBacktestMessageStage] = Field(default_factory=list)

    _stage_index: dict[str, int] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._stage_index = {stage.key: index for index, stage in enumerate(self.stages)}

    def refresh_processing_duration(self, now: datetime) -> None:
        started_at = next((stage.started_at for stage in self.stages if stage.started_at), None)
        if started_at is None:
            self.processing_duration_ms = None
            return
        active_stage = next((stage for stage in self.stages if stage.status == "active"), None)
        if active_stage is not None:
            end_at = now
        else:
            finished_stages = [stage.finished_at for stage in self.stages if stage.finished_at]
            end_at = max(finished_stages) if finished_stages else now
        self.processing_duration_ms = max(
            0,
            int((end_at - started_at).total_seconds() * 1000),
        )


class RealBacktestProgressEvent(BaseModel):
    event_type: Literal["run", "message"]
    timestamp: datetime
    phase: str
    status: Literal["queued", "running", "completed", "failed"]
    summary: str
    current_message_id: int | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    live_metrics: dict[str, str] | None = None
    live_signals: list[dict[str, Any]] | None = None
    trace: RealBacktestMessageTrace | None = None


class RealBacktestReadiness(BaseModel):
    ready: bool
    issues: list[str] = Field(default_factory=list)
    real_backtest_enabled: bool
    telegram_credentials_present: bool
    telegram_session_configured: bool
    toobit_public_market_ready: bool
    ai_gateway_enabled: bool
    regex_fallback_enabled: bool
    report_dir: str
    log_channel_enabled: bool


class RealBacktestRunRequest(BaseModel):
    channel: str
    from_date: datetime | None = None
    to_date: datetime | None = None
    hours: int | None = None
    start_message_link: str | None = None
    start_message_id: int | None = None
    interval: str
    max_messages: int
    initial_balance: Decimal = Decimal("100")
    risk_per_trade_pct: Decimal = Decimal("3")
    use_ai: bool
    send_telegram_summary: bool
    send_log_channel: bool
    log_per_message: bool = True

    @model_validator(mode="after")
    def validate_window(self) -> RealBacktestRunRequest:
        if self.hours is None and (self.from_date is None or self.to_date is None):
            raise ValueError("hours or explicit from/to range is required")
        if self.hours is not None and self.hours <= 0:
            raise ValueError("hours must be positive")
        if (
            self.from_date is not None
            and self.to_date is not None
            and self.to_date <= self.from_date
        ):
            raise ValueError("to_date must be after from_date")
        if self.start_message_id is not None and self.start_message_id <= 0:
            raise ValueError("start_message_id must be positive")
        if self.initial_balance <= Decimal("0"):
            raise ValueError("initial_balance must be positive")
        if self.risk_per_trade_pct <= Decimal("0"):
            raise ValueError("risk_per_trade_pct must be positive")
        return self

    def resolve_range(self) -> tuple[datetime, datetime]:
        if self.from_date is not None and self.to_date is not None:
            return self._utc(self.from_date), self._utc(self.to_date)
        assert self.hours is not None
        end = datetime.now(timezone.utc)
        return end - timedelta(hours=self.hours), end

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class RealBacktestResult(BaseModel):
    success: bool
    channel: str
    from_date: datetime
    to_date: datetime
    interval: str
    real_telegram_used: bool
    real_market_data_used: bool
    ai_used: bool
    regex_fallback_used: bool
    total_messages: int
    classified_messages: int
    parsed_signals: int
    valid_signals: int
    invalid_signals: int
    ignored_messages: int
    ambiguous_messages: int
    ai_failed_messages: int = 0
    symbols_found: list[str]
    candles_fetched: int
    trades_simulated: int
    trades_filled: int
    wins: int
    losses: int
    win_rate: Decimal
    total_pnl: Decimal
    profit_factor: Decimal | None
    max_drawdown: Decimal
    conservative_pnl: Decimal
    optimistic_pnl: Decimal
    channel_score: Decimal
    skipped_reasons: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime
    report_path: str | None = None
    markdown_report_path: str | None = None


@dataclass
class _ClassificationSelection:
    classifier: MessageClassifier
    ai_requested: bool
    ai_configured: bool
    warning: str | None


class RealBacktestRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        telegram_client: TelegramClientInterface | None = None,
        market_data_provider: MarketDataProvider | None = None,
        report_store: BacktestReportStore | None = None,
        log_client: TelegramLogChannelClient | None = None,
        strategy: TradeStrategy | None = None,
    ) -> None:
        self.settings = settings
        self.telegram_client = telegram_client or TelethonTelegramClient(settings)
        self.telegram_source = BacktestTelegramSource(self.telegram_client)
        self.market_data_provider = market_data_provider or build_backtest_market_data_provider(
            settings
        )
        self.report_store = report_store or BacktestReportStore(settings.REAL_BACKTEST_REPORT_DIR)
        self.log_client = log_client or TelegramLogChannelClient(settings=settings)
        self.validator = ParsedSignalValidator()
        self._log_lock = threading.Lock()
        self._last_log_send_failure_reason: str | None = None
        self._log_sending_disabled_for_run = False
        # Load strategy from config file; caller may also inject one directly.
        self.strategy: TradeStrategy = strategy or load_strategy()

    def readiness(self) -> RealBacktestReadiness:
        issues: list[str] = []
        token_hash = self.settings.TELEGRAM_API_HASH.get_secret_value()
        telegram_credentials_present = (
            self.settings.TELEGRAM_API_ID > 0
            and bool(token_hash and token_hash != "replace_me")
        )
        telegram_session_configured = bool(
            self.settings.TELEGRAM_STRING_SESSION.get_secret_value().strip()
            or (
                self.settings.TELEGRAM_SESSION_NAME
                and self.settings.TELEGRAM_SESSION_DIR
            )
        )
        historical_market_ready = bool(
            self.settings.BINANCE_PUBLIC_DATA_BASE_URL
            and self.settings.BINANCE_PUBLIC_DATA_CACHE_DIR
        )
        if not self.settings.REAL_BACKTEST_ENABLED:
            issues.append("REAL_BACKTEST_ENABLED=true is required")
        if self.settings.RUN_BACKTEST_INTEGRATION_TESTS != 1:
            issues.append("RUN_BACKTEST_INTEGRATION_TESTS=1 is required")
        if self.settings.RUN_TELEGRAM_INTEGRATION_TESTS != 1:
            issues.append("RUN_TELEGRAM_INTEGRATION_TESTS=1 is required")
        if self.settings.RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS != 1:
            issues.append("RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1 is required")
        if not telegram_credentials_present:
            issues.append("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured")
        if not telegram_session_configured:
            issues.append("TELEGRAM_SESSION_NAME and TELEGRAM_SESSION_DIR must be configured")
        if not historical_market_ready:
            issues.append("Binance public historical market-data settings are incomplete")
        Path(self.settings.REAL_BACKTEST_REPORT_DIR).mkdir(parents=True, exist_ok=True)
        Path(self.settings.BINANCE_PUBLIC_DATA_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        return RealBacktestReadiness(
            ready=not issues,
            issues=issues,
            real_backtest_enabled=self.settings.REAL_BACKTEST_ENABLED,
            telegram_credentials_present=telegram_credentials_present,
            telegram_session_configured=telegram_session_configured,
            toobit_public_market_ready=historical_market_ready,
            ai_gateway_enabled=self.settings.AI_GATEWAY_ENABLED,
            regex_fallback_enabled=self.settings.REAL_BACKTEST_USE_REGEX_FALLBACK,
            report_dir=self.settings.REAL_BACKTEST_REPORT_DIR,
            log_channel_enabled=(
                self.settings.TELEGRAM_LOG_CHANNEL_ENABLED
                and self.settings.PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL
            ),
        )

    async def run(
        self,
        request: RealBacktestRunRequest,
        *,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None = None,
    ) -> RealBacktestResult:
        self._log_sending_disabled_for_run = False
        readiness = self.readiness()
        from_date, to_date = request.resolve_range()
        warnings: list[str] = []
        self._emit_run_progress(
            progress_callback,
            phase="starting",
            status="running",
            summary="Backtest run created and waiting for readiness checks.",
        )
        if not readiness.ready:
            if request.send_log_channel:
                await self._try_send_log(
                    "Real backtest blocked before start\n"
                    f"channel={request.channel}\nissues={'; '.join(readiness.issues)}",
                    warnings=warnings,
                    warning_message=(
                        "Telegram log channel send failed before blocked backtest return; "
                        "continuing without Telegram run log delivery."
                    ),
                )
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=readiness.issues,
            )

        selection = self._select_classifier(request.use_ai)
        if request.use_ai and not selection.ai_configured:
            if request.send_log_channel:
                await self._try_send_log(
                    "Real backtest failed before classification\n"
                    f"channel={request.channel}\nreason=AI gateway required but not enabled",
                    warnings=warnings,
                    warning_message=(
                        "Telegram log channel send failed before AI-config return; "
                        "continuing without Telegram run log delivery."
                    ),
                )
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=["AI gateway is required for this backtest run but is not enabled."],
            )
        self._emit_run_progress(
            progress_callback,
            phase="fetch_history",
            status="running",
            summary="Fetching Telegram message history.",
        )
        if request.send_log_channel:
            await self._try_send_log(
                f"Real backtest started\nchannel={request.channel}\ninterval={request.interval}\n"
                f"range={from_date.isoformat()} -> {to_date.isoformat()}",
                warnings=warnings,
                warning_message=(
                    "Telegram log channel send failed at backtest start; "
                    "continuing without Telegram run log delivery."
                ),
            )

        try:
            messages, fetch_result = await self.telegram_source.fetch(
                channel=request.channel,
                start=from_date,
                end=to_date,
                limit=min(request.max_messages, self.settings.REAL_BACKTEST_MAX_MESSAGES),
                start_message_id=request.start_message_id,
            )
        except TelegramCredentialError as exc:
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=[str(exc)],
            )
        except Exception as exc:
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=[f"Telegram history fetch failed: {type(exc).__name__}"],
            )

        counts = {
            "total_messages": len(messages),
            "caption_media_candidates": sum(
                1
                for message in messages
                if bool(message.raw_payload.get("has_media"))
                and bool(message.raw_payload.get("caption_present"))
            ),
            "classified_messages": 0,
            "parsed_signals": 0,
            "valid_signals": 0,
            "invalid_signals": 0,
            "ignored_messages": 0,
            "ambiguous_messages": 0,
            "ai_failed_messages": 0,
            "trades_simulated": 0,
            "trades_filled": 0,
        }
        self._emit_run_progress(
            progress_callback,
            phase="fetch_history",
            status="completed",
            summary=(
                f"Fetched {len(messages)} Telegram messages. "
                f"Caption-media candidates for on-demand download: "
                f"{counts['caption_media_candidates']}."
            ),
            counts=counts,
        )

        engine = BacktestEngine(classifier=selection.classifier, strategy=self.strategy)
        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="running",
            summary="Classifying and validating channel messages one by one.",
            counts=counts,
        )
        prefetched_candles_by_symbol: dict[str, list[Any]] = {}
        (
            events,
            traces_by_message_id,
            signal_trace_map,
            symbol_trace_map,
            counts,
            prefetched_candles_by_symbol,
        ) = await self._build_events_with_traces(
            request=request,
            classifier=selection.classifier,
            messages=messages,
            progress_callback=progress_callback,
            counts=counts,
            warnings=warnings,
            prefetched_candles_by_symbol=prefetched_candles_by_symbol,
        )
        open_events = [event for event in events if event.action is SignalAction.OPEN]
        valid_open_events = [
            event
            for event in open_events
            if self.validator.validate_for_backtest_open(event.parsed_signal)[0]
        ]
        symbols = sorted({
            symbol
            for symbol in (
                normalize_market_symbol(event.parsed_signal.symbol)
                for event in valid_open_events
            )
            if symbol is not None
        })
        symbol_candidates_by_primary = self._build_symbol_candidates_by_primary(valid_open_events)
        ignored_messages = counts["ignored_messages"]
        ambiguous_messages = counts["ambiguous_messages"]
        invalid_signals = counts["invalid_signals"]
        ai_used = any(
            "classifier=ai" in event.debug_notes
            for event in events
        )
        regex_fallback_used = (
            any("ai-fallback=regex" in note for event in events for note in event.debug_notes)
            or isinstance(selection.classifier, RegexMessageClassifier)
        )
        if selection.warning:
            self._append_warning(warnings, selection.warning)
        if request.use_ai and not ai_used and regex_fallback_used and not selection.warning:
            self._append_warning(
                warnings,
                "AI gateway unavailable or failed during classification; regex fallback used."
            )
        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="completed",
            summary=(
                f"Classification complete: {counts['classified_messages']} processed, "
                f"{counts['valid_signals']} valid signals."
            ),
            counts=counts,
        )

        if request.send_log_channel:
            await self._try_send_log(
                "Real backtest history fetched\n"
                f"channel={request.channel}\n"
                f"messages={len(messages)}\n"
                f"symbols_detected={len(symbols)}",
                warnings=warnings,
                warning_message=(
                    "Telegram log channel send failed after history fetch; "
                    "continuing without Telegram run log delivery."
                ),
            )

        if not messages:
            if request.send_log_channel:
                await self._try_send_log(
                    "Real backtest finished with no messages\n"
                    f"channel={request.channel}\n"
                    f"range={from_date.isoformat()} -> {to_date.isoformat()}",
                    warnings=warnings,
                    warning_message=(
                        "Telegram log channel send failed before no-message failure return; "
                        "continuing without Telegram run log delivery."
                    ),
                )
            self._emit_run_progress(
                progress_callback,
                phase="classify_messages",
                status="failed",
                summary="No Telegram messages were available for the requested range.",
                counts=counts,
            )
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                real_telegram_used=fetch_result.used_real_telegram,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
                total_messages=0,
                classified_messages=0,
                parsed_signals=0,
                valid_signals=0,
                invalid_signals=0,
                ignored_messages=0,
                ambiguous_messages=0,
                errors=["No Telegram messages fetched for the requested range"],
                warnings=warnings,
            )
        if not symbols:
            if request.send_log_channel:
                await self._try_send_log(
                    "Real backtest finished without valid signals\n"
                    f"channel={request.channel}\nmessages={len(messages)}\n"
                    "reason=No structurally valid signals were detected",
                    warnings=warnings,
                    warning_message=(
                        "Telegram log channel send failed before no-signal failure return; "
                        "continuing without Telegram run log delivery."
                    ),
                )
            self._emit_run_progress(
                progress_callback,
                phase="classify_messages",
                status="failed",
                summary="No structurally valid signals were detected in the fetched messages.",
                counts=counts,
            )
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                real_telegram_used=fetch_result.used_real_telegram,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
                total_messages=len(messages),
                classified_messages=counts["classified_messages"],
                parsed_signals=len(open_events),
                valid_signals=len(valid_open_events),
                invalid_signals=invalid_signals,
                ignored_messages=ignored_messages,
                ambiguous_messages=ambiguous_messages,
                errors=["No structurally valid signals were detected"],
                warnings=warnings,
            )

        candles: list[Any] = []
        skipped_reasons: list[str] = []
        real_market_data_used = False
        self._emit_run_progress(
            progress_callback,
            phase="fetch_market_data",
            status="running",
            summary=f"Fetching market candles for {len(symbols)} symbols.",
            counts=counts,
        )
        for symbol in symbols:
            prefetched = prefetched_candles_by_symbol.get(symbol)
            if prefetched is not None:
                real_market_data_used = real_market_data_used or bool(prefetched)
                candles.extend(prefetched)
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id.get(message_id)
                    if message_trace is None:
                        continue
                    if message_trace.current_stage == "simulated":
                        self._set_trace_stage(
                            message_trace,
                            "simulated",
                            status="active",
                            detail=(
                                "Simulation tracking remains active; waiting for final replay "
                                "with future updates and candle resolution."
                            ),
                        )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"Reusing prefetched candles for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
                continue

            candidate_symbols = symbol_candidates_by_primary.get(symbol, [symbol])
            fetched: list[Any] = []
            selected_symbol = symbol
            last_error_type: str | None = None
            no_data_candidates: list[str] = []
            for message_id in symbol_trace_map.get(symbol, []):
                message_trace = traces_by_message_id.get(message_id)
                if message_trace is None:
                    continue
                self._set_trace_stage(
                    message_trace,
                    "market_data",
                    status="active",
                    detail=f"Fetching candle data for {symbol}.",
                )
                self._emit_message_progress(
                    progress_callback,
                    phase="fetch_market_data",
                    summary=f"Fetching market data for message {message_id}.",
                    counts=counts,
                    trace=message_trace,
                )
            for candidate_symbol in candidate_symbols:
                try:
                    fetched = await self.market_data_provider.get_klines(
                        candidate_symbol,
                        request.interval,
                        from_date,
                        to_date,
                    )
                except Exception as exc:
                    last_error_type = type(exc).__name__
                    continue
                if fetched:
                    selected_symbol = candidate_symbol
                    break
                no_data_candidates.append(candidate_symbol)

            if last_error_type is not None and not fetched and not no_data_candidates:
                skipped_reasons.append(f"{symbol}: candle fetch failed ({last_error_type})")
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id.get(message_id)
                    if message_trace is None:
                        continue
                    message_trace.final_status = "market_data_unavailable"
                    message_trace.result_summary = (
                        f"Candle fetch failed for {symbol}: {last_error_type}"
                    )
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="failed",
                        detail=message_trace.result_summary,
                    )
                    self._set_trace_stage(
                        message_trace,
                        "simulated",
                        status="skipped",
                        detail="Simulation skipped because market data was unavailable.",
                    )
                    self._set_trace_stage(
                        message_trace,
                        "finalized",
                        status="completed",
                        detail=message_trace.result_summary,
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"Market data failed for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
                    await self._maybe_send_message_log(request, message_trace, warnings)
                continue

            if fetched:
                real_market_data_used = True
                candles.extend(fetched)
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id.get(message_id)
                    if message_trace is None:
                        continue
                    if selected_symbol != symbol:
                        message_trace.debug_notes.append(
                            f"market_symbol_selected={selected_symbol}"
                        )
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="completed",
                        detail=f"Fetched {len(fetched)} candles for {selected_symbol}.",
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"Candles ready for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
            else:
                attempted = ", ".join(no_data_candidates or candidate_symbols)
                skipped_reasons.append(f"{symbol}: no candle data returned (tried: {attempted})")
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id.get(message_id)
                    if message_trace is None:
                        continue
                    message_trace.final_status = "market_data_unavailable"
                    message_trace.result_summary = (
                        f"No candle data returned for {symbol}. Tried: {attempted}"
                    )
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="failed",
                        detail=message_trace.result_summary,
                    )
                    self._set_trace_stage(
                        message_trace,
                        "simulated",
                        status="skipped",
                        detail="Simulation skipped because market data was unavailable.",
                    )
                    self._set_trace_stage(
                        message_trace,
                        "finalized",
                        status="completed",
                        detail=message_trace.result_summary,
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"No candle data returned for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
                    await self._maybe_send_message_log(request, message_trace, warnings)

        if request.send_log_channel:
            await self._try_send_log(
                f"Real backtest candles fetched\nchannel={request.channel}\n"
                f"candles={len(candles)}\nreal_market_data_used={real_market_data_used}",
                warnings=warnings,
                warning_message=(
                    "Telegram log channel send failed after market-data fetch; "
                    "continuing without Telegram run log delivery."
                ),
            )
        self._emit_run_progress(
            progress_callback,
            phase="fetch_market_data",
            status="completed" if candles else "failed",
            summary=f"Fetched {len(candles)} candles across {len(symbols)} symbols.",
            counts=counts,
        )

        if not candles:
            if request.send_log_channel:
                await self._try_send_log(
                    "Real backtest finished without market data\n"
                    f"channel={request.channel}\nsymbols={', '.join(symbols)}\n"
                    "reason=No candle data available for detected symbols",
                    warnings=warnings,
                    warning_message=(
                        "Telegram log channel send failed before no-candle failure return; "
                        "continuing without Telegram run log delivery."
                    ),
                )
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                real_telegram_used=fetch_result.used_real_telegram,
                real_market_data_used=real_market_data_used,
                ai_used=ai_used,
                regex_fallback_used=regex_fallback_used,
                total_messages=len(messages),
                classified_messages=counts["classified_messages"],
                parsed_signals=len(open_events),
                valid_signals=len(valid_open_events),
                invalid_signals=invalid_signals,
                ignored_messages=ignored_messages,
                ambiguous_messages=ambiguous_messages,
                skipped_reasons=skipped_reasons,
                errors=["No candle data available for detected symbols"],
                warnings=warnings,
            )

        report_request = BacktestRequest(
            channel=request.channel,
            from_date=from_date,
            to_date=to_date,
            initial_balance=request.initial_balance,
            interval=request.interval,
            fill_policy=BacktestFillPolicy(self.settings.BACKTEST_DEFAULT_FILL_POLICY),
            risk_per_trade_pct=request.risk_per_trade_pct,
            use_ai_classifier=request.use_ai,
            use_regex_fallback=self.settings.REAL_BACKTEST_USE_REGEX_FALLBACK,
            max_messages=request.max_messages,
            symbols=symbols,
        )
        self._emit_run_progress(
            progress_callback,
            phase="simulate",
            status="running",
            summary="Running simulation over the validated signal timeline.",
            counts=counts,
        )
        for _signal_id, message_id in signal_trace_map.items():
            simulation_trace = traces_by_message_id.get(message_id)
            if simulation_trace is None:
                continue
            self._set_trace_stage(
                simulation_trace,
                "simulated",
                status="active",
                detail="Simulation queued; engine is replaying candles now.",
            )
            self._emit_message_progress(
                progress_callback,
                phase="simulate",
                summary=f"Simulation started for message {message_id}.",
                counts=counts,
                trace=simulation_trace,
            )
        report = engine.run_from_events(
            request=report_request,
            events=events,
            candles=candles,
            active_signal_hours=self.settings.REAL_BACKTEST_ACTIVE_SIGNAL_HOURS,
            max_effective_leverage=Decimal(self.settings.BACKTEST_MAX_EFFECTIVE_LEVERAGE),
            default_stop_pct=Decimal(self.settings.BACKTEST_DEFAULT_STOP_PCT),
        )
        score = Decimal("0")
        if report.warnings:
            for warning in report.warnings:
                if "channel_score=" in warning:
                    try:
                        score = Decimal(warning.split("=")[1])
                        break
                    except (IndexError, ValueError):
                        continue
        trades_filled = sum(1 for trade in report.trades if trade.status != "not_filled")
        wins = sum(1 for trade in report.trades if trade.pnl > 0)
        losses = sum(1 for trade in report.trades if trade.pnl < 0)
        for signal_id, message_id in signal_trace_map.items():
            trace: RealBacktestMessageTrace | None = traces_by_message_id.get(message_id)
            if trace is None:
                continue
            trade = next((item for item in report.trades if item.signal_id == signal_id), None)
            if trade is None:
                self._set_trace_stage(
                    trace,
                    "simulated",
                    status="skipped",
                    detail="No trade was simulated for this signal.",
                )
                trace.final_status = "no_trade"
                trace.result_summary = "No trade generated from this signal."
            else:
                self._set_trace_stage(
                    trace,
                    "simulated",
                    status="completed",
                    detail=f"Trade status={trade.status}, pnl={trade.pnl}",
                )
                trace.final_status = trade.status
                trace.result_summary = (
                    f"Trade {trade.status}. Entry={trade.entry_price}, Exit={trade.exit_price}, "
                    f"PnL={trade.pnl}"
                )
            self._set_trace_stage(
                trace,
                "finalized",
                status="completed",
                detail=trace.result_summary,
            )
            self._emit_message_progress(
                progress_callback,
                phase="simulate",
                summary=f"Simulation finalized for message {message_id}.",
                counts=counts,
                trace=trace,
            )
            await self._maybe_send_message_log(request, trace, warnings)
        result = RealBacktestResult(
            success=True,
            channel=request.channel,
            from_date=from_date,
            to_date=to_date,
            interval=request.interval,
            real_telegram_used=fetch_result.used_real_telegram,
            real_market_data_used=real_market_data_used,
            ai_used=ai_used,
            regex_fallback_used=regex_fallback_used,
            total_messages=len(messages),
            classified_messages=counts["classified_messages"],
            parsed_signals=len(open_events),
            valid_signals=len(valid_open_events),
            invalid_signals=invalid_signals,
            ignored_messages=ignored_messages,
            ambiguous_messages=ambiguous_messages,
            ai_failed_messages=counts.get("ai_failed_messages", 0),
            symbols_found=symbols,
            candles_fetched=len(candles),
            trades_simulated=len(report.trades),
            trades_filled=trades_filled,
            wins=wins,
            losses=losses,
            win_rate=report.metrics.win_rate,
            total_pnl=report.metrics.total_pnl,
            profit_factor=report.metrics.profit_factor,
            max_drawdown=report.metrics.max_drawdown,
            conservative_pnl=report.metrics.conservative_pnl,
            optimistic_pnl=report.metrics.optimistic_pnl,
            channel_score=score,
            skipped_reasons=skipped_reasons,
            warnings=warnings,
            generated_at=report.generated_at,
        )
        self._emit_run_progress(
            progress_callback,
            phase="simulate",
            status="completed",
            summary=(
                f"Simulation complete: {result.trades_simulated} trades, "
                f"{result.trades_filled} filled."
            ),
            counts={
                **counts,
                "trades_simulated": result.trades_simulated,
                "trades_filled": result.trades_filled,
            },
            live_metrics={
                "live_open_positions": "0",
                "live_closed_trades": str(result.trades_filled),
                "live_wins": str(result.wins),
                "live_losses": str(result.losses),
                "live_realized_pnl": str(result.total_pnl),
                "live_unrealized_pnl": "0",
                "live_total_pnl": str(result.total_pnl),
                "live_realized_balance": str(request.initial_balance + result.total_pnl),
                "live_current_balance": str(request.initial_balance + result.total_pnl),
            },
        )
        stored = self.report_store.write(self._build_payload(result, report, score))
        result.report_path = stored.json_path
        result.markdown_report_path = stored.markdown_path
        self._emit_run_progress(
            progress_callback,
            phase="report",
            status="completed",
            summary=f"Report written to {result.report_path}.",
            counts={
                **counts,
                "trades_simulated": result.trades_simulated,
                "trades_filled": result.trades_filled,
            },
            live_metrics={
                "live_open_positions": "0",
                "live_closed_trades": str(result.trades_filled),
                "live_wins": str(result.wins),
                "live_losses": str(result.losses),
                "live_realized_pnl": str(result.total_pnl),
                "live_unrealized_pnl": "0",
                "live_total_pnl": str(result.total_pnl),
                "live_realized_balance": str(request.initial_balance + result.total_pnl),
                "live_current_balance": str(request.initial_balance + result.total_pnl),
            },
        )

        if request.send_log_channel:
            await self._try_send_log(
                f"Real backtest complete\nchannel={request.channel}\n"
                f"messages={result.total_messages}\nvalid_signals={result.valid_signals}\n"
                f"trades={result.trades_simulated}\npnl={result.total_pnl}\n"
                f"report={result.markdown_report_path}",
                warnings=result.warnings,
                warning_message=(
                    "Telegram log channel send failed at backtest completion; "
                    "continuing without Telegram run log delivery."
                ),
            )
        return result

    def run_sync(
        self,
        request: RealBacktestRunRequest,
        *,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None = None,
    ) -> RealBacktestResult:
        return asyncio.run(self.run(request, progress_callback=progress_callback))

    def latest_report_summary(self) -> dict[str, Any] | None:
        latest = self.report_store.latest()
        if latest is None:
            return None
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
        except ValueError:
            return {"report_path": str(latest), "error": "latest report is not valid JSON"}
        payload["report_path"] = str(latest)
        return dict(payload)

    def _select_classifier(self, use_ai: bool) -> _ClassificationSelection:
        if use_ai and self.settings.AI_GATEWAY_ENABLED:
            client = AjilGatewayClient(
                base_url=self.settings.AI_GATEWAY_BASE_URL,
                timeout_seconds=self.settings.AI_GATEWAY_TIMEOUT_SECONDS,
                classify_path=self.settings.AI_GATEWAY_CLASSIFY_PATH,
                auth_header_name=self.settings.AI_GATEWAY_AUTH_HEADER_NAME,
                auth_token=self.settings.AI_GATEWAY_AUTH_TOKEN.get_secret_value(),
                default_model=self.settings.AI_GATEWAY_DEFAULT_MODEL,
                provider_priority=tuple(
                    item.strip()
                    for item in self.settings.AI_GATEWAY_PROVIDER_PRIORITY.split(",")
                    if item.strip()
                ),
                text_provider=self.settings.AI_CLASSIFIER_TEXT_PROVIDER,
                text_model=self.settings.AI_CLASSIFIER_TEXT_MODEL,
                vision_provider=self.settings.AI_CLASSIFIER_VISION_PROVIDER,
                vision_model=self.settings.AI_CLASSIFIER_VISION_MODEL,
                trust_env=self.settings.AI_GATEWAY_TRUST_ENV,
                retry_attempts=self.settings.AI_GATEWAY_RETRY_ATTEMPTS,
                retry_backoff_seconds=self.settings.AI_GATEWAY_RETRY_BACKOFF_SECONDS,
            )
            classifier: MessageClassifier = AIMessageClassifier(
                settings=self.settings,
                gateway_client=client,
                regex_fallback=None,
            )
            return _ClassificationSelection(
                classifier=classifier,
                ai_requested=True,
                ai_configured=True,
                warning=None,
            )
        if use_ai:
            client = AjilGatewayClient(
                base_url=self.settings.AI_GATEWAY_BASE_URL,
                timeout_seconds=self.settings.AI_GATEWAY_TIMEOUT_SECONDS,
                classify_path=self.settings.AI_GATEWAY_CLASSIFY_PATH,
                auth_header_name=self.settings.AI_GATEWAY_AUTH_HEADER_NAME,
                auth_token=self.settings.AI_GATEWAY_AUTH_TOKEN.get_secret_value(),
                default_model=self.settings.AI_GATEWAY_DEFAULT_MODEL,
                provider_priority=tuple(
                    item.strip()
                    for item in self.settings.AI_GATEWAY_PROVIDER_PRIORITY.split(",")
                    if item.strip()
                ),
                text_provider=self.settings.AI_CLASSIFIER_TEXT_PROVIDER,
                text_model=self.settings.AI_CLASSIFIER_TEXT_MODEL,
                vision_provider=self.settings.AI_CLASSIFIER_VISION_PROVIDER,
                vision_model=self.settings.AI_CLASSIFIER_VISION_MODEL,
                trust_env=self.settings.AI_GATEWAY_TRUST_ENV,
                retry_attempts=self.settings.AI_GATEWAY_RETRY_ATTEMPTS,
                retry_backoff_seconds=self.settings.AI_GATEWAY_RETRY_BACKOFF_SECONDS,
            )
            return _ClassificationSelection(
                classifier=AIMessageClassifier(
                    settings=self.settings,
                    gateway_client=client,
                    regex_fallback=None,
                ),
                ai_requested=True,
                ai_configured=False,
                warning="AI gateway is required but not enabled.",
            )
        return _ClassificationSelection(
            classifier=RegexMessageClassifier(),
            ai_requested=use_ai,
            ai_configured=False,
            warning=(
                "AI gateway unavailable or disabled; regex fallback used."
                if use_ai
                else None
            ),
        )

    async def _send_log(self, text: str) -> object | None:
        return await self.log_client.send_text(text, real=True)

    async def _try_send_log(
        self,
        text: str,
        *,
        warnings: list[str] | None,
        warning_message: str,
    ) -> bool:
        if getattr(self, "_log_sending_disabled_for_run", False):
            return False
        self._last_log_send_failure_reason = None
        attempts = max(1, self.settings.TELEGRAM_LOG_CHANNEL_SEND_RETRIES + 1)
        delay_seconds = max(0, self.settings.TELEGRAM_LOG_CHANNEL_RETRY_DELAY_SECONDS)
        for attempt in range(attempts):
            try:
                result = await self._send_log(text)
            except Exception as exc:
                self._last_log_send_failure_reason = type(exc).__name__
            else:
                if not self._send_result_skipped(result):
                    self._last_log_send_failure_reason = None
                    return True
                self._last_log_send_failure_reason = self._send_result_skip_reason(result)
            if attempt < attempts - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        reason = self._last_log_send_failure_reason or "unknown"
        if warnings is not None:
            self._append_warning(warnings, f"{warning_message} ({reason})")
        # If any log send fails, disable later log sends for this run to avoid delays
        self._log_sending_disabled_for_run = True
        return False

    @staticmethod
    def _send_result_skipped(result: object | None) -> bool:
        if result is None:
            return True
        if isinstance(result, dict):
            return bool(result.get("skipped"))
        skipped = getattr(result, "skipped", None)
        return bool(skipped)

    @staticmethod
    def _send_result_skip_reason(result: object | None) -> str:
        if result is None:
            return "skipped:none"
        if isinstance(result, dict):
            return f"skipped:{result.get('reason') or 'unknown'}"
        reason = getattr(result, "reason", None)
        return f"skipped:{reason or 'unknown'}"

    async def _maybe_send_message_log(
        self,
        request: RealBacktestRunRequest,
        trace: RealBacktestMessageTrace,
        warnings: list[str],
        *,
        checkpoint: str | None = None,
    ) -> None:
        if not (request.send_log_channel and request.log_per_message):
            return
        checkpoint_key = checkpoint or trace.current_stage
        sent_marker = f"telegram_log_sent={checkpoint_key}"
        failed_marker = f"telegram_log_failed={checkpoint_key}"
        if sent_marker in trace.debug_notes or failed_marker in trace.debug_notes:
            return
        sent = await self._try_send_log(
            self._format_trace_for_telegram(trace),
            warnings=warnings,
            warning_message=(
                "Telegram per-message trace send failed; "
                "continuing without per-message Telegram delivery."
            ),
        )
        if not sent:
            reason = self._last_log_send_failure_reason or "unknown"
            trace.debug_notes.append(f"{failed_marker}:{reason}")
            return
        trace.debug_notes.append(sent_marker)

    @staticmethod
    def _append_warning(warnings: list[str], message: str) -> None:
        if message not in warnings:
            warnings.append(message)

    def _write_failure(
        self,
        *,
        channel: str,
        from_date: datetime,
        to_date: datetime,
        interval: str,
        real_telegram_used: bool = False,
        real_market_data_used: bool = False,
        ai_used: bool = False,
        regex_fallback_used: bool = False,
        total_messages: int = 0,
        classified_messages: int = 0,
        parsed_signals: int = 0,
        valid_signals: int = 0,
        invalid_signals: int = 0,
        ignored_messages: int = 0,
        ambiguous_messages: int = 0,
        skipped_reasons: list[str] | None = None,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> RealBacktestResult:
        result = RealBacktestResult(
            success=False,
            channel=channel,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            real_telegram_used=real_telegram_used,
            real_market_data_used=real_market_data_used,
            ai_used=ai_used,
            regex_fallback_used=regex_fallback_used,
            total_messages=total_messages,
            classified_messages=classified_messages,
            parsed_signals=parsed_signals,
            valid_signals=valid_signals,
            invalid_signals=invalid_signals,
            ignored_messages=ignored_messages,
            ambiguous_messages=ambiguous_messages,
            symbols_found=[],
            candles_fetched=0,
            trades_simulated=0,
            trades_filled=0,
            wins=0,
            losses=0,
            win_rate=Decimal("0"),
            total_pnl=Decimal("0"),
            profit_factor=None,
            max_drawdown=Decimal("0"),
            conservative_pnl=Decimal("0"),
            optimistic_pnl=Decimal("0"),
            channel_score=Decimal("0"),
            skipped_reasons=skipped_reasons or [],
            errors=errors or [],
            warnings=warnings or [],
            generated_at=datetime.now(timezone.utc),
        )
        stored = self.report_store.write(self._build_payload(result, None, Decimal("0")))
        result.report_path = stored.json_path
        result.markdown_report_path = stored.markdown_path
        return result

    def _build_payload(
        self,
        result: RealBacktestResult,
        report: Any | None,
        score: Decimal,
    ) -> dict[str, Any]:
        payload = result.model_dump(mode="json")
        payload["score_reason"] = "derived from simulator/scorer" if result.success else "failure"
        if report is not None:
            payload["report"] = report_to_json(report, score)
            payload["telegram_summary"] = report_to_markdown_summary(report, score)
        return payload

    async def _build_events_with_traces(
        self,
        *,
        request: RealBacktestRunRequest,
        classifier: MessageClassifier,
        messages: list[RawTelegramMessage],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        warnings: list[str],
        prefetched_candles_by_symbol: dict[str, list[Any]],
    ) -> tuple[
        list[BacktestEvent],
        dict[int, RealBacktestMessageTrace],
        dict[str, int],
        dict[str, list[int]],
        dict[str, int],
        dict[str, list[Any]],
    ]:
        context = ChannelContext(
            channel_id=request.channel,
            max_message_limit=max(
                request.max_messages,
                self.settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
            ),
            max_update_window_hours=min(
                self.settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
                self.settings.REAL_BACKTEST_ACTIVE_SIGNAL_HOURS,
            ),
        )
        events: list[BacktestEvent] = []
        traces_by_message_id: dict[int, RealBacktestMessageTrace] = {}
        signal_trace_map: dict[str, int] = {}
        event_index_by_signal_id: dict[str, int] = {}
        symbol_trace_map: dict[str, list[int]] = {}
        # Cache each processed message's parsed signal + its raw message so a later
        # follow-up that REPLIES to a parent which was not recognized as a signal can
        # still promote that parent into a tracked signal (msg /6285 scenario).
        parsed_by_message_id: dict[int, ParsedSignal] = {}
        raw_by_message_id: dict[int, RawTelegramMessage] = {}
        sorted_messages = sorted(messages, key=lambda item: item.date)
        context.seed_message_catalog(sorted_messages)

        for message in sorted_messages:
            trace = self._make_trace(message)
            traces_by_message_id[message.message_id] = trace
            self._set_trace_stage(
                trace,
                "received",
                status="completed",
                detail="Message pulled from Telegram history.",
            )
            self._set_trace_stage(
                trace,
                "preprocess",
                status="active",
                detail="Preparing message payload for classification.",
            )
            message = await self._prepare_message_for_classification(
                message=message,
                trace=trace,
                progress_callback=progress_callback,
                counts=counts,
                warnings=warnings,
            )
            preprocess_index = trace._stage_index.get("preprocess")
            if preprocess_index is not None:
                preprocess_stage = trace.stages[preprocess_index]
                if preprocess_stage.status != "completed":
                    self._set_trace_stage(
                        trace,
                        "preprocess",
                        status="completed",
                        detail="Message payload prepared for classification.",
                    )
            self._set_trace_stage(
                trace,
                "classified",
                status="active",
                detail="Classifier is analyzing this message.",
            )
            self._emit_message_progress(
                progress_callback,
                phase="classify_messages",
                summary=f"Reviewing message {message.message_id}.",
                counts=counts,
                trace=trace,
            )
            context.add_recent_message(message)

            if not self._message_has_processible_text(message):
                passive_signal = build_ignored_signal(
                    message,
                    invalid_reason="message has no text or caption",
                )
                trace.classification = "ignored"
                trace.parsed_action = passive_signal.action.value
                trace.confidence = "0"
                trace.final_status = "ignored"
                trace.result_summary = "Message has no text or caption; skipped from parsing."
                trace.debug_notes = ["classification_skipped=empty_message"]
                self._set_trace_stage(
                    trace,
                    "classified",
                    status="completed",
                    detail="Skipped because this Telegram message has no text or caption.",
                )
                self._mark_non_signal_trace(
                    trace,
                    "Message has no text or caption; used only as a simulation checkpoint.",
                )
                counts["ignored_messages"] += 1
                self._emit_message_progress(
                    progress_callback,
                    phase="classify_messages",
                    summary=f"Message {message.message_id} skipped due to empty text.",
                    counts=counts,
                    trace=trace,
                )
                events.append(
                    BacktestEvent(
                        timestamp=message.date,
                        action=passive_signal.action,
                        signal_id=None,
                        parsed_signal=passive_signal,
                        related_signal_id=None,
                        debug_notes=list(trace.debug_notes),
                        source_message_id=message.message_id,
                        source_text=None,
                        close_fraction=None,
                        move_stop_to_entry=False,
                    )
                )
                live_metrics, live_signals = self._update_live_simulation_state(
                    request=request,
                    events=events,
                    traces_by_message_id=traces_by_message_id,
                    signal_trace_map=signal_trace_map,
                    prefetched_candles_by_symbol=prefetched_candles_by_symbol,
                    progress_callback=progress_callback,
                    counts=counts,
                    current_message_id=message.message_id,
                )
                self._emit_run_progress(
                    progress_callback,
                    phase="classify_messages",
                    status="running",
                    summary=f"Message {message.message_id} advanced live simulation state.",
                    counts=counts,
                    live_metrics=live_metrics,
                    live_signals=live_signals,
                )
                await self._maybe_send_message_log(request, trace, warnings)
                continue

            try:
                classified = classifier.classify(message, context)
            except Exception as exc:
                # Defense-in-depth: one bad
                # message must never abort a multi-hundred-message backtest. The
                # AI classifier is expected to return a safe UNKNOWN rather than
                # raise, but we guard here too so any unexpected error is recorded
                # against the message and the run continues.
                counts["ai_failed_messages"] += 1
                ai_failed_signal = build_ignored_signal(
                    message,
                    invalid_reason=f"ai_classification_error={type(exc).__name__}",
                )
                trace.classification = "ai_failed"
                trace.parsed_action = ai_failed_signal.action.value
                trace.confidence = "0"
                trace.final_status = "ai_failed"
                trace.result_summary = (
                    f"AI classification failed ({type(exc).__name__}); "
                    "message recorded as ai_failed and excluded from trading."
                )
                trace.debug_notes = ["classifier=ai", f"ai-error={type(exc).__name__}"]
                self._set_trace_stage(
                    trace,
                    "classified",
                    status="failed",
                    detail=f"AI classification raised {type(exc).__name__}.",
                )
                self._mark_non_signal_trace(
                    trace,
                    "AI classification failed; message excluded from trading.",
                )
                self._append_warning(
                    warnings,
                    f"AI classification failed for message {message.message_id} "
                    f"({type(exc).__name__}); recorded as ai_failed.",
                )
                events.append(
                    BacktestEvent(
                        timestamp=message.date,
                        action=ai_failed_signal.action,
                        signal_id=None,
                        parsed_signal=ai_failed_signal,
                        related_signal_id=None,
                        debug_notes=list(trace.debug_notes),
                        source_message_id=message.message_id,
                        source_text=message.text,
                        close_fraction=None,
                        move_stop_to_entry=False,
                    )
                )
                counts["classified_messages"] += 1
                self._emit_message_progress(
                    progress_callback,
                    phase="classify_messages",
                    summary=f"Message {message.message_id} AI classification failed.",
                    counts=counts,
                    trace=trace,
                )
                await self._maybe_send_message_log(request, trace, warnings)
                continue
            parsed = classified.parsed_signal
            parsed_by_message_id[message.message_id] = parsed
            raw_by_message_id[message.message_id] = message
            signal_id: str | None = None
            resolved_related_id: str | None = None
            parsed_for_event = parsed
            # Guard: the AI sometimes classifies a reply/follow-up message as a
            # new OPEN signal (e.g. when the update contains entry/SL/TP numbers).
            # If this message is a Telegram reply to a message that already owns
            # a live signal, reroute it as a follow-up regardless of what the AI
            # returned.  This prevents orphan "new signal" entries that never open
            # a position while the real instruction (move SL, close, etc.) is lost.
            reply_owner = context.find_signal_by_message_reply(message.reply_to_msg_id)
            if classified.is_potential_new_signal and reply_owner is not None:
                classified.is_potential_new_signal = False
                classified.is_related_to_existing_signal = True
                if not classified.related_signal_id:
                    classified.related_signal_id = reply_owner.signal_id
                classified.debug_notes.append(
                    f"rerouted_open_to_followup; reply_owner={reply_owner.signal_id}"
                )
            if classified.is_potential_new_signal:
                signal_id = make_signal_id(message.channel_id, message.message_id)
                trace.signal_id = signal_id
                context.add_signal(
                    SignalState(
                        signal_id=signal_id,
                        channel_id=message.channel_id,
                        status=SignalStatus.PENDING_CONSOLIDATION,
                        created_from_message_id=message.message_id,
                        related_message_ids=[message.message_id],
                        current_signal=parsed,
                        version=1,
                        created_at=message.date,
                        updated_at=message.date,
                        expires_at=None,
                    ),
                    pending=True,
                )
            else:
                # Follow-up message. Decide the effective action FIRST (so a
                # mislabeled risk-free / close still resolves), then correlate
                # deterministically. The event must carry the RESOLVED signal id
                # so it reaches the right open position in the simulator.
                effective_action = normalize_related_signal_action(parsed, is_related=True)
                effective_action = apply_text_directive_action(effective_action, message.text)
                # A bare "Tp List" row of prices (often marked AMBIGUOUS by the AI)
                # is a take-profit update for the active signal; recover it so the
                # directive is applied instead of silently lost (msg /6285).
                tp_list_values: list[Decimal] = []
                if effective_action in {SignalAction.UNKNOWN, SignalAction.IGNORE}:
                    tp_list_values = detect_tp_list_update(message.text)
                    if tp_list_values:
                        effective_action = SignalAction.UPDATE_TP
                if effective_action is not parsed.action:
                    update: dict[str, Any] = {"action": effective_action}
                    if tp_list_values:
                        update["take_profits"] = tp_list_values
                    parsed_for_event = parsed.model_copy(update=update)
                    classified.debug_notes.append(
                        f"normalized_follow_up_action={effective_action.value}"
                    )
                    if tp_list_values:
                        classified.debug_notes.append(
                            f"tp_list_directive={len(tp_list_values)}"
                        )
                # msg /6285: this is a reply to a message whose own signal was never
                # tracked. If that parent parses as an openable signal, promote it now
                # (register it + inject an OPEN event at the parent's timestamp) so the
                # reply and any later updates correlate to a real, simulated position.
                promoted_parent_id = await self._maybe_promote_reply_parent(
                    message=message,
                    context=context,
                    parsed_by_message_id=parsed_by_message_id,
                    raw_by_message_id=raw_by_message_id,
                    events=events,
                    event_index_by_signal_id=event_index_by_signal_id,
                    signal_trace_map=signal_trace_map,
                    traces_by_message_id=traces_by_message_id,
                    symbol_trace_map=symbol_trace_map,
                    counts=counts,
                    request=request,
                    prefetched_candles_by_symbol=prefetched_candles_by_symbol,
                    progress_callback=progress_callback,
                    warnings=warnings,
                )
                if promoted_parent_id is not None:
                    classified.debug_notes.append(
                        f"promoted_reply_parent={promoted_parent_id}"
                    )
                correlation = resolve_related_signal_id(
                    context=context,
                    parsed=parsed,
                    raw_related_id=classified.related_signal_id,
                    message=message,
                    action=effective_action,
                    allow_last_resort=(
                        self.settings.REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH
                    ),
                )
                if correlation.note:
                    classified.debug_notes.append(correlation.note)
                if correlation.signal_id is not None:
                    signal_id = correlation.signal_id
                    resolved_related_id = correlation.signal_id
                    trace.signal_id = signal_id
                    context.attach_message(signal_id, message)
                    context.merge_signal(signal_id, parsed, message.date)
                    classified.debug_notes.append(
                        f"related_resolution={correlation.method}"
                    )
                elif effective_action in {
                    SignalAction.CLOSE,
                    SignalAction.CANCEL,
                    SignalAction.UPDATE_SL,
                    SignalAction.UPDATE_TP,
                    SignalAction.UPDATE_LEVERAGE,
                    SignalAction.UPDATE_ENTRY,
                }:
                    # A clear follow-up directive that could not be attached to
                    # any active signal must be surfaced, never silently lost.
                    classified.debug_notes.append(
                        f"followup_unattached={effective_action.value}"
                    )
                    self._append_warning(
                        warnings,
                        f"Follow-up directive '{effective_action.value}' in message "
                        f"{message.message_id} could not be attached to any active "
                        f"signal; it was not applied.",
                    )
            trace.classification = self._classify_label(classified)
            trace.parsed_action = parsed_for_event.action.value
            trace.symbol = parsed.symbol
            trace.side = parsed.side.value
            trace.confidence = str(classified.confidence)
            trace.debug_notes = list(classified.debug_notes)
            self._set_trace_stage(
                trace,
                "classified",
                status="completed",
                detail=(
                    f"classification={trace.classification}, action={trace.parsed_action}, "
                    f"confidence={trace.confidence}"
                ),
            )
            await self._maybe_send_message_log(
                request,
                trace,
                warnings,
                checkpoint="classification_complete",
            )

            if signal_id is not None:
                await self._sync_signal_tracking(
                    request=request,
                    context=context,
                    signal_id=signal_id,
                    traces_by_message_id=traces_by_message_id,
                    signal_trace_map=signal_trace_map,
                    event_index_by_signal_id=event_index_by_signal_id,
                    symbol_trace_map=symbol_trace_map,
                    events=events,
                    prefetched_candles_by_symbol=prefetched_candles_by_symbol,
                    progress_callback=progress_callback,
                    counts=counts,
                    warnings=warnings,
                )
                if classified.is_potential_new_signal:
                    refreshed_state = context.get_signal(signal_id)
                    if refreshed_state is not None and refreshed_state.current_signal is not None:
                        parsed_for_event = refreshed_state.current_signal
                        trace.parsed_action = parsed_for_event.action.value
                        trace.symbol = parsed_for_event.symbol
                        trace.side = parsed_for_event.side.value

            if classified.is_potential_new_signal and parsed.action is SignalAction.OPEN:
                self._set_trace_stage(
                    trace,
                    "validated",
                    status="active",
                    detail="Checking whether the signal is structurally complete.",
                )
                counts["parsed_signals"] += 1
                valid_for_backtest, errors = self.validator.validate_for_backtest_open(parsed)
                market_symbol = normalize_market_symbol(parsed.symbol) if parsed.symbol else None
                if valid_for_backtest and market_symbol is not None:
                    counts["valid_signals"] += 1
                    self._set_trace_stage(
                        trace,
                        "validated",
                        status="completed",
                        detail="Signal is structurally valid for backtesting.",
                    )
                    prefetched_candles = prefetched_candles_by_symbol.get(market_symbol)
                    selected_symbol = market_symbol
                    if prefetched_candles is None:
                        (
                            prefetched_candles,
                            selected_symbol,
                        ) = await self._prefetch_market_data_for_trace(
                            request=request,
                            trace=trace,
                            market_symbol=market_symbol,
                            progress_callback=progress_callback,
                            counts=counts,
                            warnings=warnings,
                        )
                        if prefetched_candles is not None:
                            prefetched_candles_by_symbol[selected_symbol] = prefetched_candles

                    if prefetched_candles is not None:
                        parsed = parsed.model_copy(update={"symbol": selected_symbol})
                        trace.symbol = selected_symbol
                        trace.final_status = "simulation_tracking"
                        trace.result_summary = (
                            "Signal validated, candle data loaded, and simulation tracking "
                            "started. Future updates and candle replay will finalize it."
                        )
                        self._set_trace_stage(
                            trace,
                            "market_data",
                            status="completed",
                            detail=(
                                f"Fetched {len(prefetched_candles)} candles for {selected_symbol}."
                            ),
                        )
                        self._set_trace_stage(
                            trace,
                            "simulated",
                            status="active",
                            detail=(
                                "Simulation tracking started; future channel updates and "
                                "candle resolution will determine the final trade outcome."
                            ),
                        )
                        symbol_trace_map.setdefault(selected_symbol, []).append(message.message_id)
                        if signal_id is not None:
                            signal_trace_map[signal_id] = message.message_id
                    else:
                        counts["invalid_signals"] += 1
                        trace.final_status = "market_data_unavailable"
                        trace.result_summary = (
                            f"No candle data returned for {market_symbol}. "
                            "Simulation cannot start for this signal."
                        )
                        self._set_trace_stage(
                            trace,
                            "market_data",
                            status="failed",
                            detail=trace.result_summary,
                        )
                        self._set_trace_stage(
                            trace,
                            "simulated",
                            status="skipped",
                            detail="Simulation skipped because market data was unavailable.",
                        )
                        self._set_trace_stage(
                            trace,
                            "finalized",
                            status="completed",
                            detail=trace.result_summary,
                        )
                        await self._maybe_send_message_log(request, trace, warnings)
                else:
                    counts["invalid_signals"] += 1
                    trace.final_status = "invalid_signal"
                    trace.result_summary = "; ".join(errors) or "Signal was not structurally valid."
                    self._set_trace_stage(
                        trace,
                        "validated",
                        status="failed",
                        detail=trace.result_summary,
                    )
                    self._set_trace_stage(
                        trace,
                        "market_data",
                        status="skipped",
                        detail="Candle lookup skipped because signal validation failed.",
                    )
                    self._set_trace_stage(
                        trace,
                        "simulated",
                        status="skipped",
                        detail="Simulation skipped because signal validation failed.",
                    )
                    self._set_trace_stage(
                        trace,
                        "finalized",
                        status="completed",
                        detail=trace.result_summary,
                    )
                    await self._maybe_send_message_log(request, trace, warnings)
            elif parsed_for_event.action is SignalAction.IGNORE:
                counts["ignored_messages"] += 1
                trace.final_status = "ignored"
                trace.result_summary = "Message was ignored by the parser."
                self._mark_non_signal_trace(trace, "Ignored message; no trading path.")
                await self._maybe_send_message_log(request, trace, warnings)
            elif parsed_for_event.action is SignalAction.UNKNOWN:
                counts["ambiguous_messages"] += 1
                trace.final_status = "ambiguous"
                trace.result_summary = "Message remained ambiguous after deterministic parsing."
                self._mark_non_signal_trace(trace, "Ambiguous message; admin/AI review needed.")
                await self._maybe_send_message_log(request, trace, warnings)
            else:
                trace.final_status = "follow_up"
                trace.result_summary = (
                    f"Detected follow-up action: {parsed_for_event.action.value}"
                )
                self._mark_non_signal_trace(trace, trace.result_summary)
                await self._maybe_send_message_log(request, trace, warnings)

            counts["classified_messages"] += 1
            self._emit_message_progress(
                progress_callback,
                phase="classify_messages",
                summary=f"Message {message.message_id} classified.",
                counts=counts,
                trace=trace,
            )
            events.append(
                BacktestEvent(
                    timestamp=message.date,
                    action=parsed_for_event.action,
                    signal_id=signal_id if classified.is_potential_new_signal else None,
                    parsed_signal=parsed_for_event,
                    related_signal_id=resolved_related_id,
                    debug_notes=list(trace.debug_notes),
                    source_message_id=message.message_id,
                    source_text=message.text,
                    close_fraction=extract_close_fraction(message.text),
                    move_stop_to_entry=detect_move_stop_to_entry(message.text),
                    leverage=parsed_for_event.leverage,
                )
            )
            if signal_id is not None and classified.is_potential_new_signal:
                event_index_by_signal_id[signal_id] = len(events) - 1
            live_metrics, live_signals = self._update_live_simulation_state(
                request=request,
                events=events,
                traces_by_message_id=traces_by_message_id,
                signal_trace_map=signal_trace_map,
                prefetched_candles_by_symbol=prefetched_candles_by_symbol,
                progress_callback=progress_callback,
                counts=counts,
                current_message_id=message.message_id,
            )
            self._emit_run_progress(
                progress_callback,
                phase="classify_messages",
                status="running",
                summary=f"Message {message.message_id} advanced live simulation state.",
                counts=counts,
                live_metrics=live_metrics,
                live_signals=live_signals,
            )
        return (
            events,
            traces_by_message_id,
            signal_trace_map,
            symbol_trace_map,
            counts,
            prefetched_candles_by_symbol,
        )

    async def _maybe_promote_reply_parent(
        self,
        *,
        message: RawTelegramMessage,
        context: ChannelContext,
        parsed_by_message_id: dict[int, ParsedSignal],
        raw_by_message_id: dict[int, RawTelegramMessage],
        events: list[BacktestEvent],
        event_index_by_signal_id: dict[str, int],
        signal_trace_map: dict[str, int],
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        symbol_trace_map: dict[str, list[int]],
        counts: dict[str, int],
        request: RealBacktestRunRequest,
        prefetched_candles_by_symbol: dict[str, list[Any]],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        warnings: list[str],
    ) -> str | None:
        """Promote a reply parent that holds an openable signal but was never tracked.

        When a follow-up replies to a message whose own OPEN signal was missed
        (e.g. classified as ambiguous), register that parent as a real signal and
        inject an OPEN event at the parent's timestamp. The simulator sorts events
        by timestamp, so the late injection is replayed in the correct order, and
        the current follow-up then correlates to a real, simulated position.
        """
        parent_id = message.reply_to_msg_id
        if parent_id is None:
            return None
        parent_parsed = parsed_by_message_id.get(parent_id)
        parent_raw = raw_by_message_id.get(parent_id)
        if parent_parsed is None or parent_raw is None:
            return None
        if parent_parsed.action is not SignalAction.OPEN or not parent_parsed.symbol:
            return None
        # Already tracked (as its own signal or attached to one)? Nothing to do.
        if parent_id in context.signal_by_message_id:
            return None
        signal_id = make_signal_id(parent_raw.channel_id, parent_id)
        if context.get_signal(signal_id) is not None:
            return None
        valid_for_backtest, _errors = self.validator.validate_for_backtest_open(parent_parsed)
        if not valid_for_backtest:
            return None
        base_trace = traces_by_message_id.get(parent_id)
        if base_trace is None:
            return None

        context.add_signal(
            SignalState(
                signal_id=signal_id,
                channel_id=parent_raw.channel_id,
                status=SignalStatus.PENDING_CONSOLIDATION,
                created_from_message_id=parent_id,
                related_message_ids=[parent_id],
                current_signal=parent_parsed,
                version=1,
                created_at=parent_raw.date,
                updated_at=parent_raw.date,
                expires_at=None,
            ),
            pending=True,
        )
        events.append(
            BacktestEvent(
                timestamp=parent_raw.date,
                action=parent_parsed.action,
                signal_id=signal_id,
                parsed_signal=parent_parsed,
                related_signal_id=None,
                debug_notes=["promoted_from_reply"],
                source_message_id=parent_id,
                source_text=parent_raw.text,
                close_fraction=extract_close_fraction(parent_raw.text),
                move_stop_to_entry=detect_move_stop_to_entry(parent_raw.text),
                leverage=parent_parsed.leverage,
            )
        )
        event_index_by_signal_id[signal_id] = len(events) - 1
        counts["parsed_signals"] += 1
        await self._sync_signal_tracking(
            request=request,
            context=context,
            signal_id=signal_id,
            traces_by_message_id=traces_by_message_id,
            signal_trace_map=signal_trace_map,
            event_index_by_signal_id=event_index_by_signal_id,
            symbol_trace_map=symbol_trace_map,
            events=events,
            prefetched_candles_by_symbol=prefetched_candles_by_symbol,
            progress_callback=progress_callback,
            counts=counts,
            warnings=warnings,
        )
        if signal_id in signal_trace_map:
            counts["valid_signals"] += 1
            return signal_id
        return None

    @staticmethod
    def _message_has_processible_text(message: RawTelegramMessage) -> bool:
        return bool((message.text or "").strip())

    async def _sync_signal_tracking(
        self,
        *,
        request: RealBacktestRunRequest,
        context: ChannelContext,
        signal_id: str,
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        signal_trace_map: dict[str, int],
        event_index_by_signal_id: dict[str, int],
        symbol_trace_map: dict[str, list[int]],
        events: list[BacktestEvent],
        prefetched_candles_by_symbol: dict[str, list[Any]],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        warnings: list[str],
    ) -> None:
        signal_state = context.get_signal(signal_id)
        if signal_state is None or signal_state.current_signal is None:
            return
        merged_signal = signal_state.current_signal
        # The base event for this signal IS the OPEN. context.merge_signal folds
        # follow-up actions (update_sl / close / ...) into current_signal.action,
        # which would otherwise overwrite the OPEN event's action when we write the
        # enriched signal back below — making the simulator skip it ("no_trade").
        # Keep the enriched entry/stop/tp/leverage but force the action to OPEN.
        if merged_signal.action is not SignalAction.OPEN:
            merged_signal = merged_signal.model_copy(update={"action": SignalAction.OPEN})
        base_message_id = signal_state.created_from_message_id
        base_trace = traces_by_message_id.get(base_message_id)
        if base_trace is None:
            return

        base_trace.signal_id = signal_id
        base_trace.symbol = merged_signal.symbol
        base_trace.side = merged_signal.side.value
        base_trace.confidence = str(merged_signal.confidence)
        base_index = event_index_by_signal_id.get(signal_id)
        if base_index is not None and base_index < len(events):
            events[base_index] = events[base_index].model_copy(
                update={"parsed_signal": merged_signal}
            )

        if signal_id in signal_trace_map:
            return

        valid_for_backtest, _errors = self.validator.validate_for_backtest_open(merged_signal)
        market_symbol = (
            normalize_market_symbol(merged_signal.symbol)
            if merged_signal.symbol is not None
            else None
        )
        if not valid_for_backtest or market_symbol is None:
            return

        prefetched_candles = prefetched_candles_by_symbol.get(market_symbol)
        selected_symbol = market_symbol
        if prefetched_candles is None:
            (
                prefetched_candles,
                selected_symbol,
            ) = await self._prefetch_market_data_for_trace(
                request=request,
                trace=base_trace,
                market_symbol=market_symbol,
                progress_callback=progress_callback,
                counts=counts,
                warnings=warnings,
            )
            if prefetched_candles is not None:
                prefetched_candles_by_symbol[selected_symbol] = prefetched_candles

        if prefetched_candles is None:
            return

        if base_trace.final_status in {"invalid_signal", "market_data_unavailable"}:
            if counts["invalid_signals"] > 0:
                counts["invalid_signals"] -= 1
            counts["valid_signals"] += 1

        merged_signal = merged_signal.model_copy(update={"symbol": selected_symbol})
        signal_state.current_signal = merged_signal
        base_trace.symbol = selected_symbol
        base_trace.debug_notes.append("signal_tracking_activated_from_follow_up")
        base_trace.final_status = "simulation_tracking"
        base_trace.result_summary = (
            "Signal became structurally tradable after follow-up messages. "
            "Simulation tracking is now active."
        )
        self._set_trace_stage(
            base_trace,
            "validated",
            status="completed",
            detail="Signal became structurally valid after related follow-up messages.",
            advance_current=False,
        )
        self._set_trace_stage(
            base_trace,
            "market_data",
            status="completed",
            detail=f"Fetched {len(prefetched_candles)} candles for {selected_symbol}.",
            advance_current=False,
        )
        self._set_trace_stage(
            base_trace,
            "simulated",
            status="active",
            detail=(
                "Simulation tracking activated after follow-up messages completed the "
                "signal structure."
            ),
        )
        symbol_trace_map.setdefault(selected_symbol, [])
        existing_ids = symbol_trace_map.get(selected_symbol, [])
        if base_message_id not in existing_ids:
            existing_ids.append(base_message_id)
            symbol_trace_map[selected_symbol] = existing_ids
        signal_trace_map[signal_id] = base_message_id
        if base_index is not None and base_index < len(events):
            events[base_index] = events[base_index].model_copy(
                update={"parsed_signal": merged_signal}
            )
        self._emit_message_progress(
            progress_callback,
            phase="classify_messages",
            summary=(
                f"Message {base_message_id} became simulation-ready after "
                f"follow-up message processing."
            ),
            counts=counts,
            trace=base_trace,
        )
        await self._maybe_send_message_log(
            request,
            base_trace,
            warnings,
            checkpoint="simulation_tracking",
        )

    async def _prefetch_market_data_for_trace(
        self,
        *,
        request: RealBacktestRunRequest,
        trace: RealBacktestMessageTrace,
        market_symbol: str,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        warnings: list[str],
    ) -> tuple[list[Any] | None, str]:
        candidate_symbols = market_symbol_candidates(market_symbol) or [market_symbol]
        self._set_trace_stage(
            trace,
            "market_data",
            status="active",
            detail=f"Fetching candle data for {market_symbol} immediately.",
        )
        self._emit_message_progress(
            progress_callback,
            phase="classify_messages",
            summary=f"Loading market data for message {trace.message_id}.",
            counts=counts,
            trace=trace,
        )
        last_error_type: str | None = None
        attempted: list[str] = []
        range_start, range_end = self._market_data_range_for_trace(request, trace)
        trace.debug_notes.append(f"market_data_start_utc={range_start.isoformat()}")
        trace.debug_notes.append(
            f"market_data_start_tehran={range_start.astimezone(TEHRAN_TZ).isoformat()}"
        )
        trace.debug_notes.append(f"market_data_end_utc={range_end.isoformat()}")
        for candidate_symbol in candidate_symbols:
            attempted.append(candidate_symbol)
            try:
                fetched = await self.market_data_provider.get_klines(
                    candidate_symbol,
                    request.interval,
                    range_start,
                    range_end,
                )
            except Exception as exc:
                last_error_type = type(exc).__name__
                continue
            if fetched:
                if candidate_symbol != market_symbol:
                    trace.debug_notes.append(f"market_symbol_selected={candidate_symbol}")
                return fetched, candidate_symbol

        if last_error_type is not None:
            self._append_warning(
                warnings,
                (
                    f"Immediate candle fetch failed for {market_symbol}; "
                    f"simulation could not start ({last_error_type})."
                ),
            )
        else:
            self._append_warning(
                warnings,
                (
                    f"Immediate candle fetch returned no data for {market_symbol}; "
                    "simulation could not start for this signal."
                ),
            )
        trace.debug_notes.append(
            "market_data_attempted=" + ",".join(attempted)
        )
        return None, market_symbol

    def _market_data_range_for_trace(
        self,
        request: RealBacktestRunRequest,
        trace: RealBacktestMessageTrace,
    ) -> tuple[datetime, datetime]:
        requested_start, requested_end = request.resolve_range()
        signal_time = self._to_utc(trace.message_date)
        if request.start_message_id is not None or request.start_message_link is not None:
            start = signal_time
        else:
            start = signal_time if signal_time > requested_start else requested_start
        minimum_end = signal_time + timedelta(
            hours=max(1, self.settings.REAL_BACKTEST_ACTIVE_SIGNAL_HOURS)
        )
        end = max(requested_end, minimum_end)
        return start, end

    @staticmethod
    def _to_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _update_live_simulation_state(
        self,
        *,
        request: RealBacktestRunRequest,
        events: list[BacktestEvent],
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        signal_trace_map: dict[str, int],
        prefetched_candles_by_symbol: dict[str, list[Any]],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        current_message_id: int,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        available_candles = [
            candle
            for candle_group in prefetched_candles_by_symbol.values()
            for candle in candle_group
        ]
        if not events or not available_candles or not signal_trace_map:
            return self._empty_live_metrics(), []

        simulator = BacktestEngine(classifier=RegexMessageClassifier()).simulator
        _trades, _balance, snapshots = simulator.simulate_with_snapshots(
            events=events,
            candles=available_candles,
            initial_balance=request.initial_balance,
            risk_per_trade_pct=request.risk_per_trade_pct,
            fill_policy=BacktestFillPolicy(self.settings.BACKTEST_DEFAULT_FILL_POLICY),
            active_signal_hours=self.settings.REAL_BACKTEST_ACTIVE_SIGNAL_HOURS,
            max_effective_leverage=Decimal(self.settings.BACKTEST_MAX_EFFECTIVE_LEVERAGE),
            default_stop_pct=Decimal(self.settings.BACKTEST_DEFAULT_STOP_PCT),
            strategy=self.strategy,
        )
        if not snapshots:
            return self._empty_live_metrics(), []
        snapshot = snapshots[-1]
        counts["trades_simulated"] = len(snapshot.signal_states)
        counts["trades_filled"] = sum(
            1 for state in snapshot.signal_states.values() if state.status != "not_filled"
        )
        metrics = self._live_metrics_from_snapshot(snapshot)
        live_signals = self._live_signals_from_snapshot(snapshot)
        self._apply_snapshot_to_traces(
            snapshot=snapshot,
            traces_by_message_id=traces_by_message_id,
            signal_trace_map=signal_trace_map,
            progress_callback=progress_callback,
            counts=counts,
            current_message_id=current_message_id,
            live_metrics=metrics,
            live_signals=live_signals,
        )
        return metrics, live_signals

    def _apply_snapshot_to_traces(
        self,
        *,
        snapshot: SimulationSnapshot,
        traces_by_message_id: dict[int, RealBacktestMessageTrace],
        signal_trace_map: dict[str, int],
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        current_message_id: int,
        live_metrics: dict[str, str],
        live_signals: list[dict[str, Any]],
    ) -> None:
        for signal_id, message_id in signal_trace_map.items():
            trace = traces_by_message_id.get(message_id)
            signal_state = snapshot.signal_states.get(signal_id)
            if trace is None or signal_state is None:
                continue
            if signal_state.status == "open":
                trace.final_status = "simulation_tracking"
                trace.result_summary = (
                    f"Live simulation through message {current_message_id}: "
                    f"mark={signal_state.mark_price}, open_qty={signal_state.open_quantity}, "
                    f"realized_pnl={signal_state.realized_pnl}, "
                    f"unrealized_pnl={signal_state.unrealized_pnl}"
                )
                self._set_trace_stage(
                    trace,
                    "simulated",
                    status="active",
                    detail=trace.result_summary,
                )
            elif signal_state.status == "not_filled":
                trace.final_status = "not_filled"
                trace.result_summary = "Entry conditions were not met in the available market data."
                self._set_trace_stage(
                    trace,
                    "simulated",
                    status="completed",
                    detail=trace.result_summary,
                )
                self._set_trace_stage(
                    trace,
                    "finalized",
                    status="completed",
                    detail=trace.result_summary,
                )
            else:
                trace.final_status = signal_state.status
                trace.result_summary = (
                    f"Trade {signal_state.status}. Exit={signal_state.exit_price}, "
                    f"PnL={signal_state.realized_pnl}"
                )
                self._set_trace_stage(
                    trace,
                    "simulated",
                    status="completed",
                    detail=trace.result_summary,
                )
                self._set_trace_stage(
                    trace,
                    "finalized",
                    status="completed",
                    detail=trace.result_summary,
                )
            self._emit_message_progress(
                progress_callback,
                phase="simulate",
                summary=f"Live simulation state updated for message {message_id}.",
                counts=counts,
                trace=trace,
                live_metrics=live_metrics,
                live_signals=live_signals,
            )

    @staticmethod
    def _live_metrics_from_snapshot(snapshot: SimulationSnapshot) -> dict[str, str]:
        return {
            "live_open_positions": str(snapshot.open_positions),
            "live_closed_trades": str(snapshot.closed_trades),
            "live_wins": str(snapshot.wins),
            "live_losses": str(snapshot.losses),
            "live_realized_pnl": str(snapshot.realized_pnl),
            "live_unrealized_pnl": str(snapshot.unrealized_pnl),
            "live_total_pnl": str(snapshot.total_pnl),
            "live_realized_balance": str(snapshot.realized_balance),
            "live_current_balance": str(snapshot.current_balance),
        }

    @staticmethod
    def _live_signals_from_snapshot(snapshot: SimulationSnapshot) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        for state in snapshot.signal_states.values():
            lifecycle: list[str] = []
            if state.targets_hit:
                lifecycle.append(f"targets_hit={state.targets_hit}")
            lifecycle.extend(state.notes)
            if state.exit_time is not None:
                lifecycle.append(f"exit_time={state.exit_time.isoformat()}")
            signals.append(
                {
                    "signal_id": state.signal_id,
                    "symbol": state.symbol,
                    "side": state.side.value,
                    "status": state.status,
                    "status_group": "active" if state.status == "open" else "inactive",
                    "entry_time": state.entry_time.isoformat(),
                    "entry_time_tehran": state.entry_time.astimezone(TEHRAN_TZ).isoformat(),
                    "exit_time": state.exit_time.isoformat() if state.exit_time else None,
                    "exit_time_tehran": (
                        state.exit_time.astimezone(TEHRAN_TZ).isoformat()
                        if state.exit_time
                        else None
                    ),
                    "original_quantity": str(state.original_quantity),
                    "entry_price": (
                        str(state.entry_price) if state.entry_price is not None else None
                    ),
                    "stop_loss": str(state.stop_loss) if state.stop_loss is not None else None,
                    "take_profits": [str(item) for item in state.take_profits],
                    "notional_value": str(state.notional_value),
                    "risk_amount": str(state.risk_amount),
                    "open_quantity": str(state.open_quantity),
                    "mark_price": str(state.mark_price),
                    "realized_pnl": str(state.realized_pnl),
                    "unrealized_pnl": str(state.unrealized_pnl),
                    "total_pnl": str(state.realized_pnl + state.unrealized_pnl),
                    "total_pnl_pct": str(state.total_pnl_pct),
                    "leverage": str(state.effective_leverage),
                    "margin": str(state.margin),
                    "targets_hit": state.targets_hit,
                    "lifecycle": lifecycle,
                }
            )
        return sorted(
            signals,
            key=lambda item: (
                item.get("status_group") != "active",
                str(item.get("entry_time") or ""),
            ),
        )

    @staticmethod
    def _empty_live_metrics() -> dict[str, str]:
        return {
            "live_open_positions": "0",
            "live_closed_trades": "0",
            "live_wins": "0",
            "live_losses": "0",
            "live_realized_pnl": "0",
            "live_unrealized_pnl": "0",
            "live_total_pnl": "0",
            "live_realized_balance": "0",
            "live_current_balance": "0",
        }

    async def _prepare_message_for_classification(
        self,
        *,
        message: RawTelegramMessage,
        trace: RealBacktestMessageTrace,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        counts: dict[str, int],
        warnings: list[str],
    ) -> RawTelegramMessage:
        payload = message.raw_payload
        needs_caption_media = (
            bool(payload.get("has_media")) and bool(payload.get("caption_present"))
        )
        if not needs_caption_media:
            return message

        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="running",
            summary=(
                f"On-demand media download for caption message {message.message_id} "
                "started."
            ),
            counts=counts,
        )
        self._set_trace_stage(
            trace,
            "preprocess",
            status="active",
            detail=(
                f"On-demand media download for caption message {message.message_id} "
                "started."
            ),
        )
        try:
            hydrated = await self.telegram_client.ensure_media_payload(message)
        except Exception as exc:
            self._append_warning(
                warnings,
                (
                    f"On-demand media download failed for message {message.message_id}; "
                    f"continuing without image context ({type(exc).__name__})."
                ),
            )
            self._set_trace_stage(
                trace,
                "preprocess",
                status="completed",
                detail=(
                    f"On-demand media download failed for message {message.message_id}; "
                    f"continuing without image context ({type(exc).__name__})."
                ),
            )
            return message

        hydrated_payload = hydrated.raw_payload
        if hydrated_payload.get("media_downloaded") is True:
            self._emit_run_progress(
                progress_callback,
                phase="classify_messages",
                status="running",
                summary=(
                    f"On-demand media download completed for message {message.message_id}."
                ),
                counts=counts,
            )
            self._set_trace_stage(
                trace,
                "preprocess",
                status="completed",
                detail=(
                    f"On-demand media download completed for message {message.message_id}."
                ),
            )
        else:
            self._set_trace_stage(
                trace,
                "preprocess",
                status="completed",
                detail=f"Message {message.message_id} was ready without media download.",
            )
        return hydrated

    def _make_trace(self, message: RawTelegramMessage) -> RealBacktestMessageTrace:
        stages = [
            RealBacktestMessageStage(key="received", label="Message Received"),
            RealBacktestMessageStage(key="preprocess", label="Preprocess"),
            RealBacktestMessageStage(key="classified", label="Classification"),
            RealBacktestMessageStage(key="validated", label="Signal Validation"),
            RealBacktestMessageStage(key="market_data", label="Market Data"),
            RealBacktestMessageStage(key="simulated", label="Trade Simulation"),
            RealBacktestMessageStage(key="finalized", label="Final Decision"),
        ]
        return RealBacktestMessageTrace(
            message_id=message.message_id,
            channel_id=message.channel_id,
            channel_username=message.channel_username,
            message_link=build_message_link(message.channel_username, message.message_id),
            message_date=message.date,
            full_text=message.text,
            preview_text=(message.text or "").strip()[:240],
            last_updated_at=message.date,
            stages=stages,
        )

    def _set_trace_stage(
        self,
        trace: RealBacktestMessageTrace,
        key: str,
        *,
        status: Literal["pending", "active", "completed", "failed", "skipped"],
        detail: str,
        advance_current: bool = True,
    ) -> None:
        if not trace._stage_index:
            trace._stage_index = {s.key: i for i, s in enumerate(trace.stages)}

        index = trace._stage_index.get(key)
        if index is None:
            return

        stage = trace.stages[index]
        now = datetime.now(timezone.utc)
        if status == "active" and stage.started_at is None:
            stage.started_at = now
        if status in {"completed", "failed", "skipped"}:
            if stage.started_at is None:
                stage.started_at = now
            stage.finished_at = now
            stage.duration_ms = max(
                0,
                int((stage.finished_at - stage.started_at).total_seconds() * 1000),
            )
        elif status == "active" and stage.started_at is not None:
            stage.duration_ms = max(
                0,
                int((now - stage.started_at).total_seconds() * 1000),
            )
        stage.status = status
        stage.detail = detail
        if advance_current:
            trace.current_stage = key
        trace.last_updated_at = now
        trace.refresh_processing_duration(now)

    def _mark_non_signal_trace(self, trace: RealBacktestMessageTrace, detail: str) -> None:
        self._set_trace_stage(
            trace,
            "validated",
            status="skipped",
            detail="Validation skipped because this was not a new OPEN signal.",
        )
        self._set_trace_stage(
            trace,
            "market_data",
            status="skipped",
            detail="Market data skipped because no trade candidate was produced.",
        )
        self._set_trace_stage(
            trace,
            "simulated",
            status="skipped",
            detail="Simulation skipped because no trade candidate was produced.",
        )
        self._set_trace_stage(trace, "finalized", status="completed", detail=detail)

    def _emit_run_progress(
        self,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        *,
        phase: str,
        status: Literal["queued", "running", "completed", "failed"],
        summary: str,
        counts: dict[str, int] | None = None,
        live_metrics: dict[str, str] | None = None,
        live_signals: list[dict[str, Any]] | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            RealBacktestProgressEvent(
                event_type="run",
                timestamp=datetime.now(timezone.utc),
                phase=phase,
                status=status,
                summary=summary,
                counts=counts or {},
                live_metrics=live_metrics,
                live_signals=live_signals,
            )
        )

    def _emit_message_progress(
        self,
        progress_callback: Callable[[RealBacktestProgressEvent], None] | None,
        *,
        phase: str,
        summary: str,
        counts: dict[str, int],
        trace: RealBacktestMessageTrace,
        live_metrics: dict[str, str] | None = None,
        live_signals: list[dict[str, Any]] | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            RealBacktestProgressEvent(
                event_type="message",
                timestamp=datetime.now(timezone.utc),
                phase=phase,
                status="running",
                summary=summary,
                current_message_id=trace.message_id,
                counts=counts,
                live_metrics=live_metrics,
                live_signals=live_signals,
                trace=trace.model_copy(deep=True),
            )
        )

    def _classify_label(self, classified: ClassifiedMessage) -> str:
        if classified.is_potential_new_signal:
            return "new_signal"
        if classified.is_related_to_existing_signal:
            return "related_update"
        action = classified.parsed_signal.action
        if action is SignalAction.IGNORE:
            return "ignored"
        if action is SignalAction.UNKNOWN:
            return "ambiguous"
        return str(action.value)

    def _build_symbol_candidates_by_primary(
        self,
        events: list[BacktestEvent],
    ) -> dict[str, list[str]]:
        candidates_by_primary: dict[str, list[str]] = {}
        for event in events:
            candidates = market_symbol_candidates(event.parsed_signal.symbol)
            if not candidates:
                continue
            primary = candidates[0]
            existing = candidates_by_primary.setdefault(primary, [])
            for candidate in candidates:
                if candidate not in existing:
                    existing.append(candidate)
        return candidates_by_primary

    def _format_trace_for_telegram(self, trace: RealBacktestMessageTrace) -> str:
        emoji = {
            "awaiting_market_data": "🟡",
            "invalid_signal": "🟥",
            "ignored": "⚪️",
            "ambiguous": "🟠",
            "follow_up": "🔵",
            "tp_hit": "🟢",
            "sl_hit": "🔴",
            "not_filled": "🟣",
            "open_until_end": "🟤",
        }.get(trace.final_status, "📌")
        lines = [
            f"{emoji} <b>Backtest Message Trace</b>",
            "",
            f"🔗 <b>Source</b>: {self._tg(trace.channel_username or trace.channel_id)}",
            f"🆔 <b>Message ID</b>: {trace.message_id}",
            f"🌐 <b>Message Link</b>: {self._tg(trace.message_link or 'not available')}",
            (
                "🕒 <b>Message Time</b>: "
                f"{trace.message_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            ),
            f"🧭 <b>Classification</b>: {self._tg(trace.classification or 'unknown')}",
            f"⚙️ <b>Action</b>: {self._tg(trace.parsed_action or 'unknown')}",
            f"🪙 <b>Symbol</b>: {self._tg(trace.symbol or 'none')}",
            f"📈 <b>Confidence</b>: {self._tg(trace.confidence or 'n/a')}",
            f"🏁 <b>Final Status</b>: {self._tg(trace.final_status)}",
            (
                "⏱ <b>Processing Duration</b>: "
                f"{self._tg(self._format_duration(trace.processing_duration_ms))}"
            ),
            "",
            "🧱 <b>Stages</b>:",
        ]
        for stage in trace.stages:
            stage_emoji = {
                "pending": "▫️",
                "active": "✨",
                "completed": "✅",
                "failed": "❌",
                "skipped": "⏭️",
            }[stage.status]
            lines.append(
                f"{stage_emoji} <b>{self._tg(stage.label)}</b>: "
                f"{self._tg(stage.detail or stage.status)}"
            )
            lines.append(
                f"   ↳ duration={self._tg(self._format_duration(stage.duration_ms))}"
            )
        if trace.preview_text:
            lines.extend(["", f"📝 <b>Preview</b>: {self._tg(trace.preview_text)}"])
        if trace.result_summary:
            lines.extend(["", f"📌 <b>Result</b>: {self._tg(trace.result_summary)}"])
        if trace.debug_notes:
            lines.extend(["", "🧪 <b>Debug Notes</b>:"])
            lines.extend(f"• {self._tg(note)}" for note in trace.debug_notes[:6])
        return "\n".join(lines)

    @staticmethod
    def _tg(value: object) -> str:
        return escape(str(value), quote=False)

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if duration_ms is None:
            return "n/a"
        if duration_ms < 1000:
            return f"{duration_ms} ms"
        seconds = duration_ms / 1000
        return f"{seconds:.2f} s"
