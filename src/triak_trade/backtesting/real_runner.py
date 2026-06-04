"""Real Telegram + public market-data backtest runner."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestEvent, BacktestRequest
from triak_trade.backtesting.report import report_to_json, report_to_markdown_summary
from triak_trade.backtesting.report_store import BacktestReportStore
from triak_trade.backtesting.symbol_mapper import normalize_market_symbol
from triak_trade.backtesting.telegram_source import BacktestTelegramSource
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import BacktestFillPolicy, SignalAction, SignalStatus
from triak_trade.domain.ids import make_signal_id
from triak_trade.domain.models import RawTelegramMessage, SignalState
from triak_trade.market_data.interfaces import MarketDataProvider
from triak_trade.market_data.toobit import ToobitMarketDataProvider
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
    debug_notes: list[str] = Field(default_factory=list)
    stages: list[RealBacktestMessageStage] = Field(default_factory=list)

    _stage_index: dict[str, int] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._stage_index = {stage.key: index for index, stage in enumerate(self.stages)}


class RealBacktestProgressEvent(BaseModel):
    event_type: Literal["run", "message"]
    timestamp: datetime
    phase: str
    status: Literal["queued", "running", "completed", "failed"]
    summary: str
    current_message_id: int | None = None
    counts: dict[str, int] = Field(default_factory=dict)
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
    interval: str
    max_messages: int
    use_ai: bool
    send_telegram_summary: bool
    send_log_channel: bool
    log_per_message: bool = False

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
    ) -> None:
        self.settings = settings
        self.telegram_client = telegram_client or TelethonTelegramClient(settings)
        self.telegram_source = BacktestTelegramSource(self.telegram_client)
        self.market_data_provider = market_data_provider or ToobitMarketDataProvider(
            base_url=settings.TOOBIT_BASE_URL,
            klines_path=settings.TOOBIT_KLINES_PATH,
            timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
            limit=settings.TOOBIT_MARKET_DATA_LIMIT,
        )
        self.report_store = report_store or BacktestReportStore(settings.REAL_BACKTEST_REPORT_DIR)
        self.log_client = log_client or TelegramLogChannelClient(settings=settings)
        self.validator = ParsedSignalValidator()
        self._log_lock = threading.Lock()

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
        toobit_ready = bool(self.settings.TOOBIT_BASE_URL and self.settings.TOOBIT_KLINES_PATH)
        if not self.settings.REAL_BACKTEST_ENABLED:
            issues.append("REAL_BACKTEST_ENABLED=true is required")
        if self.settings.RUN_BACKTEST_INTEGRATION_TESTS != 1:
            issues.append("RUN_BACKTEST_INTEGRATION_TESTS=1 is required")
        if self.settings.RUN_TELEGRAM_INTEGRATION_TESTS != 1:
            issues.append("RUN_TELEGRAM_INTEGRATION_TESTS=1 is required")
        if self.settings.RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS != 1:
            issues.append("RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1 is required")
        if not telegram_credentials_present:
            issues.append("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured")
        if not telegram_session_configured:
            issues.append("TELEGRAM_SESSION_NAME and TELEGRAM_SESSION_DIR must be configured")
        if not toobit_ready:
            issues.append("Toobit public market-data settings are incomplete")
        Path(self.settings.REAL_BACKTEST_REPORT_DIR).mkdir(parents=True, exist_ok=True)
        return RealBacktestReadiness(
            ready=not issues,
            issues=issues,
            real_backtest_enabled=self.settings.REAL_BACKTEST_ENABLED,
            telegram_credentials_present=telegram_credentials_present,
            telegram_session_configured=telegram_session_configured,
            toobit_public_market_ready=toobit_ready,
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
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=readiness.issues,
            )

        selection = self._select_classifier(request.use_ai)
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
            "classified_messages": 0,
            "parsed_signals": 0,
            "valid_signals": 0,
            "invalid_signals": 0,
            "ignored_messages": 0,
            "ambiguous_messages": 0,
        }
        self._emit_run_progress(
            progress_callback,
            phase="fetch_history",
            status="completed",
            summary=f"Fetched {len(messages)} Telegram messages.",
            counts=counts,
        )

        engine = BacktestEngine(classifier=selection.classifier)
        self._emit_run_progress(
            progress_callback,
            phase="classify_messages",
            status="running",
            summary="Classifying and validating channel messages one by one.",
            counts=counts,
        )
        (
            events,
            traces_by_message_id,
            signal_trace_map,
            symbol_trace_map,
            counts,
        ) = await self._build_events_with_traces(
            request=request,
            classifier=selection.classifier,
            messages=messages,
            progress_callback=progress_callback,
            counts=counts,
            warnings=warnings,
        )
        open_events = [event for event in events if event.action is SignalAction.OPEN]
        valid_open_events = [
            event
            for event in open_events
            if self.validator.validate_for_proposal(
                event.parsed_signal,
                max_leverage=self.settings.MAX_LEVERAGE,
                require_stop_loss=self.settings.REQUIRE_STOP_LOSS,
            )[0]
        ]
        symbols = sorted({
            symbol
            for symbol in (
                normalize_market_symbol(event.parsed_signal.symbol)
                for event in valid_open_events
            )
            if symbol is not None
        })
        ignored_messages = counts["ignored_messages"]
        ambiguous_messages = counts["ambiguous_messages"]
        invalid_signals = counts["invalid_signals"]
        ai_used = any(
            any(note.startswith("classification=") for note in event.debug_notes)
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
                classified_messages=len(events),
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
            try:
                fetched = await self.market_data_provider.get_klines(
                    symbol,
                    request.interval,
                    from_date,
                    to_date,
                )
            except Exception as exc:
                skipped_reasons.append(f"{symbol}: candle fetch failed ({type(exc).__name__})")
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id[message_id]
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="failed",
                        detail=f"Candle fetch failed for {symbol}: {type(exc).__name__}",
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"Market data failed for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
                continue
            if fetched:
                real_market_data_used = True
                candles.extend(fetched)
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id[message_id]
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="completed",
                        detail=f"Fetched {len(fetched)} candles for {symbol}.",
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"Candles ready for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )
            else:
                skipped_reasons.append(f"{symbol}: no candle data returned")
                for message_id in symbol_trace_map.get(symbol, []):
                    message_trace = traces_by_message_id[message_id]
                    self._set_trace_stage(
                        message_trace,
                        "market_data",
                        status="failed",
                        detail=f"No candle data returned for {symbol}.",
                    )
                    self._emit_message_progress(
                        progress_callback,
                        phase="fetch_market_data",
                        summary=f"No candle data returned for message {message_id}.",
                        counts=counts,
                        trace=message_trace,
                    )

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
                classified_messages=len(events),
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
            initial_balance=self.settings.BACKTEST_DEFAULT_INITIAL_BALANCE,
            interval=request.interval,
            fill_policy=BacktestFillPolicy(self.settings.BACKTEST_DEFAULT_FILL_POLICY),
            risk_per_trade_pct=self.settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT,
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
        report = engine.run_from_events(
            request=report_request,
            events=events,
            candles=candles,
        )
        score = Decimal(report.warnings[0].split("=")[1]) if report.warnings else Decimal("0")
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
            classified_messages=len(events),
            parsed_signals=len(open_events),
            valid_signals=len(valid_open_events),
            invalid_signals=invalid_signals,
            ignored_messages=ignored_messages,
            ambiguous_messages=ambiguous_messages,
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
            )
            classifier: MessageClassifier = AIMessageClassifier(
                settings=self.settings,
                gateway_client=client,
                regex_fallback=RegexMessageClassifier(),
            )
            return _ClassificationSelection(
                classifier=classifier,
                ai_requested=True,
                ai_configured=True,
                warning=None,
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

    async def _send_log(self, text: str) -> None:
        if not self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL:
            return
        await self.log_client.send_text(text, real=True)

    async def _try_send_log(
        self,
        text: str,
        *,
        warnings: list[str] | None,
        warning_message: str,
    ) -> bool:
        try:
            await self._send_log(text)
        except Exception as exc:
            if warnings is not None:
                self._append_warning(warnings, f"{warning_message} ({type(exc).__name__})")
            return False
        return True

    async def _maybe_send_message_log(
        self,
        request: RealBacktestRunRequest,
        trace: RealBacktestMessageTrace,
        warnings: list[str],
    ) -> None:
        if not (request.send_log_channel and request.log_per_message):
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
            trace.debug_notes.append("telegram_message_log_failed")

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
    ) -> tuple[
        list[BacktestEvent],
        dict[int, RealBacktestMessageTrace],
        dict[str, int],
        dict[str, list[int]],
        dict[str, int],
    ]:
        context = ChannelContext(
            channel_id=request.channel,
            max_message_limit=max(
                request.max_messages,
                self.settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
            ),
            max_update_window_hours=self.settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
        )
        events: list[BacktestEvent] = []
        traces_by_message_id: dict[int, RealBacktestMessageTrace] = {}
        signal_trace_map: dict[str, int] = {}
        symbol_trace_map: dict[str, list[int]] = {}
        sorted_messages = sorted(messages, key=lambda item: item.date)

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
            classified = classifier.classify(message, context)
            parsed = classified.parsed_signal
            trace.classification = self._classify_label(classified)
            trace.parsed_action = parsed.action.value
            trace.symbol = normalize_market_symbol(parsed.symbol) or parsed.symbol
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

            signal_id: str | None = None
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
            elif classified.related_signal_id is not None:
                signal_id = classified.related_signal_id
                trace.signal_id = signal_id
                context.attach_message(signal_id, message)
                context.merge_signal(signal_id, parsed, message.date)

            if parsed.action is SignalAction.OPEN:
                self._set_trace_stage(
                    trace,
                    "validated",
                    status="active",
                    detail="Checking whether the signal is structurally complete.",
                )
                valid, errors = self.validator.validate_for_proposal(
                    parsed,
                    max_leverage=self.settings.MAX_LEVERAGE,
                    require_stop_loss=self.settings.REQUIRE_STOP_LOSS,
                )
                counts["parsed_signals"] += 1
                if valid and trace.symbol is not None:
                    counts["valid_signals"] += 1
                    trace.final_status = "awaiting_market_data"
                    self._set_trace_stage(
                        trace,
                        "validated",
                        status="completed",
                        detail="Signal is structurally valid for backtesting.",
                    )
                    self._set_trace_stage(
                        trace,
                        "market_data",
                        status="pending",
                        detail=f"Waiting for {trace.symbol} candle data.",
                    )
                    symbol_trace_map.setdefault(trace.symbol, []).append(message.message_id)
                    if signal_id is not None:
                        signal_trace_map[signal_id] = message.message_id
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
            elif parsed.action is SignalAction.IGNORE:
                counts["ignored_messages"] += 1
                trace.final_status = "ignored"
                trace.result_summary = "Message was ignored by the parser."
                self._mark_non_signal_trace(trace, "Ignored message; no trading path.")
                await self._maybe_send_message_log(request, trace, warnings)
            elif parsed.action is SignalAction.UNKNOWN:
                counts["ambiguous_messages"] += 1
                trace.final_status = "ambiguous"
                trace.result_summary = "Message remained ambiguous after deterministic parsing."
                self._mark_non_signal_trace(trace, "Ambiguous message; admin/AI review needed.")
                await self._maybe_send_message_log(request, trace, warnings)
            else:
                trace.final_status = "follow_up"
                trace.result_summary = f"Detected follow-up action: {parsed.action.value}"
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
                    action=parsed.action,
                    signal_id=signal_id if classified.is_potential_new_signal else None,
                    parsed_signal=parsed,
                    related_signal_id=classified.related_signal_id,
                    debug_notes=list(classified.debug_notes),
                )
            )
        return events, traces_by_message_id, signal_trace_map, symbol_trace_map, counts

    def _make_trace(self, message: RawTelegramMessage) -> RealBacktestMessageTrace:
        stages = [
            RealBacktestMessageStage(key="received", label="Message Received"),
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
    ) -> None:
        index = trace._stage_index[key]
        stage = trace.stages[index]
        now = datetime.now(timezone.utc)
        if status == "active" and stage.started_at is None:
            stage.started_at = now
        if status in {"completed", "failed", "skipped"}:
            if stage.started_at is None:
                stage.started_at = now
            stage.finished_at = now
        stage.status = status
        stage.detail = detail
        trace.current_stage = key
        trace.last_updated_at = now

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
            f"🔗 <b>Source</b>: {trace.channel_username or trace.channel_id}",
            f"🆔 <b>Message ID</b>: {trace.message_id}",
            f"🌐 <b>Message Link</b>: {trace.message_link or 'not available'}",
            (
                "🕒 <b>Message Time</b>: "
                f"{trace.message_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            ),
            f"🧭 <b>Classification</b>: {trace.classification or 'unknown'}",
            f"⚙️ <b>Action</b>: {trace.parsed_action or 'unknown'}",
            f"🪙 <b>Symbol</b>: {trace.symbol or 'none'}",
            f"📈 <b>Confidence</b>: {trace.confidence or 'n/a'}",
            f"🏁 <b>Final Status</b>: {trace.final_status}",
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
            lines.append(f"{stage_emoji} <b>{stage.label}</b>: {stage.detail or stage.status}")
        if trace.preview_text:
            lines.extend(["", f"📝 <b>Preview</b>: {trace.preview_text}"])
        if trace.result_summary:
            lines.extend(["", f"📌 <b>Result</b>: {trace.result_summary}"])
        if trace.debug_notes:
            lines.extend(["", "🧪 <b>Debug Notes</b>:"])
            lines.extend(f"• {note}" for note in trace.debug_notes[:6])
        return "\n".join(lines)
