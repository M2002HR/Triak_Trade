"""Real Telegram + public market-data backtest runner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from triak_trade.agents.classifier import MessageClassifier, RegexMessageClassifier
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.backtesting.engine import BacktestEngine
from triak_trade.backtesting.models import BacktestRequest
from triak_trade.backtesting.report import report_to_json, report_to_markdown_summary
from triak_trade.backtesting.report_store import BacktestReportStore
from triak_trade.backtesting.symbol_mapper import normalize_market_symbol
from triak_trade.backtesting.telegram_source import BacktestTelegramSource
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import BacktestFillPolicy, SignalAction
from triak_trade.market_data.interfaces import MarketDataProvider
from triak_trade.market_data.toobit import ToobitMarketDataProvider
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.client import TelegramClientInterface
from triak_trade.telegram.telethon_client import TelegramCredentialError, TelethonTelegramClient


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

    async def run(self, request: RealBacktestRunRequest) -> RealBacktestResult:
        readiness = self.readiness()
        from_date, to_date = request.resolve_range()
        if not readiness.ready:
            return self._write_failure(
                channel=request.channel,
                from_date=from_date,
                to_date=to_date,
                interval=request.interval,
                errors=readiness.issues,
            )

        selection = self._select_classifier(request.use_ai)
        if request.send_log_channel:
            await self._send_log(
                f"Real backtest started\nchannel={request.channel}\ninterval={request.interval}\n"
                f"range={from_date.isoformat()} -> {to_date.isoformat()}"
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

        engine = BacktestEngine(classifier=selection.classifier)
        events = engine.build_events(channel_id=request.channel, messages=messages)
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
        ignored_messages = sum(1 for event in events if event.action is SignalAction.IGNORE)
        ambiguous_messages = sum(1 for event in events if event.action is SignalAction.UNKNOWN)
        invalid_signals = len(open_events) - len(valid_open_events)
        ai_used = any(
            any(note.startswith("classification=") for note in event.debug_notes)
            for event in events
        )
        regex_fallback_used = (
            any("ai-fallback=regex" in note for event in events for note in event.debug_notes)
            or isinstance(selection.classifier, RegexMessageClassifier)
        )
        warnings: list[str] = []
        if selection.warning:
            warnings.append(selection.warning)
        if request.use_ai and not ai_used and regex_fallback_used and not selection.warning:
            warnings.append(
                "AI gateway unavailable or failed during classification; regex fallback used."
            )

        if request.send_log_channel:
            await self._send_log(
                "Real backtest history fetched\n"
                f"channel={request.channel}\n"
                f"messages={len(messages)}\n"
                f"symbols_detected={len(symbols)}"
            )

        if not messages:
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
                continue
            if fetched:
                real_market_data_used = True
                candles.extend(fetched)
            else:
                skipped_reasons.append(f"{symbol}: no candle data returned")

        if request.send_log_channel:
            await self._send_log(
                f"Real backtest candles fetched\nchannel={request.channel}\n"
                f"candles={len(candles)}\nreal_market_data_used={real_market_data_used}"
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
        report = engine.run_from_events(
            request=report_request,
            events=events,
            candles=candles,
        )
        score = Decimal(report.warnings[0].split("=")[1]) if report.warnings else Decimal("0")
        trades_filled = sum(1 for trade in report.trades if trade.status != "not_filled")
        wins = sum(1 for trade in report.trades if trade.pnl > 0)
        losses = sum(1 for trade in report.trades if trade.pnl < 0)
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
        stored = self.report_store.write(self._build_payload(result, report, score))
        result.report_path = stored.json_path
        result.markdown_report_path = stored.markdown_path

        if request.send_log_channel:
            await self._send_log(
                f"Real backtest complete\nchannel={request.channel}\n"
                f"messages={result.total_messages}\nvalid_signals={result.valid_signals}\n"
                f"trades={result.trades_simulated}\npnl={result.total_pnl}\n"
                f"report={result.markdown_report_path}"
            )
        return result

    def run_sync(self, request: RealBacktestRunRequest) -> RealBacktestResult:
        return asyncio.run(self.run(request))

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
