"""CLI entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import httpx
import typer

from triak_trade import __version__
from triak_trade.admin_bot.auth import AdminAuthService, normalize_username
from triak_trade.admin_bot.callbacks import parse_admin_callback
from triak_trade.admin_bot.errors import AdminUnauthorizedError
from triak_trade.admin_bot.formatter import AdminActionFormatter
from triak_trade.admin_bot.runtime import (
    dump_json,
    get_admin_bot_status,
    run_admin_bot_smoke_test,
    run_admin_bot_sync,
    start_admin_bot_process,
    stop_admin_bot_process,
    tail_admin_bot_logs,
)
from triak_trade.admin_bot.service import AdminApprovalService
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.agents.context import ChannelContext
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.ai.runtime import (
    ai_gateway_logs,
    ai_gateway_safe_config,
    ai_gateway_status,
    ensure_local_ai_gateway_ready,
    start_ai_gateway_process,
    stop_ai_gateway_process,
)
from triak_trade.backtesting import (
    BacktestEngine,
    BacktestRequest,
    RealBacktestRunner,
    RealBacktestRunRequest,
    run_fixture_backtest,
)
from triak_trade.config.settings import Settings, get_settings
from triak_trade.core.health import run_health_checks
from triak_trade.core.logging import configure_logging
from triak_trade.core.time import parse_user_datetime_to_utc
from triak_trade.dashboard.runtime import (
    dashboard_logs,
    dashboard_safe_config,
    dashboard_smoke_test,
    dashboard_status,
    dashboard_token_hint,
    run_dashboard,
    start_dashboard_process,
    stop_dashboard_process,
)
from triak_trade.db.engine import build_engine_from_settings
from triak_trade.domain.enums import (
    BacktestFillPolicy,
    CandleSource,
    ProposedActionType,
    SignalStatus,
)
from triak_trade.domain.models import Candle, ProposedAction, RawTelegramMessage, SignalState
from triak_trade.exchange.toobit.account import ToobitAccountClient
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.demo_execution import DemoExecutionAdapter
from triak_trade.exchange.toobit.errors import ToobitError
from triak_trade.exchange.toobit.spot import ToobitSpotClient
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.toobit import ToobitMarketDataProvider
from triak_trade.observability.formatters import format_processing_audit_for_telegram
from triak_trade.observability.processing_audit import (
    ProcessingAuditService,
    build_sample_processing_audit_event,
)
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.client import FakeTelegramClient, TelegramClientInterface
from triak_trade.telegram.telethon_client import TelethonTelegramClient
from triak_trade.verification.models import VerificationStatus
from triak_trade.verification.report import find_latest_report, render_terminal_summary
from triak_trade.verification.runner import VerificationMode, VerificationRunner

app = typer.Typer(no_args_is_help=True)


def _load_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings)
    return settings


def _build_toobit_client(settings: Settings) -> ToobitClient:
    return ToobitClient(
        base_url=settings.TOOBIT_BASE_URL,
        api_key=settings.TOOBIT_API_KEY.get_secret_value(),
        api_secret=settings.TOOBIT_API_SECRET.get_secret_value(),
        timeout_seconds=settings.TOOBIT_SIGNED_TIMEOUT_SECONDS,
        recv_window=settings.TOOBIT_RECV_WINDOW,
        time_path=settings.TOOBIT_TIME_PATH,
        exchange_info_path=settings.TOOBIT_EXCHANGE_INFO_PATH,
    )


def _build_binance_public_provider(settings: Settings) -> BinancePublicFuturesProvider:
    return BinancePublicFuturesProvider(
        base_url=settings.BINANCE_PUBLIC_DATA_BASE_URL,
        rest_base_url=settings.BINANCE_FUTURES_REST_BASE_URL,
        klines_path=settings.BINANCE_FUTURES_KLINES_PATH,
        ticker_price_path=settings.BINANCE_FUTURES_TICKER_PRICE_PATH,
        cache_dir=settings.BINANCE_PUBLIC_DATA_CACHE_DIR,
        timeout_seconds=settings.BINANCE_PUBLIC_DATA_TIMEOUT_SECONDS,
    )


def _build_real_backtest_runner(
    settings: Settings,
    telegram_client: TelegramClientInterface | None = None,
) -> RealBacktestRunner:
    return RealBacktestRunner(settings=settings, telegram_client=telegram_client)


def _build_ai_gateway_client(
    settings: Settings,
    *,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> AjilGatewayClient:
    return AjilGatewayClient(
        base_url=base_url or settings.AI_GATEWAY_BASE_URL,
        timeout_seconds=settings.AI_GATEWAY_TIMEOUT_SECONDS,
        classify_path=settings.AI_GATEWAY_CLASSIFY_PATH,
        auth_header_name=settings.AI_GATEWAY_AUTH_HEADER_NAME,
        auth_token=settings.AI_GATEWAY_AUTH_TOKEN.get_secret_value(),
        default_model=settings.AI_GATEWAY_DEFAULT_MODEL,
        provider_priority=tuple(
            item.strip()
            for item in settings.AI_GATEWAY_PROVIDER_PRIORITY.split(",")
            if item.strip()
        ),
        text_provider=settings.AI_CLASSIFIER_TEXT_PROVIDER,
        text_model=settings.AI_CLASSIFIER_TEXT_MODEL,
        vision_provider=settings.AI_CLASSIFIER_VISION_PROVIDER,
        vision_model=settings.AI_CLASSIFIER_VISION_MODEL,
        trust_env=settings.AI_GATEWAY_TRUST_ENV,
        retry_attempts=settings.AI_GATEWAY_RETRY_ATTEMPTS,
        retry_backoff_seconds=settings.AI_GATEWAY_RETRY_BACKOFF_SECONDS,
        transport=transport,
    )


@app.command("version")
def version_cmd() -> None:
    """Show app version."""
    typer.echo(__version__)


@app.command("health")
def health_cmd(
    include_services: bool = typer.Option(False, help="Include DB/Redis checks."),
) -> None:
    """Run health checks."""
    settings = _load_settings()
    result = run_health_checks(settings=settings, include_services=include_services)
    typer.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@app.command("config-check")
def config_check_cmd() -> None:
    """Validate config and print safe status."""
    _load_settings()
    typer.echo("Configuration is valid")


@app.command("db-check")
def db_check_cmd() -> None:
    """Build DB engine from config without connecting."""
    settings = _load_settings()
    engine = build_engine_from_settings(settings)
    typer.echo(f"DB engine configured (dialect={engine.dialect.name})")


@app.command("parse-message")
def parse_message_cmd(message: str) -> None:
    """Normalize, parse, and validate a single message safely."""
    settings = _load_settings()

    raw = RawTelegramMessage(
        channel_id="cli",
        channel_username=None,
        message_id=1,
        text=message,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()
    validator = ParsedSignalValidator()

    normalized = normalizer.normalize(raw)
    parsed = parser.parse(normalized)
    ok, errors = validator.validate_for_proposal(
        parsed,
        max_leverage=settings.MAX_LEVERAGE,
        require_stop_loss=settings.REQUIRE_STOP_LOSS,
    )

    payload = {
        "normalized_text": normalized.normalized_text,
        "detected_symbols": normalized.detected_symbols,
        "detected_keywords": normalized.detected_keywords,
        "parsed": parsed.model_dump(mode="json"),
        "proposal_valid": ok,
        "validation_errors": errors,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("agent-dry-run")
def agent_dry_run_cmd() -> None:
    """Run a deterministic in-memory channel agent simulation."""
    settings = _load_settings()
    start = datetime.now(timezone.utc)
    clock = FakeClock(start)
    agent = ChannelAgent(channel_id="dry-run-channel", settings=settings, clock=clock)

    sequence = [
        (0, "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x"),
        (30, "SL: 67400"),
        (60, "TP: 69000 / 70000"),
        (90, "Join VIP now!"),
    ]

    immediate_actions: list[dict[str, object]] = []
    for idx, (offset_sec, text) in enumerate(sequence, start=1):
        clock.advance(seconds=offset_sec - (0 if idx == 1 else sequence[idx - 2][0]))
        message = RawTelegramMessage(
            channel_id="dry-run-channel",
            channel_username="dry",
            message_id=idx,
            text=text,
            date=clock.now(),
            edited_at=None,
            reply_to_msg_id=1 if idx in {2, 3} else None,
        )
        for action in agent.ingest_message(message):
            immediate_actions.append(action.model_dump(mode="json"))

    clock.advance(seconds=max(0, settings.SIGNAL_CONSOLIDATION_SECONDS - 90))
    tick_actions = [action.model_dump(mode="json") for action in agent.tick(clock.now())]

    snapshot = agent.get_context_snapshot()
    payload = {
        "pending_signal_ids": snapshot.get("pending_signal_ids", []),
        "signal_statuses": snapshot.get("signals", {}),
        "immediate_actions": immediate_actions,
        "tick_actions": tick_actions,
        "counts": {
            "immediate_actions": len(immediate_actions),
            "tick_actions": len(tick_actions),
        },
        "safety": {"requires_admin_approval_default": True, "no_execution": True},
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("ai-classify-dry-run")
def ai_classify_dry_run_cmd(message: str, real_gateway: bool = typer.Option(False)) -> None:
    """Run AI classifier in safe dry-run mode."""
    settings = _load_settings()
    now = datetime.now(timezone.utc)
    raw = RawTelegramMessage(
        channel_id="ai-dry-run",
        channel_username="dry",
        message_id=1,
        text=message,
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    context = ChannelContext(
        channel_id="ai-dry-run",
        max_message_limit=settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
        max_update_window_hours=settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
    )
    context.add_recent_message(raw)

    using_real_gateway = (
        real_gateway
        and settings.AI_GATEWAY_ENABLED
        and settings.RUN_AI_GATEWAY_INTEGRATION_TESTS == 1
    )

    if using_real_gateway:
        ensure_local_ai_gateway_ready(settings)
        client = _build_ai_gateway_client(settings)
        mode = "real-gateway"
    else:
        def handler(request: httpx.Request) -> httpx.Response:
            body: dict[str, object] = {
                "classification": "UNKNOWN",
                "action": "unknown",
                "market": "unknown",
                "symbol": "BTCUSDT" if "btc" in message.lower() else None,
                "side": "long" if "long" in message.lower() else "unknown",
                "entry_type": "unknown",
                "entry_low": None,
                "entry_high": None,
                "stop_loss": None,
                "take_profits": [],
                "leverage": None,
                "related_signal_id": None,
                "relation_reason": None,
                "confidence": "0.35",
                "reasoning_summary": "dry-run mock response",
                "risk_notes": ["no real gateway call"],
                "requires_admin_confirmation": True,
                "raw_provider_metadata": {"mode": "mock"},
            }
            if "entry" in message.lower() and "sl" in message.lower() and "tp" in message.lower():
                body.update(
                    {
                        "classification": "NEW_SIGNAL",
                        "action": "open",
                        "market": "futures",
                        "entry_type": "range",
                        "entry_low": "68000",
                        "entry_high": "68200",
                        "stop_loss": "67400",
                        "take_profits": ["69000", "70000"],
                        "leverage": 5,
                        "confidence": "0.88",
                        "reasoning_summary": "structured signal detected",
                    }
                )
            elif "profit" in message.lower() or "tp1 hit" in message.lower():
                body.update(
                    {
                        "classification": "RESULT_REPORT",
                        "action": "ignore",
                        "confidence": "0.85",
                        "reasoning_summary": "result report not a new signal",
                    }
                )
            elif (
                "promo" in message.lower()
                or "giveaway" in message.lower()
                or "join now" in message.lower()
            ):
                body.update(
                    {
                        "classification": "ADVERTISEMENT",
                        "action": "ignore",
                        "confidence": "0.86",
                        "reasoning_summary": "promotional content",
                    }
                )
            return httpx.Response(200, json=body)

        client = _build_ai_gateway_client(
            settings,
            base_url="http://mocked.local",
            transport=httpx.MockTransport(handler),
        )
        mode = "mock-gateway"

    classifier = AIMessageClassifier(settings=settings, gateway_client=client)
    httpx_logger = logging.getLogger("httpx")
    previous_httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        classified = classifier.classify(raw, context)
    finally:
        httpx_logger.setLevel(previous_httpx_level)
    payload: dict[str, object] = {
        "mode": mode,
        "real_gateway_requested": real_gateway,
        "real_gateway_used": using_real_gateway,
        "parsed_action": classified.parsed_signal.action.value,
        "parsed_symbol": classified.parsed_signal.symbol,
        "classification_confidence": str(classified.confidence),
        "is_potential_new_signal": classified.is_potential_new_signal,
        "is_related_to_existing_signal": classified.is_related_to_existing_signal,
        "debug_notes": classified.debug_notes,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("ai-gateway-check")
def ai_gateway_check_cmd() -> None:
    """Show non-secret AI gateway configuration status."""
    settings = _load_settings()
    typer.echo(dump_json(ai_gateway_safe_config(settings)))


@app.command("ai-gateway-start")
def ai_gateway_start_cmd() -> None:
    """Start local Ajil gateway as a background process."""
    settings = _load_settings()
    try:
        result = start_ai_gateway_process(settings)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(dump_json(result))


@app.command("ai-gateway-status")
def ai_gateway_status_cmd() -> None:
    """Print non-secret AI gateway runtime status."""
    settings = _load_settings()
    typer.echo(dump_json(ai_gateway_status(settings)))


@app.command("ai-gateway-stop")
def ai_gateway_stop_cmd() -> None:
    """Stop local Ajil gateway background process if present."""
    settings = _load_settings()
    typer.echo(dump_json(stop_ai_gateway_process(settings)))


@app.command("ai-gateway-restart")
def ai_gateway_restart_cmd() -> None:
    """Restart local Ajil gateway background process."""
    settings = _load_settings()
    stopped = stop_ai_gateway_process(settings)
    try:
        started = start_ai_gateway_process(settings)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(dump_json({"stopped": stopped, "started": started}))


@app.command("ai-gateway-logs")
def ai_gateway_logs_cmd(lines: int = typer.Option(100, "--lines", min=1)) -> None:
    """Tail redacted local Ajil gateway logs."""
    settings = _load_settings()
    for line in ai_gateway_logs(settings, lines=lines):
        typer.echo(line)


@app.command("telegram-check")
def telegram_check_cmd() -> None:
    """Validate Telegram config without connecting."""
    settings = _load_settings()
    api_hash = settings.TELEGRAM_API_HASH.get_secret_value()
    payload = {
        "api_id_present": settings.TELEGRAM_API_ID > 0,
        "api_hash_present": bool(api_hash and api_hash != "replace_me"),
        "string_session_present": bool(settings.TELEGRAM_STRING_SESSION.get_secret_value().strip()),
        "session_dir": settings.TELEGRAM_SESSION_DIR,
        "session_name": settings.TELEGRAM_SESSION_NAME,
        "real_test_channel": settings.TELEGRAM_REAL_TEST_CHANNEL,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("telegram-history-dry-run")
def telegram_history_dry_run_cmd(
    channel: str,
    limit: int = typer.Option(5, min=1),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Fetch Telegram history in fake mode by default."""
    settings = _load_settings()
    use_real = real and settings.RUN_TELEGRAM_INTEGRATION_TESTS == 1
    if real and not use_real:
        raise typer.BadParameter(
            "Real Telegram mode requires RUN_TELEGRAM_INTEGRATION_TESTS=1 in root .env.local"
        )

    if use_real:
        client: TelegramClientInterface = TelethonTelegramClient(settings)
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "real"
    else:
        now = datetime.now(timezone.utc)
        fake_messages = [
            RawTelegramMessage(
                channel_id=channel,
                channel_username="fake_channel",
                message_id=i + 1,
                text=f"sample message {i + 1}",
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
            for i in range(limit)
        ]
        client = FakeTelegramClient(history_by_channel={channel: fake_messages})
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "fake"

    payload = {
        "mode": mode,
        "channel": channel,
        "count": len(messages),
        "sample": [
            {
                "channel_id": item.channel_id,
                "message_id": item.message_id,
                "date": item.date.isoformat(),
            }
            for item in messages[:5]
        ],
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("telegram-tofan-dry-run")
def telegram_tofan_dry_run_cmd(
    limit: int = typer.Option(20, min=1),
    real: bool = typer.Option(False, "--real"),
    show_text: bool = typer.Option(False, "--show-text"),
) -> None:
    """Run dry-run fetch for configured real test channel."""
    settings = _load_settings()
    channel = settings.TELEGRAM_REAL_TEST_CHANNEL
    use_real = real and settings.RUN_TELEGRAM_INTEGRATION_TESTS == 1
    if real and not use_real:
        raise typer.BadParameter(
            "Real Telegram mode requires RUN_TELEGRAM_INTEGRATION_TESTS=1 in root .env.local"
        )

    if use_real:
        client: TelegramClientInterface = TelethonTelegramClient(settings)
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "real"
    else:
        now = datetime.now(timezone.utc)
        fake_messages = [
            RawTelegramMessage(
                channel_id=channel,
                channel_username="tofan_trade",
                message_id=i + 100,
                text=f"fixture text {i + 1}",
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
            for i in range(limit)
        ]
        client = FakeTelegramClient(history_by_channel={channel: fake_messages})
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "fake"

    sorted_messages = sorted(messages, key=lambda item: item.date)
    payload: dict[str, object] = {
        "mode": mode,
        "channel": channel,
        "count": len(messages),
        "text_message_count": sum(1 for item in messages if item.text),
        "sample_message_ids": [item.message_id for item in messages[:5]],
    }
    if sorted_messages:
        payload["first_date"] = sorted_messages[0].date.isoformat()
        payload["last_date"] = sorted_messages[-1].date.isoformat()
    if show_text:
        payload["sample_text"] = [item.text for item in messages[:3]]
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("market-data-dry-run")
def market_data_dry_run_cmd(
    symbol: str,
    interval: str = typer.Option("1m"),
    minutes: int = typer.Option(5, min=1),
) -> None:
    """Generate fake candle output without network calls."""
    _load_settings()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candles: list[Candle] = []
    base = Decimal("100")
    for idx in range(minutes):
        open_time = now - timedelta(minutes=minutes - idx)
        open_price = base + Decimal(str(idx))
        close_price = open_price + Decimal("0.2")
        candles.append(
            Candle(
                symbol=symbol.upper(),
                interval=interval,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=open_price,
                high=close_price + Decimal("0.1"),
                low=open_price - Decimal("0.1"),
                close=close_price,
                volume=Decimal("10"),
                source=CandleSource.FIXTURE,
            )
        )
    payload = {
        "symbol": symbol.upper(),
        "interval": interval,
        "candle_count": len(candles),
        "first_open_time": candles[0].open_time.isoformat() if candles else None,
        "last_open_time": candles[-1].open_time.isoformat() if candles else None,
        "source": CandleSource.FIXTURE.value,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-klines-dry-run")
def toobit_klines_dry_run_cmd(
    symbol: str,
    interval: str = typer.Option("1m"),
    minutes: int = typer.Option(5, min=1),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Run guarded Toobit public kline fetch."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real with integration guard enabled.")
    if settings.RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real mode requires RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1 in root .env.local"
        )

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes)
    provider = ToobitMarketDataProvider(
        base_url=settings.TOOBIT_BASE_URL,
        klines_path=settings.TOOBIT_KLINES_PATH,
        mark_price_klines_path=settings.TOOBIT_FUTURES_MARK_PRICE_KLINES_PATH,
        index_klines_path=settings.TOOBIT_FUTURES_INDEX_KLINES_PATH,
        contract_ticker_price_path=settings.TOOBIT_FUTURES_TICKER_PRICE_PATH,
        timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
        limit=settings.TOOBIT_MARKET_DATA_LIMIT,
    )
    candles = asyncio.run(provider.get_klines(symbol, interval, start_time, end_time))
    payload = {
        "symbol": symbol.upper(),
        "interval": interval,
        "candle_count": len(candles),
        "first_open_time": candles[0].open_time.isoformat() if candles else None,
        "last_open_time": candles[-1].open_time.isoformat() if candles else None,
        "source": CandleSource.TOOBIT.value if candles else "none",
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("binance-public-klines-dry-run")
def binance_public_klines_dry_run_cmd(
    symbol: str,
    interval: str = typer.Option("1m"),
    minutes: int = typer.Option(5, min=1),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Run guarded Binance public historical futures fetch without auth."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real with integration guard enabled.")
    if settings.RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real mode requires "
            "RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1 in root .env.local"
        )

    end_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_time = end_time - timedelta(minutes=minutes)
    provider = _build_binance_public_provider(settings)
    candles = asyncio.run(provider.get_klines(symbol, interval, start_time, end_time))
    payload = {
        "symbol": symbol.upper(),
        "interval": interval,
        "candle_count": len(candles),
        "first_open_time": candles[0].open_time.isoformat() if candles else None,
        "last_open_time": candles[-1].open_time.isoformat() if candles else None,
        "source": CandleSource.BINANCE.value if candles else "none",
        "cache_dir": settings.BINANCE_PUBLIC_DATA_CACHE_DIR,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-check")
def toobit_check_cmd() -> None:
    """Show safe Toobit configuration presence summary."""
    settings = _load_settings()
    payload = {
        "base_url": settings.TOOBIT_BASE_URL,
        "api_key_present": bool(
            settings.TOOBIT_API_KEY.get_secret_value()
            and settings.TOOBIT_API_KEY.get_secret_value() != "replace_me"
        ),
        "api_secret_present": bool(
            settings.TOOBIT_API_SECRET.get_secret_value()
            and settings.TOOBIT_API_SECRET.get_secret_value() != "replace_me"
        ),
        "execution_mode": settings.EXECUTION_MODE,
        "live_blocked": True,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-public-check")
def toobit_public_check_cmd() -> None:
    """Call public Toobit endpoints and print safe summary."""
    settings = _load_settings()
    client = _build_toobit_client(settings)
    try:
        time_payload = asyncio.run(client.get_server_time())
        exchange_payload = asyncio.run(client.get_exchange_info())
        payload = {
            "server_time_keys": sorted(list(time_payload.keys())),
            "exchange_info_keys": sorted(list(exchange_payload.keys())),
            "public_check_success": True,
        }
    except Exception as exc:
        payload = {
            "public_check_success": False,
            "error_type": type(exc).__name__,
            "detail": "public endpoint check failed safely",
        }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-signed-check")
def toobit_signed_check_cmd(real: bool = typer.Option(False, "--real")) -> None:
    """Run guarded signed safe account check."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable guard.")
    if settings.RUN_TOOBIT_SIGNED_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real signed mode requires RUN_TOOBIT_SIGNED_INTEGRATION_TESTS=1 in root .env.local"
        )

    client = _build_toobit_client(settings)
    account = ToobitAccountClient(client, settings.TOOBIT_SAFE_ACCOUNT_PATH)
    try:
        result = asyncio.run(account.safe_account_check())
    except ToobitError as exc:
        raise typer.BadParameter(f"signed check failed safely: {type(exc).__name__}") from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command("toobit-order-test")
def toobit_order_test_cmd(
    symbol: str = typer.Option(..., "--symbol"),
    side: str = typer.Option(..., "--side"),
    order_type: str = typer.Option(..., "--type"),
    quantity: str = typer.Option(..., "--quantity"),
    price: str | None = typer.Option(None, "--price"),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Run guarded spot orderTest only (no live order)."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable guard.")
    if settings.RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "OrderTest real mode requires RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS=1 in .env.local"
        )
    if settings.EXECUTION_MODE != "demo":
        raise typer.BadParameter("OrderTest requires EXECUTION_MODE=demo")
    try:
        quantity_decimal = Decimal(quantity)
        price_decimal = Decimal(price) if price is not None else None
    except Exception as exc:
        raise typer.BadParameter("quantity/price must be valid decimals") from exc

    client = _build_toobit_client(settings)
    spot = ToobitSpotClient(client, settings)
    adapter = DemoExecutionAdapter(settings, spot)
    try:
        result = asyncio.run(
            adapter.create_demo_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity_decimal,
                price=price_decimal,
                run_order_test=True,
            )
        )
    except Exception as exc:
        raise typer.BadParameter(f"order-test failed safely: {type(exc).__name__}") from exc

    payload = {
        "accepted": result.accepted,
        "symbol": result.symbol,
        "side": result.side,
        "type": result.order_type,
        "status": result.status,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("backtest-fixture")
def backtest_fixture_cmd() -> None:
    _load_settings()
    report_json, summary = run_fixture_backtest()
    typer.echo(json.dumps({"summary": summary, "report": report_json}, indent=2, sort_keys=True))


@app.command("backtest-dry-run")
def backtest_dry_run_cmd(
    channel: str = typer.Option("https://t.me/Tofan_Trade", "--channel"),
    from_date: str = typer.Option(..., "--from"),
    to_date: str = typer.Option(..., "--to"),
    interval: str = typer.Option("1m", "--interval"),
    real: bool = typer.Option(False, "--real"),
) -> None:
    settings = _load_settings()
    if real:
        raise typer.BadParameter(
            "Real backtest mode blocked unless RUN_BACKTEST_INTEGRATION_TESTS=1 and related guards."
        )
    request = BacktestRequest(
        channel=channel,
        from_date=parse_user_datetime_to_utc(from_date),
        to_date=parse_user_datetime_to_utc(to_date),
        initial_balance=settings.BACKTEST_DEFAULT_INITIAL_BALANCE,
        interval=interval,
        fill_policy=BacktestFillPolicy.CONSERVATIVE,
        risk_per_trade_pct=settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT,
        use_ai_classifier=settings.BACKTEST_USE_AI_CLASSIFIER,
        use_regex_fallback=settings.BACKTEST_USE_REGEX_FALLBACK,
        max_messages=settings.BACKTEST_MAX_MESSAGES,
        symbols=None,
    )
    report = BacktestEngine().run(request)
    payload = {
        "channel": report.channel_id,
        "final_balance": str(report.final_balance),
        "max_drawdown": str(report.metrics.max_drawdown),
        "profit_factor": (
            str(report.metrics.profit_factor) if report.metrics.profit_factor is not None else None
        ),
        "total_pnl": str(report.metrics.total_pnl),
        "win_rate": str(report.metrics.win_rate),
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("real-backtest-check")
def real_backtest_check_cmd() -> None:
    """Show non-secret readiness for the real backtest pipeline."""
    settings = _load_settings()
    runner = _build_real_backtest_runner(settings)
    readiness = runner.readiness().model_dump(mode="json")
    readiness["admin_bot_running"] = get_admin_bot_status(settings)["running"]
    readiness["dashboard_running"] = dashboard_status(settings)["running"]
    typer.echo(json.dumps(readiness, indent=2, sort_keys=True))


@app.command("real-backtest-run")
def real_backtest_run_cmd(
    channel: str = typer.Option(..., "--channel"),
    from_date: str | None = typer.Option(None, "--from"),
    to_date: str | None = typer.Option(None, "--to"),
    hours: int | None = typer.Option(None, "--hours"),
    interval: str = typer.Option("1m", "--interval"),
    max_messages: int = typer.Option(1000, "--max-messages"),
    use_ai: bool = typer.Option(True, "--use-ai/--no-ai"),
    send_telegram_summary: bool = typer.Option(
        True,
        "--send-telegram-summary/--no-send-telegram-summary",
    ),
    send_log_channel: bool = typer.Option(
        True,
        "--send-log-channel/--no-send-log-channel",
    ),
) -> None:
    """Run a guarded real Telegram + Toobit public backtest."""
    settings = _load_settings()
    runner = _build_real_backtest_runner(settings)
    request = RealBacktestRunRequest(
        channel=channel,
        from_date=parse_user_datetime_to_utc(from_date) if from_date else None,
        to_date=parse_user_datetime_to_utc(to_date) if to_date else None,
        hours=hours,
        interval=interval,
        max_messages=max_messages,
        initial_balance=settings.BACKTEST_DEFAULT_INITIAL_BALANCE,
        risk_per_trade_pct=settings.BACKTEST_DEFAULT_RISK_PER_TRADE_PCT,
        use_ai=use_ai,
        send_telegram_summary=send_telegram_summary,
        send_log_channel=send_log_channel,
        log_per_message=settings.REAL_BACKTEST_LOG_PER_MESSAGE,
    )
    logger_overrides = {
        "telethon": logging.ERROR,
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
    }
    previous_levels = {name: logging.getLogger(name).level for name in logger_overrides}
    for name, level in logger_overrides.items():
        logging.getLogger(name).setLevel(level)
    try:
        result = runner.run_sync(request)
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)
    typer.echo(
        json.dumps(
            {
                "success": result.success,
                "channel": result.channel,
                "from_date": result.from_date.isoformat(),
                "to_date": result.to_date.isoformat(),
                "interval": result.interval,
                "real_telegram_used": result.real_telegram_used,
                "real_market_data_used": result.real_market_data_used,
                "ai_used": result.ai_used,
                "regex_fallback_used": result.regex_fallback_used,
                "total_messages": result.total_messages,
                "parsed_signals": result.parsed_signals,
                "valid_signals": result.valid_signals,
                "trades_simulated": result.trades_simulated,
                "trades_filled": result.trades_filled,
                "total_pnl": str(result.total_pnl),
                "channel_score": str(result.channel_score),
                "skipped_reasons": result.skipped_reasons,
                "warnings": result.warnings,
                "errors": result.errors,
                "report_path": result.report_path,
                "markdown_report_path": result.markdown_report_path,
            },
            indent=2,
            sort_keys=True,
        )
    )


@app.command("real-backtest-tofan")
def real_backtest_tofan_cmd(
    hours: int = typer.Option(24, "--hours"),
    interval: str = typer.Option("1m", "--interval"),
    max_messages: int = typer.Option(1000, "--max-messages"),
    use_ai: bool = typer.Option(True, "--use-ai/--no-ai"),
) -> None:
    """Run the default guarded real backtest against the configured real channel."""
    settings = _load_settings()
    real_backtest_run_cmd(
        channel=settings.REAL_BACKTEST_DEFAULT_CHANNEL,
        from_date=None,
        to_date=None,
        hours=hours,
        interval=interval,
        max_messages=max_messages,
        use_ai=use_ai,
        send_telegram_summary=settings.REAL_BACKTEST_SEND_TO_ADMIN_BOT,
        send_log_channel=settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL,
    )


@app.command("backtest-show-latest")
def backtest_show_latest_cmd() -> None:
    """Show the latest stored real backtest summary."""
    settings = _load_settings()
    runner = _build_real_backtest_runner(settings)
    payload = runner.latest_report_summary()
    if payload is None:
        raise typer.BadParameter("No real backtest report found yet.")
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-check")
def admin_check_cmd() -> None:
    settings = _load_settings()
    usernames = [normalize_username(item) for item in settings.ADMIN_TELEGRAM_USERNAMES]
    payload = {
        "bot_token_present": bool(
            settings.TELEGRAM_BOT_TOKEN.get_secret_value()
            and settings.TELEGRAM_BOT_TOKEN.get_secret_value() != "replace_me"
        ),
        "admin_usernames_count": len(usernames),
        "admin_usernames": sorted(usernames),
        "deprecated_admin_user_ids_present": len(settings.ADMIN_USER_IDS) > 0,
        "integration_guard": settings.RUN_TELEGRAM_BOT_INTEGRATION_TESTS == 1,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-format-dry-run")
def admin_format_dry_run_cmd() -> None:
    _load_settings()
    formatter = AdminActionFormatter()
    now = datetime.now(timezone.utc)
    action = ProposedAction(
        action_id="test_action_123",
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="sig_1",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.81"),
        reason="consolidation completed",
        payload={"channel_id": "chan", "symbol": "BTCUSDT", "side": "LONG", "entry": "68000-68200"},
        created_at=now,
    )
    signal = SignalState(
        signal_id="sig_1",
        channel_id="chan",
        status=SignalStatus.PENDING_CONSOLIDATION,
        created_from_message_id=1,
        related_message_ids=[1],
        current_signal=None,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=None,
    )
    formatted = formatter.format_action(action, signal=signal)
    payload = {
        "text": formatted.text,
        "buttons": [button.__dict__ for button in formatted.buttons],
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-callback-dry-run")
def admin_callback_dry_run_cmd(
    callback_data: str,
    username: str = typer.Option(..., "--username"),
) -> None:
    settings = _load_settings()
    auth = AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES)
    try:
        auth.require_authorized_username(username)
    except AdminUnauthorizedError as exc:
        raise typer.BadParameter("username is not authorized") from exc
    parsed = parse_admin_callback(callback_data)
    payload = {
        "authorized": True,
        "username": normalize_username(username),
        "action_id": parsed.action_id,
        "decision": parsed.decision.value,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-register-dry-run")
def admin_register_dry_run_cmd(
    username: str = typer.Option(...),
    chat_id: int = typer.Option(...),
) -> None:
    settings = _load_settings()
    bot = TelegramAdminBot(
        bot_token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
        parse_mode=settings.ADMIN_BOT_PARSE_MODE,
        disable_web_preview=settings.ADMIN_BOT_DISABLE_WEB_PAGE_PREVIEW,
    )
    reg = bot.handle_start(username=username, chat_id=chat_id)
    payload = {"username": reg.username, "chat_id": reg.chat_id, "registered": True}
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-send-test")
def admin_send_test_cmd(
    real: bool = typer.Option(False, "--real"),
    chat_id: int | None = None,
) -> None:
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable guard.")
    if settings.RUN_TELEGRAM_BOT_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real admin bot mode requires RUN_TELEGRAM_BOT_INTEGRATION_TESTS=1 in .env.local"
        )
    if not settings.ADMIN_TELEGRAM_USERNAMES:
        raise typer.BadParameter("ADMIN_TELEGRAM_USERNAMES is required")

    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value()
    if not token or token == "replace_me":
        raise typer.BadParameter("TELEGRAM_BOT_TOKEN is required")

    bot = TelegramAdminBot(
        bot_token=token,
        parse_mode=settings.ADMIN_BOT_PARSE_MODE,
        disable_web_preview=settings.ADMIN_BOT_DISABLE_WEB_PAGE_PREVIEW,
    )
    target_chat_id = chat_id
    target_username: str | None = None
    if target_chat_id is None:
        normalized = normalize_username(settings.ADMIN_TELEGRAM_USERNAMES[0])
        reg = bot.registrations.get(normalized)
        if reg is None:
            raise typer.BadParameter(
                "Admin must start the bot first or pass --chat-id for guarded test."
            )
        target_chat_id = reg.chat_id
        target_username = normalized

    response = asyncio.run(
        bot.send_test_message(target_chat_id, settings.ADMIN_BOT_TEST_MESSAGE_TEXT)
    )
    result = response.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    payload = {
        "message_sent": True,
        "recipient_username": target_username,
        "telegram_message_id": message_id,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("admin-backtest-dry-run")
def admin_backtest_dry_run_cmd(username: str = typer.Option(..., "--username")) -> None:
    settings = _load_settings()
    service = AdminApprovalService(
        auth=AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES),
        bot=TelegramAdminBot(
            bot_token=settings.TELEGRAM_BOT_TOKEN.get_secret_value(),
            parse_mode=settings.ADMIN_BOT_PARSE_MODE,
            disable_web_preview=settings.ADMIN_BOT_DISABLE_WEB_PAGE_PREVIEW,
        ),
        settings=settings,
    )
    try:
        menu = service.backtest_menu(username)
        run = service.run_backtest_dry(username)
    except AdminUnauthorizedError as exc:
        raise typer.BadParameter("username is not authorized") from exc
    typer.echo(json.dumps({"menu": menu, "run": run}, indent=2, sort_keys=True))


@app.command("log-channel-check")
def log_channel_check_cmd() -> None:
    """Show safe Telegram log-channel status without connecting."""
    settings = _load_settings()
    payload = TelegramLogChannelClient(settings=settings).safe_status()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("log-channel-format-dry-run")
def log_channel_format_dry_run_cmd() -> None:
    """Print a safe English processing audit report without network calls."""
    settings = _load_settings()
    event = build_sample_processing_audit_event(settings)
    typer.echo(format_processing_audit_for_telegram(event))


@app.command("log-channel-send-test")
def log_channel_send_test_cmd(real: bool = typer.Option(False, "--real")) -> None:
    """Send one guarded non-secret test processing report to the log channel."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable log-channel guard.")
    client = TelegramLogChannelClient(settings=settings)
    if not client.enabled_for_real_send():
        raise typer.BadParameter(
            "Real log-channel send requires TELEGRAM_LOG_CHANNEL_ENABLED=true, "
            "PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=true, "
            "RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=1, and TELEGRAM_BOT_TOKEN."
        )
    event = build_sample_processing_audit_event(settings)
    result = asyncio.run(client.send_event(event, real=True))
    payload = {
        "sent": result.sent,
        "skipped": result.skipped,
        "reason": result.reason,
        "message_id": result.message_id,
        "log_channel_username": settings.TELEGRAM_LOG_CHANNEL_USERNAME,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("process-message-audit-dry-run")
def process_message_audit_dry_run_cmd() -> None:
    """Run a fake message through ChannelAgent with processing audit enabled."""
    settings = _load_settings()
    start = datetime.now(timezone.utc)
    clock = FakeClock(start)
    agent = ChannelAgent(channel_id="audit-dry-run", settings=settings, clock=clock)
    raw = RawTelegramMessage(
        channel_id="audit-dry-run",
        channel_username="@AuditDryRun",
        message_id=101,
        text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
        date=start,
        edited_at=None,
        reply_to_msg_id=None,
    )
    service = ProcessingAuditService(settings=settings, clock=clock)
    result = service.process_message_with_audit(raw, agent)
    payload = {
        "audit_event": {
            "event_id": result.event.event_id,
            "status": result.event.status.value,
            "classification": result.event.classification,
            "parsed_action": result.event.parsed_action,
            "signal_id": result.event.signal_id,
            "state_before": result.event.state_before,
            "state_after": result.event.state_after,
            "duration_ms": result.event.duration_ms,
        },
        "proposed_actions": [action.model_dump(mode="json") for action in result.proposed_actions],
        "formatted_message": result.formatted_message,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("dashboard-check")
def dashboard_check_cmd() -> None:
    """Show safe dashboard configuration."""
    settings = _load_settings()
    typer.echo(json.dumps(dashboard_safe_config(settings), indent=2, sort_keys=True))


@app.command("run-dashboard")
def run_dashboard_cmd(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
    max_runtime_seconds: int | None = typer.Option(None, "--max-runtime-seconds", min=1),
) -> None:
    """Run dashboard in foreground."""
    settings = _load_settings()
    result = run_dashboard(
        settings,
        host=host,
        port=port,
        reload=reload,
        max_runtime_seconds=max_runtime_seconds,
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("dashboard-start")
def dashboard_start_cmd() -> None:
    """Start dashboard as a background process."""
    settings = _load_settings()
    try:
        result = start_dashboard_process(settings)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("dashboard-status")
def dashboard_status_cmd() -> None:
    """Show dashboard runtime status."""
    settings = _load_settings()
    typer.echo(json.dumps(dashboard_status(settings), indent=2, sort_keys=True))


@app.command("dashboard-stop")
def dashboard_stop_cmd() -> None:
    """Stop background dashboard process."""
    settings = _load_settings()
    typer.echo(json.dumps(stop_dashboard_process(settings), indent=2, sort_keys=True))


@app.command("dashboard-restart")
def dashboard_restart_cmd() -> None:
    """Restart background dashboard process."""
    settings = _load_settings()
    stopped = stop_dashboard_process(settings)
    started = start_dashboard_process(settings)
    typer.echo(json.dumps({"stopped": stopped, "started": started}, indent=2, sort_keys=True))


@app.command("dashboard-logs")
def dashboard_logs_cmd(lines: int = typer.Option(100, "--lines", min=1)) -> None:
    """Tail redacted dashboard logs."""
    settings = _load_settings()
    for line in dashboard_logs(settings, lines=lines):
        typer.echo(line)


@app.command("dashboard-smoke-test")
def dashboard_smoke_test_cmd() -> None:
    """Run local TestClient checks without external calls."""
    settings = _load_settings()
    typer.echo(json.dumps(dashboard_smoke_test(settings), indent=2, sort_keys=True))


@app.command("dashboard-token-hint")
def dashboard_token_hint_cmd() -> None:
    """Show where the local dashboard token is stored without printing it."""
    typer.echo(dashboard_token_hint())


@app.command("run-admin-bot")
def run_admin_bot_cmd(
    real: bool = typer.Option(False, "--real"),
    watch: bool = typer.Option(False, "--watch"),
    once: bool = typer.Option(False, "--once"),
    max_runtime_seconds: int | None = typer.Option(None, "--max-runtime-seconds", min=1),
) -> None:
    """Run the admin bot runtime in foreground; fake mode is default."""
    settings = _load_settings()
    try:
        result = run_admin_bot_sync(
            settings,
            real=real,
            watch=watch,
            once=once,
            max_runtime_seconds=max_runtime_seconds,
        )
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(dump_json(result))


@app.command("admin-bot-start")
def admin_bot_start_cmd(
    real: bool = typer.Option(False, "--real"),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    """Start admin bot as a background process."""
    settings = _load_settings()
    try:
        result = start_admin_bot_process(settings, real=real, watch=watch)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(dump_json(result))


@app.command("admin-bot-status")
def admin_bot_status_cmd() -> None:
    """Print non-secret admin bot runtime status."""
    settings = _load_settings()
    typer.echo(dump_json(get_admin_bot_status(settings)))


@app.command("admin-bot-stop")
def admin_bot_stop_cmd() -> None:
    """Stop background admin bot process if present."""
    settings = _load_settings()
    typer.echo(dump_json(stop_admin_bot_process(settings)))


@app.command("admin-bot-restart")
def admin_bot_restart_cmd(
    real: bool = typer.Option(False, "--real"),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    """Restart background admin bot process."""
    settings = _load_settings()
    stopped = stop_admin_bot_process(settings)
    try:
        started = start_admin_bot_process(settings, real=real, watch=watch)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(dump_json({"stopped": stopped, "started": started}))


@app.command("admin-bot-logs")
def admin_bot_logs_cmd(lines: int = typer.Option(100, "--lines", min=1)) -> None:
    """Tail redacted admin bot runtime logs."""
    settings = _load_settings()
    for line in tail_admin_bot_logs(settings, lines=lines):
        typer.echo(line)


@app.command("admin-bot-smoke-test")
def admin_bot_smoke_test_cmd() -> None:
    """Run a fake admin bot smoke test without network calls."""
    settings = _load_settings()
    typer.echo(dump_json(run_admin_bot_smoke_test(settings)))


@app.command("verify-system")
def verify_system_cmd(
    mode: str = typer.Option("safe", "--mode"),
    write_report: bool = typer.Option(False, "--write-report/--no-write-report"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Run safe or guarded real system verification."""
    settings = _load_settings()
    if mode not in {"safe", "real", "all"}:
        raise typer.BadParameter("mode must be safe, real, or all")
    if output_format not in {"text", "json"}:
        raise typer.BadParameter("format must be text or json")

    report = VerificationRunner(settings).run(
        mode=cast(VerificationMode, mode),
        write_report=write_report,
    )
    if output_format == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        typer.echo(render_terminal_summary(report))
    if report.overall_status is VerificationStatus.FAIL:
        raise typer.Exit(code=1)


@app.command("verify-real")
def verify_real_cmd(
    write_report: bool = typer.Option(False, "--write-report/--no-write-report"),
) -> None:
    """Shortcut for guarded real verification."""
    settings = _load_settings()
    report = VerificationRunner(settings).run(mode="real", write_report=write_report)
    typer.echo(render_terminal_summary(report))
    if settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1:
        typer.echo("Real smoke guard is disabled; real checks were skipped.")
    if report.overall_status is VerificationStatus.FAIL:
        raise typer.Exit(code=1)


@app.command("show-last-report")
def show_last_report_cmd() -> None:
    """Show the latest generated Markdown verification report path and preview."""
    settings = _load_settings()
    latest = find_latest_report(settings.VERIFICATION_REPORT_DIR)
    if latest is None:
        typer.echo("No verification reports found.")
        return
    lines = latest.read_text(encoding="utf-8").splitlines()
    preview = "\n".join(lines[:20])
    typer.echo(json.dumps({"path": str(latest), "preview": preview}, indent=2, sort_keys=True))
